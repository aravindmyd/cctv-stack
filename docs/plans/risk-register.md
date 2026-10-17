# Risk register

Risks that could prevent the project from delivering what the PRD asks for. Reviewed at
each milestone boundary.

**Scoring:** Likelihood and Impact are L/M/H. Each risk has an owner action — a risk with
no action is an accepted risk, and should say so explicitly.

---

## Top risks at a glance

| # | Risk | Likelihood | Impact | Status |
|---|---|---|---|---|
| R1 | Frigate's API or MQTT behaviour differs from the contract | M | H | Mitigate |
| R2 | Disk exhaustion destroys clips and recordings | **H** | **H** | Mitigate |
| R3 | B2 versioning makes "deleted" objects billable forever | M | M | Mitigate |
| R4 | A cleanup bug deletes un-backed-up footage | L | **H** | Mitigate |
| R5 | Integration cannot be verified without real hardware/cameras | **H** | M | Mitigate |
| R6 | Raspberry Pi performance disappoints | M | M | Accept + inform |
| R7 | Monitoring tool cannot be provisioned automatically | M | M | Mitigate |
| R8 | Frigate's recording retention expires footage before we copy it | M | H | Mitigate |
| R9 | A cleanup service cannot run for weeks | L | M | Accept |
| R10 | Scope creep delays a usable release | M | M | Mitigate |
| R11 | Secret leaks into the repository or a diagnostics bundle | L | **H** | Mitigate |
| R12 | SQLite corruption from a misused backup or restore | M | H | Mitigate |
| R13 | Upgrade fails and leaves a broken installation | M | H | Mitigate |
| R14 | Real cameras have RTSP quirks the defaults do not handle | **H** | M | Mitigate |
| R15 | Nobody can install it without help | M | H | Mitigate |
| R16 | Clips are generated but are unusable — truncated, holed, or missing the action | **H** | **H** | Mitigate |

---

## R1 — Frigate contract drift

**Risk.** Frigate's API, MQTT payloads, or behaviour differ from what
[`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md)
documents. The design depends on several specific behaviours, most importantly that
`GET /api/events/{id}/clip.mp4` returns a usable clip and that
`POST /api/events/{id}/retain` protects source footage.

**Likelihood:** M — the contract was derived from Frigate's `dev` branch and the docs,
not from a running pinned instance. Some details are explicitly marked `VERIFY`.

**Impact:** H — a wrong assumption invalidates the clip-worker's design, which is the
foundation of everything else.

**Mitigation**
- M1 exists specifically to verify the contract before anything is built on it, and its
  exit criteria require the §9 checklist to be complete and recorded
- All usage is confined to one contract document and one adapter module, so a correction
  is localised
- The Frigate version is pinned by digest
  ([ADR-0014](../adr/0014-pinned-image-digests.md)), so drift cannot arrive unnoticed
- The parser tolerates unknown fields, so additive upstream changes are harmless
- The runtime version check reports `FRIGATE_VERSION_MISMATCH` as `DEGRADED`

**Residual risk:** a Frigate release changes behaviour within a pinned version. Low.

**Owner action:** complete the contract checklist in M1. Do not proceed past M1 without it.

---

## R2 — Disk exhaustion

**Risk.** Storage fills. This is the **highest-severity** risk because it is the only
failure that can destroy both new clips and Frigate's existing recordings, and because
it degrades gradually right up until it is catastrophic. Frigate's own emergency
behaviour (deleting the oldest hour of recordings when under an hour of space remains)
does not protect our clips.

Compounding factor: by design ([INV-1](../architecture/invariants.md)), cleanup will
**not** delete un-uploaded clips, so a stalled uploader causes unbounded local growth.
This is the correct trade, but it means disk pressure is the most likely way an operator
gets hurt.

**Likelihood:** H — it is a matter of when, not if, at some point in a deployment's life.

**Impact:** H — loss of recent recordings, and eventual total pipeline failure.

**Mitigation**
- Continuous recording is **disabled by default**; the default motion window is 3 days
- The installer projects usage from camera count and resolution, and warns or refuses
  if the projection exceeds available space, **stating the arithmetic**
- `projected_days_to_full` is a first-class health metric, computed from a 7-day
  trailing growth rate
- `DISK_PRESSURE` at 15 % free, `DISK_CRITICAL` at 5 % or fewer than 30 projected days
- `clips_overdue_retention` reports the bytes held specifically because uploads have not
  succeeded, with a growth rate and a projected fill date
- The status page shows bytes free and projected days as two of its four numbers
- `FORCE_DELETED` exists as a deliberate, audited escape valve
- The disk-full path disables the pipeline and alerts rather than retry-storming

**Residual risk:** an operator ignores the alerts. Mitigated by making them `critical`
and early.

**Owner action:** confirm the default recording retention in
[OQ-10](open-questions.md#oq-10--frigate-recording-retention-defaults).

---

## R3 — B2 versioning makes "deleted" objects billable forever

**Risk.** B2 retains file versions. An S3 `DELETE` against a bucket that keeps all
versions creates a hide marker; the object still exists and still counts toward storage.
A cleanup service that reports success would silently grow the bill forever.

**Likelihood:** M — easy to miss, hard to notice (nothing fails).

**Impact:** M — ongoing cost, no data loss.

**Mitigation**
- The bucket is configured so deletes take effect, verified during install
- A lifecycle rule with `daysFromHidingToDeleting: 1` on the site prefix purges any
  hidden version
- `cleanup` verifies deletion with a `HEAD`; a delete that did not take effect is **not**
  recorded as `EXPIRED_CLOUD`
- The install runs a write-read-delete-verify probe, catching the trap at install time
- Documented explicitly as a trap in
  [`cloud-object-layout.md`](../architecture/contracts/cloud-object-layout.md#53-file-versions-and-the-deletion-trap)

**Residual risk:** a provider API change in how hiding is expressed. Covered by the
`VERIFY` note in the cloud contract.

**Owner action:** none once implemented; verify during M4.

---

## R4 — A cleanup bug deletes un-backed-up footage

**Risk.** The one catastrophic failure mode this project could have. A bug in the
retention query, a schema change, a path-resolution error, or an ill-considered "delete
orphans" feature destroys the only copy of footage. It would present as a healthy
system with footage missing from a period — discovered months later.

**Likelihood:** L — deliberately engineered against, with five independent guards.

**Impact:** H — irreversible loss of the thing the system exists to protect.

**Mitigation** — all of these are requirements, not aspirations:
- The deletion query **positively enumerates** permitted states, so a new state is safe
  by default
- A per-clip assertion re-checks state immediately before the unlink
- `UPLOADED` rows must also have `cloud_key` and `uploaded_at`
- Paths are resolved and validated to stay within `data/clips/` before every unlink
- Cleanup never walks the filesystem
- `DRY_RUN` is honoured by every writer
- [INV-1](../architecture/invariants.md) has a dedicated test that blocks B2 for 120
  simulated days and asserts **zero** deletions
- Reconciliation **reports only** and never auto-repairs
- `data/media/` and `data/frigate/` are asserted byte-identical before and after a full
  cleanup cycle
- The uploader mounts `clips/` read-only, so it structurally cannot delete

**Owner action:** none. Reviewed at M4 exit.

---

## R5 — Integration cannot be verified without real hardware and cameras

**Risk.** CI has no Coral, no Raspberry Pi, no real RTSP cameras, and no real B2 bucket.
Untested paths — device passthrough, accelerator configuration, real camera streams,
provider quirks — fail on an operator's machine rather than in CI.

**Likelihood:** H — this is inherent to a hardware-integration project.

**Impact:** M — broken installs and support burden, rather than data loss.

**Mitigation**
- `dev-synthetic` profile exercises the full pipeline in CI without cameras or hardware
- RustFS provides an S3-compatible test double, so the adapter is exercised end to end
- Detection is tested against **recorded fixtures** (`uname`, `/proc`, `lsusb`, `lspci`),
  covering hardware we do not own
- `doctor.sh` produces a structured diagnostics bundle, turning "it didn't work" into a
  supportable report with the detection JSON included
- The install verification phase catches hardware problems at install time, not later
- The host profile is deliberately conservative when uncertain, so the failure mode is
  "slower than it could be" rather than "broken"
- M6 exit criteria require testing on real Raspberry Pi and x86 hardware

**Residual risk:** exotic hardware. Documented as best-effort in the support matrix.

**Owner action:** if possible, acquire or borrow one Coral TPU and one Hailo for M6
verification. Otherwise accept untested profiles for those accelerators and label them
as such.

---

## R6 — Raspberry Pi performance disappoints

**Risk.** The owner has a Raspberry Pi, and it is a popular target. CPU-only detection
is genuinely slow, and a Pi running four cameras will be hot, slow, and may drop
detection frames. Detection quality degrades before it fails visibly, so the operator
may not realise anything is wrong.

**Likelihood:** M.

**Impact:** M — disappointment and abandonment, not data loss.

**Mitigation**
- The `pi-cpu` profile caps cameras (2), detection resolution (640×360), and framerate (3)
- The installer **states the limitation explicitly** during detection, before the
  operator commits
- Continuous recording is disabled by default for this profile
- The install recommends a Coral or Hailo, and the reference hardware page makes the
  trade clear
- `DETECTION_FALLING_BEHIND` is a health check, so a Pi that cannot keep up reports it
  rather than silently dropping work
- SD-card wear is warned about, with a USB/NVMe storage recommendation

**Residual risk:** the owner runs it anyway on a Pi and is unhappy. This is an informed
choice by then.

**Owner action:** decide whether a Pi is a first-class target or a "supported with
stated limits" target. Current design assumes the latter.

---

## R7 — Monitoring tool cannot be provisioned automatically

**Risk.** If the chosen monitoring tool has no official API that can create monitors,
the installer cannot configure it, and the operator must click through an import or set
up monitors by hand. That is both friction and a source of drift, since cameras change.

**Likelihood:** M — Uptime Kuma has exactly this problem. Gatus does not.

**Impact:** M — an extra manual step, and monitoring that can silently be wrong.

**Mitigation**
- The `MonitoringAdapter` interface makes the tool a configuration choice
- Gatus is recommended specifically because it is config-file driven
- If Uptime Kuma is chosen, the installer generates an import file and prints exact
  instructions, and the step is documented in the install runbook
- Dynamic items (per-camera state) use **push heartbeats**, so the tool's config does not
  need to change when cameras are added — which reduces the drift risk regardless of tool
- The installer verifies that monitors exist after provisioning, and reports a warning
  if they do not

**Residual risk:** a socket.io-based client breaking on a Kuma upgrade.

**Owner action:** answer [OQ-3](open-questions.md#oq-3-monitoring-tool).

---

## R8 — Frigate's retention expires footage before we copy it

**Risk.** Frigate does not store clips as files; it cuts them from recordings on demand.
Its own retention expires those recordings. If the clip-worker is down, or Frigate's
recording retention is configured too short, clips become **permanently unrecoverable** —
the only failure in the system that no later action can fix.

**Likelihood:** M — a long clip-worker outage, or an operator shortening Frigate's
retention to save disk.

**Impact:** H — permanent, silent footage loss.

**Mitigation**
- Events are **pinned** (`POST /api/events/{id}/retain`) before download, protecting the
  source for the duration of the copy
- The generated default keeps a 3-day motion recording window, which is the clip-worker's
  recovery budget
- `retention_headroom` is a health check that goes `DEGRADED` at 6 hours and `UNHEALTHY`
  at 1 hour remaining
- Config validation refuses a recording window shorter than 2 days without an explicit
  override
- The sweep recovers missed MQTT messages, so a broker problem does not consume the
  budget
- Pin timeout and pin-count limits prevent a dead worker from filling Frigate's disk
  while holding pins
- `EXPIRED_BEFORE_FETCH` is counted and alerted as **data loss**, never silently skipped

**Residual risk:** an unrecoverable loss if the worker is down for longer than the
window. Unavoidable given Frigate's design; the design's job is to make it visible and
briefly windowed.

**Owner action:** confirm the recovery budget in
[OQ-10](open-questions.md#oq-10--frigate-recording-retention-defaults).

---

## R9 — Cleanup service cannot run for weeks → **accepted**

**Risk.** An edge case in cleanup requires a fix, release, and `./update.sh` on a remote
machine (a parents' home) that nobody visits for weeks.

**Likelihood:** L.

**Impact:** M — nothing is deleted (safe by design), so only the cloud/disk side is
affected. **This asymmetry is the point**: a broken cleanup holds data rather than
losing it.

**Accepted because** the failure mode is benign and the mitigation is structural: the B2
lifecycle rule provides independent cloud retention, and `clips_overdue_retention` makes
local growth visible and alertable.

**Owner action:** none. Accepted.

---

## R10 — Scope creep delays a usable release

**Risk.** The PRD describes a complete system, and the temptation is to build it all
before anything works. The result is a project that is 70 % complete for a long time and
delivers nothing.

**Likelihood:** M.

**Impact:** M — delay, and possibly abandonment.

**Mitigation**
- The roadmap is sliced so that **every milestone produces something usable**: M1 gives
  canonical clips, M3 gives a cloud backup
- M1 is deliberately tiny and has a decision gate
- Each milestone has explicit "not in scope" statements
- A curated deferred list exists in the HLD (§17) and in `1.1+` candidates, so
  good-but-later ideas have somewhere to go instead of into the current milestone
- The invariants list what we will not compromise on, which makes "good enough" safe to
  reach

**Owner action:** resist adding items to a milestone in progress. Add to `1.1+` instead.

---

## R11 — Secret leaks into the repository or a diagnostics bundle

**Risk.** A B2 application key, camera password, or Frigate token is committed, printed
in a log, or included in a diagnostics bundle shared on a forum. Git history makes
committed secrets effectively permanent.

**Likelihood:** L — but it only has to happen once.

**Impact:** H — real-world security incident; someone else's footage is accessible.

**Mitigation**
- `.gitignore` covers `.env`, keys, and certs; only `.env.example` ships
- Pre-commit hook and CI job scan for high-entropy strings and credential patterns
- Config values use redacting types, so `repr(config)` and `str(config)` print `***`
  ([INV-2](../architecture/invariants.md))
- Logs strip URLs' userinfo; RTSP URLs with credentials are never logged whole
- The diagnostics bundle redacts by value, and the redaction is **tested** — a bundle
  that leaks a key into a forum post is worse than the original problem
- Secrets are passed as environment variables or files, never as command-line arguments
  (visible in `ps`)
- `.env` is written `chmod 600`, and the installer refuses a world-readable one

**Owner action:** enable GitHub secret scanning and push protection on the repository.

---

## R12 — SQLite corruption from a misused backup or restore

**Risk.** An operator copies `state.db` while it is live (producing a corrupt file),
or restores a backup without removing the stale `-wal`/`-shm` files (producing
inconsistency). Both are the classic mistakes, and both are easy to make.

**Likelihood:** M — `cp` is the intuitive action.

**Impact:** H — loss of upload state, causing re-uploads (recoverable) or a false belief
in backups (not recoverable without reconciliation).

**Mitigation**
- `backup.sh` uses the SQLite online backup API and never `cp`
- `restore.sh` removes `-wal`/`-shm` and refuses to run with services up
- Every service runs `PRAGMA integrity_check` at startup and refuses to serve on failure
- Backup output is itself verified by reopening it and checking integrity
- `doctor.sh` surfaces integrity status, WAL size, and migration state
- Documentation states the `cp` hazard **where an operator will read it** — in the
  runbook, not only in a design doc
- Reconstruction from the manifest and local files covers the worst case
  ([`lld/08`](../architecture/lld/08-backup-and-restore.md#63-scenario-c--statedb-corrupted-clips-intact))

**Owner action:** none.

---

## R13 — Upgrade fails and leaves a broken installation

**Risk.** `./update.sh` fails partway: migrations applied but containers not started, or
new code running against an old schema. The operator is left with a non-working system
and no clear path back, possibly at a remote site.

**Likelihood:** M — upgrades are the most complex operation in the system.

**Impact:** H — loss of service, and potentially of state if recovery is mishandled.

**Mitigation**
- The compatibility gate refuses to start rather than failing midway
- A pre-migration database backup is taken **automatically, always** — this is not
  optional
- Migrations run in transactions and are forward-only and checksummed
- Validation happens **before** anything is pulled or stopped
- Only clip-worker and cleanup are stopped; Frigate keeps recording throughout
- A failing migration restores the backup and prints the exact rollback command
- `--rollback` is a tested script, not a documented procedure
- CI tests the upgrade path from the previous MINOR and the previous MAJOR, on every
  released snapshot
- Version skipping across a MAJOR is refused with the intermediate step named

**Residual risk:** a bug in a migration that passes tests and fails in production.
Mitigated by additive-first migration rules and by the automatic backup.

**Owner action:** none.

---

## R14 — Real cameras have RTSP quirks the defaults do not handle

**Risk.** Real cameras behave in ways defaults do not anticipate: non-standard RTSP URL
paths, H.265 streams, missing sub-streams, connection limits, authentication oddities,
unstable streams, or a camera that only exposes RTSP once and then refuses a second
connection. The operator's experience is "nothing works" on the first camera they try.

**Likelihood:** H — this is close to universal in the NVR world.

**Impact:** M — installation friction, not data loss. Frigate handles most of it; our
job is to not get in the way.

**Mitigation**
- Frigate does the hard part, and it has broad camera support
- The interview supports optional sub-streams with a warning when absent
- The installer **validates each camera at entry**, so a bad URL surfaces immediately
  rather than at verification
- A camera that fails verification is **non-fatal**: the stack installs and works for the
  others
- Validation warns when two cameras share an RTSP URL
- The install runbook includes a camera-troubleshooting section, and `doctor.sh` reports
  per-camera status
- `host.yml` and the generated `ffmpeg` arguments are inspectable, so an operator can see
  what was configured

**Residual risk:** unusual cameras need per-camera `ffmpeg` tuning, which is blocked on
[OQ-4](open-questions.md#oq-4--config-escape-hatch-for-raw-frigate-options).

**Owner action:** consider promoting OQ-4 if M1 reveals camera-quirk friction.

---

## R15 — Nobody can install it without help

**Risk.** The installer, docs, or error messages assume knowledge the operator does not
have. The PRD's central promise — "a configuration exercise, not a software-engineering
exercise" — fails, and the project becomes a personal deployment rather than a product.

**Likelihood:** M — the author's own knowledge makes it easy to underestimate.

**Impact:** H — the project fails its stated purpose.

**Mitigation**
- Every validation error names the file, the line, the offending value, and a `fix`
  string
- The installer explains what it detected and why, in plain language
- Non-fatal warnings are printed in a final summary rather than scrolled past
- The diagnostics bundle makes a failed install supportable
- M8 exit criterion **M8.9** requires observing a stranger install it unaided — the only
  real test of this risk
- Runbooks are written for an operator, not a developer, and every runbook is followed
  by someone other than its author before M8

**Owner action:** find a test subject for M8.9 early. Do not leave it to the release.

---

## R16 — Clips are produced but are unusable

**Risk.** The system reports healthy, clips appear on disk, upload to B2, and play in
VLC — and they are wrong. They start after the action began, end before it finished, or
have a silent jump cut where a recording segment went missing. **This was the owner's
reported experience with Frigate clip generation**, which is why it is recorded as a
first-class risk rather than a feature request.

This is the most dangerous class of defect in the project, because **every other check
passes**. A truncated clip is a valid MP4 with a plausible thumbnail, a correct path, a
matching checksum, and a successful upload. It passes [INV-5](../architecture/invariants.md),
INV-6, INV-7 and INV-10. Health is green. The failure only surfaces when somebody needs
the footage and it is not there.

**Likelihood:** H — the owner has already experienced it, and Frigate's clip-cutting
mechanism has several independent ways to produce it.

**Impact:** H — the system keeps and backs up footage that cannot answer the question it
was kept for. Silent, and discovered at the worst possible moment.

**MEASURED (M1, Frigate 0.18.0). This risk is real and reproduced.**

| Probe | Result |
|---|---|
| Truncation at low fetch lag | **5 of 6 sampled lags returned `200` with a short clip** |
| First lag with full-length footage | **10 s** — the design default was 5 s, i.e. **wrong** |
| `-c copy` cut precision | errs **outward**, mean **+1.88 s** per 10 s |
| Error semantics | `400 "No recordings found"` — retryable classification confirmed |
| Audio | preserved as AAC |
| Packet gaps | **3.55 s in a healthy clip, larger than the 2.55 s keyframe interval** |

Two consequences:

1. **`segment_flush_grace_s` 5 s → 15 s.** With the original default, essentially every
   promptly-fetched clip would have ended early, silently, with the dashboard green. The
   defect would have shipped.
2. **The gap detector as designed does not work.** A healthy clip reports a packet gap
   larger than its keyframe interval, so PTS deltas cannot distinguish a missing segment
   from a `-c copy` timestamp discontinuity at a segment join. Left unchanged, every clip
   would be flagged as holed and the signal would be worthless. A different criterion is
   required before it ships — see [`lld/02`](../architecture/lld/02-clip-worker.md#9-configuration).

Evidence: [`spike/RESULTS.md`](../spike/RESULTS.md).

**Status: partially retired.** The truncation cause is confirmed and the fix is
calibrated; the gap-detection criterion is an open design task, not a mitigation.

**Four independent causes** (all addressed in
[ADR-0015](../adr/0015-clip-boundaries-and-padding.md)):

| Cause | Effect | Mitigation |
|---|---|---|
| Boundaries are the event's boundaries | Clip starts after the action, ends before it finishes | Configurable `pre_roll_s` / `post_roll_s`, asymmetric, via the range endpoint |
| **The fetch races Frigate's segment flush** | **Silent truncation** — segments are written to cache, then moved to disk | `fetch_not_before = event.end + post_roll + segment_flush_grace_s`; the fetch cannot run earlier |
| A mid-event segment is missing | Silent jump cut | Gap detection sets `has_gaps` and records `max_gap_s`; the clip is kept and counted |
| `-c copy` cannot cut frame-exactly | Boundaries off by up to a GOP, in either direction | We **err outward** and never trim, so imprecision can only ever add footage |

**Mitigation, in detail**
- Fetch range is `[event.start − pre_roll_s, event.end + post_roll_s]` with independent,
  per-camera-controllable values, so no footage is lost to a symmetric approximation
- Fetching is scheduled after the flush grace and cannot be triggered earlier
- Coverage (`actual / requested`) is measured on **every** clip and persisted
- Below `coverage_retry` the clip is **not accepted** — it is re-fetched
- After retries, the **best result is stored and flagged**; a short clip is never discarded
- Gaps are detected from packet timestamps and recorded; never repaired, because the
  footage is gone
- Audio is preserved (`-map 0`, never `-an`) and its codec recorded
- Long clips are stored in full, never truncated
- `CLIP_COVERAGE_LOW`, `CLIP_HAS_GAPS`, `clip_quality` and `flush_pressure` are health
  signals, and they are **the only signals capable of detecting this class of defect**
- **Boundary correctness is tested in CI** against a synthetic source with a per-second
  colour code, so "do clips start and end in the right place?" is an assertion rather
  than a spot check
- A dedicated runbook ([`clip-boundaries.md`](../runbooks/clip-boundaries.md)) makes the
  quality of any period measurable with one SQL query

**Residual risk:** the flush grace is an estimate, not a signal — Frigate exposes no
"segment flushed" event. Mitigated by the coverage retry, which recovers the case the
estimate gets wrong, and by `flush_pressure` health, which makes a wrong estimate visible
instead of silent. M1 measures the real delay so the default is grounded in this host's
behaviour rather than a guess.

**Owner action:** confirm the default pre/post-roll values in
[OQ-16](open-questions.md#oq-16--default-pre-roll-and-post-roll-values).

---

## Accepted risks

Stated explicitly so they are decisions rather than oversights.

| Risk | Why accepted |
|---|---|
| No continuous recording backup to the cloud | Cost is two orders of magnitude higher than clip backup; the PRD excludes it (§14) |
| No cross-site visibility | Owner's explicit choice ([ADR-0002](../adr/0002-independent-per-site-deployments.md)) |
| No clip encryption at rest | Key management we cannot do well yet; provider-side encryption is available |
| Windows support is best-effort | WSL2 caveats (USB passthrough, filesystem) are not worth engineering around |
| macOS is development-only | No hardware acceleration path from inside a container |
| Un-uploaded clips grow the disk without limit | The correct trade ([ADR-0009](../adr/0009-cleanup-safety-invariant.md)); mitigated by alerting, not by deletion |
| Monitoring tool's own state is not backed up | Monitors are regenerated; only history is lost |
| The host dying is out of scope | Nothing on the host can report its own death |
| A second storage provider is not implemented | No second use case yet; the adapter bounds the eventual cost |
