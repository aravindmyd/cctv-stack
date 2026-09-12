# Runbook: Clip boundaries

> **Status:** 🚧 Planned.
> **Time:** 5 minutes to check, longer to remedy.

Clips that start too late, end too early, or have a jump in the middle. This runbook
covers the four distinct causes, how to tell them apart, and what each one means.

---

## The one-line version

A clip is a **cut** from Frigate's recordings, not a file Frigate stored. Its quality
depends on four things: what range we asked for, whether Frigate had flushed the
segments, whether any segments were missing, and what `-c copy` could actually cut at.

---

## 1. Check the clip's measured quality

Every clip records what was requested, what was obtained, and whether there are holes.

```bash
docker compose exec uploader curlie -s localhost:8082/admin/queue \
  | jq '.recent_clips[:5]'
```

```json
[
  { "clip_id": 8123, "camera": "front-door", "event_id": "1734567890.123456-abcxyz",
    "requested_start": 1757590360.12, "requested_end": 1757590395.65,
    "requested_s": 35.5, "actual_s": 35.2, "coverage_ratio": 0.99,
    "max_gap_s": 0.0, "has_gaps": false, "has_audio": true,
    "video_codec": "h264", "degraded": false, "fetch_attempts": 1 }
]
```

Or straight from the database:

```bash
docker compose exec clip-worker sqlite3 -readonly /data/state/state.db "
  SELECT clip_id, camera, datetime(start_time,'unixepoch') AS started,
         round(requested_s,1) AS asked, round(actual_s,1) AS got,
         round(coverage_ratio,2) AS cov, round(max_gap_s,1) AS gap,
         fetch_attempts AS tries, degraded
    FROM clips
   WHERE start_time > strftime('%s','now') - 86400
   ORDER BY start_time DESC LIMIT 20;"
```

| Reading | Meaning | Section |
|---|---|---|
| `coverage_ratio` ≥ 0.95 | Healthy | — |
| `coverage_ratio` 0.80–0.95 | Slightly short | §2 |
| `coverage_ratio` < 0.80 | **Truncated** | §2 |
| `max_gap_s` > 2.0 | **There is a hole** | §3 |
| `has_audio: false`, camera has a mic | Audio was dropped | §5 |
| `fetch_attempts` > 1 | Flush retries happened — normal, and informative | §2 |

---

## 2. The clip is too short (`coverage_ratio` < 0.95)

Three possible causes, in order of likelihood.

### Cause A — the flush race (most likely)

Frigate writes recording segments to cache and moves them to disk shortly after. If we
fetch before the final segment lands, the clip ends early. **Silently** — no error, just
a short clip.

The design waits (`clip.pre_roll_s`, `clip.post_roll_s`, `segment_flush_grace_s`) and
retries if the result is short. If you are still seeing truncation:

```bash
# How long is Frigate's recording segment?
docker compose exec frigate grep -ri 'segment' /config/config.yml || echo "using Frigate defaults"

# Current settings
grep -E 'post_roll|pre_roll|flush_grace' config/app.yml config/cameras.yml
```

**Fix: increase the grace period.**

```yaml
# config/app.yml
clip:
  segment_flush_grace_s: 15     # was 5 — Frigate's default segment is ~10 s
```

Then:

```bash
./update.sh --regenerate-config
docker compose restart clip-worker
```

Watch for the effect:

```bash
docker compose logs -f clip-worker | grep -E 'COVERAGE|FLUSH_RETRY'
```

**Signs this was the cause:** `fetch_attempts: 2` or `3` on the affected clips, and the
final attempt achieving better coverage than the first.

### Cause B — the recordings were already gone

Frigate's own retention removed the segments before we fetched. This happens when the
clip-worker was down, or when Frigate's recording retention is short.

```bash
docker compose exec status-collector curlie -s localhost:8084/health \
  | jq '.checks[] | select(.name=="retention_headroom")'
```

```json
{ "name": "retention_headroom", "state": "UNHEALTHY",
  "code": "RETENTION_HEADROOM_LOW",
  "detail": "oldest unmaterialised event started 71h ago; Frigate recordings expire in 1h 12m" }
```

**This is the serious one.** Once the recordings expire, the footage is gone permanently
and no retry can help.

**Fix:** increase `record.motion.days` in Frigate's generated config. That value is the
clip-worker's recovery budget — see [OQ-10](../plans/open-questions.md).

```bash
# config/cameras.yml — or set it globally
./update.sh --regenerate-config
```

**Signs this was the cause:** `EXPIRED_BEFORE_FETCH` in the logs, `retention_headroom`
degraded, or a burst of short clips after a period of downtime.

### Cause C — the requested range extends past what exists

Pre-roll reaching before the recordings begin, or post-roll reaching into the future.

**Fix:** reduce `pre_roll_s` / `post_roll_s`. Keep pre-roll modest anyway — the pin
protects the *event*, so footage *before* `start_time` is less well protected.

---

## 3. The clip has a hole (`max_gap_s` > 2.0)

A gap means a recording segment is missing between the start and end of the event —
usually the camera dropped offline mid-event, or a segment was expired.

```bash
docker compose exec uploader curlie -s localhost:8082/admin/queue \
  | jq '.recent_clips[] | select(.has_gaps) | {clip_id, camera, max_gap_s}'
```

**The clip is not repaired, and that is deliberate.** The footage in the gap no longer
exists anywhere; re-fetching, re-encoding, or trimming cannot restore it. The clip is
stored, flagged, and reported so you know the recording is incomplete rather than
believing it is whole.

**What to investigate is the camera, not the clip:**

```bash
docker compose logs frigate | grep -i 'offline\|restart'
docker compose exec frigate curlie -s localhost:5000/api/stats \
  | jq '.cameras | to_entries[] | {camera: .key, offline: .value.record.offline}'
```

Repeated gaps from one camera usually mean an unstable stream — Wi-Fi, power, or a
camera rebooting. Fix the camera.

---

## 4. The clip starts too late or ends too early (but `coverage` is fine)

Coverage measures length, not alignment. A clip can be exactly the right duration and
still be shifted.

Coverage is fine but the content is wrong, which means the **requested range** is wrong
relative to the event.

```bash
docker compose exec clip-worker sqlite3 -readonly /data/state/state.db "
  SELECT clip_id, camera,
         round(requested_start - start_time, 2) AS pre_roll_used,
         round(requested_end   - end_time,   2) AS post_roll_used
    FROM clips ORDER BY start_time DESC LIMIT 5;"
```

| Symptom | Fix |
|---|---|
| Action begins before the clip does | Increase `pre_roll_s` |
| Subject still in frame when the clip ends | Increase `post_roll_s` |
| Clip starts well before anything happens | Decrease `pre_roll_s` |

```yaml
# config/app.yml — global defaults
clip:
  pre_roll_s: 5
  post_roll_s: 10      # more post-roll than pre-roll is the common need

# config/cameras.yml — per camera, e.g. a driveway where cars are slow
cameras:
  driveway:
    clip:
      post_roll_s: 20
```

```bash
./update.sh --regenerate-config
docker compose restart clip-worker
```

**Alignment cannot be frame-exact.** Frigate cuts with a stream copy, which can only cut
at keyframes. Ask for 10 s of post-roll and you may get 11 or 12. That is intentional:
**we always err outward.** Overshoot is harmless; undershoot is lost evidence.

---

## 5. The clip has no audio

First, confirm the camera actually has a microphone:

```bash
docker compose exec frigate ffprobe -v error -show_streams -rtsp_transport tcp \
  "rtsp://...main stream..." | grep -E 'codec_type|codec_name'
```

If the camera has audio and the clip does not:

```bash
docker compose exec clip-worker sqlite3 -readonly /data/state/state.db "
  SELECT clip_id, has_audio, audio_codec FROM clips ORDER BY start_time DESC LIMIT 5;"
```

| `has_audio` | `audio_codec` | Meaning |
|---|---|---|
| `false` | null | Frigate did not include an audio stream |
| `true` | `aac` | Present, plays everywhere |
| `true` | `pcm_alaw`, `pcm_mulaw` | Present but **most browsers will not play it** |

We never transcode audio. If a camera emits G.711 and you need browser playback, that is
a camera configuration change (switch it to AAC) rather than something to fix in the
pipeline — re-encoding would degrade the evidence.

---

## 6. The clip will not play

| Symptom | Cause |
|---|---|
| Plays in VLC, not in a browser | H.265. Chrome 108+, Edge, and Safari handle it; Firefox and older browsers do not. Set the camera to H.264 |
| Will not play anywhere | Corruption. Re-fetch it — §7 |
| Plays but the duration is wrong in the player | Fragmented MP4 not remuxed. Check `remuxed: true` in the sidecar |

```bash
docker compose exec clip-worker ffprobe -v error -show_entries format=format_name,duration \
  /data/clips/front-door/2026/09/12/14-32-07.mp4
```

---

## 7. Re-fetching one clip

If a specific clip is corrupt or was cut short and Frigate still holds the recordings:

```bash
docker compose exec clip-worker curlie -X POST localhost:8081/admin/rematerialise \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
  -d '{"event_id":"1734567890.123456-abcxyz","reason":"clip was truncated; re-fetching with a longer flush grace"}'
```

The re-fetch uses the **current** config, so if you have just increased the grace period,
this is how you apply it to an existing clip. The deterministic path means it overwrites
in place — no duplicate, and upload state is unaffected if the clip was already uploaded.

Check whether the source still exists first:

```bash
docker compose exec clip-worker curl -sS -o /dev/null -w '%{http_code}\n' \
  "http://frigate:5000/api/front-door/start/1757590360/end/1757590395/clip.mp4"
```

`200` means it can be re-fetched. `400` with `"No recordings found"` means the footage has
expired — the clip cannot be recovered.

---

## 8. Auditing a whole period

Before and after a configuration change, measure the distribution rather than eyeballing
clips:

```bash
docker compose exec clip-worker sqlite3 -readonly /data/state/state.db "
  SELECT date(start_time,'unixepoch')            AS day,
         count(*)                                AS clips,
         round(avg(coverage_ratio), 3)           AS avg_coverage,
         min(round(coverage_ratio,3))            AS worst,
         sum(CASE WHEN coverage_ratio < 0.95 THEN 1 ELSE 0 END) AS short,
         sum(CASE WHEN has_gaps THEN 1 ELSE 0 END)              AS gapped,
         round(avg(fetch_attempts), 2)           AS avg_tries
    FROM clips
   WHERE start_time > strftime('%s','now') - 7*86400
   GROUP BY day ORDER BY day DESC;"
```

A healthy installation looks like:

```text
day         clips  avg_coverage  worst  short  gapped  avg_tries
2026-09-12     214         0.993  0.912      2       0       1.01
2026-09-11     198         0.996  0.947      0       0       1.00
```

What to look for:

| Pattern | Meaning |
|---|---|
| `avg_tries` > 1.2 | Flush retries are common → increase `segment_flush_grace_s` |
| `short` > 2 % of clips | Something systematic → §2 |
| A single day with many `short` | An outage, or a camera problem → §3 |
| `worst` < 0.5 | Investigate those specific clips individually |
| `gapped` > 0 | Camera instability → §3 |

---

## 9. Configuration reference

```yaml
# config/app.yml — defaults, overridable per camera in cameras.yml
clip:
  pre_roll_s: 5                  # footage before the event starts
  post_roll_s: 10                # footage after the event ends
  segment_flush_grace_s: 5       # wait for Frigate to flush the final segment
  max_flush_retries: 3           # re-fetch attempts when the result is short
  flush_retry_delay_s: 15        # delay between re-fetches
  coverage_ok: 0.95              # accept at or above this
  coverage_retry: 0.80           # below this, do not accept — retry
  max_gap_s: 2.0                 # packet delta above this counts as a hole
  gap_check_max_s: 300           # above this duration, sample rather than scan
  long_clip_warn_s: 1800         # long clips are stored in full, and flagged
  trim_to_range: false           # never on by default — see the ADR
  min_clip_bytes: 1024
  min_clip_duration_s: 1.0
```

Every value is explained in
[`lld/02`](../architecture/lld/02-clip-worker.md#9-configuration-b) and the reasoning
behind the whole approach is in
[ADR-0015](../adr/0015-clip-boundaries-and-padding.md).

**Do not set `trim_to_range: true` unless you have a specific reason.** It can only remove
footage, it cannot cut exactly where you asked, and the default exists to prevent exactly
that.
