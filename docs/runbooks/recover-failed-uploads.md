# Runbook: Recover failed uploads

> **Status:** 🚧 Planned.
> **Time:** 5 minutes to diagnose, longer to actually fix.

Clips are not reaching Backblaze B2, or the queue is not draining. This runbook tells you
what to look at, in order, and what to do about each finding.

---

## First: nothing is lost

Say this before doing anything else. By design, a clip that has not uploaded is **never
deleted**, however old it is ([ADR-0009](../adr/0009-cleanup-safety-invariant.md)). Your
footage is on disk. The cost of this problem is **disk growth**, not data loss — for as
long as you leave it unfixed.

So: diagnose calmly, and do not reach for the force-delete option.

---

## 1. What does the system say?

```bash
curl -s http://<host>:8080/status | jq '{answer, backlog, storage}'
```

```json
{
  "answer": "Recording is working. Cloud backup stalled 47 minutes ago.",
  "backlog": {
    "pending": 3,
    "uploading": 1,
    "retry_wait": 0,
    "failed_permanent": 0,
    "oldest_pending_age_s": 2820,
    "clips_overdue_retention": 0
  },
  "storage": { "free_bytes": 318342512640, "projected_days_to_full": 412 }
}
```

| Reading | Meaning | Action |
|---|---|---|
| `pending` high, `failed_permanent` 0, rising | Backlog draining slower than clips arrive, or stuck | §2 |
| `failed_permanent` > 0 | At least one clip will not retry | §4 |
| `oldest_pending_age_s` > 6 h | Serious | §2 |
| `clips_overdue_retention` > 0 | **Clips past their retention date are being held** | §5 |
| Everything zero and `uploaded_24h` healthy | Nothing is wrong | — |

---

## 2. Find the actual error

```bash
docker compose exec uploader curlie localhost:8082/admin/queue
docker compose logs --tail=200 uploader | grep -E '"level":"(ERROR|CRITICAL)"'
```

The logs are JSON. Look at `last_error_class` on the failed clips — it tells you
everything:

| `error_class` | Meaning | Fix |
|---|---|---|
| `transient` | Network, `5xx`, timeout | Wait. This resolves itself, with backoff up to 1 hour |
| `throttled` | Rate limited | Wait. The uploader applies a global pause |
| `auth` | **Credentials rejected** | §3 |
| `not_found` | **Bucket does not exist** | §3 |
| `quota` | Cap reached, or a payment problem | Resolve in the Backblaze console |
| `checksum_mismatch` | The stored object does not match the local file | §4 |
| `file_missing` | The database says a clip exists and it does not | §6 |
| `invalid_request` | Our bug, or a malformed configuration | Report it. Include the logs |

### Backup paths are stale: nothing is reaching the cloud at all

```bash
docker compose exec uploader curlie localhost:8082/health | jq '.checks[] | select(.state!="HEALTHY")'
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
docker compose exec uploader curlie localhost:8082/health | jq '.checks[] | select(.name=="storage_credentials")'
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
   grep B2_BUCKET .env
   docker compose exec uploader curlie localhost:8082/admin/queue | jq '.bucket_expected'
   ```
3. **Is the key scoped to the right bucket?** A key scoped to `cctv-home` cannot write to
   `cctv-parents-home`
4. **Does the key have write permission?** Read-only keys upload nothing
5. **Is the key ID correct?** `B2_KEY_ID` is the *keyID*, not the key *name*

After fixing, reset the failed clips and retry:

```bash
docker compose exec uploader curlie -X POST localhost:8082/admin/retry \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
  -d '{"reason":"rotated B2 application key after 403 errors"}'
```

Watch it drain:

```bash
docker compose exec uploader watch -n2 'curlie -s localhost:8082/admin/queue | jq .counts'
```

---

## 4. Permanent failures

`FAILED_PERMANENT` means the uploader tried 12 times over roughly 5 hours and gave up.
The local file is **still there** and is **not** going to be deleted.

```bash
docker compose exec uploader curlie localhost:8082/admin/queue | jq '.failed_permanent'
```

```json
[
  { "clip_id": 8123, "camera": "front-door", "event_id": "1734567890.123456-abcxyz",
    "start_time": 1757590365.12, "attempts": 12,
    "last_error_class": "checksum_mismatch",
    "last_error": "provider checksum 3a9f… != local sha256 3a2b…" }
]
```

| `last_error_class` | What to do |
|---|---|
| `auth`, `not_found`, `quota`, `invalid_request` | Fix the cause, then `retry` |
| `checksum_mismatch` | **Do not just retry.** Verify the local file first — §4.1 |
| `file_missing` | §6 |
| `transient` after 12 attempts | Something systematic. Check the provider status page, then `retry` |

### 4.1 Investigating a checksum mismatch

Something is wrong with one of the two copies. Find out which before retrying.

```bash
# Verify the local file
docker compose exec clip-worker sha256sum /data/clips/front-door/2026/09/11/18-32-45.mp4

# Compare against what the database recorded
docker compose exec uploader curlie -s localhost:8082/admin/queue \
  | jq '.failed_permanent[] | select(.clip_id==8123) | .sha256'

# Compare against what is actually in the cloud
docker compose exec uploader curlie -X POST localhost:8082/admin/verify \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" -d '{"clip_id":8123}'
```

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

This is the system working as designed: **it will not delete your only copy**
([ADR-0009](../adr/0009-cleanup-safety-invariant.md)). The fix is to make the uploads
succeed, not to delete the clips.

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

The database says a clip exists; the file does not. This means something outside the
system interfered — a manual deletion, a restore that did not include clips, a disk that
lost data, or a partially-completed `restore.sh`.

```bash
docker compose exec cleanup curlie localhost:8083/health | jq '.checks[] | select(.name=="reconciliation")'
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
4. Report it, even if you resolve it. Repeated `file_missing` indicates a real problem.

---

## 7. Force-deleting a clip

**This is the last resort.** It destroys the only local copy of footage. It exists for the
case where a clip can never be validly uploaded and the disk space is needed.

```bash
docker compose exec cleanup curlie -X POST localhost:8083/admin/force-delete \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
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
