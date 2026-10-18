# Glossary

Frigate's vocabulary is subtle, and most confusion traces back to words that look like
synonyms but are not. These are the terms this stack uses in its configuration, its
health output and its file names.

---

## Frigate concepts (upstream terms — do not redefine)

| Term | Meaning |
|---|---|
| **Camera** | A named video source configured in Frigate. Has a main stream (recording) and an optional sub stream (detection). |
| **Detector** | The hardware/software that runs object detection. `edgetpu`, `openvino`, `hailo8`, `onnx`, `cpu`, `rknn`, … |
| **Detected object / tracked object** | A single tracked instance of a label (e.g. one `person`) within a camera over time. Each has an **event id**. |
| **Event** | A completed or in-progress tracked object. Has `id`, `camera`, `label`, `start_time`, `end_time`, `zones`, `top_score`, and the flags `has_clip` / `has_snapshot` / `retain_indefinitely`. **No longer the unit we clip on** — see review item. |
| **Review item / review segment** | Frigate's grouping of one or more events into a period of activity, classified as `alert` or `detection`. Has `id`, `camera`, `start_time`, `end_time` (`null` while still open), `severity`, `thumb_path`, and `data.detections[]` (the event ids inside it), `data.objects[]`, `data.sub_labels[]`, `data.zones[]`, `data.audio[]`. Frigate's own UI list is built from these. **Reviews for a single camera cannot overlap, and are contiguous** — when one closes, the next begins at exactly the previous one's `end_time`. |
| **`cutoff_time`** | Frigate's **visit threshold**: the quiet period after which an open review is closed. `review.alerts.cutoff_time` (default **40 s**), `review.detections.cutoff_time` (default **30 s**). This is what decides whether a subject who leaves the frame and returns is **one clip or two**. |
| **Recording** | Continuous video segments written by Frigate to `/media/frigate/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4`. Frigate builds that path from its own container `TZ`. |
| **Snapshot** | A JPEG/WebP still captured at an event's best frame. Stored under `/media/frigate/clips/`. |
| **Zone** | A named polygon within a camera frame, used for filtering and alerting. |
| **Sub-label** | A recognisable identity on an event, e.g. a face name or a number plate. |

> ⚠️ **Frigate does not store event clips as MP4 files any more.**
> `frigate/events/cleanup.py` contains `file_extension = None  # mp4 clips are no longer stored in /clips`.
> An "event clip" is *cut on demand* from the recording segments by ffmpeg when you
> call `GET /api/events/{event_id}/clip.mp4`.
> This is the single most important fact in the whole design.

---

## Project vocabulary (our terms — use these exactly)

| Term | Meaning | Not to be confused with |
|---|---|---|
| **Clip** | *Our* artefact: a self-contained MP4 in the canonical tree `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`, owned by us, independent of Frigate's retention. | Frigate's "clip", which is generated on demand and not a file |
| **Canonical clip** | A clip that has been fully materialised, hashed, and recorded in the database. | An in-flight download |
| **Recording** *(ours)* | We do **not** manage Frigate's recordings. When a doc says "recording", clarify whose. | Clip |
| **Site** | One independent deployment of the stack, at one physical location. | Camera |
| **Location** | The human-readable name of a site, from `LOCATION` in `.env`. | Site |
| **Host profile** | The detected-and-confirmed capability set of the machine: architecture, GPU, accelerator, storage class. Drives which compose profile is enabled. | |
| **Component** | One deployable unit of the stack: Frigate, clip-worker, uploader, cleanup, status-collector, Uptime Kuma. | Container (many components could be several containers) |
| **Materialise** | The act of turning a Frigate **review item** into a canonical clip file on disk. | Upload |
| **Visit** | A review item, in human terms: one period of activity by one or more subjects. **One visit is one clip**, unless the subjects are separated by more than `cutoff_time`. | Between-visit gap |
| **Upload state** | The lifecycle position of a clip in the cloud pipeline: `PENDING` → `UPLOADING` → `UPLOADED` / `FAILED`. | Transfer state |
| **Storage adapter** | The interface through which clips are written to and deleted from cloud storage. `b2` is the only implementation in v1. | |
| **Pinned / retained** | An event we have asked Frigate to keep (`retain_indefinitely`) so its source footage survives until we have materialised our own copy. | Our retention policy |
| **Force delete** | An operator action that allows cleanup to remove a clip whose upload never succeeded. Never automatic. | |
| **Degraded** | A component that is running but cannot do its job (e.g. uploader running but B2 unreachable). Distinct from **unhealthy**. | |
| **Pre-roll** | Footage included *before* `review.start_time`, so the clip shows what led up to the first detection. Default 5 s. | |
| **Post-roll** | Footage included *after* `review.end_time`, so the clip does not cut off while the subject is still in frame. Default 10 s. | |
| **Review padding** | Frigate's own `REVIEW_PADDING` of **±4 s**, applied symmetrically when its UI exports a review. **We do not use it** — our pre-roll and post-roll are asymmetric and operator-configured. | Pre-roll / post-roll |
| **Segment flush** | Frigate writes new recording segments to cache and moves them to disk shortly afterwards. Until the flush happens, the footage is not available to a clip request. | |
| **Flush grace** | The delay we wait after `review.end_time + post_roll_s` before fetching, so the final segment has reached disk. Default 5 s. | |
| **Coverage ratio** | `actual_duration / requested_duration` for a materialised clip. Below 0.95 the clip is short; below 0.80 it is rejected and re-fetched. | |
| **Gap** | A hole in a clip caused by a missing recording segment — a camera dropout, or a segment expired before we fetched. Recorded as `max_gap_s`; never repaired, because the footage is gone. | |
| **Erring outward** | The rule that clip boundaries always over-run rather than under-run. Overshoot costs storage; undershoot loses evidence. | |

---

## State vocabulary

Health states are a small closed set. Do not invent new ones.

| State | Meaning |
|---|---|
| `HEALTHY` | Component is running and its dependencies are reachable and behaving. |
| `DEGRADED` | Component is running but a non-fatal dependency is failing. It still serves health and reports why. |
| `UNHEALTHY` | Component cannot do its job, or its critical dependency is unreachable. |
| `UNKNOWN` | No recent observation. Reported when a component has not reported within its staleness window. Also the initial state of a camera before its first observation. |
| `DISABLED` | Deliberately turned off by configuration. Not an error. |

Camera states reuse Frigate's own: `ONLINE`, `OFFLINE`, `DISABLED`.

---

## Naming and path rules

| Rule | Rationale |
|---|---|
| Camera names are `lowercase-kebab-case`, `[a-z0-9-]+`, max 32 chars. | They appear in filesystem paths and cloud object keys across multiple OSes and filesystems. Uppercase causes case-collision bugs on macOS and breaks URL/object-key sanity. |
| Timestamps in the database are **epoch seconds (float)** — an instant, which carries no timezone. | Frigate's API speaks epoch seconds. An instant is comparable regardless of zone; rendering the *same instant* in two different zones is the classic source of duplicate and missing clips. |
| Clip **filenames** are rendered in the site timezone (`site.display_timezone`, defaulting to `site.timezone`). | Humans browse these directories. The instant stays recoverable from the database and the `.json` sidecar, so nothing is lost. |
| Every generated file carries a header comment saying it is generated. | Prevents humans editing generated files and losing work. |

---

## Abbreviations

| | |
|---|---|
| **B2** | Backblaze B2 Cloud Storage |
| **SSOT** | Single source of truth |
| **RTSP** | Real Time Streaming Protocol — how cameras are ingested |
| **VAAPI / QSV** | Video Acceleration API / Intel Quick Sync — hardware video decode |
| **TPU** | Tensor Processing Unit, e.g. Google Coral Edge TPU |
