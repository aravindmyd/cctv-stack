# Install

Two ways to run cctv-stack. Use Docker Compose unless you already run Frigate yourself and
only want the additional services.

## Requirements

| | |
|---|---|
| Host | A machine that stays on: a mini-PC, a NAS, a Raspberry Pi 5 |
| Docker | Engine 24 or newer with the Compose v2 plugin |
| Storage | An SSD sized for your clip retention. Avoid SD cards |
| Cameras | RTSP URLs, including credentials. ONVIF discovery is not used |
| Cloud backup | Optional. A Backblaze B2 bucket and an application key |
| Network | Outbound access for B2. No inbound access is required |

## Docker Compose

The released image is public, so nothing in this guide needs the source repository.
Compose needs three files: `compose.yml`, an environment file, and `cameras.yml`.

```bash
mkdir cctv-stack && cd cctv-stack
mkdir -p config

curl -fsSLO https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/compose.yml
curl -fsSL https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/.env.example -o .env
curl -fsSL https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/config/cameras.example.yml -o config/cameras.yml

chmod 600 .env
```

### 1. Edit `.env`

The file is commented; the values you must set are:

```ini
# The published image. Pin the release you are installing; `edge` follows the default
# branch and is not a stable target.
CCTV_IMAGE=ghcr.io/aravindmyd/cctv-stack:0.3.1

# RTSP URLs. Replace the placeholder user:password and host.
CAMERA_FRONT_DOOR_RTSP=rtsp://user:password@192.168.1.10:554/Streaming/Channels/101
CAMERA_FRONT_DOOR_SUB=rtsp://user:password@192.168.1.10:554/Streaming/Channels/102

# Must match `site.timezone` in config/cameras.yml.
TZ=Asia/Kolkata
```

Leave `CCTV_STORAGE_BUCKET` empty for a local-only install. The uploader will run and
report `DISABLED`; nothing is uploaded and nothing is hidden.

!!! note "Image tags"
    `0.3.1` is the current release and matches the configuration shown here. `:0.3` follows
    the newest 0.3.x patch, and `:edge` is rebuilt on every merge to `master` — useful for
    testing a change, not for a site you rely on.

### 2. Edit `config/cameras.yml`

Set the site name and timezone, and make sure each camera references the variable names
you used in `.env`.

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

Every key is documented in [Configure](configure.md), and the example file carries a
comment on each one.

### 3. Validate before starting

Validation runs in the same image that generates Frigate's config, so "valid" means the
same thing here and at start-up.

```bash
docker compose pull
docker compose run --rm --no-deps config \
  cctv-config validate --cameras /config/cameras.yml
```

A valid file prints `ok: 1 camera(s) enabled: front-door`. An invalid one prints the
reason and exits 78 (`EX_CONFIG`). Fix it before going further; a bad camera URL that
reaches Frigate produces a camera that looks configured and never connects.

### 4. Start

```bash
docker compose up -d
```

What happens, in order:

1. The one-shot `config` service writes `config/frigate/config.yml` from
   `config/cameras.yml` and exits. Frigate waits for it.
2. Frigate starts and begins recording. The first start pulls its image, which is a large
   download.
3. `clip-worker`, `uploader`, and `cleanup` connect to the shared SQLite database and run
   their first pass.

Frigate's UI is at <https://127.0.0.1:8971>. It is HTTPS with a self-signed certificate,
so the browser warns once; plain `http://` on that port returns `400`.

### 5. Check that it is running

```bash
docker compose ps
docker compose logs --tail=50 config     # should end with "wrote /frigate/config.yml"
docker compose logs --tail=50 frigate    # "Starting Frigate", then camera status
curl -s http://127.0.0.1:8082/health     # uploader
curl -s http://127.0.0.1:8083/health     # cleanup
```

The uploader reports `DISABLED` when no B2 bucket is configured. That is a supported
state, not an error. See [Health](health.md) for the full report format.

### Where your data lives

```
cctv-stack/
├── .env
├── compose.yml
├── config/
│   ├── cameras.yml
│   └── frigate/config.yml     # generated; contains camera credentials
└── data/                      # $CCTV_DATA_DIR, default ./data
    ├── clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
    │   <camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
    ├── cctv.db                # upload and retention state
    ├── media/                 # Frigate's own recordings and snapshots
    └── mosquitto/
```

Point `CCTV_DATA_DIR` at another path if `./data` is not where you keep data. It is the
only directory that must survive a container rebuild.

## pip

Use this when Frigate already runs on the host and you want the four services from PyPI.
The package installs four commands: `cctv-config`, `cctv-clip-worker`, `cctv-uploader`,
and `cctv-cleanup`.

```bash
python -m pip install "cctv-stack[mqtt,storage]"
```

`mqtt` adds the MQTT client used by the clip worker, and `storage` adds the S3 client used
by the uploader. Both are optional so a deployment that does not need them does not
install them.

`ffprobe` must be on `PATH`. A pip install does not provide it; install `ffmpeg` from your
distribution first.

```bash
export CCTV_CONFIG_DIR=/etc/cctv
export CCTV_DATA_DIR=/var/lib/cctv
export CCTV_DB=/var/lib/cctv/cctv.db
export CCTV_FRIGATE_URL=http://127.0.0.1:5000
export CCTV_MQTT_HOST=127.0.0.1          # omit for sweep-only operation

cctv-config validate --cameras "$CCTV_CONFIG_DIR/cameras.yml"
cctv-config generate --cameras "$CCTV_CONFIG_DIR/cameras.yml" --out /etc/frigate/config.yml

cctv-clip-worker
cctv-uploader
cctv-cleanup
```

Each service is long-running and takes no command-line arguments; everything is
environment configuration. Run them under systemd, a process supervisor, or a container
of your own. `cctv-clip-worker` works without MQTT: it polls Frigate for reviews, so a
missed message costs time rather than a clip.

Frigate's internal port `5000` must be reachable from these processes. It must not be
published to your LAN: Frigate treats requests on that port as anonymous admin.

## Upgrading

Compose:

```bash
# change CCTV_IMAGE in .env, then
docker compose pull
docker compose up -d
```

pip:

```bash
python -m pip install --upgrade cctv-stack
```

Database migrations run automatically when a service starts. They are forward-only:
downgrading the image after a schema migration is not supported, so keep the previous
`CCTV_IMAGE` value if you may need to roll back.

## Removing the stack

```bash
docker compose down
```

Containers stop and are removed. `data/`, `config/`, and `.env` are left in place, so
starting the stack again resumes where it left off.

To remove everything, delete those three paths and decide what happens to the B2 bucket
separately. If you leave a bucket behind, set a lifecycle rule or delete it; a bucket that
no longer belongs to a running site is easy to forget and keeps billing.

## Troubleshooting the first start

| Symptom | Cause |
|---|---|
| `config` exits 78; Frigate never starts | `cameras.yml` failed validation. Run the validate command above; the message names the key |
| `pull access denied` for cctv-stack | `CCTV_IMAGE` is still `cctv-stack:local`, or the tag does not exist |
| Uploader exits with code 78 | `CCTV_STORAGE_BUCKET` is set but a key is missing. Set the credentials, or unset the bucket for local-only |
| Frigate restarts in a loop | Its log names the rejected config key. The generated file is overwritten on the next `docker compose up -d` |
| `http://127.0.0.1:8971` returns 400 | The port speaks HTTPS only. Use `https://` |
| The UI is unreachable from another machine | `8971` is bound to loopback on purpose. Use an SSH tunnel or a reverse proxy |
| Health says `DISABLED` | No storage bucket is configured. Expected for a local-only install |
