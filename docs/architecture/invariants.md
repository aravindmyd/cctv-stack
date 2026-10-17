# System invariants

Rules that must hold **at all times**, in every deployment, under every failure
mode. They are not guidelines. If a change breaks one of these, the change is
wrong — or the invariant needs an explicit, documented renegotiation via ADR.

Each invariant lists: the statement, **why** it exists, **how it is enforced**
(so it is not merely a promise), and **how we detect** a violation.

---

## INV-1 — Never delete a clip that has not been safely backed up

**Statement.** A canonical clip may only be deleted locally when its upload state
is terminal-success, **or** when an operator explicitly forces deletion of that
specific clip. No automatic process may ever reach the second path.

**Why.** This is the entire point of the system. A 90-day local retention window
that deletes clips which failed to upload turns a transient cloud outage into
permanent data loss. Retention is a *space* policy; it must never override a
*durability* policy.

**Enforcement.**
- The cleanup service operates on a SQL query whose `WHERE` clause requires
  `upload_state IN ('UPLOADED', 'SKIPPED_BY_POLICY')`. It never walks the
  filesystem to decide what to delete. See
  [`lld/05-cleanup.md`](architecture/lld/05-cleanup.md).
- Clips in `FAILED`, `PENDING`, or `UPLOADING` state are invisible to the local
  retention sweep. They age without limit and surface as an alert instead.
- Force deletion requires an explicit flag and is logged with the operator's
  identity and the reason.
- A filesystem sweep exists only as a **reconciliation** job, it never deletes;
  it reports orphans.

**Detection.** A startup self-check asserts that no clip row in a non-terminal
upload state has a missing file. The status endpoint reports
`clips_overdue_retention` — clips past their local retention date that are still
held for safety. A non-zero value is a `DEGRADED` health signal.

---

## INV-2 — No secret ever enters the repository

**Statement.** Credentials (B2 keys, camera passwords, Frigate API tokens, MQTT
passwords, notification tokens) exist only in the operator's `.env` on the host,
with restrictive permissions. They never appear in git, in a Docker image layer,
in a log line, in an error message, or in a health response.

**Why.** The project is intended to be open-sourced. A leaked camera credential or
B2 application key is a real-world security incident, and git history makes leaks
effectively permanent.

**Enforcement.**
- The repository ships `.env.example` with empty values and never `.env`.
- `.gitignore` includes `.env`, `*.key`, `*.pem`, `config/**/secrets*`, `data/`.
- A pre-commit hook and a CI job scan for high-entropy strings and known credential
  patterns.
- Configuration objects implement `__repr__`/`__str__` that redact secret fields, so
  accidental logging prints `B2_APPLICATION_KEY=***`.
- The installer writes `.env` with `chmod 600` and refuses to continue if the file
  is world-readable.
- Secrets are never passed as command-line arguments (visible in `ps`); they are
  passed as environment variables or read from files.

**Detection.** CI secret-scanning job. A grep-based test asserts that no
`B2_APPLICATION_KEY=` follows a non-empty value in any tracked file.

---

## INV-3 — Camera configuration exists in exactly one place

**Statement.** `config/cameras.yml` is the single source of truth for cameras.
Frigate's `config.yml`, our service configuration, the dashboard, and the cloud
key layout are all **derived** from it. No other file may define a camera name,
RTSP URL, or camera credential.

**Why.** The PRD requires adding a camera to be a configuration change (§6). That
only holds if there is one place to change. Three hand-maintained copies of the
camera list guarantee drift, and drift in this system means silently not recording
a camera.

**Enforcement.**
- A generator renders `config/frigate/config.yml` from `config/cameras.yml`. The
  generated file carries a `# GENERATED — DO NOT EDIT` header and is overwritten
  on every `install.sh` / `update.sh` / config change.
- Services read cameras from `cameras.yml` (or from the state database seeded from
  it), never from their own config.
- No RTSP URL or camera name appears in application source code.
- A CI test asserts that adding a camera to a fixture `cameras.yml` produces a valid
  Frigate config with no source changes.

**Detection.** A drift check compares the hash of the expected generated config with
the file on disk and reports drift through health.

---

## INV-4 — Containers are disposable

**Statement.** Deleting and recreating every container loses no data and loses no
configuration. All persistent state lives under the host data directory, mounted in.

**Why.** PRD §5. Disposability is what makes upgrades, rollbacks, and disaster
recovery tractable. A container-local SQLite file or an in-container cache is a
ticking data-loss bomb.

**Enforcement.**
- Every writable path used by a service is bind-mounted from `DATA_DIR`.
- Containers run with a read-only root filesystem where the image permits it.
- Service images contain no `VOLUME` declarations that could create anonymous
  volumes holding state.
- The state database lives on the host, never inside a container.

**Detection.** The acceptance test for every milestone includes
`docker compose down && docker compose up -d` with an assertion that state and
health are unchanged. A "kill -9 the container mid-upload" test asserts the upload
resumes.

---

## INV-5 — Clip paths are deterministic and derivable

**Statement.** The path of a clip is a pure function of its camera name,
**review** start time, and the configured timezone. Given the metadata, any process can
compute the path without asking another component. Materialisation is therefore
idempotent: doing it twice produces the same path.

**Why.** Determinism is what makes retries safe, makes the cloud key layout stable,
and makes "did we already handle this event?" answerable with a path comparison
rather than a scan.

**Enforcement.**
- One implementation of the path function, in the shared internal library, used by
  clip-worker, uploader, cleanup, and backup. No component re-implements it.
- The database stores both the derived path and the metadata, and a test asserts
  they agree for every row.
- If two events from the same camera share a start second, a deterministic suffix
  is appended (never a random one).

**Detection.** A property test generates random camera/time inputs and asserts the
function is pure and collision-free within a camera/second.

---

## INV-6 — Health never lies

**Statement.** A component reported as `HEALTHY` is genuinely able to do its job.
A component that is running but cannot reach a dependency reports `DEGRADED` or
`UNHEALTHY` and names the failing dependency. Absence of a recent report is
reported as `UNKNOWN`, never assumed healthy.

**Why.** PRD §11 asks the dashboard to answer *"is my CCTV system actually
working?"*. A health check that only confirms the process is alive answers a
different, useless question. Silent health failures are worse than loud ones,
because they are discovered by discovering missing footage.

**Enforcement.**
- The health contract in
  [`contracts/service-contracts.md`](architecture/contracts/service-contracts.md)
  defines a fixed schema with a mandatory `checks[]` array naming each dependency.
- `HEALTHY` is returned by *derivation*: every check in the array must pass. It is
  never a hardcoded literal.
- Bounded staleness: a component whose last observation is older than its staleness
  window is reported `UNKNOWN` by the collector, regardless of what it last said.
- Camera health is derived from Frigate's own `frigate/<camera>/status/<role>` MQTT
  topics and `/api/stats`, not independently guessed.

**Detection.** A test harness stops each dependency in turn and asserts the
dependent component's health changes from `HEALTHY` to the expected non-healthy
state within the staleness window.

---

## INV-7 — Uploads are idempotent

**Statement.** Uploading the same clip twice results in exactly one cloud object
with the same content. Re-delivery of a job, a crash between "uploaded" and
"recorded", or a manual retry must never create a duplicate or corrupt object.

**Why.** PRD §9 requires the uploader to survive restarts without losing track of
what is uploaded. The inverse failure — uploading twice — is equally damaging: it
doubles cost and creates ambiguous state.

**Enforcement.**
- The cloud key is deterministic (derived, per INV-5), not generated.
- The clip's SHA-256 is computed before upload and stored. If the object already
  exists with a matching checksum, the upload is a no-op recorded as success.
- Uploads use a write-then-commit sequence: the database row is only moved to
  `UPLOADED` after the storage adapter confirms the object and the provider
  checksum matches.
- `UPLOADING` is a lease with a timeout. An expired lease is recoverable, not stuck.

**Detection.** An integration test uploads a fixture clip three times, including
once with a simulated crash after the object write but before the state update, and
asserts one object and state `UPLOADED`.

---

## INV-8 — Upgrades never destroy state

**Statement.** Moving from any version to a later version preserves all clips,
metadata, upload state, and configuration. Schema changes are forward-migrated and
are idempotent. There is no upgrade path that requires manual database surgery.

**Why.** PRD §13. An updater that can lose the upload queue silently converts a
routine update into data loss, which destroys trust in `./update.sh` entirely.

**Enforcement.**
- The schema carries an explicit version and a numbered migration chain. Migrations
  run before services accept traffic and are transactional.
- Downgrade is not supported (and is detected and refused rather than attempted).
- A backup of the database is taken automatically before any migration runs.
- Upgrade order is documented and enforced by compose `depends_on` conditions.

**Detection.** CI runs the migration chain against a database snapshot from every
previous released version, then asserts row counts and state distribution are
unchanged.

---

## INV-9 — Materialise before the source can expire

**Statement.** When Frigate reports a completed event with `has_clip = true`, we
either materialise the clip or we deliberately, visibly drop it by policy. An event
is never lost through inaction.

**Why.** This is a direct consequence of the most important upstream fact: Frigate
does not store event clips as files. It cuts them on demand from recording
segments, and its own cleanup expires the underlying recordings. If our pipeline is
broken or slow for longer than Frigate's recording retention, the clip is gone
forever and no retry can recover it.

**Enforcement.**
- On receiving a completed event (MQTT `frigate/events`, `type: end`, `has_clip:
  true`), the clip-worker immediately calls `POST /api/events/{event_id}/retain`
  to pin the event and protect its source footage.
- The event is then queued for materialisation. After the canonical clip is written
  and hashed, the event is unpinned (configurable — the default keeps it pinned for
  a short grace period).
- The health model exposes **retention headroom**: the gap between the oldest
  unmaterialised event and Frigate's own recording expiry. A shrinking headroom is
  a `DEGRADED` signal that escalates to `UNHEALTHY` before expiry.
- Operators configuring aggressive Frigate recording retention get a validation
  warning if the window is shorter than a documented floor.

**Detection.** A test compresses Frigate's recording retention to a few seconds,
blocks the clip-worker, and asserts that headroom alerts fire before any clip is
lost.

---

## INV-10 — Every cloud object is accounted for

**Statement.** Every object in the bucket that we created has a row in the
database, and every row marked `UPLOADED` has an object in the bucket. Neither
orphan objects nor dangling rows accumulate.

**Why.** Cloud retention correctness and cost control both depend on this. An
orphan object is paid for forever; a dangling row means cleanup believes a clip is
safe when the backup does not exist. This is also what makes restore verifiable.

**Enforcement.**
- Object keys are namespaced by `LOCATION` and are listed from the database, never
  enumerated by listing the bucket for correctness-critical decisions.
- A reconciliation job (part of cleanup, low frequency) compares the object listing
  under this site's prefix against the database and reports three classes of
  discrepancy: orphan object, dangling row, checksum mismatch.
- Reconciliation **reports**; it does not silently delete or repair. Repairs are an
  explicit, audited operator command.
- B2 lifecycle rules are a seconds-belts safety net for objects that escape this
  bookkeeping, and are scoped by prefix so they can never touch another site's data.

**Detection.** Reconciliation discrepancy count is exported as a health metric. Any
non-zero, sustained value is `DEGRADED`.

---

## INV-11 — Configuration is validated before anything starts

**Statement.** No container is started against invalid configuration. Validation
covers schema, camera name rules, RTSP URL well-formedness, path collisions,
retention sanity, storage availability, and credential presence.

**Why.** A half-started stack is the worst outcome: the operator sees containers
running and assumes recording is working. Failing fast and loudly before startup is
far safer than failing quietly after.

**Enforcement.**
- `install.sh` and `update.sh` run a validation pass and abort on failure with an
  actionable message naming the file, key, and problem.
- Service entrypoints also validate independently, so a hand-started container
  cannot bypass the checks.
- Retention values are cross-validated (`local_retention_days >= cloud_retention_days`
  is **not** required, but a mismatch that would delete before backup is rejected).
- A camera whose name violates the naming rule is a hard error, not a warning —
  it would produce invalid paths.

**Detection.** The test suite includes a corpus of invalid configurations, each of
which must produce a specific, asserted error message and a non-zero exit code.

---

## INV-12 — Time is stored as an instant, rendered in the site timezone

**Statement.** Every timestamp persisted in the database and every comparison with
Frigate is a **timezone-free instant** (epoch seconds). The site timezone is applied
only when a value is rendered for a human: clip path components, logs, the `/status`
page, and cloud object metadata. Exactly one function performs that rendering, and it
takes the timezone explicitly.

**Why.** An instant is the same everywhere, so storing one cannot be wrong and cannot
lose information — any zone stays derivable from it. What *can* be wrong is rendering
the same instant in two different zones, which produces duplicate clips and clips that
are an hour off once a year: the hardest class of bug to notice and diagnose.

**Enforcement.**
- The database stores epoch seconds as `REAL`. No timezone-aware or naive datetime
  strings are persisted.
- The path function takes an explicit timezone argument, and the timezone is resolved
  once at config load, not per call and not from the process environment.
- `site.timezone` is mandatory and is never guessed from the host.
- All internal comparisons are between instants. There is no conversion step that could
  be applied twice or in the wrong direction.
- A lint rule / test forbids `datetime.now()` without an explicit timezone and forbids
  `time.localtime()` in service code.

**Detection.** A DST-transition test renders instants on both sides of a forward and a
backward transition and asserts no collisions and no gaps. A second test asserts that
`Asia/Kolkata` (UTC+05:30, no DST) renders a 6:45 pm instant under an `18-45-29`
filename without the code branching on the offset.

See [ADR-0017](../adr/0017-time-handling.md) for why the stored value is an instant
rather than a rendered local-time string.

---

## INV-13 — Removing a camera is non-destructive

**Statement.** Removing a camera from `cameras.yml` stops ingest for that camera and
nothing else. Its existing clips, upload state, and cloud objects are untouched and
remain restorable. Deleting a camera's data is a separate, explicit, confirmed
action.

**Why.** Cameras get renamed, replaced, and temporarily removed during
troubleshooting. If a config edit silently orphans or deletes historical footage,
operators learn to fear editing configuration — which defeats the project's central
premise.

**Enforcement.**
- Cameras are soft-deleted in the state database with a `retired_at` timestamp.
- Cleanup treats retired cameras' clips by the normal retention rules, not as
  immediate deletions.
- Frigate's generated config simply omits the camera; no pruning of `data/clips/`
  is triggered by a config change.
- Camera removal prints what will happen to existing data before applying.

**Detection.** A test removes a camera from a fixture config, re-runs the config
generator and cleanup, and asserts zero clips deleted and zero rows lost.

---

## INV-14 — Clip boundaries err outward; a clip never omits footage that was available

**Statement.** When materialising a clip, the pipeline requests a range that is wider
than the event on both sides, never fetches before Frigate has flushed the recording
segments, and **never truncates a clip to make a boundary match a request**. If the
obtained clip is shorter than requested, the system retries and then **keeps the best
result obtained** rather than discarding it.

**Why.** The clip is the evidence. Two failure modes make clips useless in practice,
and both are silent:

- **Undershoot** — the clip starts after the action began, or ends before it finished.
  Frigate's `review.start_time` is when *activity* began, so a clip cut to the review's own
  boundaries always misses the approach. Worse, Frigate cuts clips from recording
  segments that are written to cache first and flushed to disk later, so a fetch issued
  immediately after an event ends can be truncated with no error at all.
- **Holes** — a missing segment mid-event produces a silent jump cut. The clip looks
  complete and is not.

Both are invisible to every other check in this system. A truncated clip is a valid MP4,
plays fine, has a plausible thumbnail, uploads successfully, and passes health. The only
way to catch it is to measure it and refuse to hide it.

**Overshoot is acceptable; undershoot is not.** Extra footage costs storage. Missing
footage cannot be recovered once Frigate's recordings expire. Every decision here follows
from that asymmetry.

**Enforcement.**
- Fetch range is `[event.start − pre_roll_s, event.end + post_roll_s]` via the range
  endpoint, so pre-roll and post-roll are independently configurable and neither is
  inflated to match the other ([ADR-0015](adr/0015-clip-boundaries-and-padding.md)).
- Fetching is scheduled at `event.end + post_roll + segment_flush_grace` and cannot run
  earlier, so the final recording segment has time to reach disk.
- Coverage is computed as `actual_duration / requested_duration` and persisted on every
  clip. Below `coverage_retry` the clip is **not accepted** — the fetch is retried.
- After retries are exhausted, the **best** result is stored, flagged `degraded`, and
  alerted. Nothing is ever discarded for being imperfect.
- There is no trimming step by default. `clip.trim_to_range` exists, is off, and is
  documented as being able to remove footage but never restore it.
- Every stream is preserved (`-c copy`, never `-an`). Audio is never stripped.
- Packet gaps above `max_gap_s` are recorded as `max_gap_s` / `has_gaps`, and the clip is
  kept and reported. Gaps are never repaired, because the footage in them no longer
  exists anywhere.
- Long clips are stored in full. A duration cap would be data loss with a tidy name.
- Stream layout (`has_audio`, video codec, audio codec) is recorded, so "the clip has no
  sound" is distinguishable from "the camera has no microphone".
- Requested and achieved ranges are both persisted, so boundary correctness is a query
  rather than an opinion, and can be asserted in CI.

**Detection.** `coverage_ratio`, `max_gap_s`, `has_gaps`, `fetch_attempts` and `degraded`
are columns on every clip, aggregated into `CLIP_COVERAGE_LOW` and `CLIP_HAS_GAPS` health
codes. A rising share of clips below `coverage_ok` is a `DEGRADED` signal, and a rising
`fetch_attempts` average means the flush grace period is too short for the host.

---

## Immutable decisions

These are invariants in a different dress: decisions we have already made, where
revisiting them is a new ADR, not a code change.

| Decision | ADR |
|---|---|
| Frigate is the detection and recording engine; we never reinvent it | [0001](../adr/0001-frigate-as-detection-engine.md) |
| Sites are fully independent; there is no central hub | [0002](../adr/0002-independent-per-site-deployments.md) |
| `config/cameras.yml` is the only camera source of truth | [0003](../adr/0003-cameras-yml-single-source-of-truth.md) |
| SQLite is the state store | [0004](../adr/0004-sqlite-for-state.md) |
| Cloud storage is behind an adapter; B2 is the only v1 implementation | [0005](../adr/0005-b2-behind-storage-adapter.md) |
| The canonical clip path layout is fixed as specified | [0006](../adr/0006-canonical-clip-layout.md) |
| Clip detection is event-driven via MQTT + HTTP, never filesystem watching | [0007](../adr/0007-mqtt-api-over-filesystem-watching.md) |
| Monitoring UI and alerting are an external tool, not something we build | [0010](../adr/0010-external-monitoring-tool.md) |
| Custom services are Python | [0011](../adr/0011-python-fastapi-services.md) |

---

## Anti-patterns

Behaviours that violate the spirit of the above even when they technically compile.

| Anti-pattern | Violates | Why it is wrong |
|---|---|---|
| `if camera == "front-door":` | INV-3 | Cameras are data |
| `Path(f"clips/{camera}/{time.time()}.mp4")` | INV-5 | Non-deterministic path |
| `rm -rf` in a cleanup cron | INV-1 | Retention bypasses upload state |
| `{"status": "healthy"}` as a literal | INV-6 | Health must be derived |
| Storing the DB in the container layer | INV-4 | Data loss on recreate |
| `docker compose pull` on a floating tag in `update.sh` | INV-8 | Untested version jumps |
| Listing the bucket to decide what to delete | INV-10 | Eventually-consistent basis for deletion |
| `datetime.now()` for clip naming | INV-12 | DST corruption |
| Deleting rows when a camera is removed | INV-13 | Destroys history on a config edit |
| Truncating a clip so its duration matches the requested range | INV-14 | Removes footage to satisfy a number |
| Discarding a short clip because a better one might arrive | INV-14 | Converts degraded evidence into no evidence |
| Fetching a clip immediately after `frigate/events` reports `end` | INV-14 | Races Frigate's segment flush; produces silent truncation |
| Stripping audio in the remux to save space | INV-14 | Audio is evidence, and the saving is negligible |
