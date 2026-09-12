# Milestones and exit criteria

Each milestone has a **demo** (what you can show) and **exit criteria** (what must be
true before the next one starts). Exit criteria are pass/fail, not aspirational — a
milestone that is "mostly done" is not done, because everything after it assumes it.

---

## Summary

| # | Version | Milestone | Demo | Blocking risk retired |
|---|---|---|---|---|
| M1 | `0.1.0` | Walking skeleton | An `.mp4` appears in the right place | Can Frigate give us clips at all? |
| M2 | `0.2.0` | Complete, idempotent materialisation | Kill it repeatedly, lose nothing | Is MQTT + sweep actually complete? |
| M3 | `0.3.0` | Durable upload | Objects in B2, restart-safe | Is the state machine correct? |
| M4 | `0.4.0` | Safe retention | Retention runs, deletes nothing un-backed-up | Can we delete safely? |
| M5 | `0.5.0` | Honest health | One screen answers the question | Do we know when it is broken? |
| M6 | `0.6.0` | One-command install | Fresh machine → working system | Can a stranger install it? |
| M7 | `0.9.0` | Lifecycle | Wipe a machine, recover it; upgrade; roll back | Can it survive years? |
| M8 | `1.0.0` | Public release | A stranger installs it unaided | Is it safe to hand over? |

---

## M1 — Walking skeleton `0.1.0`

### Demo

```text
$ curl -s localhost:8080/health
{"component":"clip-worker","state":"DEGRADED","checks":[{"name":"frigate_api","state":"HEALTHY"}]}

[[ a person walks past the camera ]]

$ ls -R data/clips/
front-door/2026/09/12/14-32-07.mp4
front-door/2026/09/12/14-32-07.mp4.json

$ ffprobe -v error -show_entries format=duration -of csv=p=0 \
    data/clips/front-door/2026/09/12/14-32-07.mp4
25.530000
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M1.1 | A real event produces a clip at the canonical path | Manual, on a real camera |
| M1.2 | The clip plays in a browser and in VLC | Manual |
| M1.3 | Duration is within 5 s of the event's duration | Automated |
| M1.4 | The event is pinned before download and unpinned after | Log assertion |
| M1.5 | The integration contract §9 checklist is **complete**, with the Frigate version recorded | Reviewed and committed |
| M1.6 | Download, remux, and CPU cost measured for a 30 s clip | Recorded in the contract |
| M1.7 | A 404 on the clip is handled without crashing the worker | Automated || M1.8 | **Pre-roll and post-roll are visible in the clip** — a person walking past is on screen before and after the event window | Manual, on a real camera |
| M1.9 | **The segment flush delay is measured** — the time from `event.end_time` to the final segment being retrievable | Recorded in the contract |
| M1.10 | **An immediate fetch is shown to truncate** (or not) — reproducing the failure is what proves the fix is needed | Recorded in the contract |
| M1.11 | **Cut precision with `-c copy` is measured** — does it err outward, and by how much | Decides `max_gap_s` |
| M1.12 | Audio survives the cut on a camera with a microphone | Manual |
### Decision gate

> If clip retrieval is unreliable, slow, or produces unusable video, **stop**. Revisit
> [ADR-0001](../adr/0001-frigate-as-detection-engine.md) before building anything further.

### Explicitly out of scope

Database, uploader, cleanup, health dashboard, installer. A shell command and a log
line is the whole deliverable.

---

## M2 — Complete, idempotent materialisation `0.2.0`

### Demo

```text
$ ./scripts/chaos.sh --kill-during-materialise --iterations 50
  killed at: after-pin(9) after-download(11) after-remux(7) after-rename(12) after-insert(11)
  50 iterations
  clips created:      50   (expected 50)
  duplicate files:     0
  duplicate rows:      0
  clips lost:          0

$ ./scripts/verify-sweep-gap.sh --downtime 30m
  MQTT messages dropped: 47
  events recovered by sweep: 47
  gaps in event sequence: 0
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M2.1 | Five deliveries of one event → one file, one row | Automated (INV-7) |
| M2.2 | `kill -9` at each of five points → no loss, no duplication | Chaotic test, 50 iterations |
| M2.3 | 30 minutes of clip-worker downtime with MQTT dropped → sweep recovers every event | Automated |
| M2.4 | A crash mid-sweep does not advance the watermark | Automated |
| M2.5 | DST forward and backward produce no collisions and no misnamed clips | Automated (INV-12) |
| M2.6 | Two events in the same camera-second produce two distinct deterministic paths | Automated |
| M2.7 | An unknown camera is dropped with a WARNING and no retry loop | Automated |
| M2.8 | An unknown MQTT field does not break parsing | Automated |
| M2.9 | Headroom health transitions `HEALTHY → DEGRADED → UNHEALTHY` before any loss | Automated, with compressed Frigate retention |
| M2.10 | `dev-synthetic` runs the pipeline in CI without cameras | CI green |
| M2.11 | Path function purity and collision-freedom hold under property testing | Automated |
| M2.12 | The uploader is handed clips: every new row is `PENDING` | Automated |
| M2.13 | **Boundary accuracy** — first and last decoded frames match `start − pre_roll` / `end + post_roll` within tolerance | Automated, colour-coded synthetic source ([INV-14](../architecture/invariants.md)) |
| M2.14 | **Asymmetric rolls** — `pre_roll_s: 5, post_roll_s: 20` yields that range, with no 20 s lead-in | Automated |
| M2.15 | **Erring outward** — the clip is never shorter than requested when the footage exists | Automated |
| M2.16 | **Flush race closed** — truncation with `segment_flush_grace_s: 0`, correctness with the default | Automated |
| M2.17 | **Coverage retry** — a 60 % first fetch triggers a delayed re-fetch and the better result is stored | Automated |
| M2.18 | **A short clip is never discarded** — best result stored, `degraded=1`, zero clips lost | Automated |
| M2.19 | **`400 "No recordings found"` is retryable**, not a permanent failure | Automated |
| M2.20 | **Gap detection** — a removed middle segment sets `has_gaps` and a plausible `max_gap_s`, and the clip is kept | Automated |
| M2.21 | **Audio preserved** — a clip from a source with audio has audio, and the codec is recorded | Automated |
| M2.22 | **Long clips stored in full** — a 2-hour event is flagged, never truncated | Automated |

---

## M3 — Durable upload `0.3.0`

### Demo

```text
$ aws s3 ls --recursive s3://cctv-parents-home/parents-home/clips/ | tail -3
2026-09-12 14:32:09     7864320 parents-home/clips/front-door/2026/09/12/14-32-07.mp4
2026-09-12 14:32:09        1204 parents-home/clips/front-door/2026/09/12/14-32-07.mp4.json

$ curl -s localhost:8082/health | jq '.checks[] | select(.name=="backlog_age")'
{"name":"backlog_age","state":"HEALTHY","detail":"0 pending, oldest 0s; 214 uploaded in 24h; 0 failed"}
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M3.1 | Clips appear at the deterministic key | Automated |
| M3.2 | Object key == `<location>/clips/` + relative path, for every row | Automated |
| M3.3 | Three uploads, one with a crash between PUT and state update → one object | Automated (INV-7) |
| M3.4 | B2 unreachable for 1 h with clips arriving → all upload on recovery, none duplicated | Automated |
| M3.5 | Revoked credentials → no retry, `UNHEALTHY`, actionable message, no log storm | Automated |
| M3.6 | Checksum mismatch → `FAILED_PERMANENT`, file preserved, alert | Automated |
| M3.7 | Backlog drains oldest-first | Automated |
| M3.8 | 200 concurrent failures do not produce synchronised retries | Automated |
| M3.9 | `attempts >= max_attempts` → `FAILED_PERMANENT`, file retained, alert | Automated |
| M3.10 | A restart mid-backlog resumes without re-uploading completed clips | Automated |
| M3.11 | The uploader cannot delete a clip (read-only mount) | Automated |
| M3.12 | 8 workers × 500 clips → each uploaded exactly once | Automated |

---

## M4 — Safe retention `0.4.0`

### Demo

```text
$ ./scripts/verify-inv1.sh
  scenario: B2 unreachable for 120 days of simulated time
  cleanup runs:        120
  clips eligible:      1,432
  clips deleted:       0        ← INV-1 holds
  clips held:          1,432
  health:              DEGRADED  CLIPS_OVERDUE_RETENTION
  reported:            "1.4 GB held, growing 300 MB/day, disk full in 41 days"

$ ./scripts/verify-retention.sh --with-cloud-up
  local>90d deleted:   214
  local<=90d preserved: 1,218
  cloud>60d deleted:   186
  deletion verified:   yes (HEAD returns 404)
  orphan objects:      0
  dangling rows:       0
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M4.1 | B2 unreachable → repeated cleanup deletes **zero** clips | Automated (INV-1) |
| M4.2 | `PENDING`, `UPLOADING`, `RETRY_WAIT`, `FAILED_PERMANENT` are never deleted | Automated, for each state |
| M4.3 | A hypothetical future state is undeletable by default | Automated |
| M4.4 | `UPLOADED` clips past `local_days` are deleted with their sidecars | Automated |
| M4.5 | Cloud retention counts from `uploaded_at` | Automated |
| M4.6 | A delete that only hides an object is **not** recorded as success | Automated |
| M4.7 | The B2 lifecycle rule is applied and read back | Automated |
| M4.8 | Orphans and dangling rows are reported and never auto-repaired | Automated (INV-10) |
| M4.9 | A `clips.path` of `../../etc/passwd` deletes nothing | Automated |
| M4.10 | `data/media/` and `data/frigate/` are byte-identical after a full cycle | Automated |
| M4.11 | Removing a camera deletes nothing | Automated (INV-13) |
| M4.12 | `DRY_RUN=1` deletes nothing and logs every intention | Automated |
| M4.13 | `force-delete` requires a clip id and a reason, and records `FORCE_DELETED` | Automated |
| M4.14 | Two cleanup instances cannot run concurrently | Automated |

---

## M5 — Honest health `0.5.0`

### Demo

```text
$ curl -s localhost:8080/status | jq '{overall, answer, cameras: [.cameras[]|{name,state}]}'
{
  "overall": "DEGRADED",
  "answer": "Recording is working. Cloud backup stalled 47 minutes ago.",
  "cameras": [
    {"name":"front-door","state":"ONLINE"},
    {"name":"garage",    "state":"ONLINE"},
    {"name":"backyard",  "state":"OFFLINE"}
  ]
}

$ ./scripts/verify-alert-quiet.sh
  simulated: 7 days, healthy system
  notifications sent: 0        ← no noise
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M5.1 | Stopping each dependency produces the correct non-healthy state in time | Automated (INV-6) |
| M5.2 | A stopped reporter becomes `UNKNOWN`, never stays `HEALTHY` | Automated |
| M5.3 | No component ever returns a literal `"healthy"` | Static check + review |
| M5.4 | The answer names the specific problem for each of the eight questions | Automated, one fixture each |
| M5.5 | A camera online with events stopped becomes `QUIET` | Automated |
| M5.6 | A camera with little history never becomes `QUIET` | Automated |
| M5.7 | A 60 s camera flap raises no alert; 10 minutes does | Automated |
| M5.8 | A calm system produces zero notifications in a simulated week | Automated |
| M5.9 | Every alert type has a matching recovery notification | Automated |
| M5.10 | Stopping the collector makes the monitoring tool alert | Automated |
| M5.11 | Disk projection is suppressed below 3 days of history | Automated |
| M5.12 | `/status` HTML is complete with JavaScript disabled | Automated |
| M5.13 | ntfy and Telegram each receive a test notification during install | Manual, once |
| M5.14 | Partial observation still renders a useful page | Automated |

---

## M6 — One-command install `0.6.0`

### Demo

```text
$ ssh fresh-debian-box
$ git clone https://github.com/<owner>/cctv-stack && cd cctv-stack
$ ./install.sh
  [detection, warnings, interview]
  [1/7] Frigate API reachable ……………………… ✓
  ...
  [7/7] Cameras online ……………………………… 2/2
  Installation complete.
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M6.1 | Clean Debian: clone + `install.sh` → healthy, recording, uploading | Manual, on a fresh VM |
| M6.2 | Raspberry Pi 5: installs, detects the profile, warns about CPU limits | Manual, on real hardware |
| M6.3 | An unsupported host is refused with an explanation | Automated |
| M6.4 | Detection returns `unknown` rather than guessing for unconfirmable capabilities | Automated |
| M6.5 | A corpus of invalid configs produces specific codes, `fix` strings, exit 78 | Automated (INV-11) |
| M6.6 | Re-running `install.sh` is idempotent and deletes nothing | Automated |
| M6.7 | An existing `.env` is preserved and diffed, never clobbered | Automated |
| M6.8 | A bad B2 key fails during install with an actionable message | Automated |
| M6.9 | Foreign lifecycle rules survive a re-install | Automated |
| M6.10 | The probe object round-trip catches the hide-versus-delete trap | Automated |
| M6.11 | `--yes` completes with no prompts | Automated |
| M6.12 | Ctrl-C during each phase leaves a re-runnable state | Automated |
| M6.13 | The diagnostics bundle contains no credentials | Automated |
| M6.14 | `uninstall.sh` default leaves `DATA_DIR` byte-identical | Automated |
| M6.15 | `--purge` requires the typed location name | Automated |
| M6.16 | Every generated file carries a `GENERATED` header | Automated |
| M6.17 | Regenerating an unchanged config differs only in the header timestamp | Automated |
| M6.18 | `shm_size` is sized from camera count and resolution | Automated |

---

## M7 — Lifecycle `0.9.0`

### Demo

```text
$ ./backup.sh --verify
  state-20260912T140000Z.db        integrity ok, schema 7, 14,203 clips
  config-20260912T140000Z.tar.gz   encrypted, cameras.yml valid, 3 cameras
  manifest check                   ok
  clip count: backup 14,203 / live 14,203 ✓

$ ./update.sh
  [1/8] … [8/8]
  Upgrade complete. Running version 0.9.0.
  Rollback: ./update.sh --rollback

$ ./restore.sh --scenario B --from /mnt/usb/backups
  cameras restored: 3     clips verified: 14,203
  doctor: all checks pass
```

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M7.1 | Full recovery on a clean machine yields correct cameras, clips, and health | Automated, end to end |
| M7.2 | Cloud clip recovery restores every object to its predicted path | Automated |
| M7.3 | `state.db` reconstruction without a backup classifies all four categories | Automated |
| M7.4 | Reconstruction never trusts a hash from the manifest | Automated |
| M7.5 | Unknown-state clips become `PENDING`, never `UPLOADED` | Automated |
| M7.6 | `backup.sh` never uses `cp` on a live database | Automated |
| M7.7 | A corrupt source aborts the backup with a clear message | Automated |
| M7.8 | A truncated output fails verification | Automated |
| M7.9 | An unencrypted config bundle is refused without `--unencrypted` | Automated |
| M7.10 | All backup artefacts are `chmod 600` | Automated |
| M7.11 | An upgrade preserves every clip row and upload state | Automated (INV-8) |
| M7.12 | A failing migration restores the backup and prints the rollback command | Automated |
| M7.13 | `--rollback` produces a working installation | Automated |
| M7.14 | Clips created during the upgrade window are recovered by reconciliation | Automated |
| M7.15 | Recording continues throughout an upgrade | Automated |
| M7.16 | A MAJOR skip is refused with the intermediate step named | Automated |
| M7.17 | Digest mismatch aborts the upgrade | Automated |
| M7.18 | A non-empty upload backlog produces an explicit pre-upgrade warning | Automated |
| M7.19 | `--migrate-config` preserves comments and is idempotent | Automated |
| M7.20 | `cameras.yml` is never modified without confirmation | Automated |
| M7.21 | Backups are taken for every released version and migrate cleanly | CI, on all snapshots |

---

## M8 — Public release `1.0.0`

### Demo

Give the repository URL to someone who has never seen the project, and watch. Do not
help.

### Exit criteria

| # | Criterion | Verified by |
|---|---|---|
| M8.1 | Every one of INV-1…INV-13 has a named, passing test | CI reports per invariant |
| M8.2 | Upgrade tested from every released version | CI |
| M8.3 | Rollback tested | CI |
| M8.4 | arm64 image builds and passes a smoke test | CI |
| M8.5 | Secret scan clean | CI |
| M8.6 | All digests resolve and match their tags | CI |
| M8.7 | `api_contract_verified` == `frigate.version` | CI gate |
| M8.8 | Docs: no dead links, no unresolved `TODO`s | Automated check |
| M8.9 | A stranger installs it from the README without asking a question | Manual, observed |
| M8.10 | All five runbooks exist and have been followed by someone other than the author | Manual |
| M8.11 | Licence, contributing guide, security policy, code of conduct present | Review |
| M8.12 | Release notes include a breaking-changes section (or an explicit "none") | Template enforced |

---

## Progress tracking

Update this table as milestones complete. An agent picking up work should read it first
to know the current state.

| Milestone | Status | Version | Completed |
|---|---|---|---|
| M1 Walking skeleton | ⬜ not started | `0.1.0` | — |
| M2 Materialisation | ⬜ not started | `0.2.0` | — |
| M3 Durable upload | ⬜ not started | `0.3.0` | — |
| M4 Safe retention | ⬜ not started | `0.4.0` | — |
| M5 Honest health | ⬜ not started | `0.5.0` | — |
| M6 One-command install | ⬜ not started | `0.6.0` | — |
| M7 Lifecycle | ⬜ not started | `0.9.0` | — |
| M8 Public release | ⬜ not started | `1.0.0` | — |

**Status key:** ⬜ not started · 🟡 in progress · ✅ complete · ⛔ blocked
