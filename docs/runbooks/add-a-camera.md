# Runbook: Add or change a camera

> **Status:** 🚧 Planned.
> **Time:** 5 minutes.

Adding a camera is a configuration change, not a code change. `config/cameras.yml` is the
only place cameras are defined; Frigate's own config is generated from it.

---

## Add a camera

### 1. Get the RTSP URLs

You need the **main** stream (for recording) and, if available, the **sub** stream (for
detection). Using a sub stream is the single biggest CPU saving available.

| Brand | Main | Sub |
|---|---|---|
| Hikvision | `/Streaming/Channels/101` | `/Streaming/Channels/102` |
| Dahua | `/cam/realmonitor?channel=1&subtype=0` | `…&subtype=1` |
| Reolink | `/h264Preview_01_main` | `/h264Preview_01_sub` |
| Axis | `/axis-media/media.amp` | `/axis-media/media.amp?resolution=640x360` |
| Amcrest | `/cam/realmonitor?channel=1&subtype=0` | `…&subtype=1` |

Test before you edit anything:

```bash
docker compose exec frigate ffprobe -v error -rtsp_transport tcp \
  "rtsp://user:pass@192.168.1.50:554/Streaming/Channels/102"
```

If this produces no output, the URL or credentials are wrong. Do not proceed until it
works — debugging it here takes a minute; debugging it after a failed config apply takes
considerably longer.

### 2. Put the credentials in `.env`

```bash
# .env
GARAGE_USER=admin
GARAGE_PASSWORD=...
```

```env
# config/cameras.yml
cameras:
  garage:
    display_name: "Garage"
    rtsp:
      main: "rtsp://${GARAGE_USER}:${GARAGE_PASSWORD}@192.168.1.51:554/Streaming/Channels/101"
      sub:  "rtsp://${GARAGE_USER}:${GARAGE_PASSWORD}@192.168.1.51:554/Streaming/Channels/102"
    detect:
      objects: [person, car]
```

Never put a real password directly in `cameras.yml` if you might share that file. Both
files are `chmod 600`, but `.env` is the file people expect to hold secrets, and
`${VAR}` references keep it that way.

### 3. Name it correctly

Camera names become **directory names** and **cloud object keys**:

| Rule | Example |
|---|---|
| ✅ lowercase, hyphens | `front-door`, `garage-2`, `backyard` |
| ❌ uppercase | `FrontDoor` |
| ❌ spaces, underscores, dots | `front door`, `front_door`, `front.door` |
| ❌ reserved | `all`, `none`, `review`, `latest`, `tmp` |

The installer and validator enforce this. The rule exists because these names travel
across filesystems and object stores, and a name that works on Linux and breaks on a
restore to macOS is a bug that only appears at the worst possible moment.

### 4. Preview, then apply

```bash
./update.sh --regenerate-config --dry-run    # show what would change
./update.sh --regenerate-config              # apply
```

The dry run prints the diff to the generated Frigate config and the validation result.
Read the diff — it is the last chance to catch a typo cheaply.

### 5. Verify

```bash
./scripts/doctor.sh                    # the new camera appears and is online
docker compose logs -f clip-worker     # watch for the first event
```

The status page at `http://<host>:8080/status` should show the camera. If clips are not
appearing after a few minutes, check whether the camera is producing events at all in
Frigate's own UI — a camera can be perfectly online and simply have nothing happening in
front of it.

---

## Change a camera

| Change | How | Notes |
|---|---|---|
| Display name | Edit `display_name` | Cosmetic only; paths are unaffected |
| Credentials | Edit `.env`, then `./update.sh --regenerate-config` | |
| RTSP URLs | Edit `cameras.yml`, then apply | The camera key must stay the same to keep history |
| Detection objects | Edit `detect.objects` | More objects means more CPU |
| Detection resolution | Edit `detect.width`/`height` | Even numbers ≥ 64. Lower = faster |
| Detection rate | Edit `detect.fps` | 3–5 is usually plenty |
| Retention | Edit `retention.local_days` / `cloud_days` | Applies on the next cleanup run |
| Recording mode | Edit `record.mode` | **Watch your disk.** See below |
| Turn detection off | `detect.enabled: false` | Camera still records; no object detection |

### Changing `record.mode` — read this first

| Mode | Storage per camera |
|---|---|
| `motion` (default) | ~3 days of motion footage |
| `alerts` | Footage around alerts only |
| `continuous` | **~21 GB/day, ~1.9 TB over 90 days** |

Continuous recording is roughly 30× the storage of clip retention. Check
`./scripts/doctor.sh` for projected days to full before enabling it.

### Don't lower Frigate's recording window casually

The generated `motion.days` is **not** arbitrary — it is the clip-worker's recovery
budget if the pipeline is ever down. If the recording window is shorter than a pipeline
outage, clips that were not yet copied are **gone permanently**.

Validation warns below 2 days. Heed it.

---

## Remove a camera

```bash
# Set enabled: false to keep the config for reference, or delete the block entirely
```

```yaml
  old-garage:
    enabled: false
```

Then:

```bash
./update.sh --regenerate-config
```

**Nothing is deleted.** This is deliberate:

- Existing clips stay on disk and follow normal retention
- Rows stay in the database, marked `retired_at`
- Cloud objects stay until their normal expiry
- Re-adding a camera with the same name restores its history

To actually delete a retired camera's footage, wait for retention to expire it, or use
the explicit `force-delete` admin action per clip. There is no bulk delete, on purpose.

---

## Renaming a camera

A rename is **a new camera**, not a move. Existing clips stay under the old name, because
that is where their paths and cloud keys point.

If you want the history to follow:

1. Add the camera under the new name
2. Verify it works
3. Disable the old one
4. `./update.sh --regenerate-config`

The old name's clips remain under the old directory until retention removes them. If you
need them under the new name, move the files **and** update `clips.path` and
`cloud_key` — which is a deliberate, manual operation, not a supported one.

---

## Troubleshooting the new camera

| Symptom | Likely cause |
|---|---|
| `offline` in the status page | Wrong URL, wrong credentials, or the camera's connection limit is reached |
| Online but no clips | Detection is off, or nothing matching `detect.objects` has appeared |
| Online but detection is slow | `DETECTION_FALLING_BEHIND` — lower resolution, lower fps, or fewer cameras |
| No sub stream available | Detection runs on the main stream, which is much more expensive. Set `detect.width`/`height` lower to compensate |
| H.265 stream | Frigate supports it, but some browsers cannot play it. Prefer H.264 if the camera offers it |
| Clips appear but never upload | See [`recover-failed-uploads.md`](recover-failed-uploads.md) |

```bash
docker compose exec frigate ffprobe -v error -rtsp_transport tcp "<url>"
./scripts/doctor.sh
docker compose logs --tail=100 clip-worker
```

---

## Reference

```yaml
cameras:
  <name>:                               # lowercase-kebab-case, max 32 chars
    display_name: "Human readable"      # optional
    enabled: true                       # optional, default true
    rtsp:
      main: "rtsp://..."                # required
      sub:  "rtsp://..."                # optional but strongly recommended
    detect:
      enabled: true                     # optional, default true
      objects: [person, car]            # replaces the default list, does not merge
      width: 1280                       # even, >= 64
      height: 720                       # even, >= 64
      fps: 5                            # 1-15
      min_score: 0.4
    record:
      mode: motion                      # motion | continuous | alerts | detections | off
    retention:
      local_days: 90                    # our canonical clips
      cloud_days: 60
      upload: true                      # false = local only, no alerts
    notify:
      enabled: true
      labels: [person]                  # must be a subset of detect.objects
```

Every field, with its validation rules and defaults, is documented in
[`config/cameras.example.yml`](https://github.com/aravindmyd/cctv-stack/blob/master/config/cameras.example.yml).
