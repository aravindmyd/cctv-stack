# Backlog

> **Historical note (2026-10-17).** This file was written against the v0.1 happy path.
> Upload (M3) and local retention (M4 J1) are now built, and every service exposes a
> health surface. The items below remain the record of what was deliberately deferred and
> what was learned; [`milestones.md`](milestones.md) is the current authority for what
> ships when. Nothing below has been deleted, so the reasoning is still reviewable.

Everything deliberately **not** in the original v0.1 happy path, recorded so it is neither
lost nor re-discovered later.

**The v0.1 happy path was deliberately narrow:**

> Install it on a site. Cameras detect a person. A clip lands at the canonical path. That
> is all.

No upload, no retention, no health at that point. That ordering was safe rather than merely convenient:
**nothing is ever deleted**, so [INV-1](../architecture/invariants.md) holds trivially and
the two riskiest failure modes — deleting an un-backed-up clip, and a disk that fills
silently — are simply not present yet.

`docs/plans/milestones.md` is the authority for *when* something ships. This file is the
authority for *what we learned and deferred*, including things that are too small for a
milestone but too sharp to forget.

---

## 1. Findings that need work (not yet in any milestone)

### 1.1 Long visits cannot be fetched

`clip.fetch_timeout_s` is `min(120 + requested_s, 1800)`. The ceiling is **below the clip
length** for anything over **28 minutes**, so a long visit gets a timeout shorter than its
own duration: it times out, retries, and eventually lands in `FAILED_PERMANENT`.

This is the exact failure the function's own docstring warns about — *"losing the clip
entirely rather than taking longer over it"* — reintroduced by the ceiling. It is the
worst possible clip to lose, because a long visit is the interesting one.

**Fix:** raise `DEFAULT_FETCH_TIMEOUT_CEILING_S` so footage-length plus cut/transfer
headroom fits (7200 s proposed), and make `reap_tmp`'s age gate derive from it — the reaper
must never delete a temp file from a fetch that is still running.

**Blocks:** nothing in v0.1. A doorstep visit is seconds, not hours.

### 1.2 `DEFAULT_LONG_CLIP_WARN_S` is calibrated for the wrong normal

Currently 600 s (10 min). It is flag-only and never truncates, so it is not a correctness
bug — but if a long visit is legitimate, every one of them logs a warning that means
nothing. A warning that always fires is a warning nobody reads.

**Fix:** raise to 3600 s, or redefine it as *"longer than the configured maximum visit"*
rather than a fixed number.

### 1.3 An hour-long visit is unmeasured

**Does Frigate keep a single review open through a long, continuous presence?**

This is the load-bearing unknown for the product requirement "a person at the door for an
hour is one clip". **We do not segment visits — Frigate does.** Our boundaries come
straight from `review.start_time` and `review.end_time`. If Frigate closes the review
part-way through and opens a new one, we produce two clips, and no amount of our own code
rejoins them unless we write review-merging logic ourselves.

The lever is `detect.stationary.interval` (default **50** frames): Frigate throttles
re-detection of a stationary object, and if the person stops being considered present the
review closes.

**How to measure:** build a fixture holding a subject stationary for a full hour (the
script in `dev-synthetic/fixture-from-video.sh` can be extended to do this) and observe
whether one review spans it. See
[`frigate-integration.md` §3.5](../architecture/contracts/frigate-integration.md) for the
measurement method that already works.

**If Frigate splits:** that is the *only* place we may need segmentation logic Frigate does
not have — merging adjacent reviews whose gap is under `clip.visit_gap_s`. Write it only if
the measurement says it is needed.

### 1.4 The `review.start_time` lag is not confirmed against live detections

Three manual-event samples showed `review.start_time` landing ~5 s after the triggering
event's own start. With `pre_roll_s: 5` that is covered *only just*, and a camera
configured with `pre_roll_s: 0` would begin every clip about 5 s after the subject
appeared. The live-detection run captured `start_time` values but they were never compared
against the events.

### 1.5 Event pinning is designed but never exercised

`POST` / `DELETE /api/events/{id}/retain` both exist and are admin-scoped in Frigate's
source, and `ClipMaterialiser` pins every `event_id` before fetching and unpins in a
`finally`. No test has run against real pinning, so "the pin actually prevented Frigate
from pruning the source" is assumed, not demonstrated.

### 1.6 Audio has never been tested end to end

Clip 1 and clip 2 both have audio (AAC 8 kHz mono) and the fixture builder deliberately
drops it, because Frigate must be configured to record audio at all — a separate config
change. M1.12 ("audio survives the cut") is still open, and the source clips make it
testable.

### 1.7 Fixture: colour range is handled implicitly

The source clips are `yuvj420p` (full range). `fixture-from-video.sh` converts them with
`format=yuv420p` and ffmpeg warns *"deprecated pixel format used, make sure you did set
range correctly"*. The result is `color_range=unknown`. Detection is unaffected — the
measured luma spread is healthy — but contrast may be slightly shifted. Harmless here;
worth fixing if the fixture ever becomes a visual reference.

### 1.8 `/admin/verify` is designed but not built

`lld/04` §9 and `service-contracts.md` §4 both list `POST /admin/verify`, which re-downloads
an object and re-hashes it against the row's `sha256`. It is the **only** admin operation that
costs egress, and downloads are the one thing B2 bills for. Nothing in M3's exit criteria
needs it, and every object carries its own `sha256` in its metadata, so
`aws s3api head-object` answers the same question for free.

**Fix, if it is ever wanted:** add `get_object` to `StorageAdapter` (streaming, with a
forced download and a re-hash) and wire the route. Deliberately left out rather than half
built. [`runbooks/recover-failed-uploads.md`](../runbooks/recover-failed-uploads.md) §4.1
documents the `head-object` alternative the operator should use today.

### 1.9 B2's `ChecksumSHA256` semantics are assumed, not measured

The adapter sends `ChecksumSHA256` (base64 of the local SHA-256) on a single-part PUT and
compares whatever comes back. That is the S3 specification, but it has only been exercised
against a fake client — `tests/test_storage.py` — never against real Backblaze.

The failure mode is safe rather than silent: if the value is not comparable, the comparison
fails, the clip goes to `FAILED_PERMANENT` with `checksum`, the local file is preserved, and
`UPLOAD_FAILED_PERMANENT` raises. A clip that cannot be *uploaded* is recoverable; a clip
falsely marked *uploaded* is not.

**Fix:** put a real object in a B2 bucket and record the response header in
[`cloud-object-layout.md`](../architecture/contracts/cloud-object-layout.md) §4.2, where the
VERIFY note still stands.

**Partly settled 2026-10-06.** The integration harness
([`tests/integration/`](../../tests/integration/README.md), [ADR-0019](../adr/0019-rustfs-as-s3-test-double.md))
now runs a single-part PUT against a live S3-compatible server (RustFS), which returns a
comparable `ChecksumSHA256` — the adapter records `verified_by="sha256"`, so the happy path is
real and not assumed. What remains is B2-specific: the same PUT against the real provider,
recorded in the contract.

### 1.10 Multipart uploads have never left the fake client

Above `multipart_threshold_bytes` (default 100 MB) the adapter switches to
`upload_file`, where the provider reports a composite digest and the only available check is
a `HEAD` comparing size and our own metadata. Real clips are 1–50 MB, so no clip has ever
taken this path, and neither has `TransferConfig`. The logic is unit-tested through a fake
`upload_file`; the wiring with real `boto3` is verified only to the point of constructing
`TransferConfig` successfully.

**Blocks nothing:** the switch is at 100 MB, and a clip that large is already exceptional.
Worth exercising with a `dd`-generated file the first time a real deployment stores one.

**Partly settled 2026-10-06.** The integration harness drives the multipart path against a
live server by lowering `multipart_threshold_bytes` to 5 MB rather than generating a 100 MB
clip, and asserts the adapter's honesty: a multipart PUT reports `verified_by="size"` (size
plus our own `sha256` metadata), never `"sha256"`. What remains is a real clip above the
default 100 MB threshold against the real provider.

---

## 2. Deferred features

These already have milestones. Listed here only so the shape of v0.1 is unambiguous.

**Upload to B2 shipped with M3** (`0.3.0`) and is no longer deferred: `src/cctv/uploader.py`,
`src/cctv/storage.py` and `src/cctv/health.py`, with the `uploader` service in `compose.yml`.
Its two outstanding measurements are §1.9 and §1.10 below.

| Feature | Milestone | Why deferred |
|---|---|---|
| Retention and cleanup (local + cloud) | M4 | **Cannot precede M3** — deleting before upload is terminal is the bug INV-1 exists to prevent. Now unblocked |
| Health as a first-class feature | M5 | v0.1 has no health surface at all; `component_health` tables sit empty |
| Backup and restore | M7 | Needs the DB and upload state to be worth backing up |
| Upgrade and rollback | M7 | Needs a released version to move between |

---

## 3. Known and accepted in v0.1

Recorded so they are choices rather than oversights.

- **Clips are only complete once the review closes.** A visit of *n* seconds produces a
  file roughly *n + cutoff_time + flush grace* after it began. There is no partial clip by
  design — clipping an open review would capture half a visit with no way to extend it.
- **The CPU detector.** `dev-synthetic` uses Frigate's CPU detector, which Frigate itself
  warns is for testing only. Real sites get a Coral or equivalent.
- **One camera in all testing so far.** Multi-camera behaviour is designed
  (`INV-3`, per-camera config) but has never been run with two cameras.
