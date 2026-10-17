# Contract: `cameras.yml` schema

**Parties:** operator → config generator → frigate, clip-worker, uploader, cleanup,
status-collector

`config/cameras.yml` is the **single source of truth** for cameras
([INV-3](../invariants.md), [ADR-0003](../../adr/0003-cameras-yml-single-source-of-truth.md)).
Everything else that needs to know about a camera is derived from it.

---

## 1. Shape

Three top-level keys, in this order:

```yaml
site:      # identity of this installation
defaults:  # values every camera inherits
cameras:   # the cameras
```

Merge rules:

| Rule | Detail |
|---|---|
| Inheritance | A camera inherits `defaults`, then applies its own keys |
| Deep merge | Nested mappings merge key-by-key; the child wins on leaves |
| Lists | **Replaced, not concatenated** — `objects: [person]` on a camera replaces the default list entirely. Merging lists silently is the top cause of "why is it detecting cars?" |
| `enabled: false` | The camera is generated out of Frigate's config, and our services treat it as retired ([INV-13](../invariants.md)) |
| Unknown keys | **Hard error.** A typo silently doing nothing is worse than a failed install |
| `null` | Explicitly "unset, use the inherited value" |

---

## 2. Annotated example

```yaml
site:
  location: parents-home                  # required, [a-z0-9-], part of the cloud key prefix
  timezone: Australia/Sydney              # required, IANA name, validated against tzdata
  display_timezone: Australia/Sydney      # optional, defaults to site.timezone; used for clip filenames

defaults:
  detect:
    enabled: true
    objects: [person]                     # labels to track
    min_initialized: 2
    min_score: 0.4
    fps: 5                                # detection framerate on the sub stream
  record:
    mode: motion                          # motion | continuous | alerts | detections | off
  snapshots:
    enabled: true
    retain_days: 30                       # Frigate-side snapshot retention
  clip:
    pre_roll_s: 5                         # footage before the review starts
    post_roll_s: 10                       # footage after the review ends
    visit_gap_s: 40                       # quiet period that closes a visit (Frigate cutoff_time)
  retention:
    local_days: 90                        # OUR canonical clip retention on disk
    cloud_days: 60                        # OUR cloud retention
    upload: true                          # false => SKIPPED_BY_POLICY, never deleted early
  notify:
    enabled: true
    labels: [person]                      # which labels create alerts

cameras:

  front-door:
    display_name: "Front Door"
    rtsp:
      main: "rtsp://${FRONT_DOOR_USER}:${FRONT_DOOR_PASSWORD}@192.168.1.50:554/stream1"
      sub:  "rtsp://${FRONT_DOOR_USER}:${FRONT_DOOR_PASSWORD}@192.168.1.50:554/stream2"
    detect:
      objects: [person, car, dog]
      width: 1280
      height: 720
    record:
      mode: alerts                        # override: only record around alerts
    clip:
      post_roll_s: 20                     # cars take a while to clear this driveway
    retention:
      local_days: 120                     # keep this one longer
    notify:
      labels: [person, car]

  garage:
    rtsp:
      main: "rtsp://${GARAGE_USER}:${GARAGE_PASSWORD}@192.168.1.51:554/stream1"
      sub:  "rtsp://${GARAGE_USER}:${GARAGE_PASSWORD}@192.168.1.51:554/stream2"
    detect:
      enabled: false                      # camera exists, detection off

  backyard:
    rtsp:
      main: "rtsp://${BACKYARD_USER}:${BACKYARD_PASSWORD}@192.168.1.52:554/stream1"
    detect:
      objects: [person]
    retention:
      upload: false                       # local-only, deliberately

  old-garage:
    enabled: false                        # retired; config retained for reference
    rtsp:
      main: "rtsp://192.168.1.99:554/stream1"
```

---

## 3. Field reference

### 3.1 `site`

| Field | Type | Req | Default | Validation |
|---|---|---|---|---|
| `location` | string | ✅ | — | `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$`; also in `.env` as `LOCATION` — must match |
| `timezone` | string | ✅ | — | Must exist in `zoneinfo`; no offsets like `+10:00` |
| `display_timezone` | string | — | `site.timezone` | Must exist in `zoneinfo` |

### 3.2 `defaults` and per-camera keys

Any key valid under `defaults` is valid on a camera. The table below applies to
both.

| Field | Type | Default | Validation | Effect |
|---|---|---|---|---|
| `display_name` | string | camera key | 1–64 chars, no control chars | UI label only; never used in a path |
| `enabled` | bool | `true` | — | Removes the camera from the generated Frigate config |
| `rtsp.main` | string | — | `rtsp://` or `rtsps://`, parseable, no spaces | Recording + detection source |
| `rtsp.sub` | string | — | same | Detection source; falls back to `main` if absent, with a performance warning |
| `detect.enabled` | bool | `true` | — | Frigate `detect.enabled` |
| `detect.objects` | list[str] | `[person]` | Non-empty when `detect.enabled`; each a known Frigate label | Tracked labels |
| `detect.width` / `.height` | int | 1280 × 720 | ≥ 64, ≤ 8192, even numbers | Detection resolution — the main lever on CPU/accelerator load |
| `detect.fps` | int | 5 | 1–15 | Detection framerate |
| `detect.min_score` | float | 0.4 | 0.0–1.0 | Confidence floor |
| `detect.min_initialized` | int | 2 | ≥ 1 | Frames before an object is confirmed |
| `record.mode` | enum | `motion` | `motion`\|`continuous`\|`alerts`\|`detections`\|`off` | Maps to Frigate `record:` retention blocks (§4) |
| `snapshots.enabled` | bool | `true` | — | Frigate snapshots |
| `snapshots.retain_days` | int | 30 | ≥ 1 | Frigate-side snapshot retention |
| `clip.pre_roll_s` | float | 5 | 0–60; *warn* above 30 | Footage included **before** `review.start_time`, so the clip shows what led up to the detection |
| `clip.post_roll_s` | float | 10 | 0–120 | Footage included **after** `review.end_time`, so the clip does not cut off mid-action |
| `clip.visit_gap_s` | int | — (Frigate's own: 40 alert / 30 detection) | 5–3600 | The quiet period after which a visit is considered finished, applied to **both** severities. **Projected into Frigate** as `review.alerts.cutoff_time` and `review.detections.cutoff_time`. **Raising it is what keeps a subject who leaves and returns inside one clip** — and it delays every clip by the same amount, because we cannot materialise until the review closes. Unset means the key is not emitted and Frigate's own defaults apply |
| `clip.severities` | list | `[alert, detection]` | ⊆ `{alert, detection}` | Which Frigate review severities become canonical clips. `[alert]` cuts volume substantially |
| `clip.objects` | list | `[person]` | Must be ⊆ `detect.objects` | A review is clipped only if `data.objects` contains at least one of these |
| `retention.local_days` | int | 90 | 1–3650 | **Our** canonical clip retention |
| `retention.cloud_days` | int | 60 | 1–3650 | **Our** cloud retention |
| `retention.upload` | bool | `true` | — | `false` → `SKIPPED_BY_POLICY`; clips are still retained locally, never deleted early |
| `notify.enabled` | bool | `true` | — | Generates an Uptime Kuma monitor |
| `notify.labels` | list[str] | `[person]` | must be ⊆ `detect.objects` | Alert-creating labels |

**Secret interpolation.** `${VAR}` references are resolved from the environment at
generation time. A reference to an unset variable is a **hard error**, never an
empty string — an empty camera password produces a confusing camera-offline
condition instead of an honest configuration failure.

Storing credentials inline in `cameras.yml` is permitted (the file is `chmod 600`)
but `${VAR}` is strongly recommended; the installer writes credentials to `.env`
and references them.

---

## 4. Mapping to Frigate's config

The generator owns this mapping. It is deterministic: same input, byte-identical
output.

| `cameras.yml` | Generated Frigate key |
|---|---|
| `site.timezone` | **Not passed to Frigate, deliberately.** Frigate 0.18 removed its top-level `timezone` key and rejects the entire config — starting in safe mode with no cameras — if one is present. Frigate's own zone comes from the container `TZ` ([ADR-0017](../../adr/0017-time-handling.md) treats the two as independently configured, and a disagreement is a `doctor.sh` check). Our clip paths are rendered from `site.timezone` |
| `camera.rtsp.main` | `cameras.<name>.ffmpeg.inputs[0].path` (roles: `record`) |
| `camera.rtsp.sub` | `cameras.<name>.ffmpeg.inputs[1].path` (roles: `detect`) |
| `detect.enabled` | `cameras.<name>.detect.enabled` |
| `detect.objects` → `track` | `cameras.<name>.objects.track` |
| `detect.fps` | `cameras.<name>.detect.fps` |
| `detect.width`/`height` | `cameras.<name>.detect.width` / `.height` |
| `detect.min_score` | `cameras.<name>.objects.filters.<label>.min_score` (per tracked label) |
| `record.mode` | `cameras.<name>.record:` retention blocks (below) |
| `snapshots.*` | `cameras.<name>.snapshots.*` |
| `clip.pre_roll_s`, `clip.post_roll_s` | **Not passed to Frigate.** Used by the clip-worker to build the fetch range ([ADR-0015](../../adr/0015-clip-boundaries-and-padding.md)) |
| `clip.visit_gap_s` | `cameras.<name>.review.alerts.cutoff_time` **and** `.review.detections.cutoff_time`. It defines what a visit *is*, so Frigate must own it — filtering it at our layer would be too late |
| `clip.severities`, `clip.objects` | **Not passed to Frigate.** Consumed by the clip-worker as a filter on incoming review items. Deliberate: Frigate's own review list stays complete, so an operator browsing Frigate sees everything while we only pay to store what they asked for. Changing these never requires regenerating Frigate's config |
| `retention.*` | **Not passed to Frigate.** Consumed by our services via `state.db` |
| `notify.*` | **Not passed to Frigate.** Consumed by `status-collector` |
| host profile | `ffmpeg.hwaccel_args`, `detectors:`, `model:` |

### 4.1 `record.mode` → retention blocks

Frigate retains a recording when any block matches, and picks the largest matching
retention. The generator translates our intent like this:

| Our mode | Generated `record:` |
|---|---|
| `off` | `record.enabled: false` |
| `continuous` | `continuous.days: 7`, `motion.days: 7`, alerts/detections retain 30 |
| `motion` **(default)** | `continuous.days: 0`, `motion.days: 3`, alerts retain 30, detections retain 14 |
| `alerts` | `continuous.days: 0`, `alerts.retain.days: 30` |
| `detections` | `continuous.days: 0`, `detections.retain.days: 30` |

**The `motion` default deliberately keeps a 3-day window.** That window is the
clip-worker's recovery budget if it is down ([INV-9](../invariants.md)). The
generator refuses to emit a window shorter than `min_headroom_days` (default 2)
without an explicit override flag, because a shorter window means a clip-worker
outage of the same length causes permanent loss.

`continuous` mode triggers a capacity warning at generation time (§14 of the HLD).

### 4.2 What the generator produces (abridged)

```yaml
# GENERATED FILE — DO NOT EDIT
# Source: config/cameras.yml
mqtt:
  enabled: true
  host: mosquitto
  port: 1883
  user: "{FRIGATE_MQTT_USER}"
  password: "{FRIGATE_MQTT_PASSWORD}"

auth:
  enabled: true

detectors:
  ov:
    type: openvino
    device: GPU

ffmpeg:
  hwaccel_args: preset-vaapi

record:
  enabled: true
  continuous:
    days: 0
  motion:
    days: 3
  alerts:
    retain:
      days: 30
  detections:
    retain:
      days: 14

cameras:
  front-door:
    ffmpeg:
      inputs:
        - path: "rtsp://..."     # secret, injected at runtime not written here
          roles: [record]
        - path: "rtsp://..."
          roles: [detect]
    detect:
      enabled: true
      width: 1280
      height: 720
      fps: 5
    objects:
      track: [person, car, dog]
      filters:
        person:
          min_score: 0.4
        car:
          min_score: 0.4
        dog:
          min_score: 0.4
    record:
      enabled: true
```

> **Secrets never appear in the generated file.** Credentials are referenced by
> environment variable name, and the container resolves them at startup. A
> generated file on disk is a file the operator might paste into a bug report.

---

## 5. Validation rules

Run by `install.sh`, `update.sh`, and by every service at startup
([INV-11](../invariants.md)). All are errors unless marked *warn*.

| # | Check | Message shape |
|---|---|---|
| V1 | YAML parses; no duplicate keys | `cameras.yml:14: duplicate key "garage"` |
| V2 | No unknown keys anywhere | `cameras.yml:23: unknown key "detection" (did you mean "detect"?)` |
| V3 | `site.location` matches `.env` `LOCATION` | `LOCATION mismatch: cameras.yml says "parents-home", .env says "parent-home"` |
| V4 | `site.timezone` exists in tzdata | `unknown timezone "Australia/Sydneyy"` |
| V5 | Camera keys match `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` | `camera "FrontDoor": must be lowercase kebab-case` |
| V6 | Camera keys are unique and not reserved | `camera "all" is a reserved name` |
| V7 | Every enabled camera has `rtsp.main` | `camera "garage": rtsp.main is required` |
| V8 | RTSP URIs parse and use `rtsp`/`rtsps` | `camera "garage": unsupported scheme "http"` |
| V9 | All `${VAR}` references resolve | `undefined variable FRONT_DOOR_PASSWORD referenced at cameras.yml:31` |
| V10 | `detect.objects` non-empty when detection enabled | `camera "garage": detect.objects cannot be empty` |
| V11 | `notify.labels` ⊆ `detect.objects` | `camera "garage": notify.labels contains "car" which is not in detect.objects` |
| V12 | `retention.cloud_days ≤ retention.local_days` *(warn)* | `cloud retention exceeds local retention; clips may be deleted locally before upload completes` — actually this is fine, but a large gap is worth flagging |
| V13 | `retention.local_days ≥ 1`; `cloud_days` ≥ 1 when uploading | `retention.cloud_days must be at least 1` |
| V14 | `detect.width`/`height` even and ≥ 64 | `detect.width must be an even number ≥ 64` |
| V15 | No two enabled cameras share a `rtsp.main` URL *(warn)* | `cameras "front-door" and "porch" use the same RTSP URL` |
| V16 | At least one enabled camera | `no enabled cameras: nothing would be recorded` |
| V17 | Generated Frigate retention ≥ `min_headroom_days` unless overridden | `record.mode "motion" gives a 3-day window; this is the clip-worker recovery budget` |
| V18 | Projected storage fits available space *(warn/error)* | `projected 90-day usage 412 GB exceeds 360 GB available` |
| V19 | `display_timezone` differs from `timezone` *(warn)* | `clip filenames and log timestamps will disagree` |
| V20 | `clip.pre_roll_s` in 0–60 | `clip.pre_roll_s must be between 0 and 60 seconds` |
| V21 | `clip.post_roll_s` in 0–120 | `clip.post_roll_s must be between 0 and 120 seconds` |
| V22 | `clip.pre_roll_s` above 30 *(warn)* | `pre-roll above 30s reaches outside what the event pin protects; this footage may be missing` |
| V23 | `clip.pre_roll_s + clip.post_roll_s` below the event's typical duration *(warn)* | `the clip will be dominated by padding; consider whether pre/post-roll are set correctly` |

Error messages must name the file, the line where possible, the offending value,
and the fix. "Invalid configuration" is not an acceptable message.

---

## 6. Change workflow

```text
edit config/cameras.yml
        │
        ▼
./update.sh --regenerate-config        (or ./install.sh, which does the same)
        │
        ├── validate (V1–V19)
        ├── diff and show what changes   ← operator confirms
        ├── regenerate config/frigate/config.yml
        ├── reload Frigate's config (via its API) or restart the container
        ├── sync cameras into state.db   (add/retire)
        ├── update Uptime Kuma monitors
        └── report health
```

Adding a camera is: append a block to `cameras.yml`, run `./update.sh`. No code
change, no container juggling, no manual Frigate config editing. That is the whole
point of [ADR-0003](../../adr/0003-cameras-yml-single-source-of-truth.md).

**First-run behaviour.** On first start, `state.db` is seeded from `cameras.yml`.
On every subsequent start, the generator and the state store reconcile:

| Situation | Action |
|---|---|
| Camera in YAML, not in DB | Insert; log `camera added` |
| Camera in both, enabled | Update metadata; keep history |
| Camera in DB, absent from YAML | Mark `retired_at`; keep all clips and rows ([INV-13](../invariants.md)) |
| Camera in both, `enabled: false` | Mark disabled; keep history; stop ingest |
| Camera re-added after retirement | Clear `retired_at`; same camera key means the same logical camera |
