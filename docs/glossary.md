# Glossary

Frigate uses words that look interchangeable and are not. These are the terms these pages
use, and the ones the stack uses in its configuration, health output and file names.

## Frigate's terms

| Term | Meaning |
|---|---|
| **Camera** | A named video source in Frigate. It has a main stream for recording and, usually, a lower-resolution sub stream for detection |
| **Detector** | The hardware or software that runs object detection: a Coral, a GPU, or the CPU |
| **Event** | One tracked object over time, for example a single person. It has an id, a camera, a label and a start and end time |
| **Review** | Frigate's grouping of nearby events into one period of activity, classified as `alert` or `detection`. This is what becomes a clip |
| **Cutoff time** | How long a subject may be absent before Frigate closes the review. Forty seconds for alerts and thirty for detections by default. This decides whether a person who leaves and returns produces one clip or two |
| **Recording** | Continuous video segments Frigate writes to `data/media/recordings/YYYY-MM-DD/HH/<camera>/`. Frigate builds those paths from its container timezone |
| **Snapshot** | A still image Frigate keeps for a detected object |
| **Zone** | A named polygon inside the camera's view, used to filter detections |

Frigate does not keep a video file per event. It records continuously and cuts a clip on
demand. Every design decision here follows from that.

## This stack's terms

| Term | Meaning |
|---|---|
| **Clip** | The file this stack produces: `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`, independent of Frigate's own retention |
| **Canonical path** | The deterministic location of a clip, derived from its camera and start time. The cloud object key mirrors it |
| **Sidecar** | The `.mp4.json` file beside each clip, recording what was requested, what was measured, and the codecs |
| **Materialise** | Turn a Frigate review into a clip file on disk |
| **Visit** | A review in human terms: one period of activity. One visit is one clip, unless the subjects are separated by more than the cutoff time |
| **Site** | One independent deployment at one physical location |
| **Location** | The site's name, from `site.location`. It appears in cloud object keys |
| **Pre-roll** | Footage included before the review starts, so the clip shows what led up to it. Five seconds by default |
| **Post-roll** | Footage included after the review ends, so the clip does not cut off while the subject is still there. Ten seconds by default |
| **Coverage ratio** | Clip duration divided by requested duration. Below 0.95 the clip is short; below 0.80 the attempt is rejected and retried |
| **Degraded** | A clip or service that is working with a known limitation, reported rather than hidden |
| **Upload state** | Where a clip sits in the backup pipeline. See below |
| **Held clip** | A clip past its retention date that has not uploaded, and therefore is not deleted |

## Upload states

| State | Meaning |
|---|---|
| `PENDING` | Waiting for an upload attempt |
| `UPLOADING` | Claimed by the uploader and in flight |
| `RETRY_WAIT` | Failed, waiting for its next attempt |
| `UPLOADED` | Stored and verified |
| `FAILED_PERMANENT` | Out of attempts. The local file is kept and reported |
| `SKIPPED_BY_POLICY` | The camera has `retention.upload: false`, so there is nothing to upload |
| `DELETED_LOCAL` | Uploaded, then removed from disk by retention |
| `FORCE_DELETED` | Removed by an operator despite never uploading. No command creates this today |

## Health states

| State | Meaning |
|---|---|
| `HEALTHY` | Working, and its dependencies are reachable |
| `DEGRADED` | Working, with something that needs attention |
| `UNHEALTHY` | Cannot do its job right now |
| `UNKNOWN` | No recent observation |
| `DISABLED` | Turned off by configuration. Not an error |

Camera states are Frigate's own: `ONLINE`, `OFFLINE`, `DISABLED`.

## Other terms

| | |
|---|---|
| **B2** | Backblaze B2 Cloud Storage |
| **RTSP** | Real Time Streaming Protocol, how cameras deliver video |
| **H.264 / H.265** | Video codecs. H.265 is smaller and less widely supported in browsers |
| **Go2rtc** | The restreamer inside Frigate. The live view plays its streams rather than the camera's |
| **SSOT** | Single source of truth, used here for `cameras.yml` |
