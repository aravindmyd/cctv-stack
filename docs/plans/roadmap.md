# Roadmap

What gets built, in what order, and why that order.

**Principle: every milestone produces something an operator could use.** No milestone
is scaffolding whose value only appears two milestones later. The first milestone
produces something better than what the operator has today; each subsequent one adds a
guarantee on top.

---

## Release plan

| Version | Milestone | Headline | Addresses |
|---|---|---|---|
| `0.1.0` | M1 | A clip lands on disk in the right place | PRD §7 |
| `0.2.0` | M2 | The clip pipeline is complete and never loses an event | PRD §7, §9 (partly) |
| `0.3.0` | M3 | Clips reach Backblaze B2 durably, surviving restarts and outages | PRD §8, §9 |
| `0.4.0` | M4 | Local and cloud retention work, and cannot delete an un-backed-up clip | PRD §10 |
| `0.5.0` | M5 | The system answers "is my CCTV working?" and alerts when it isn't | PRD §11 |
| `0.6.0` | M6 | `./install.sh` on a bare machine | PRD §1–§5 |
| `0.9.0` | M7 | Backup, restore, update, rollback | PRD §12–§14 |
| `1.0.0` | M8 | Public release: documented, tested, contribution-ready | PRD §15 |
| `1.1+` | M9+ | Extension, guided by real use | — |

`1.0.0` is not "feature complete" — it is **"safe to give to a stranger"**. That means
every invariant has a test, the upgrade path works, and the docs are good enough that
someone can install and operate this without asking us.

---

## Execution phases

The milestones above are the *release* plan. This is the *build* order — the same
sequence as [`lld/00-overview.md`](../architecture/lld/00-overview.md) §1, with what each
phase needs before it can start and how it is verified.

| Phase | Deliverable | LLD | Milestone | Needs | Verifiable without a camera? |
|---|---|---|---|---|---|
| 0 | Review-lifecycle measurement — the last M1 gap | — | M1 | a camera | ❌ the point is real subjects |

> **Phase 0 is deferred, not blocking.** It needs a subject in front of a camera, and the
> owner cannot produce one at will. Nothing in phases 1–3 depends on the answer, so the
> work continues and the measurement happens when it can. See
> [`spike/RUN-SHEET.md`](../../spike/RUN-SHEET.md) for the camera-free alternative.
| 1 | Shared library: config model, `paths`, `time` | [01](../architecture/lld/01-config-model.md) | M2 | — | ✅ pure functions, property tests |
| 2 | State store: schema, migrations, claim query | [03](../architecture/lld/03-state-store.md) | M2 | 1 | ✅ temp-file SQLite |
| 3 | clip-worker: trigger, fetch, remux, verify, sweep | [02](../architecture/lld/02-clip-worker.md) | **M2** | 1, 2 | ✅ `dev-synthetic` profile |
| 4 | Uploader, `StorageAdapter`, B2 implementation | [04](../architecture/lld/04-uploader.md) | **M3** | 1, 2 | ✅ in-process fake adapter |
| 5 | Cleanup, force-delete audit trail | [05](../architecture/lld/05-cleanup.md) | **M4** | 2, 4 | ✅ fake clock |
| 6 | Health, `/status`, Gatus, notifications | [06](../architecture/lld/06-health-and-monitoring.md) | **M5** | 2 | ✅ fault injection |
| 7 | Installer, host profiles, preflight | [07](../architecture/lld/07-installer-and-host-profiles.md) | **M6** | 1–6 | ⚠️ needs a clean host |
| 8 | Backup, restore, upgrade, rollback | [08](../architecture/lld/08-backup-and-restore.md), [09](../architecture/lld/09-upgrade-and-versioning.md) | **M7** | 7 | ⚠️ needs a clean host |
| 9 | Public release | — | **M8** | all | — |

**Inverted dependencies — do these before the phase that needs them, not with it.**

| Requirement | Needed by | Why it cannot wait |
|---|---|---|
| `cctv.paths` timezone argument and determinism tests | Phase 1 | [INV-5](../architecture/invariants.md) and [INV-12](../architecture/invariants.md) are only enforceable while there is one implementation; retrofitting a timezone argument after cleanup exists means auditing two call sites that have already diverged |
| `dev-synthetic` source | Phase 3 | Phase 3's boundary, gap, and long-clip tests are all impossible without a deterministic video source. Building it with the worker risks "we will test it later" |
| Migration `0001` written as a forward-only migration | Phase 2 | [INV-8](../architecture/invariants.md) requires forward migrations. Creating the schema with `create_all` and adding migrations later leaves no upgrade path to test |

**Two ordering properties are deliberate and must not be rearranged.**

- **Upload (4) before cleanup (5).** You must be able to back something up before you are
  allowed to delete it. Reversing this makes [INV-1](../architecture/invariants.md)
  untestable, because there is nothing for cleanup to consult.
- **Phases 1–3 stand alone.** They produce local clips in a sane layout with no cloud
  dependency — a useful system for an operator who does not want B2 at all. That is what
  `retention.upload: false` and `SKIPPED_BY_POLICY` exist for.

---

## M1 — A clip lands on disk (walking skeleton `0.1.0`)

> **Status: four of twelve criteria already met by measurement**, not by building — the
> flush delay (M1.9), the truncation reproduction (M1.10), cut precision (M1.11), and
> audio survival (M1.12). Evidence: [`spike/RESULTS.md`](../../spike/RESULTS.md).
> What remains is the **review-item lifecycle**, which is the one thing the harness could
> not cover and which decides whether the trigger works at all.

**Goal.** Prove the single riskiest assumption end to end, with the narrowest possible
slice.

**Build**
- The shared library skeleton: config loading, `paths.py`, `timeutil.py`, logging
- Frigate + mosquitto in a compose file, one real camera
- `clip-worker` in its simplest form: MQTT → pin → download → rename → log. No
  database, no remux, no sweep
- A minimal config: hand-written `cameras.yml`, hand-written Frigate config

**Why first.** The architecture rests on one upstream fact: Frigate stores no clips as
files, and a clip is cut on demand from recording segments. That fact is now **measured**
— including the truncation race that would otherwise have shipped silently at a 5 s flush
floor. What is *not* measured is the **review-item lifecycle**, and it has since become
the single assumption the design pivots on: if reviews do not group a visit the way the
source suggests, the trigger, the dedup key, and the path basis all change
([ADR-0016](../adr/0016-clip-per-review-item.md)).

**Exit criteria**
- A real **review item** produces a real `.mp4` at `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`
- The file plays and has a plausible duration
- **The clip contains footage from before the review started and after it ended.** Verified
  by eye on a person walking past, using the `pre_roll_s` / `post_roll_s` defaults
- **A returning subject is one clip.** A person who leaves the frame and returns inside
  `clip.visit_gap_s` produces **one** file; the same person returning after it produces
  **two**. This is the acceptance test for [ADR-0016](../adr/0016-clip-per-review-item.md)
  and the only criterion the harness cannot fake
- **A review left open by a Frigate restart is recovered.** Frigate clears in-progress
  reviews at its own start-up, so the `type: end` message never arrives; the sweep must
  still produce the clip. Verified by restarting Frigate mid-visit
- **`type: end` is or is not reliable**, and the answer is recorded. If it is not, the
  sweep stops being a backstop and becomes the primary path — which changes the health
  model, not just the code
- **The segment flush delay is measured**: how long after `review.end_time` the final
  recording segment becomes retrievable, and whether an immediate fetch truncates the
  clip. This is the unknown behind [INV-14](../architecture/invariants.md) and it decides
  the `segment_flush_grace_s` default
- **The achieved cut precision is measured** with `-c copy`: does the result err outward or
  inward, and by how much. This decides `max_gap_s`, so that keyframe alignment is not
  mistaken for a missing segment
- The pin is held during the download and released after
- The integration contract's verification checklist (§9) is **fully completed and
  recorded**, with the exact Frigate version noted
- Measured: download time, remux time, and CPU cost for a 30-second clip

**Explicitly not in M1.** Database, uploader, cleanup, health, installer, dashboard.
The output is a shell command and a log line.

**Decision gate.** If the clip download is unusable — too slow, wrong content, or
unavailable for a meaningful fraction of events — stop and revisit
[ADR-0001](../adr/0001-frigate-as-detection-engine.md) before building further.

---

## M2 — The pipeline never loses an event (`0.2.0`)

**Goal.** Make materialisation complete, idempotent, and honest about what it cannot
recover.

**Build**
- SQLite schema and migrations ([`lld/03`](../architecture/lld/03-state-store.md))
- clip-worker proper: the full sequence, sidecar JSON, `ffprobe` verification, atomic
  writes
- The reconciliation sweep with a persisted watermark
- Pin reaper, `.tmp` reaper
- Retention headroom as a health check (the check only, not the dashboard)
- `dev-synthetic` compose profile so CI can exercise the pipeline without cameras

**Why here.** Now that downloading works, the next risk is completeness: MQTT is lossy,
and Frigate's retention is a clock. This milestone makes the pipeline *correct*, before
anything else is layered on.

**Exit criteria**
- Delivering the same review item five times produces one file and one row ([INV-7](../architecture/invariants.md))
- **Clip boundaries are provably correct.** A synthetic source with a per-second colour
  code lets the first and last decoded frames be checked against
  `review.start − pre_roll` and `review.end + post_roll` ([INV-14](../architecture/invariants.md))
- **A long review is fetched in full.** `fetch_timeout_s` scales with the review's
  duration. A fixed 120 s timeout silently fails every review longer than two minutes,
  which with `visit_gap_s` configured generously is every interesting one
- **The flush race is closed**, demonstrated by reproducing truncation with
  `segment_flush_grace_s: 0` and showing it does not occur with the default
- **A short clip is retried and then kept**, never discarded — the best result obtained is
  what gets stored
- **A clip with a hole is kept and flagged**, not discarded and not silently accepted
- `kill -9` at every step of the sequence loses nothing and duplicates nothing
- Killing clip-worker for 30 minutes and restarting recovers every event via the sweep,
  with no gaps in the event id sequence
- A DST transition produces no collisions and no misnamed files ([INV-12](../architecture/invariants.md))
- Headroom health goes `DEGRADED` then `UNHEALTHY` before an event is lost, verified by
  compressing Frigate's retention
- **Chaos test passes as part of CI**, not manually

---

## M3 — Durable upload (`0.3.0`)

**Goal.** Every clip reaches B2, exactly once, surviving restarts and provider outages.

**Build**
- `StorageAdapter` protocol + B2 implementation, S3-compatible
- Uploader: claims, leases, backoff, error classification, checksum verification
- Cloud key derivation and the key-equals-path test
- `upload_attempts` history, backlog age and staleness metrics
- The admin endpoints needed to debug it (`/admin/queue`, `/admin/retry`)

**Why here.** This is the first guarantee that matters to the operator, and it comes
before retention deliberately — you must be able to back something up before you are
allowed to delete it.

**Exit criteria**
- Clips appear at `<location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`
- **INV-7 test passes**: three uploads of the same clip, one of them with a crash
  between the PUT and the state update, produce exactly one object
- B2 unreachable for an hour while clips keep arriving → everything uploads on recovery,
  nothing duplicated, nothing deleted
- Revoked credentials → fast-fail, no retry storm, `UNHEALTHY`, actionable log
- `oldest_pending_age_s` and `upload_staleness` are correct and exposed
- Backlog drains oldest-first

---

## M4 — Retention that cannot destroy data (`0.4.0`)

**Goal.** Enforce 90-day local and 60-day cloud retention, with the safety invariant
enforced structurally rather than by care.

**Build**
- cleanup service: J1 local, J2 cloud, J3 reconciliation, J4 housekeeping, J5 reporting
- The positive-enumeration deletion query and the per-clip assertion
- `FORCE_DELETED` and the `force-delete` admin endpoint
- B2 lifecycle rule provisioning and read-back verification
- `clips_overdue_retention` reporting

**Why here.** This is the only component that can destroy data, and it is built only
once there is something safe to destroy. Building it earlier would mean testing it
against `PENDING` clips — which is to say, testing that it refuses to delete anything.

**Exit criteria**
- **INV-1 test passes**: with B2 unreachable, repeated cleanup runs delete **zero**
  clips, however old
- A `FAILED_PERMANENT` clip is never deleted
- An `UPLOADED` clip past 90 days is deleted locally and marked `DELETED_LOCAL`
- Cloud retention counts from `uploaded_at`, so a late-backfilled clip gets its full
  window
- The B2 lifecycle rule is applied, read back, and verified; a delete that only hides
  an object is **not** recorded as success
- Orphan objects and dangling rows are reported and never auto-repaired
- `data/media/` and `data/frigate/` are byte-identical before and after a full cycle
- Removing a camera deletes nothing ([INV-13](../architecture/invariants.md))
- `DRY_RUN=1` performs no deletion and logs every intended one

---

## M5 — Honest health and monitoring (`0.5.0`)

**Goal.** One screen that answers "is my CCTV working?", and alerts that are worth
reading.

**Build**
- `status-collector`: collection, staleness, camera state, silent-camera detection,
  disk projection, the generated answer
- The `/status` JSON and HTML views, `/metrics`
- The `MonitoringAdapter` and the chosen tool's config generation
- Notification setup for ntfy and Telegram
- The full alert policy

**Why here.** Health needs something to report on. Built earlier, it would report on
components that do not yet exist.

**Exit criteria**
- Stopping each dependency in turn produces the correct non-healthy state within its
  staleness window ([INV-6](../architecture/invariants.md))
- A component that stops reporting becomes `UNKNOWN`, never remains `HEALTHY`
- The answer sentence names the specific problem for each of the eight questions in
  [`lld/06`](../architecture/lld/06-health-and-monitoring.md#1-the-question-decomposed)
- A camera online with events stopped becomes `QUIET`
- A calm system produces **zero** notifications over a simulated week
- Every alert type produces a matching recovery notification
- Stopping the collector causes the monitoring tool to alert on its own
- The HTML page is complete and readable with JavaScript disabled

---

## M6 — One-command install (`0.6.0`)

**Goal.** PRD §15: a bare machine becomes a working installation.

**Build**
- `install.sh` with all ten phases, `detect-host.sh`, the config generator end to end
- Host profiles for the primary targets: `intel-igpu`, `coral-usb`, `cpu-x86`, `pi-cpu`
- The interactive interview and `--yes`
- All validation rules, with their messages and `fix` strings
- B2 provisioning including the versioning and lifecycle traps
- The verification phase, the diagnostics bundle, and `doctor.sh`
- `uninstall.sh`
- The signed release bundle, `versions.yml` digest pinning, and the offline archive
  ([`packaging.md`](packaging.md), [ADR-0020](../adr/0020-release-artifacts-and-delivery-modes.md))

**Why here.** Once every component works individually, packaging them is the remaining
risk — and it is the risk PRD §1–§3 are entirely about.

**Exit criteria**
- On a clean Debian machine: a verified release bundle installs with `./install.sh` to
  healthy, recording, uploading, with no git checkout and no Python on the host
- On a Raspberry Pi 5: the arm64 bundle installs, detection runs within the profile's
  stated limits, and the installer warns clearly about the CPU limitation
- An air-gapped machine installs from the offline archive with no egress
- A tampered bundle or a digest mismatch fails closed before any container starts
- The compatibility matrix is honest — an unsupported host is refused with an
  explanation
- Detection degrades to `unknown` rather than guessing wrong
- `install.sh` re-run is idempotent and deletes nothing
- A bad B2 key fails during install with an actionable message
- Every invalid-config fixture produces its specific code, `fix` string, and exit 78
- The diagnostics bundle contains no credential values

---

## M7 — Lifecycle (`0.9.0`)

**Goal.** The data can leave and come back, and a machine can move forward safely.

**Build**
- `backup.sh` with encryption, integrity verification, and the manifest
- `restore.sh` for all four scenarios, including `--clips-from-cloud`
- `update.sh` with the compatibility gate, pre-upgrade backup, digests, migrations,
  and `--rollback`
- `versions.yml`, the config migration tool, and the release process

**Why here.** These are the guarantees that make the system maintainable over years
rather than months. They need a complete system to operate on — and the PRD puts them
last for the same reason.

**Exit criteria**
- Full recovery onto a clean machine: cameras, clip count, and health all correct
- `state.db` reconstruction without a backup classifies all four categories correctly
  and never marks an unknown clip `UPLOADED`
- Cloud clip recovery restores every object to its predicted path
- `backup.sh --verify` detects a stale or truncated backup
- An upgrade from the previous release preserves every clip row and upload state
- A deliberately failing migration restores the backup and prints the rollback command
- `--rollback` returns a working installation, and clips created during the window are
  recovered by reconciliation
- A MAJOR version skip is refused with the intermediate step named
- Recording continues throughout an upgrade (Frigate is never stopped)

---

## M8 — Public release (`1.0.0`)

**Goal.** Safe to hand to a stranger.

**Build**
- CI: all invariant tests, migration tests on every released snapshot, upgrade from the
  previous MINOR and MAJOR, rollback, secret scanning, arm64 build
- Complete docs: every runbook, the contributing guide, the security policy, the licence
- Reference hardware recommendation
- Release-notes template with a mandatory breaking-changes section

**Exit criteria**
- Every invariant INV-1…INV-13 has a named, passing test
- The upgrade path from every released version is tested
- A person unfamiliar with the project installs it from the README without asking a
  question — verified by asking one
- The docs have no dead links and no `TODO`s
- The repository contains no secrets, and CI proves it
- All images are digest-pinned and every digest resolves

---

## M9+ — After 1.0

Deliberately uncommitted. Candidates, in rough order of expected value:

| Candidate | Trigger |
|---|---|
| A second storage provider (S3, Wasabi) | A real second use case |
| Prometheus + Grafana via the existing `/metrics` | A desire for history and trends |
| Snapshot backup alongside clips | Operators wanting event stills in the cloud |
| A read-only cross-site status view | Operator pain with several sites |
| Guided Coral/Hailo purchase and setup | Repeated "which accelerator should I buy?" |
| A clip browser | Frigate's UI proving insufficient for a common task |
| A config escape hatch for raw Frigate options | Repeated advanced-user requests |
| Non-container installation | A supported host where Docker is unwelcome |

---

## Sequencing rationale, in one table

| Ordering choice | Reason |
|---|---|
| Prove the clip download **first** | The whole design depends on it; nothing else should be built on an unverified assumption |
| Correctness (M2) before upload (M3) | Uploading an incomplete set of clips is worse than not uploading, because it looks like success |
| Upload (M3) before retention (M4) | You must be able to back something up before you may delete it. This is [INV-1](../architecture/invariants.md) reflected in the schedule |
| Health (M5) before installer (M6) | The installer's verification phase needs health to verify against |
| Installer (M6) before lifecycle (M7) | Backup and upgrade operate on an installed system |
| Lifecycle (M7) before release (M8) | A stack that installs but cannot be upgraded or restored is not ready for strangers |
| Open source last | It is a documentation and process milestone, not a coding one, and it is cheap to do late and expensive to redo |
