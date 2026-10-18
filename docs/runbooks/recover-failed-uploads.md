# Runbook: Recover failed uploads

> **Status:** ✅ Implemented for the uploader itself (`/health`, `/admin/queue`, `/admin/retry`).
> The aggregate `/status` page in §1 and the cleanup-dependent parts of §5 and §7 need
> services that are not built yet; `/admin/verify` is not built — see §4.1.
> **Time:** 5 minutes to diagnose, longer to actually fix.

Clips are not reaching Backblaze B2, or the queue is not draining. This runbook tells you
what to look at, in order, and what to do about each finding.

---

## First: nothing is lost

Say this before doing anything else. By design, a clip that has not uploaded is **never
deleted**, however old it is. Your footage is on disk. The cost of this problem is **disk growth**, not data loss — for as
long as you leave it unfixed.

So: diagnose calmly, and do not reach for the force-delete option.

---

## 1. What does the system say?

```bash
# The uploader's own view. The aggregate `/status` page is not built yet.
# The port is published on loopback only.
curl -s http://127.0.0.1:8082/health | jq '{state, summary}'
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  | jq '{pending, uploading, retry_wait, failed_permanent, uploaded_24h, oldest_pending_age_s}'
```

```json
{
  "pending": 3,
  "uploading": 1,
  "retry_wait": 0,
  "failed_permanent": 0,
  "uploaded_24h": 214,
  "oldest_pending_age_s": 2820
}
```

| Reading | Meaning | Action |
|---|---|---|
| `pending` high, `failed_permanent` 0, rising | Backlog draining slower than clips arrive, or stuck | §2 |
| `failed_permanent` > 0 | At least one clip will not retry | §4 |
| `oldest_pending_age_s` > 6 h | Serious | §2 |
| `state` is `DEGRADED` | Something is wrong but uploads still work | §2 |
| `state` is `UNHEALTHY` | Stop and read the failing check first | §2 |
| `state` is `DISABLED` | **No bucket is configured.** Nothing is being backed up, by choice | `.env` → `CCTV_STORAGE_BUCKET` |
| Everything zero and `uploaded_24h` healthy | Nothing is wrong | — |

---

## 2. Find the actual error

```bash
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  | jq '.oldest_pending, .failed'
docker compose logs --tail=200 uploader | grep -E '"level":"(ERROR|CRITICAL)"'
```

The logs are JSON. Look at `last_error_class` on the stuck clips — it tells you
everything. There are **six** classes, because `clips.last_error_class` is a closed set in
schema; the specific cause is in the `last_error` message and in the audit log.

| `error_class` | Cause it covers | Retry? | Fix |
|---|---|---|---|
| `transient` | Network, `5xx`, timeout, throttling, and anything unrecognised | ✅, backoff to 1 h | Wait. This resolves itself |
| `auth` | **Credentials rejected** (`401`/`403`) | ❌ | §3 |
| `not_found` | **Bucket does not exist** | ❌ | §3 |
| `quota` | Cap reached, or a payment problem | ✅, backoff to 6 h | Resolve in the Backblaze console |
| `checksum` | The stored object does not match the local file | ❌ | §4.1 |
| `unknown` | A `400` from the provider, or **the local file is missing** | ❌ | §4.1 for a checksum, §6 for a missing file |

Provider-specific codes — `throttled`, `file_missing`, `invalid_request` — are folded into
these six; the failure message and the audit event name which one it was.

### Backup paths are stale: nothing is reaching the cloud at all

```bash
docker compose exec uploader curl -s localhost:8082/health \
  | jq '.checks[] | select(.state!="HEALTHY")'
```

```json
{ "name": "storage_reachable", "state": "UNHEALTHY",
  "code": "STORAGE_UNREACHABLE",
  "detail": "connection to s3.eu-central-003.backblazeb2.com timed out" }
```

A reachability failure is almost always network, and almost always temporary:

```bash
docker compose exec uploader curl -sS -o /dev/null -w '%{http_code}\n' https://s3.<region>.backblazeb2.com
docker compose exec uploader getent hosts s3.<region>.backblazeb2.com
```

If DNS or the connection fails from a shell but the rest of your internet works, check
the host's DNS and whether the container can reach the internet at all.

---

## 3. Credentials or bucket problems

These do **not** retry — by design. Retrying a rejected credential thousands of times per
hour helps nobody.

```bash
curl -s http://127.0.0.1:8082/health | jq '.checks[] | select(.name=="storage_credentials")'
```

```json
{ "name": "storage_credentials", "state": "UNHEALTHY", "code": "STORAGE_AUTH_FAILED",
  "detail": "403 Forbidden: invalid application key" }
```

### Check in this order

1. **Is the key revoked or expired?** Backblaze console → Application Keys. If the key
   was rotated, use [`rotate-credentials.md`](rotate-credentials.md)
2. **Does the bucket name match exactly?** The check is case-sensitive
   ```bash
   grep CCTV_STORAGE_BUCKET .env
   curl -s http://127.0.0.1:8082/health | jq '.checks[] | select(.name=="storage_reachable")'
   ```
3. **Is the key scoped to the right bucket?** A key scoped to `cctv-home` cannot write to
   `cctv-parents-home`
4. **Does the key have write permission?** Read-only keys upload nothing
5. **Is the key ID correct?** `B2_KEY_ID` is the *keyID*, not the key *name*

After fixing, reset the failed clips and retry. **A reason is mandatory**, and the request
must name a clip, a camera, or a `since` — a bare retry of everything is refused on purpose:

```bash
curl -s -X POST http://127.0.0.1:8082/admin/retry \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -d '{"camera":"front-door","reason":"rotated B2 application key after 403 errors"}'
```

```json
{ "requeued": 37, "clip_ids": [8123, 8124] }
```

Watch it drain:

```bash
watch -n2 'curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $INTERNAL_API_TOKEN" | jq .counts'
```

---

## 4. Permanent failures

`FAILED_PERMANENT` means the uploader tried 12 times over roughly 5 hours and gave up.
The local file is **still there** and is **not** going to be deleted.

```bash
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  | jq '.failed_permanent, .failed'
```

```json
[8123]
[
  { "id": 8123, "camera": "front-door", "path": "clips/front-door/2026/09/11/18-32-45.mp4",
    "attempts": 12, "last_error_class": "checksum",
    "last_error": "The provider stored different bytes…" }
]
```

| `last_error_class` | What to do |
|---|---|
| `auth`, `not_found`, `quota` | Fix the cause, then `retry` |
| `checksum` | **Do not just retry.** Verify the local file first — §4.1 |
| `unknown` with `UPLOAD_LOCAL_FILE_MISSING` in the audit log | §6 |
| `transient` after 12 attempts | Something systematic. Check the provider status page, then `retry` |

### 4.1 Investigating a checksum mismatch

Something is wrong with one of the two copies. Find out which before retrying.

```bash
# 1. Hash the local file, and compare it with what the database recorded.
docker compose exec clip-worker sha256sum /data/clips/front-door/2026/09/11/18-32-45.mp4

# 2. Compare with the object in the bucket. No download: every object carries its own
#    sha256 in its metadata.
BUCKET=$(grep CCTV_STORAGE_BUCKET .env | cut -d= -f2)
LOCATION=$(sed -n 's/^ *location: *//p' config/cameras.yml)
aws s3api head-object --bucket "$BUCKET" --key "$LOCATION/clips/front-door/2026/09/11/18-32-45.mp4" \
  | jq '.Metadata.sha256, .ContentLength'
```

> `/admin/verify` is **not implemented**. It re-downloads the whole object, and downloads
> are the one thing B2 bills for, so it is opt-in work. `aws s3api head-object` above
> answers the same question for free, because every object carries its `sha256` in its own
> metadata.

| Local matches DB? | Cloud object exists? | Conclusion |
|---|---|---|
| Yes | Yes, different content | A stale or foreign object is at that key. Investigate before overwriting |
| Yes | No | Safe to retry — this was likely a partial upload |
| No | — | **The local file is corrupt.** Do not retry. Check the disk and whether other clips are affected |

**Retry only when you understand what went wrong.** A retry that produces the same
mismatch wastes five hours.

---

## 5. `CLIPS_OVERDUE_RETENTION` — the disk-growth case

```json
{ "clips_overdue_retention": 37, "bytes_held": 1503238553 }
```

```text
37 clips are past their 90-day retention date and are being held
because they have not been uploaded.
1.4 GB held. Growing ~300 MB/day at current rate. Disk full in ~41 days.
```

This is the system working as designed: **it will not delete your only copy**. The fix is
to make the uploads succeed, not to delete the clips.

| Option | When it is appropriate |
|---|---|
| Fix the upload (§2–§4) | **Always the first answer** |
| Force-delete specific clips | Only when you have decided that specific footage is expendable — §7 |
| Add storage | Legitimate. A USB drive or a larger disk buys time |
| Lower retention in `cameras.yml` | Only affects *future* clips. It will not free space now, because it cannot delete un-uploaded clips |

**One thing to watch:** if the disk fills, Frigate deletes its oldest **recordings** to
keep running. Your clips are protected, but recent continuous footage is not. Free space
before it reaches that point.

---

## 6. `file_missing` — a consistency problem

The database says a clip exists; the file does not. The uploader records this as a
`critical` audit event (`UPLOAD_LOCAL_FILE_MISSING`) and marks the clip `FAILED_PERMANENT`
without deleting anything:

```bash
docker compose exec uploader python - <<'PY'
import sqlite3
db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
for row in db.execute(
    "SELECT at, message FROM system_events WHERE code = 'UPLOAD_LOCAL_FILE_MISSING' "
    "ORDER BY at DESC LIMIT 20"
):
    print(dict(row))
PY
```

```bash
# Confirm the file really is absent, and see whether its neighbours are too.
ls -l data/clips/front-door/2026/09/11/
```

**Do not set these clips to `UPLOADED` to make the warning go away.** That is the one
action that could cause real loss, because it tells cleanup the local file is safe to
delete.

Correct handling:

1. Check whether the clip exists in the cloud:
   ```bash
   aws s3 ls s3://<bucket>/<location>/clips/front-door/2026/09/11/
   ```
2. **If it is in the cloud:** the backup succeeded; only the local copy is gone. Leave it
   alone — reconciliation will report it and retention will handle the row.
3. **If it is in neither place:** the clip is genuinely lost. Record it and find out why.
   Run `./scripts/doctor.sh` and check the disk's health.
4. Report it, even if you resolve it. Repeated `UPLOAD_LOCAL_FILE_MISSING` events mean a
   real problem, not a one-off.

---

## 7. Force-deleting a clip

**This is the last resort.** It destroys the only local copy of footage. It exists for the
case where a clip can never be validly uploaded and the disk space is needed.

```bash
docker compose exec cleanup curl -s localhost:8083/admin/force-delete \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -d '{"clip_id": 8123, "reason": "B2 key permanently invalid for this bucket; duplicate footage exists on camera SD card until 2026-10-01"}'
```

| Requirement | Why |
|---|---|
| One clip at a time | There is no bulk operation, deliberately |
| A written reason | It is recorded in `system_events` with your answer, permanently |
| An explicit decision | The result is the state `FORCE_DELETED`, which stays visible forever |

Write a reason that would make sense to you in six months. `"cleanup"` is not a reason.

---

## 8. Confirm recovery

```bash
./scripts/doctor.sh
curl -s http://<host>:8080/status | jq '.answer, .backlog'
```

You are looking for:

- `failed_permanent: 0`
- `oldest_pending_age_s` under 60
- `clips_overdue_retention: 0`
- the answer sentence mentioning only what is genuinely still wrong

Then verify a clip actually arrived:

```bash
aws s3 ls --recursive s3://<bucket>/<location>/clips/ | tail -5
```

---

## 9. If you cannot fix it

The system is safe to leave broken. It will keep recording, keep materialising clips, and
keep holding them locally. Nothing is deleted. Disk is the only cost.

Before reporting a problem, collect:

```bash
./scripts/doctor.sh --bundle
docker compose logs --tail=500 uploader > uploader.log
docker compose logs --tail=500 clip-worker > clip-worker.log
```

Include the `error_class` from failed clips and what you already tried. The
`error_class` values are stable machine codes, so they make a report directly actionable.
