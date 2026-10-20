# Configure

Two files, and the split between them is worth keeping:

| File | Holds | Contains secrets |
|---|---|---|
| `config/cameras.yml` | Site name, cameras, detection, clip padding, retention | No |
| `.env` | RTSP URLs, B2 keys, paths, ports | Yes |

`cameras.yml` is the only place cameras are defined. Frigate's own configuration is
generated from it, and anything you edit in `config/frigate/config.yml` is overwritten the
next time the stack starts.

You do not have to write either file by hand. The install interview does it, with the same
validation the stack uses:

```bash
docker compose run --rm --no-deps init cctv-init
```

It asks for the location, timezone, each camera's name and RTSP URLs, and optional B2
credentials, then writes both files — `.env` as `0600`, secrets never echoed. It will not
replace a `cameras.yml` you have edited unless you pass `--force`, so it is safe to re-run
to change the site name or add the cloud bucket. Everything below is for the cases the
interview does not cover.

## A minimal `cameras.yml`

```yaml
site:
  location: home
  timezone: Asia/Kolkata

cameras:
  front-door:
    display_name: Front Door
    rtsp:
      main: ${CAMERA_FRONT_DOOR_RTSP}
      sub: ${CAMERA_FRONT_DOOR_SUB}
```

`${VAR}` is read from the environment at load time. A variable that is not set is an
error, not an empty string, so a typo fails before Frigate starts.

Unknown keys are rejected. A misspelled `retention:` block fails validation instead of
being silently ignored.

## `site`

| Key | Required | Meaning |
|---|---|---|
| `location` | yes | Site name. Lowercase, digits and hyphens, 1–32 characters. Appears in cloud object keys |
| `timezone` | yes | IANA timezone used to render clip directory and file names |
| `display_timezone` | no | Defaults to `timezone`. Set it when you want clips named in a zone other than the site's |

Camera names follow the same character rule: lowercase letters, digits, and hyphens,
starting and ending with a letter or digit. `front-door` and `gate2` are valid;
`FrontDoor`, `front_door`, and `front.door` are not. These names become directory names
and cloud keys, so the rule travels with the data.

## `defaults`

Values under `defaults` apply to every camera. A camera can override any block, and nested
mappings merge while lists replace. Omitting a camera key means "inherit".

```yaml
defaults:
  detect:
    objects: [person]
    width: 1280
    height: 720
    fps: 5
    min_score: 0.4
  clip:
    pre_roll_s: 5
    post_roll_s: 10
  record:
    mode: motion
  retention:
    local_days: 90
    cloud_days: 60
    upload: true
```

### `detect`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Object detection on or off. Recordings continue either way |
| `objects` | `[person]` | Labels to detect and track. Replaces the list; it does not merge |
| `width`, `height` | `1280`, `720` | Resolution Frigate analyses. Lower costs less CPU and loses small objects |
| `fps` | `5` | Frames analysed per second. Three to five is usually enough |
| `min_score` | `0.4` | Detection confidence floor |
| `min_initialized` | `2` | Frames an object must be seen in before it counts |

### `clip`

| Key | Default | Meaning |
|---|---|---|
| `pre_roll_s` | `5` | Seconds of footage included before the review starts |
| `post_roll_s` | `10` | Seconds included after it ends |
| `visit_gap_s` | Frigate's own (40 s alerts, 30 s detections) | How long a subject may be absent before the visit ends. Changing it changes what counts as one clip |
| `severities` | `[alert, detection]` | Which review severities become clips |
| `objects` | `[person]` | Which object labels make a review worth clipping |

### `record`

Frigate's own recording retention, which is also the clip worker's recovery budget. If the
worker is down longer than this window, the footage it needs has already been deleted and
no retry can recover it.

| Mode | Frigate keeps |
|---|---|
| `motion` (default) | 3 days of motion footage |
| `continuous` | 7 days of continuous footage |
| `alerts` | Footage around alerts, 30 days |
| `detections` | Footage around detections, 30 days |
| `off` | Nothing |

`min_headroom_days` (top level, default `2`) is enforced: a mode that leaves less than two
days of recording is refused unless you raise the value. The default warning is there
because a shorter window than your worst outage means permanent loss.

### `retention`

| Key | Default | Meaning |
|---|---|---|
| `local_days` | `90` | How long a clip stays on disk after it is uploaded |
| `cloud_days` | `60` | Recorded in the database. Cloud deletion is not implemented yet; use a B2 lifecycle rule on the bucket prefix if you want objects to expire |
| `upload` | `true` | Set to `false` to keep a camera local-only. Its clips are marked `SKIPPED_BY_POLICY` and become eligible for local retention without an upload |

### `snapshots`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Let Frigate keep a still for each detected object |
| `retain_days` | `30` | How long Frigate keeps them |

## `cameras`

```yaml
cameras:
  front-door:
    display_name: Front Door
    enabled: true
    rtsp:
      main: ${CAMERA_FRONT_DOOR_RTSP}
      sub: ${CAMERA_FRONT_DOOR_SUB}
    detect:
      objects: [person, car]
    clip:
      post_roll_s: 20
```

The sub stream is optional and worth using. Detection runs on it, which is far cheaper
than decoding the main stream, and every IP camera provides one. Without a sub stream the
main stream handles both roles.

## Adding a camera

1. Put the credentials in `.env`, using a name that matches the camera:

   ```ini
   CAMERA_GARAGE_RTSP=rtsp://user:password@192.168.1.51:554/Streaming/Channels/101
   CAMERA_GARAGE_SUB=rtsp://user:password@192.168.1.51:554/Streaming/Channels/102
   ```

2. Add the camera block to `config/cameras.yml`:

   ```yaml
   cameras:
     garage:
       display_name: Garage
       rtsp:
         main: ${CAMERA_GARAGE_RTSP}
         sub: ${CAMERA_GARAGE_SUB}
   ```

3. Test the URL before applying anything. This runs `ffprobe` inside the Frigate container,
   which already has the network access the cameras need:

   ```bash
   docker compose exec -T frigate ffprobe -v error -rtsp_transport tcp \
     "$CAMERA_GARAGE_SUB"
   ```

   No output means the URL or the credentials are wrong.

4. Validate and apply:

   ```bash
   docker compose run --rm --no-deps config \
     cctv-config validate --cameras /config/cameras.yml
   docker compose up -d
   ```

   `docker compose up -d` re-runs the one-shot `config` service, which rewrites
   `config/frigate/config.yml`, and recreates Frigate if the file changed.

5. Confirm the camera in Frigate's UI at <https://127.0.0.1:8971>. A camera that is online
   but produces no clips is usually a detection or object-label mismatch, not a broken
   stream.

Typical stream paths:

| Brand | Main | Sub |
|---|---|---|
| Hikvision | `/Streaming/Channels/101` | `/Streaming/Channels/102` |
| Dahua, Amcrest | `/cam/realmonitor?channel=1&subtype=0` | `…&subtype=1` |
| Reolink | `/h264Preview_01_main` | `/h264Preview_01_sub` |
| Axis | `/axis-media/media.amp` | `/axis-media/media.amp?resolution=640x360` |

## Changing a camera

| Change | Effect |
|---|---|
| `display_name` | Cosmetic; paths are unchanged |
| RTSP URLs or credentials | Apply as above. Keep the camera key, or the existing clips stop matching it |
| `detect.*` | Restart Frigate to pick up the generated change |
| `clip.pre_roll_s`, `post_roll_s` | Applies to new clips; the clip worker reads the config on start, so restart it |
| `retention.local_days` | Future cleanup runs use the new value; nothing is deleted early |
| `record.mode` | Watch the disk. Continuous recording is roughly thirty times the storage of clips |

## Removing a camera

Set `enabled: false` rather than deleting the block if you may bring it back.

```yaml
cameras:
  old-garage:
    enabled: false
```

Then apply with `docker compose up -d`.

Nothing is deleted. Clips stay on disk and follow normal retention, database rows stay
(marked retired), and cloud objects stay until their own expiry. Re-enabling the camera
restores its history, because the name is the link.

## Regenerating Frigate's config

Any change to `cameras.yml` reaches Frigate through the `config` service:

```bash
docker compose run --rm --no-deps config \
  cctv-config generate --cameras /config/cameras.yml --out /frigate/config.yml
docker compose restart frigate
```

`docker compose up -d` does the same thing and is usually what you want, because it also
recreates any container whose configuration changed.

The generated file contains camera credentials, so it is written with mode `600` and is
excluded from version control.

## Environment variables

Set these in `.env`. Defaults are what the services use when the variable is absent.

### Paths and connectivity

| Variable | Default | Meaning |
|---|---|---|
| `CCTV_IMAGE` | `cctv-stack:local` | Image used by Compose. Set the published tag on a released install |
| `CCTV_DATA_DIR` | `./data` | Host directory mounted at `/data` |
| `CCTV_CONFIG_DIR` | `/config` | Directory containing `cameras.yml` inside the container |
| `CCTV_CAMERAS` | `$CCTV_CONFIG_DIR/cameras.yml` | Override the config file path |
| `CCTV_DB` | `$CCTV_DATA_DIR/cctv.db` | SQLite state database |
| `CCTV_FRIGATE_URL` | `http://frigate:5000` | Frigate API base URL |
| `CCTV_MQTT_HOST` | unset | Unset means sweep-only. Set to `mosquitto` in Compose |
| `CCTV_MQTT_PORT` | `1883` | Broker port |
| `CCTV_MQTT_USERNAME`, `CCTV_MQTT_PASSWORD` | unset | Only needed for an authenticated broker |
| `CCTV_SWEEP_INTERVAL_S` | `60` | How often the worker asks Frigate for reviews it may have missed |
| `CCTV_LOG_LEVEL` | `INFO` | Log verbosity |

### Cloud backup

| Variable | Default | Meaning |
|---|---|---|
| `CCTV_STORAGE_BUCKET` | empty | Empty means `DISABLED`. A bucket with no key exits 78 |
| `CCTV_STORAGE_KEY_ID` | empty | B2 application key ID |
| `CCTV_STORAGE_APPLICATION_KEY` | empty | B2 application key |
| `CCTV_STORAGE_REGION` | `us-west-004` | Must match the bucket |
| `CCTV_STORAGE_ENDPOINT_URL` | derived from the region | Only for a non-standard endpoint |
| `CCTV_UPLOAD_CONCURRENCY` | `4` | Parallel uploads |
| `CCTV_UPLOAD_MAX_ATTEMPTS` | `12` | Retries before a clip becomes `FAILED_PERMANENT` |
| `CCTV_UPLOAD_BACKOFF_BASE_S`, `CCTV_UPLOAD_BACKOFF_MAX_S` | `15`, `3600` | Retry spacing |
| `DRY_RUN` | `0` | Log intended actions without uploading or deleting |

`B2_BUCKET`, `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_REGION`, and `B2_ENDPOINT_URL` are
accepted as aliases.

### Health and retention

| Variable | Default | Meaning |
|---|---|---|
| `UPLOADER_HEALTH_PORT` | `8082` | Host port for the uploader, bound to loopback |
| `CLEANUP_HEALTH_PORT` | `8083` | Host port for cleanup, bound to loopback |
| `CCTV_HEALTH_HOST` | `0.0.0.0` | Bind address inside the container |
| `INTERNAL_API_TOKEN` | empty | Enables `/admin/queue` and `/admin/retry` on the uploader |
| `CCTV_CLEANUP_RUN_INTERVAL_S` | `3600` | Local retention cadence |
| `CCTV_CLEANUP_BATCH_SIZE` | `200` | Clips per transaction |
| `CCTV_CLEANUP_RETIRE_GRACE_DAYS` | `0` | Extra retention for a camera removed from the config |
| `CCTV_CLEANUP_OVERDUE_WARN` | `1` | Held clips before cleanup reports `DEGRADED` |
| `CCTV_CLEANUP_OVERDUE_CRITICAL` | `10` | Held clips before it reports `UNHEALTHY` |
| `CCTV_CLEANUP_OVERDUE_AGE_CRITICAL_DAYS` | `30` | Age of the oldest held clip that counts as critical |
| `CCTV_HEALTH_DISK_WARN_PCT`, `CCTV_HEALTH_DISK_CRITICAL_PCT` | `15`, `5` | Free-space thresholds |

## Frigate's login

The generated Frigate config has its web login off unless you turn it on in `cameras.yml`:

```yaml
frigate:
  auth:
    enabled: true
```

See [Frigate UI login](frigate-ui.md) for the password, the reset procedure, and why the
pipeline keeps working either way.
