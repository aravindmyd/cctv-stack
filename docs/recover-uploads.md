# Recover failed uploads

Clips are not reaching Backblaze B2, or the queue is not draining. This page is ordered so
that each step narrows the cause; the first two take a minute.

## Nothing has been lost

A clip that has not uploaded is never deleted, however old it is. Every upload state except
`UPLOADED`, `SKIPPED_BY_POLICY`, `DELETED_LOCAL`, `EXPIRED_CLOUD`, and `FORCE_DELETED`
holds the local file.

The cost of a broken upload is disk growth. Diagnose calmly.

## 1. Read the queue

```bash
TOKEN=$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)

curl -s http://127.0.0.1:8082/health | jq '.state'
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $TOKEN" | jq .
```

```json
{
  "counts": { "FAILED_PERMANENT": 1, "PENDING": 3, "UPLOADED": 4211 },
  "oldest_pending_age_s": 2820.4,
  "uploaded_24h": 214,
  "last_successful_upload_at": 1757590201.2
}
```

| Reading | Meaning |
|---|---|
| `PENDING` rising, `FAILED_PERMANENT` at 0 | The queue is draining slower than clips arrive, or is stuck |
| `FAILED_PERMANENT` above 0 | At least one clip will not retry on its own |
| `oldest_pending_age_s` over 21600 (6 hours) | Something is wrong; continue below |
| `uploaded_24h` steady and the oldest pending is under a minute | Normal |
| `state` is `DISABLED` | No bucket is configured. Set `CCTV_STORAGE_BUCKET`, or accept local-only |

## 2. Find the error

The uploader's log is JSON. The chatty errors are the ones to read:

```bash
docker compose logs --tail=200 uploader | grep -E '"level": *"(ERROR|CRITICAL)"'
```

Each clip keeps its last failure in the database, with a class from a closed set of six.
The message next to it names the provider's own code.

```bash
docker compose exec -T uploader python - <<'PY'
import sqlite3
db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
for row in db.execute(
    "SELECT id, camera, upload_state, attempts, last_error_class, last_error "
    "FROM clips WHERE upload_state IN ('FAILED_PERMANENT', 'RETRY_WAIT') "
    "ORDER BY updated_at DESC LIMIT 20"
):
    print(dict(row))
PY
```

| `last_error_class` | Covers | Retries on its own? | What to do |
|---|---|---|---|
| `transient` | Network errors, 5xx, timeouts, throttling, anything unrecognised | Yes, backing off to one hour | Wait. If it persists, check network and the provider status page |
| `auth` | 401 or 403 from the provider | No | See section 3 |
| `not_found` | The bucket does not exist | No | See section 3 |
| `quota` | Cap reached, or a billing problem | Yes, backing off to six hours | Resolve it in the Backblaze console |
| `checksum` | The stored object does not match the local file | No | See section 4 |
| `unknown` | A 400 from the provider, or a missing local file | No | See section 4, or section 5 for a missing file |

## 3. Credentials and the bucket

```bash
curl -s http://127.0.0.1:8082/health \
  | jq '.checks[] | select(.name | startswith("storage"))'
```

```json
{ "name": "storage_credentials", "state": "UNHEALTHY",
  "code": "STORAGE_AUTH_FAILED",
  "detail": "403 Forbidden: invalid application key" }
```

Check, in this order:

1. Is the key still listed in the Backblaze console, and not expired? If it was rotated,
   follow [Rotate credentials](rotate-credentials.md).
2. Does `CCTV_STORAGE_BUCKET` match the bucket name exactly? Bucket names are
   case-sensitive.
3. Is the key scoped to that bucket? A key for `cctv-home` cannot write to
   `cctv-parents-home`.
4. Does the key allow writes? A read-only key uploads nothing and fails permanently.
5. Is `CCTV_STORAGE_KEY_ID` the key ID rather than the key's display name?

After fixing the cause, requeue the failures. A reason and a filter are both mandatory:

```bash
curl -s -X POST http://127.0.0.1:8082/admin/retry \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"camera":"front-door","reason":"rotated the B2 application key after 403s"}'
```

```json
{ "requeued": 37, "clip_ids": [8123, 8124] }
```

Watch the queue drain with the command from section 1. `retry` accepts `clip_id`, `camera`,
or `since` as a filter, and refuses a request with none of them.

## 4. Permanent failures

`FAILED_PERMANENT` means twelve attempts spread over about five hours, and then the uploader
stopped. The file is still on disk.

| Class | Next step |
|---|---|
| `auth`, `not_found`, `quota` | Fix the cause, then retry |
| `checksum` | Inspect the local file and the cloud object before retrying |
| `unknown` with a missing-file event | See section 5 |
| `transient` after twelve attempts | Something systematic. Check the provider, then retry |

### Checksum mismatch

Something differs between the local file and the stored object. Find out which copy is
wrong; retrying a corrupt file wastes another five hours.

```bash
# The hash the database recorded, next to one computed now.
docker compose exec -T uploader python - <<'PY'
import hashlib, sqlite3
from pathlib import Path

db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
row = db.execute("SELECT path, sha256, size_bytes FROM clips WHERE id = ?", (8123,)).fetchone()
path = Path("/data") / row["path"]
actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
print({"recorded": row["sha256"], "on_disk": actual, "size": row["size_bytes"]})
PY
```

Then compare with the object in the bucket. Every object stores its own `sha256` in its
metadata, so no download is needed. With the AWS CLI on the host:

```bash
BUCKET=$(grep '^CCTV_STORAGE_BUCKET=' .env | cut -d= -f2)
LOCATION=$(grep -E '^\s*location:' config/cameras.yml | awk '{print $2}')
aws s3api head-object --bucket "$BUCKET" \
  --key "$LOCATION/clips/front-door/2026/09/11/18-32-45.mp4" \
  --endpoint-url "https://s3.$(grep '^CCTV_STORAGE_REGION=' .env | cut -d= -f2).backblazeb2.com" \
  | jq '.Metadata.sha256, .ContentLength'
```

| Local file matches the database | Object exists | Conclusion |
|---|---|---|
| Yes | With different content | A stale or foreign object is at that key. Investigate before overwriting |
| Yes | No | Safe to retry; the earlier upload was probably partial |
| No | Either | The local file is corrupt. Do not retry it. Check the disk and whether other clips are affected |

## 5. A file the database expects is missing

The uploader records a `critical` event and marks the clip `FAILED_PERMANENT` without
deleting anything. Look for it, and for its neighbours on disk:

```bash
docker compose exec -T uploader python - <<'PY'
import sqlite3
db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
for row in db.execute(
    "SELECT at, message FROM system_events WHERE code = 'UPLOAD_LOCAL_FILE_MISSING' "
    "ORDER BY at DESC LIMIT 20"
):
    print(dict(row))
PY

ls -l data/clips/front-door/2026/09/11/
```

Do not mark such a clip as uploaded to silence the warning. That tells cleanup the local
copy is safe to delete, which is the one action that turns a reporting problem into data
loss.

Check whether the object is in the bucket. If it is, the backup succeeded and only the
local copy is missing. If it is in neither place, the clip is gone; record which one and
find out why before trusting the disk again.

## 6. Clips held past retention

Cleanup reports this as `CLIPS_OVERDUE_RETENTION`, and the uploader's health carries the
same count.

```bash
curl -s http://127.0.0.1:8083/health \
  | jq '.checks[] | select(.name == "clips_overdue_retention")'
```

```json
{ "name": "clips_overdue_retention", "state": "DEGRADED",
  "code": "CLIPS_OVERDUE_RETENTION",
  "detail": "37 clips, 1.4 GB held; oldest 12 days past its retention date" }
```

This is the retention rule doing its job: a clip whose upload has not succeeded is not
deleted. Options, in order of preference:

1. Fix the upload. This is almost always the right answer.
2. Add disk. A USB drive buys time and loses nothing.
3. Lower `retention.local_days`. It affects future clips only; held clips stay until they
   upload.

There is no force-delete command today. The only way to remove a held clip is to delete the
file and the row yourself, which destroys the only copy of that footage. If you do that,
record why.

If the disk fills, Frigate deletes its oldest recordings to keep running. Your clips are
protected; recent continuous footage is not, and a clip that has not been materialised yet
cannot be recovered from recordings that no longer exist.

## 7. Confirm the recovery

```bash
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $TOKEN" | jq .
curl -s http://127.0.0.1:8082/health | jq '.state'
curl -s http://127.0.0.1:8083/health | jq '.state'
aws s3 ls --recursive "s3://$BUCKET/$LOCATION/clips/" | tail -5
```

You are done when `FAILED_PERMANENT` is zero, `oldest_pending_age_s` is small, and objects
are appearing in the bucket.

## 8. If you cannot fix it

Leaving it broken is safe. The stack keeps recording, keeps materialising clips, and keeps
holding them locally. Disk is the only cost.

Before asking for help, collect:

```bash
docker compose logs --tail=500 uploader > uploader.log
docker compose logs --tail=500 clip-worker > clip-worker.log
curl -s http://127.0.0.1:8082/health > uploader-health.json
```

Include the `last_error_class` and `last_error` values from section 2. They are stable
codes, which makes a report directly actionable.
