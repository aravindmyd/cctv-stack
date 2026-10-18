# LLD 05 — cleanup

**Implements:** [INV-1](../invariants.md), [INV-10](../invariants.md), [INV-13](../invariants.md)
**Specified by:** [`contracts/database-schema.md`](../contracts/database-schema.md), [`contracts/cloud-object-layout.md`](../contracts/cloud-object-layout.md)

Retention enforcement and reconciliation. This is the only component in the system
with the ability to destroy data, and therefore the one that must be the most
conservative.

---

## 1. Responsibilities

| Job | Cadence | Responsibility |
|---|---|---|
| **J1 — Local retention** | hourly | Delete local clip files past `local_days`, **only** when the upload state is terminal-success |
| **J2 — Cloud retention** | hourly | Delete cloud objects past `cloud_days`, verify the deletion actually took effect |
| **J3 — Reconciliation** | daily | Report orphan objects, dangling rows, checksum mismatches, unexpected local files |
| **J4 — Housekeeping** | daily | Empty directories, `*.bad` files, old `db-backups/`, health-history pruning |
| **J5 — Retention reporting** | hourly | Report clips held past retention because uploads have not succeeded |

**J5 exists so that J1 can be safe.** The alternative to holding an un-uploaded clip
forever is deleting it; the alternative to deleting it is unbounded disk growth. J5
makes the growth visible and actionable rather than a silent trend.

---

## 2. The safety invariant, structurally

[INV-1](../invariants.md) is the reason this component exists in the form it does.
The implementation must make the unsafe thing *impossible*, not merely unlikely.

### 2.1 The query is the only entry point

```sql
SELECT id, camera, path, sidecar_path, size_bytes, sha256, upload_state, start_time
  FROM clips
 WHERE upload_state IN ('UPLOADED', 'SKIPPED_BY_POLICY')
   AND start_time < :cutoff
   AND local_deleted_at IS NULL
 ORDER BY start_time ASC
 LIMIT :batch;
```

The `IN`, not a negation. Enumerating the states that permit deletion means a **new
state added later is safe by default** — it will not be deleted until someone
deliberately adds it to this list and thinks about it.

An implementation using `WHERE upload_state NOT IN ('PENDING', …)` would be
catastrophically wrong: any future state would immediately become deletable.

### 2.2 Five independent guards

| # | Guard | Catches |
|---|---|---|
| 1 | The query admits only `UPLOADED` / `SKIPPED_BY_POLICY` | The primary defence |
| 2 | Per-clip assertion before unlink: re-read the row, re-check the state | A state change between the query and the unlink (a race) |
| 3 | For `UPLOADED` clips: `cloud_key IS NOT NULL` **and** `uploaded_at IS NOT NULL` | A row marked uploaded without a recorded upload |
| 4 | Path resolution must stay within `data/clips/` | A corrupted or tampered `path` value |
| 5 | `DRY_RUN` mode logs intended deletions without performing them | Operator verification before an unusual change |

### 2.3 What cleanup never does

| Never | Why |
|---|---|
| Walk the filesystem to decide what to delete | Filesystem-derived deletion loses a file the moment the DB disagrees with the disk |
| Delete a clip with any non-terminal upload state | [INV-1](../invariants.md) |
| Delete a clip for a retired camera differently | Retired cameras keep normal retention ([INV-13](../invariants.md)) |
| Delete a `.mp4` without its `.json`, or vice versa | Ordering matters — see §3.3 |
| Delete anything under `data/media/` or `data/frigate/` | Frigate's property |
| List the bucket to decide what to delete | [INV-10](../invariants.md) — listing is for reconciliation only |
| Re-upload, repair, or re-materialise | Not this component's job |
| Auto-resolve a reconciliation finding | Findings are reported; repair is an audited operator action |

---

## 3. J1 — Local retention

### 3.1 Algorithm

```text
for each camera in cameras (including retired):
    cutoff = now − local_days(camera) × 86400
    for batch of clips matching the query:
        for clip in batch:
            assert clip.upload_state in {'UPLOADED', 'SKIPPED_BY_POLICY'}
            if clip.upload_state == 'UPLOADED':
                assert clip.cloud_key and clip.uploaded_at

            path = resolve_within_clips_dir(clip.path)     # guard 4
            if not path.exists():
                # Already gone. Record it and move on — do NOT treat as an error.
                mark_local_deleted(clip)
                record_finding('missing_local_file', clip)
                continue

            unlink(path)
            unlink_sidecar_if_present(clip.sidecar_path)
            mark_local_deleted(clip)
            emit_log(INFO, 'CLIP_LOCAL_EXPIRED', clip)

        commit after each batch       # bounded transaction
```

### 3.2 Ordering within a clip

1. Delete the `.mp4`
2. Delete the `.json`
3. Update the row

A crash between 1 and 2 leaves an orphan sidecar — harmless, reaped by J4. A crash
between 2 and 3 leaves a row pointing at nothing — detected by J3. Neither order of
failure loses data.

The **reverse** order (sidecar first) would be worse: a crash would leave a clip with
no metadata, and the sidecar is what makes a future recovery possible.

### 3.3 Batch commits

One transaction per batch (default 200 clips), not per clip and not for the whole run.

| Choice | Reason |
|---|---|
| Not per clip | 100 000 transactions a year is wasteful, and the lock churn is unnecessary |
| Not whole run | A crash mid-run would roll back work already done on disk, leaving the DB claiming files exist that are gone |
| Per batch | Bounded rollback, bounded lock, observable progress |

The asymmetry is deliberate: it is always safer for the DB to *under*-report a
deletion (the file is gone but the row still says it exists) than to over-report one.
J3 catches the former.

### 3.4 Dry run

`DRY_RUN=1` or the `--dry-run` flag logs every intended deletion with the camera,
path, size, and upload state, and performs nothing. The install and runbook
documentation points operators here first when retention behaviour is surprising.

---

## 4. J2 — Cloud retention

### 4.1 Algorithm

```text
cutoff = now − cloud_days(camera) × 86400
for batch of clips where upload_state = 'UPLOADED' and uploaded_at < cutoff:
    key = clip.cloud_key or derive(clip)        # derive as a fallback, and log it
    result = adapter.delete_object(key=key)
    if result.actually_removed:
        mark_cloud_deleted(clip)                # → EXPIRED_CLOUD
    else:
        record_finding('cloud_delete_failed', clip)
        # state stays UPLOADED; retried next run; health degrades after N failures
```

`actually_removed` is the crux. A provider that reports success while only creating a
hide marker would leave the object billed forever. See
[`cloud-object-layout.md` §5.3](../contracts/cloud-object-layout.md#53-file-versions-and-the-deletion-trap).

Verification, when `storage.verify_after_delete` is true (default): a `HEAD` on the key
must return not-found. If it still exists, the deletion is **not** recorded as
successful.

### 4.2 Idempotency

A `DELETE` on a missing object is treated as success. The desired end state is "the
object does not exist", and that state is reached.

### 4.3 Base time: `uploaded_at`, not `start_time`

Cloud retention counts from when the clip was **uploaded**, not when it was recorded.

Why: if the uploader was down for two weeks and then caught up, `start_time`-based
retention would immediately expire fourteen days of freshly-uploaded clips — deleting
the backup we just spent two weeks creating. `uploaded_at` gives every object its full
`cloud_days` of life, which is what an operator expects and what makes the B2 lifecycle
rule (which is `daysFromUploadingToHiding`) agree with us.

The two mechanisms must use the **same base time**, or they will disagree in confusing
ways. They do.

---

## 5. J3 — Reconciliation

The enforcement mechanism for [INV-10](../invariants.md). Runs daily; **reports only**.

### 5.1 What it compares

```text
Listing under <location>/clips/          ← the provider
    vs
Rows in clips where cloud_key IS NOT NULL ← state.db
```

| Finding | Definition | Severity |
|---|---|---|
| `orphan_object` | Object exists, no row references it | `warning` — cost, and possibly a clip cleanup will never touch |
| `dangling_row` | Row says `UPLOADED`, object does not exist | **`critical`** — we believe a backup exists and it does not |
| `checksum_mismatch` | Object metadata `sha256` ≠ row `sha256` | **`critical`** — one of the two copies is wrong |
| `unexpected_local_file` | File under `clips/` with no row | `warning` — a partial write survived, or a row was lost |
| `missing_local_file` | Row with `local_deleted_at IS NULL`, file absent | `warning` — expected after a bad crash; informational |
| `key_mismatch` | `cloud_key` ≠ `location/clips/` + `path` | **`critical`** — key construction is broken |

### 5.2 Cost control

The listing is paginated and rate-limited. On a site with 5 000 objects it is
trivially cheap; the job is written to remain linear if that number grows a hundredfold.

The listing is **never** used to decide a deletion. It only produces findings.

### 5.3 Findings are not repairs

Findings are written to `reconciliation_findings` and surfaced through health and
`/status`. Repair requires an explicit operator command with a reason, which is
audited.

**Why no auto-repair.** Because every repair is a guess about which side is right.
Auto-deleting an "orphan" object may delete the only surviving copy of a clip whose
row was lost to a database restore. Auto-re-uploading a "dangling" row may be correct
or may be pointless. A human with the finding in front of them, and a reason field to
fill in, makes a better decision than a heuristic.

The one exception is benign and explicit: `missing_local_file` rows are marked
`local_deleted_at` during J1 when the file is already absent, because that is not a
guess — the desired state has been reached.

---

## 6. J4 — Housekeeping

| Task | Rule | Why |
|---|---|---|
| Empty directories | Remove `clips/<camera>/YYYY/MM/DD/` when empty, then `MM/`, `YYYY/` | Keeps the browsable tree browsable |
| `*.bad` files | Delete older than 7 days | Diagnostic value decays; the audit row keeps the record |
| Stale `.tmp` in `clips/` | Delete older than 24 h | Belt-and-braces with the clip-worker's own reaper |
| `db-backups/` | Keep the newest 10 | Bounded |
| `health_history` | Prune past 7 days | Ring buffer |
| `upload_attempts` | Prune past 30 days, except non-terminal clips | Debugging value decays; correctness value does not |
| `system_events` | Prune past 90 days | Long enough to investigate a report |
| `pins` table | Prune released/expired pins past 30 days | |

**Never reaped by J4:** anything under `data/media/`, `data/frigate/`, or any `.mp4`
or `.json` under `data/clips/`.

---

## 7. J5 — Retention reporting

```text
clips_overdue = COUNT(clips)
                WHERE upload_state NOT IN ('UPLOADED','SKIPPED_BY_POLICY')
                  AND start_time < now − local_days × 86400
```

| Value | Health | Meaning |
|---|---|---|
| 0 | `HEALTHY` | Retention is intact |
| 1–10 | `DEGRADED`, `CLIPS_OVERDUE_RETENTION` | A small backup backlog is being held safely |
| > 10 or any older than 30 days | `UNHEALTHY` | Disk growth is unbounded until the upload problem is fixed |

Reported with the total size held, because "37 clips held" is abstract and "1.4 GB
held, growing 300 MB/day, disk full in 41 days" is a decision.

This is the honest version of the trade [INV-1](../invariants.md) makes: we will hold
your data rather than delete it, and here is exactly what that costs.

---

## 8. Scheduling and concurrency

| Job | Cadence | Concurrency note |
|---|---|---|
| J1 local | 3600 s | Single-threaded |
| J2 cloud | 3600 s | Bounded parallelism (4) for deletes |
| J3 reconciliation | 86400 s | Sequential paginated listing |
| J4 housekeeping | 86400 s | Single-threaded |
| J5 reporting | 3600 s | Runs with J1 |

**Cleanup is single-instance by construction:** only one container runs it, and it
takes a database-level advisory lock (a row in `sweep_state` with a lease) so that two
instances cannot run simultaneously even if someone scales the service by mistake. Two
concurrent cleanups would be survivable but confusing.

> **Not implemented (2026-10-18).** `sweep_state` has no lease columns and no lock is
taken; M4.14 is deferred by [ADR-0021](../../adr/0021-defer-force-delete-and-advisory-lock.md).
> Deployments are a single compose stack on one host, operated by hand, so the scenario this
> guards against cannot arise. The guards that prevent a duplicate pass from deleting an
> ineligible clip are the ones in `src/cctv/cleanup.py`, and they are built and tested. The
> lock should arrive with J2 cloud retention at the latest — see the ADR's revisit triggers.

J1 and J2 run sequentially within a cycle so that cloud deletion never races local
deletion of the same clip.

---

## 9. Health checks

| Check | HEALTHY | DEGRADED | UNHEALTHY |
|---|---|---|---|
| `state_db` | ok | — | cannot query |
| `clips_dir` | readable, writable | — | not writable |
| `storage_reachable` | `HEAD` ok | latency high | fails (cloud retention cannot run) |
| `last_run` | < 2 × `run_interval_s` | 2–6 × interval | > 6 × interval |
| `clips_overdue_retention` | 0 | 1–10 | > 10 or oldest > 30 days |
| `cloud_delete_failures` | 0 | 1–5 | > 5 |
| `reconciliation_findings` | 0 unresolved | any `warning` | any `critical` |
| `disk_free` | > 15 % | 5–15 % | < 5 % or projected full < 30 days |

`storage_reachable` failing makes cloud retention impossible, but local retention still
works — so this is `DEGRADED`, not `UNHEALTHY`. Partial capability is reported as
partial, per [INV-6](../invariants.md).

---

## 10. Force delete

The only path by which an un-uploaded clip is ever removed.

```http
POST /admin/force-delete
{ "clip_id": 8123, "reason": "B2 key permanently invalid; footage duplicated on camera SD" }
```

| Requirement | Detail |
|---|---|
| Explicit `clip_id` | No filters, no bulk. One clip at a time, deliberately tedious |
| Mandatory `reason` | Written to `system_events` with the resulting state |
| Resulting state | `FORCE_DELETED`, which is terminal and visible in `/status` forever |
| Never available from a scheduled job | Only via the admin endpoint |
| Never the default answer | The runbook's first suggestion is to fix the upload, not to delete |

The friction is the feature. `FORCE_DELETED` exists in the state machine so that the
act of giving up on a clip leaves a permanent, countable record rather than looking
identical to a successful upload.

> **Not implemented (2026-10-18).** No `/admin/force-delete` route exists; M4.13 is deferred
> by [ADR-0021](../../adr/0021-defer-force-delete-and-advisory-lock.md). The state, the
> `FAILED_PERMANENT → FORCE_DELETED` transition and the `system_events` audit table are all in
> place, so implementing it later is a handler plus a route rather than a schema change. Until
> then the only removal path is the manual one in
> [`docs/recover-uploads.md`](../../../docs/recover-uploads.md) §6, which is not audited by the
> software.

---

## 11. Configuration

Environment variables, as everywhere else in this project: there are no CLI arguments to get
wrong. The names below are the `CCTV_CLEANUP_*` / `CCTV_HEALTH_*` forms.

| Key | Default | Effect |
|---|---|---|
| `run_interval_s` | 3600 | J1/J2/J5 cadence |
| `reconcile_interval_s` | 86400 | J3 cadence — **not implemented yet** |
| `housekeeping_interval_s` | 86400 | J4 cadence — **not implemented yet** |
| `batch_size` | 200 | Clips per transaction |
| `delete_concurrency` | 4 | Parallel cloud deletes — J2 |
| `clips_overdue_warn` / `clips_overdue_critical` | **1 / 10** | J5 thresholds, per §9's behaviour table. §11 previously said `10 / 10`, which contradicts §9 and would have made a single held clip invisible |
| `overdue_age_critical_days` | 30 | A clip held longer than this past its deadline is `UNHEALTHY`, however few are held |
| `disk_warn_pct` / `disk_critical_pct` | 15 / 5 | Free-space thresholds, shared with every other service's health surface |
| `verify_after_delete` | `true` | `HEAD` after `DELETE` — J2 |
| `delete_failure_critical` | 5 | `cloud_delete_failures` threshold — J2 |
| `keep_db_backups` | 10 | Rotation — J4 |
| `local_days` / `cloud_days` | 90 / 60 | Defaults; overridable per camera |
| `retire_grace_days` | 0 | Extra local retention for retired cameras, if an operator wants it |

> **Implemented so far (J1):** `run_interval_s`, `batch_size`, `retire_grace_days`,
> `clips_overdue_warn`, `clips_overdue_critical`, `overdue_age_critical_days`,
> `disk_warn_pct`, `disk_critical_pct`, plus the health host and port. The J2/J3/J4 rows above
> are the design for the jobs that have not been built; they are listed so the shape is
> visible, not because they do anything yet.

---

## 12. Tests

| Test | Asserts |
|---|---|
| **INV-1** | With B2 unreachable, run cleanup repeatedly → **zero** clips deleted |
| **INV-1** | A `FAILED_PERMANENT` clip is never deleted, however old |
| **INV-1** | `PENDING` and `RETRY_WAIT` clips are never deleted |
| New-state safety | Adding a hypothetical future state does not make it deletable (the query enumerates permitted states) |
| Uploaded clip expiry | An `UPLOADED` clip past `local_days` is deleted, file and sidecar, and marked `DELETED_LOCAL` |
| Dry run | `DRY_RUN=1` deletes nothing and logs every intended deletion |
| Path escape | A `clips.path` of `../../etc/passwd` raises and deletes nothing |
| Missing file | An already-absent file is recorded, not treated as an error |
| Crash mid-batch | A crash between unlink and row update is detected by J3 as `missing_local_file` |
| Cloud base time | A clip uploaded 2 weeks late gets its full `cloud_days`, not immediate expiry |
| Delete verification | A provider that only hides an object leaves the row at `UPLOADED` and raises a finding |
| Orphan object | An injected orphan object is reported, and **not** deleted |
| Dangling row | An injected dangling row is reported as `critical` |
| Auto-repair refusal | No finding is ever resolved without an explicit command |
| Retired camera | Removing a camera deletes nothing on the next cleanup run ([INV-13](../invariants.md)) |
| Single instance | A second cleanup instance cannot acquire the advisory lock |
| Overdue reporting | Held clips are counted and reported with total size |
| Force delete | Requires a `clip_id` and a non-empty `reason`; results in `FORCE_DELETED`; audited |
| Sibling preservation | `data/media/` and `data/frigate/` are byte-identical before and after a full cleanup cycle |
