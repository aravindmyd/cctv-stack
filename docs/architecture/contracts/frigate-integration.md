# Contract: Frigate integration

**Parties:** `clip-worker`, `status-collector`, `cleanup` (read-only), the config
generator → **frigate**

Frigate is treated as a third-party subsystem. This contract is the only place
where knowledge of its API, MQTT topics, and storage layout is allowed to live.
No other document or code file may assume Frigate behaviour.

---

## 1. Version compatibility

| | |
|---|---|
| Pinned version | Exactly one Frigate version per release of cctv-stack, pinned by image **digest** ([ADR-0014](../../adr/0014-pinned-image-digests.md)) |
| Declared in | `compose.yml` + `versions.yml` (generated) |
| Supported upgrade path | Only forward, only between versions listed in [`lld/09`](../lld/09-upgrade-and-versioning.md) |
| Rationale | Frigate's API and MQTT payloads evolve. Floating tags would let a `pull` change our input contract without a test run |

**Rule:** any use of a Frigate API endpoint or MQTT field that is not listed below
is a bug. Add it here first, with the version it was verified against.

> **VERIFY AT IMPLEMENTATION.** The exact call signature, payload shape, and
> auth requirement of every endpoint below must be re-verified against the pinned
> Frigate version and the results recorded in this file (see §9). Frigate's API is
> documented at `https://docs.frigate.video/integrations/api/`.

---

## 2. Authentication

Frigate's API is role- and camera-scoped (`require_role(["admin"])`,
`require_camera_access`, `allow_any_authenticated`).

| Requirement | Detail |
|---|---|
| Service identity | A dedicated Frigate user created by the installer, never the operator's account |
| Required role | `admin` — we call `retain`/`unretain`, which are admin-scoped |
| Credential storage | `.env` only, `chmod 600`, never passed on a command line |
| Transport | Internal compose network over plain HTTP; the request never leaves the host |
| Failure mode | `401`/`403` → surface the exact upstream message, stop retrying, alert. **Never** silently degrade to "no clips" |

> **VERIFY:** how a long-lived machine credential is obtained in the pinned Frigate
> version. Preferred order: (a) a static API key if the version supports one,
> (b) a long-lived JWT obtained via the login endpoint and refreshed by
> `update.sh`, (c) disabling auth — **rejected**, it would require publishing the
> unauthenticated port 5000.

Unauthenticated Frigate API on port `5000` is **never** reachable from our
services. The installer fails validation if the compose file publishes it.

---

## 3. MQTT

Broker: mosquitto on the internal network, authenticated, anonymous disabled.
Topic prefix: `frigate` (Frigate's default; we do not change it).

### 3.1 Topics we consume

| Topic | QoS | Consumer | Purpose |
|---|---|---|---|
| `frigate/events` | 1 | clip-worker | Primary trigger for materialisation |
| `frigate/reviews` | 1 | (future) | Review-level grouping — not used in v1 |
| `frigate/available` | 1 (retained) | status-collector | Frigate liveness |
| `frigate/stats` | 0 | status-collector | Detection FPS, inference speed, camera state |
| `frigate/<camera>/status/<role>` | 1 | status-collector | Per-camera per-role `online` / `offline` / `disabled` |

### 3.2 `frigate/events` payload — fields we depend on

```jsonc
{
  "type": "new" | "update" | "end",
  "before": { /* previous state */ },
  "after": {
    "id": "1734567890.123456-abcxyz",   // event id; stable across new/update/end
    "camera": "front-door",             // must match a name in cameras.yml
    "label": "person",
    "sub_label": null,
    "zones": ["driveway"],
    "start_time": 1734567890.123456,
    "end_time": 1734567925.654321,      // null while in progress
    "has_clip": true,                   // authoritative: is a clip retrievable
    "has_snapshot": true,
    "top_score": 0.91,
    "false_positive": false,
    "retain_indefinitely": false
  }
}
```

**Trigger condition.** The clip-worker enqueues materialisation when:

```text
after.end_time != null
AND after.has_clip == true
AND after.false_positive != true
AND after.camera is known in cameras.yml
AND event.id not already in state.db
```

`type == "end"` is the expected carrier, but the condition above is evaluated on
**every** message regardless of `type`. This is deliberate: Frigate publishes
`update` messages when it finds a better snapshot or a zone change, and an event
can be ended by a final message that we should not depend on receiving.

**Fields we deliberately ignore:** `snapshot`, `box`, `area`, `region`, `ratio`,
`current_zones`, `entered_zones`, `thumbnail`, `motionless_count`,
`position_changes`, `current_estimated_speed`, `average_estimated_speed`,
`velocity_angle`, `recognized_license_plate`, `attributes`. They are stored
verbatim in the clip's JSON sidecar (§5.3) so nothing is lost, but no logic
branches on them.

**Unknown fields must not break parsing.** The parser is tolerant: it reads known
keys, ignores everything else, and never validates exhaustively. A Frigate release
adding a field must not take down our pipeline.

**Unknown cameras must not break parsing.** An event for a camera not in
`cameras.yml` is logged at `WARNING` with the camera name and dropped. (This is
normal when a camera was just removed — see [INV-13](../invariants.md).)

**Message ordering is not guaranteed.** Processing is keyed by event id and is
idempotent, so duplicate and out-of-order `update` messages are harmless.

### 3.3 MQTT is an optimisation, not the source of truth

Delivery is at-most-once from our perspective: a broker restart, a network blip,
or clip-worker downtime drops messages. Losing a message must never lose a clip.
The reconciliation sweep (§4.3) is the correctness mechanism.

---

## 4. HTTP endpoints we call

### 4.1 Pin and unpin an event

```http
POST   /api/events/{event_id}/retain     → {"success": true, "message": "..."}
DELETE /api/events/{event_id}/retain     → {"success": true, "message": "..."}
```

| | |
|---|---|
| Purpose | Protect an event's source footage from Frigate's own retention until we hold our own copy ([INV-9](../invariants.md)) |
| Role | `admin` |
| When | Pin: immediately on enqueue. Unpin: after the canonical clip is durably written |
| Idempotency | Must be treated as idempotent by us; a repeated pin is not an error |
| Failure to pin | **Fatal for that event.** Do not download without a successful pin. Log, record `PIN_FAILED`, alert — because the clip may be lost |
| Failure to unpin | Non-fatal. Log, and let the retention sweep unpin later. A stuck pin wastes Frigate disk, it does not lose data |
| Safety valve | A `pinned_at` timeout reaper unpins events pinned longer than N hours, so a dead clip-worker cannot fill the disk with pinned events |

> **VERIFY:** that this endpoint is available and admin-scoped in the pinned
> version. It exists in the current `dev` branch. If a newer mechanism supersedes
> it (the events API also exposes a `favorites` / `retain_indefinitely` concept),
> use that and record the change here.

### 4.2 Download a clip for a time range

```http
GET /api/{camera_name}/start/{start_ts}/end/{end_ts}/clip.mp4
```

**This is the primary clip-retrieval endpoint, and it is deliberately not the
event-scoped one.** The operator needs footage before `event.start_time` and after
`event.end_time`, asymmetrically — and only this endpoint can express that
([ADR-0015](../../adr/0015-clip-boundaries-and-padding.md)).

The range we request is:

```text
start_ts = event.start_time − clip.pre_roll_s
end_ts   = event.end_time   + clip.post_roll_s
```

| | |
|---|---|
| Semantics | **Not a file read.** Frigate runs ffmpeg over the recording segments overlapping the range, using a concat playlist with `inpoint`/`outpoint` and `-c copy -movflags frag_keyframe+empty_moov`, and streams a **fragmented MP4** |
| Response | `200` streaming `video/mp4` |
| No footage | **`400`** with `{"success": false, "message": "No recordings found for the specified time range"}`. Must be classified as **retryable** — see below |
| Cut precision | Stream copy can only cut at keyframes, so the achieved range will not match the request exactly. We **err outward** and never trim ([INV-14](../invariants.md)) |
| Audio | Preserved by `-c copy`. We never pass `-an`. Some cameras emit codecs browsers cannot play (G.711); we record the codec rather than transcode |
| Cost | CPU and disk I/O on the Frigate container for the duration of the cut. A stream copy, not a re-encode, but not free |
| Concurrency | Bounded by `clip-worker.fetch_concurrency` (default 2) so Frigate's detection is not starved |
| Timeout | Generous but bounded (`fetch_timeout_s`, default 120). Timeout → retry, not immediate failure |
| Partial content | A truncated download is detected by comparing bytes received against `Content-Length` when present. A short read is a retryable failure |

#### Why not the event endpoint with `?padding=`

`GET /api/events/{event_id}/clip.mp4?padding=N` also exists, and its padding is
**symmetric**: Frigate requests `start_time − N` through `end_time + N`. There is no way
to ask for a different pre-roll and post-roll.

A deployment wanting 5 s of pre-roll and 20 s of post-roll would therefore have to pass
`padding=20` and accept 15 s of unwanted lead-in on **every** clip — roughly **34 GB**
over 90 days at 100 events/day and 2 Mbps. The range endpoint gives the exact range, so
pre-roll and post-roll are independent and neither is inflated.

The event endpoint remains the **documented fallback**, used with
`padding = max(pre_roll_s, post_roll_s)`. It has one advantage we give up: it checks
`has_clip` and returns `404` when a clip is unavailable. We do not need that check — the
event payload we already hold from MQTT or the sweep carries `has_clip`.

#### Never fetch before the segment is flushed

> ⚠️ **Frigate writes new recording segments to cache and moves them to disk shortly
afterwards.** When `frigate/events` reports an event as ended, the final segment
containing the end of that event may not be on disk yet. A clip fetched immediately is
therefore **silently truncated** — it ends early, with no error and no indication that
anything is missing.

The clip-worker schedules every fetch at:

```text
fetch_not_before = event.end_time + post_roll_s + segment_flush_grace_s
```

and does not touch the item before it is due. A `400 "No recordings found"` is
classified as **retryable** for the same reason — it usually means the flush has not
happened yet, not that the request was malformed. Treating it as a permanent error would
turn every flush lag into a lost clip.
| Cost | CPU and disk I/O on the Frigate container for the duration of the cut. It is a stream copy, not a re-encode, but it is not free |
| Concurrency | Bounded by `clip-worker.fetch_concurrency` (default 2) to avoid starving Frigate's detection work. This limit exists for a reason — raising it can degrade detection latency |
| Timeout | Generous but bounded (`fetch_timeout_s`, default 120). Timeout → retry, not immediate failure |
| Partial content | A truncated download is detected by comparing bytes received against `Content-Length` when present. A short read is a retryable failure |

**Consequence for `clip-worker`:** because the response is a fragment-keyframe MP4
stream and has no meaningful `Content-Length` guarantee, we always write to a
`.tmp` file, then remux, then verify with `ffprobe`, then atomically rename. See
[`lld/02-clip-worker.md`](../lld/02-clip-worker.md).

**Not used:** the HLS/VOD endpoints and `review/{id}/clip.mp4`. They overlap in function
but are not scoped to a time range we control.

Note the iOS caveat Frigate's docs attach to this endpoint — Safari does not reliably
play progressive MP4, so Frigate suggests HLS for iOS *playback*. That concerns live
playback, not retrieval: our remux to a faststart MP4 happens immediately after download,
and the stored file plays in every client we care about.

### 4.3 Reconciliation sweep

```http
GET /api/events?has_clip=1&in_progress=0&after={watermark}&limit={n}&sort=date_asc
```

| | |
|---|---|
| Purpose | **Correctness backstop.** Catch every completed event with a clip that MQTT did not deliver |
| Frequency | `sweep_interval_s`, default 300 |
| Watermark | Highest `start_time` for which **every** returned event has been durably recorded in `state.db`. Persisted, so restarts resume |
| Overlap | The sweep always starts at `watermark - overlap_window` (default 600 s) to tolerate clock skew and events that end after their start time |
| Pagination | Loop until fewer than `limit` results are returned |
| Failure | On any error, do **not** advance the watermark. Retry next tick |
| Backlog bound | If the watermark lags more than `max_backlog_s` (default 6 h), raise health to `UNHEALTHY` — this means events are at risk of expiring in Frigate |
| Staleness detection | Frigate's own `has_clip` may flip to `false` once recordings expire. Such events are recorded as `EXPIRED_BEFORE_FETCH` and counted as **data loss**, not silently skipped |

### 4.4 Health and statistics

```http
GET /api/stats      → object with cameras, detectors, service uptimes, storage
GET /api/version    → running version, for the compatibility check
```

Used by `status-collector` to populate health. Also used at startup for a
compatibility assertion: if the running Frigate version does not match
`versions.yml`, the stack reports `DEGRADED` with an explicit message rather than
running against an untested contract.

> **VERIFY:** the exact shape of `/api/stats` in the pinned version. Only the fields
> listed in [`lld/06`](../lld/06-health-and-monitoring.md) may be read; no logic may
> depend on undocumented keys.

### 4.5 Endpoints we deliberately do not use

| Endpoint | Why not |
|---|---|
| `DELETE /api/events/{id}` | We never delete Frigate's data. Frigate owns its retention |
| `POST /api/events/{id}/plus` | Frigate+ is the operator's choice, not ours |
| `/{camera}/{label}/clip.mp4` | Latest-event semantics, not event-scoped |
| `/api/events/{id}/snapshot.jpg` | Not needed for v1; may be added as a sidecar later |
| `/api/restart`, `/api/media/sync` | Frigate lifecycle is not ours to drive |

---

## 5. Storage layout we read

### 5.1 Frigate's paths

| Path | Contents | Retention |
|---|---|---|
| `/media/frigate/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4` | Recording segments, **UTC** | Frigate's `record:` config |
| `/media/frigate/clips/` | Event snapshots, review thumbnails, faces | Frigate's `snapshots:` config |
| `/config/frigate.db` | Frigate's database (events, recordings, reviews) | n/a — treated as a cache |

**We do not read any of these paths directly.** All access is via the HTTP API.
This is deliberate: the on-disk layout is an internal detail of Frigate and has
changed across versions (most notably when clips stopped being stored as files).
Reading files would couple us to that instability.

### 5.2 The retention clock

The clip-worker's recovery window is bounded by Frigate's recording retention:

```text
headroom_s = frigate_record_retention_s − age_of_oldest_unmaterialised_event
```

Frigate's retention is known to us because **we generate its config**. The
generated default must keep this window comfortably large:

| Setting | Default | Rationale |
|---|---|---|
| `record.continuous.days` | `0` (disabled) | Continuous retention is ~2 orders of magnitude more expensive than clip retention |
| `record.motion.days` | `3` | Gives the clip-worker a 72-hour recovery window |
| `record.alerts.retain.days` | `90` local policy reflected where Frigate supports it | Keeps alert footage available to Frigate's UI |
| `record.detections.retain.days` | `14` | Bounded |

**Validation rule:** `record.motion.days` below `min_headroom_days` (default 2)
produces a hard validation warning, because the clip-worker's backlog could then
outlive its source. See §6 of this document and [INV-9](../invariants.md).

### 5.3 Clip JSON sidecar

Alongside each canonical clip we write `<name>.mp4.json` containing the original
event payload verbatim plus our own metadata. This is the escape hatch that makes
"we ignored field X" safe: nothing Frigate told us is discarded.

---

## 6. Error handling matrix

| Upstream condition | Our behaviour | Retry? | Health effect |
|---|---|---|---|
| `401` / `403` | Stop, surface the message, alert | No | `UNHEALTHY` |
| `404` on `retain` | Event ended and was pruned already | No | `WARNING`, count as `EXPIRED_BEFORE_FETCH` |
| **`400` + `"No recordings found"`** | Segments not flushed yet, or expired | **Yes**, with a delay — this is the flush race, not a bad request | `DEGRADED` if it persists past the retries |
| `404` on the clip range | Range endpoint unavailable; fall back to the event endpoint once | Yes, then fallback | `WARNING` |
| Connection refused | Frigate down | Yes, backoff | `UNHEALTHY` |
| Timeout mid-download | Truncated stream | Yes, discard `.tmp`, backoff | `DEGRADED` |
| `5xx` | Upstream fault | Yes, backoff | `DEGRADED` |
| Malformed MQTT JSON | Parse failure | No (message dropped) | `WARNING` counter |
| Unknown camera | Drop with a warning | No | none |
| Frigate version mismatch | Continue, flag it | n/a | `DEGRADED` |
| Frigate unreachable at startup | Wait and retry forever | Yes | `UNHEALTHY` until reachable |

---

## 7. Resource limits we impose on ourselves

| Limit | Default | Why |
|---|---|---|
| `fetch_concurrency` | 2 | Protect Frigate's detection pipeline — we are a guest |
| `sweep_interval_s` | 300 | Bound API load |
| `sweep_page_size` | 100 | Bound memory and response size |
| Max pinned events | 500 | Prevent a stuck worker from filling Frigate's disk |
| Pin timeout | 6 h | Safety valve on the above |
| MQTT client id | Stable per service | Avoids reconnect storms |

---

## 8. Testing without real cameras

`dev-synthetic` profile ([ADR-0013](../../adr/0013-compose-profiles.md)) substitutes a
synthetic RTSP source for real cameras, letting CI exercise the full pipeline:

```text
synthetic RTSP source → frigate → frigate/events → clip-worker → canonical clip
                                                        ↓
                                                  uploader → MinIO (B2-compatible test double)
```

Requirements this creates:

- The storage adapter must be testable against an S3-compatible endpoint
  (`STORAGE_ENDPOINT_URL` override), not only real B2.
- Object keys must not encode anything B2-specific.
- The synthetic source must be able to produce deterministically-timed events so
  that clip path derivation can be asserted.

---

## 9. Verification checklist (must be completed before implementation is approved)

Every item below must be checked against the pinned Frigate version and the answer
recorded here, because a wrong assumption here invalidates the LLD.

- [ ] `POST` / `DELETE /api/events/{id}/retain` exist, are admin-scoped, and behave idempotently
- [ ] `GET /api/events/{id}/clip.mp4` returns a usable stream for a completed event
- [ ] **`GET /api/{camera}/start/{start}/end/{end}/clip.mp4` accepts arbitrary asymmetric ranges** and returns the expected footage
- [ ] The range endpoint returns `400 "No recordings found"` (not `404`) when there is no footage — confirming the retryable classification
- [ ] Measure the **segment flush delay**: how long after `event.end_time` before the final segment is retrievable, and whether an immediate fetch is truncated
- [ ] Measure the **achieved cut precision** with `-c copy`: does the result err outward or inward, and by how much
- [ ] Confirm **audio survives** the cut and the remux on a camera with a microphone
- [ ] Confirm the **keyframe/GOP interval** so `max_gap_s` can be set above it without false gap positives
- [ ] The downloaded stream is playable by the clients we care about **after** our remux step
- [ ] `GET /api/events?has_clip=1&in_progress=0&after=…&sort=date_asc` supports the parameters we rely on
- [ ] The `sort` value for ascending-by-start-time is correct
- [ ] `frigate/events` payload matches §3.2 for `new`, `update`, and `end`
- [ ] `frigate/<camera>/status/<role>` values match `online` / `offline` / `disabled`
- [ ] `/api/stats` contains the fields §4.4 requires
- [ ] The auth mechanism in §2 exists and can be provisioned non-interactively
- [ ] Port `5000` really is unauthenticated (confirming why it must never be published)
- [ ] Frigate's recording retention defaults are configurable as §5.2 assumes
- [ ] The image digest for the host profile's variant is available
