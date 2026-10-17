# Handoff — context for a new agent

**Snapshot: 2026-09-13.** This is a *point-in-time* note, not a design document. Once it is
superseded, delete it: [`AGENTS.md`](../../AGENTS.md), [`docs/index.md`](../index.md) and the
ADRs are authoritative, and a stale handoff that contradicts them is worse than no handoff.

---

## 1. What this is

A packaging-first, self-hosted CCTV stack built **on top of Frigate**. Frigate does detection,
recording and review segmentation; it deliberately does not do durable clip storage, cloud
backup, independent retention, or health reporting. Those four are the product.

Read first: [`AGENTS.md`](../../AGENTS.md) → [`docs/index.md`](../index.md) → then
[`docs/glossary.md`](../glossary.md) (Frigate's vocabulary is subtle and the docs assume it).

**Working rule in this repo:** design docs are the source of truth. Code follows docs. If
reality disagrees with a doc, the doc is the bug and gets fixed in the same change.

---

## 2. What works right now — verified by running it, not by inspection

The v0.1 happy path is **detect → materialise at the canonical clip path → installable**.

Measured on 2026-09-13 against a live Frigate fed by real footage:

```text
sweep: 22 considered, 21 materialised, 1 skipped, 0 deferred, 0 failed
review handled  review_id=1789305830.148929-5w632d
                path=clips/synthetic/2026/09/13/18-53-50.mp4  coverage=0.9897
sweep: 1 considered, 0 materialised, 1 skipped, 0 deferred, 0 failed
```

- The **sweep** recovered 21 reviews that closed while no worker existed.
- **MQTT** handled a *live* review ~20 s after it closed.
- The third line is the important one: the sweep re-offered the review MQTT had just
  materialised, and it was skipped as already known. **Both paths raced on the same review and
  produced one clip** — INV-7 idempotence, proven live rather than only unit-tested.
- Result: 22 clips, 22 distinct reviews, **0 duplicates, 0 degraded, every one on the first
  fetch attempt**, coverage 0.98–1.10.

Clip boundaries verified end to end: the sidecar's `requested_start_ts` is the review start
minus 5 s (`pre_roll_s`) and `requested_end_ts` is the review end plus 10 s (`post_roll_s`).

Lint and tests: `ruff` clean, 90 files formatted, **510 tests passing**.

---

## 3. What does NOT exist

Upload (B2), retention/cleanup, health, backup, upgrade. **Nothing is ever deleted**, which is
why those parts are *absent* rather than broken — there is no cleanup that could lose a clip,
and no health check that could report healthy while the disk fills.

All of it is tracked in [`docs/plans/backlog.md`](backlog.md) with reasons.

---

## 4. Unit tests for the four new modules — DONE

**Done 2026-09-13.** They had *none* — the count was stuck at 368 because the new code was
verified only by running the whole system, which is strong evidence for the wiring and weak
evidence for the edges. The count is now **510 passing** (142 new):

| File | Tests | What it pins |
|---|---|---|
| `tests/test_logging.py` | 32 | `redact_url`; the filter, for the message, the args **and** the promoted extra fields; the payload allowlist; every `configure_logging` level path |
| `tests/test_mqtt.py` | 36 | that `paho` stays optional (an import hook makes it unimportable, and the hook is proved to work); malformed payloads counted rather than raised; `connect_async` and a re-subscribe on every connect |
| `tests/test_worker.py` | 44 | `Settings.from_environ`; `handle`'s five drop paths; a sweep that raises; the sweep interval; the wiring; signals; `main`'s exit codes |
| `tests/test_config_cli.py` | 30 | the emitted key order; sub-stream → `detect`; determinism; the header warnings; `validate`/`generate` exit codes and file mode |

`tests/conftest.py` holds the only two shared fixtures. `_root_logger_restored` is autouse
because `configure_logging` replaces the root handlers by design — right for a process,
hostile to a test session, and the failures land on whichever test runs next.

### Two INV-2 holes, found by writing them

This was not a formality. Two redaction leaks were found and fixed in `src/cctv/logging.py`:

1. **A password containing `@` was only half-redacted.** `_USERINFO_RE` stopped at the first
   `@`, so `rtsp://admin:p@ssw0rd@cam.local/stream` became
   `rtsp://***@ssw0rd@cam.local/stream` — the tail of the password, published on a line that
   *looks* redacted. The userinfo runs to the **last** `@` before the path; the character
   class is now `[^/\s]*`.
2. **Promoted `extra` fields were never redacted at all.** `extra={"path": url}` — the
   idiomatic way to log which clip was fetched — reaches the payload as a record *attribute*,
   so it travels through neither `record.msg` nor `record.args`. The formatter now applies
   `redact_url` to every string it promotes, which is the other half of the rule the filter
   enforces.

Both were confirmed by running the test against the old code first, not by reading it.

Also worth keeping: **do not write placeholder tests.** Three filler tests were written in an
earlier session and each was caught only by reading them back, never by running the suite.

---

## 5. Layout

```text
src/cctv/                 the shared library + two entry points
  worker.py               cctv-clip-worker — the service
  config_cli.py           cctv-config — cameras.yml → Frigate's config
  logging.py              JSON logs, RTSP credential redaction
  mqtt.py                 paho adapter (lazy import)
  dispatch.py             bounded hand-off from the network thread
  reviews.py              review payload → candidate, and the clip/no-clip decision
  materialise.py          fetch → verify → store
  clip.py                 when to fetch, and whether the result is good enough
  ffprobe.py              measures what actually came back
  sweep.py                the reconciliation guarantee
  frigate.py              HTTP client
  db.py, migrations/      SQLite state, checksummed forward migrations
  paths.py                THE clip path function (INV-5 — there is exactly one)
  config.py, timeutil.py, atomic.py

compose.yml, Dockerfile, install.sh, .env.example
config/cameras.example.yml      the single source of truth's template (INV-3)
dev-synthetic/                  camera-free harness — KEEP, this is how CI runs it
spike/                          throwaway measurement harness — delete when M1 closes
docs/                           authoritative design
```

---

## 6. Commands

```bash
uv run pytest                                   # 368 tests
uv run ruff check . && uv run ruff format --check .
uv run cctv-config validate --cameras config/cameras.yml

# the camera-free harness (real Frigate, generated test pattern, manual-event API)
cd dev-synthetic && ./reviews.sh up

# the same harness with REAL footage, which is the only way to get real detections
cd dev-synthetic && FIXTURE=detect.ts ./reviews.sh up
```

To reproduce the end-to-end run: point a `cameras.yml` at the running harness's Frigate and run
the built image on its compose network:

```bash
docker build -t cctv-stack:local .
docker run --rm --network dev-synthetic_default \
  -v /tmp/cctv-e2e:/config:ro -v /tmp/cctv-e2e/data:/data \
  -e CCTV_CONFIG_DIR=/config -e CCTV_DATA_DIR=/data \
  -e CCTV_FRIGATE_URL=http://frigate:5000 -e CCTV_MQTT_HOST=mosquitto \
  cctv-stack:local cctv-clip-worker
```

---

## 7. Measured facts about Frigate — do not re-derive these

Full detail and method: [`docs/architecture/contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md) §3.5.

| Fact | Why it matters |
|---|---|
| `end_time` is the **last activity** time, not the time the review closed (`get_data(ended=True)` returns `last_alert_time`) | A 30 s visit with a 40 s cutoff *closes* 70 s after it began but reports `end_time` 30 s after it began. Cutting to `end_time + post_roll_s` captures the visit exactly |
| `end` is the **only** review message carrying a non-null `end_time`; `new` and `update` send `null` | The trigger must evaluate on every message, never on `type` |
| `frigate/reviews` reaches MQTT via `Dispatcher._receive`'s **fall-through** for unhandled topics — *not* via `handle_upsert_review_segment`, which only writes the DB | Reading that function alone suggests reviews never reach MQTT at all. They do. This cost a wrong conclusion once |
| A Frigate restart closes open reviews by writing `end_time = now` **directly to the DB**, bypassing the MQTT publish path | Those reviews publish no `end`, so an MQTT-only trigger loses them. This is precisely why the sweep exists |
| Alert cutoff is 40 s, detection cutoff 30 s; `review.alerts.labels` defaults to `["person","car"]` | A `person` review is an `alert` with no zone configuration needed |
| Gaps of 25 s and 35 s **merge**; 45 s and 75 s **split** | `cutoff_time` is a merge window. `clip.visit_gap_s` projects straight onto it — we deliberately do **not** implement our own visit-grouping |
| Frigate does not store event clips as files; it cuts them on demand from recording segments | Hence `/api/{camera}/start/{ts}/end/{ts}/clip.mp4` |
| `-c copy` cut precision errs **outward** (~+1.9 s per 10 s) | A slightly-long clip is normal and is accepted, not retried |
| The largest packet gap in a healthy clip was **3.55 s** — larger than the 2.55 s keyframe interval | So `has_gaps` must never be derived from a packet gap |

---

## 8. Traps that have already cost time here

1. **`ruff`'s `exclude` replaces the defaults**, silently un-excluding `.venv`; `ruff format`
   then rewrote 392 files inside the virtualenv. Use `extend-exclude`.
2. **PyYAML implements YAML 1.1**, so `record.mode: off` loads as boolean `False`. There is a
   custom `_Yaml12Loader`. Never "fix" it back.
3. **Frigate vendors ffmpeg/ffprobe off `PATH`** at `/usr/lib/ffmpeg/<version>/bin/`.
4. **Compose substitutes `$VAR` in YAML before the container starts.** Use `$$` to escape. And
   note `$${VAR}` inside a command is evaluated *in the container*, where the variable is
   unset — a log line written that way cheerfully named the wrong file while ffmpeg read the
   right one.
5. **Port 5000 must never be published.** Frigate resolves requests on it as anonymous admin
   (contract §2). `auth.enabled: false` is safe *only* because of this.
6. **`/api/version` returns an empty body.** A readiness probe against it can never pass. Use
   `/api/stats` — the spike had already measured this and the lesson was lost once.
7. **`sqlite3.connect` creates a 0-byte database on open.** Read with
   `file:...?mode=ro`, or you will leave stray files behind while hunting a phantom.
8. **`urllib.parse.quote` leaves `/` unescaped** (it is in the default safe set), so a camera
   name can inject a path segment. Use `safe=""`.
9. **`uv sync` installs the root project *editable* by default.** In a runtime image, copying
   only `.venv` gives you entry points that exist and a package that does not. Use
   `--no-editable` — which also makes the image exercise the real wheel.
10. **`python:3.12-slim` has no `ffprobe`.** Every fetch then *succeeds* and every verification
    fails, silently burning the whole backoff per review. Unit tests inject a fake prober, so
    they cannot see it.

---

## 9. The product requirement to honour

The operator's requirement, stated explicitly:

> A person present at the door, with absences shorter than ~40 s, is **one clip** — however
> long that lasts, up to about an hour. If someone stands there for an hour, it is a one-hour
> video. These events are rare, so correctness matters far more than cost. Prefer a long clip
> over a fragmented one.

Consequences already known:

- **The single-clip guarantee is Frigate's, not ours.** Our boundaries come from
  `review.start_time` / `end_time`. If Frigate splits a long visit, we split it. The one place
  we might need segmentation logic Frigate lacks is merging adjacent reviews whose gap is under
  `clip.visit_gap_s` — **write it only if the measurement says it is needed.**
- **The real cap is recording retention, not a number we choose.** You cannot cut footage that
  has been deleted. See INV-9.
- **Four constants currently assume short clips** and will break a 1-hour clip. All four are in
  [`backlog.md`](backlog.md) §1.1–1.2; the sharpest is `DEFAULT_FETCH_TIMEOUT_CEILING_S = 1800`,
  which is *below the clip length* for anything over 28 minutes.
- **Do not add a truncation cap.** INV-14 forbids truncating, and truncating a long visit
  discards the most interesting part.
- A clip is only complete once the review closes, so a 1-hour visit yields a file ~1 h 40 s
  after it began. There is no partial clip by design.

---

## 10. How to work with this user

- **Do not overclaim.** "Everything is ready" was said once when only the library existed, and
  it was correctly challenged. State what is *verified* separately from what is *written*, and
  say how each was verified. Prefer "I ran it and here is the output" over "this works".
- **Plan before building, and record the plan in `docs/`.** The user asks for decisions to be
  tracked in the repo so other agents and humans can read them.
- **Fix docs in the same change as the code.** A behaviour change without a doc change is
  incomplete here.
- **Say when you were wrong.** A false conclusion about Frigate's MQTT topics was reached by
  reading one function and was caught only by measuring all topics. Reporting that honestly was
  welcomed; hiding it would have poisoned the contract.
- **The user will cut a rabbit hole.** They said: stop optimising, build the happy path so it
  can be installed at a site. Deferred work goes in `backlog.md`, not in the critical path.
- **Never write placeholder tests.** It has happened repeatedly under time pressure and is only
  ever caught by reading the test back.
