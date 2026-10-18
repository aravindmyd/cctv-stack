# Clip boundaries

A clip starts too late, ends too early, or sounds wrong. This page explains how a clip is
built, what the stack records about it, and how to tell the four causes apart.

## How a clip is made

Frigate does not store event videos. It records continuous segments and cuts a clip on
demand. cctv-stack asks for a range and checks what came back:

1. The range is `review.start_time - pre_roll_s` to `review.end_time + post_roll_s`.
2. The fetch waits for Frigate to flush its last recording segment, then asks for the range.
3. The result is measured. Duration compared with the request gives `coverage_ratio`.
4. If coverage is between 0.80 and 0.95, the clip is kept, flagged as `degraded`, and the
   worker tries again up to three times. Below 0.80 the attempt is not accepted.
5. The accepted file is written to the canonical path with a JSON sidecar.

The cut is a stream copy, so it can only split at keyframes. Ask for ten seconds of
post-roll and you may get eleven. The default errs outward on purpose: overshoot costs disk
space, while a clip that ends early loses evidence.

## Where the evidence is

Every clip has a sidecar beside it with the request, the measurement, and the codecs:

```bash
cat data/clips/front-door/2026/09/12/14-32-07.mp4.json
```

```json
{
  "requested_start_ts": 1757590360.12,
  "requested_end_ts": 1757590395.65,
  "requested_s": 35.53,
  "duration_s": 35.2,
  "coverage_ratio": 0.991,
  "max_gap_s": 2.31,
  "has_gaps": false,
  "has_audio": true,
  "video_codec": "h264",
  "audio_codec": "aac",
  "post_roll_s": 10.0,
  "degraded": false,
  "long_clip": false
}
```

The same values are in the `clips` table, which is easier to analyse across many clips:

```bash
docker compose exec -T clip-worker python - <<'PY'
import sqlite3
db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT id, camera, round(requested_s,1) AS asked, round(duration_s,1) AS got, "
    "       round(coverage_ratio,3) AS coverage, round(max_gap_s,2) AS gap, "
    "       fetch_attempts, degraded, has_audio, video_codec "
    "FROM clips ORDER BY start_time DESC LIMIT 20"
)
for row in rows:
    print(dict(row))
PY
```

## What healthy looks like

| Reading | Meaning |
|---|---|
| `coverage_ratio` at or above 0.95 | The clip is the length that was asked for |
| `coverage_ratio` between 0.80 and 0.95 | Kept and flagged `degraded`; worth investigating if it repeats |
| `coverage_ratio` below 0.80 | Rejected and retried. Clips that survive repeatedly are suspicious |
| `fetch_attempts` of 1 | First attempt accepted |
| `fetch_attempts` above 1 | A flush race happened; normal occasionally |
| `degraded: true` | The clip was accepted below the good threshold |

Ignore `max_gap_s` when judging quality. A clip cut with a stream copy has a packet gap
roughly the size of the camera's keyframe interval, so a two-second gap appears on perfectly
healthy clips. The field is recorded for reference, and `has_gaps` is always false because
the detector behind it cannot tell a keyframe gap from a missing segment. Truncation is
detected by coverage.

## The clip is too short

### Frigate had not flushed the final segment

This is the common case. Frigate writes segments to cache and moves them to disk a moment
later. The worker waits fifteen seconds after the review ends before asking, and retries if
the result is short, which covers it on most hardware.

If short clips cluster at a predictable time of day, check the disk and the camera first.
There is no setting for the flush grace; it is a constant in the worker. If you are seeing
`fetch_attempts` above 1 on most clips, the machine is slow enough that the wait is
marginal, and a faster disk is the fix.

### The recordings had already expired

The clip worker's recovery window is Frigate's `record` retention. If the worker was down
for longer than that, the footage is gone and no retry brings it back.

```bash
docker compose logs --tail=200 clip-worker | grep -iE 'expired|missing'
```

Increase the retention for that mode in `cameras.yml` (`record.mode`), and remember that a
shorter window than your worst outage means permanent loss. Validation refuses a mode that
leaves less than two days unless you raise `min_headroom_days` deliberately.

### The range reaches past the available footage

Pre-roll can point before the recordings start, especially on a fresh install or after a
Frigate restart. Reduce `pre_roll_s` / `post_roll_s` for that camera.

## The clip is the right length but starts or ends wrong

Coverage measures duration, not alignment. A clip can be exactly as long as requested and
still begin after the action.

| Symptom | Change |
|---|---|
| The action begins before the clip does | Increase `clip.pre_roll_s` |
| The subject is still in frame when the clip ends | Increase `clip.post_roll_s` |
| The clip starts long before anything happens | Decrease `clip.pre_roll_s` |

```yaml
cameras:
  driveway:
    clip:
      pre_roll_s: 8
      post_roll_s: 20
```

Restart the clip worker after the change; it reads the config at start.

Frigate's visit threshold also moves the boundary. `visit_gap_s` decides how long a subject
can be absent before the visit ends, which starts a new clip. Setting it higher merges
nearby activity into one file; setting it lower splits a long visit into several.

## The clip has no audio

```bash
docker compose exec -T frigate ffprobe -v error -show_streams -rtsp_transport tcp \
  "rtsp://camera-url-here" | grep -E 'codec_type|codec_name'
```

If the camera carries audio and the clip does not, check the recorded codec:

| `has_audio` | `audio_codec` | Meaning |
|---|---|---|
| false | null | Frigate's generated config does not ask for audio, or the stream has none |
| true | `aac` | Plays in every browser |
| true | `pcm_alaw`, `pcm_mulaw` | Present, but most browsers cannot play it |

Audio is never transcoded. If a camera sends G.711 and you need browser playback, change
the camera's audio encoding to AAC.

## The clip will not play

| Symptom | Cause |
|---|---|
| Plays in VLC, not in a browser | H.265. Some browsers and platforms cannot decode it; set the camera to H.264 |
| Will not play anywhere | Corruption. Check the file with `ffprobe` |
| Duration looks wrong in a player | Fragmented MP4 metadata. The file is still valid; remux it if it matters |

```bash
docker compose exec -T clip-worker ffprobe -v error \
  -show_entries format=format_name,duration \
  /data/clips/front-door/2026/09/12/14-32-07.mp4
```

## Auditing a week at a time

A single bad clip is noise. A distribution tells you whether a change helped.

```bash
docker compose exec -T clip-worker python - <<'PY'
import sqlite3
db = sqlite3.connect("/data/cctv.db")
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT date(start_time, 'unixepoch', 'localtime') AS day, "
    "       count(*) AS clips, "
    "       round(avg(coverage_ratio), 3) AS avg_coverage, "
    "       round(min(coverage_ratio), 3) AS worst, "
    "       sum(CASE WHEN coverage_ratio < 0.95 THEN 1 ELSE 0 END) AS short, "
    "       round(avg(fetch_attempts), 2) AS avg_tries "
    "FROM clips WHERE start_time > strftime('%s','now') - 7*86400 "
    "GROUP BY day ORDER BY day DESC"
)
for row in rows:
    print(dict(row))
PY
```

| Pattern | Likely cause |
|---|---|
| `avg_tries` above 1.2 | The flush wait is marginal on this machine |
| More than a couple of `short` clips a day | Something systematic; check the camera and the disk |
| One day with many shots | An outage, or a camera problem that day |
| `avg_coverage` drifting down | Retention is expiring footage before it is fetched |

## Settings that affect boundaries

In `cameras.yml`, per camera or under `defaults`:

| Key | Default | Effect |
|---|---|---|
| `clip.pre_roll_s` | 5 | Seconds before the review |
| `clip.post_roll_s` | 10 | Seconds after it |
| `clip.visit_gap_s` | Frigate's defaults | When one visit ends and the next begins |
| `clip.severities` | `[alert, detection]` | Which reviews become clips |
| `record.mode` | `motion` | The recovery window, and therefore what can still be fetched |

Fixed in the worker, not configurable:

| Value | Default |
|---|---|
| Wait before fetching, after the review ends | 15 seconds |
| Fetch attempts before giving up | 3 |
| Coverage at or above which a clip is good | 0.95 |
| Coverage below which an attempt is rejected | 0.80 |
| Reviews longer than this are flagged, never split | 600 seconds |

There is no re-fetch command. The worker skips a review that already has a clip row, so
re-materialising one means removing that row and its file and letting the sweep offer the
review again, which only works while Frigate still holds the recording. Treat that as a
manual repair, not a routine step.
