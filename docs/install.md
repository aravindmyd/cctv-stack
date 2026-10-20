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
CCTV_IMAGE=ghcr.io/aravindmyd/cctv-stack:0.5.1

# RTSP URLs. Replace the placeholder user:password and host.
CAMERA_FRONT_DOOR_RTSP=rtsp://user:password@192.168.1.10:554/Streaming/Channels/101
CAMERA_FRONT_DOOR_SUB=rtsp://user:password@192.168.1.10:554/Streaming/Channels/102

# Must match `site.timezone` in config/cameras.yml.
TZ=Asia/Kolkata
```

Leave `CCTV_STORAGE_BUCKET` empty for a local-only install. The uploader will run and
report `DISABLED`; nothing is uploaded and nothing is hidden.

!!! note "Image tags"
    `0.5.1` is the current release and matches the configuration shown here. `:0.5` follows
    the newest 0.5.x patch, and `:edge` is rebuilt on every merge to `master` — useful for
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

To see what the stack thinks this machine is — architecture, accelerator, and the
`shm_size` it would use — run the same detection the installer runs:

```bash
docker compose run --rm --no-deps config cctv-host
```

It exits 78 on a host the support matrix does not cover, rather than starting a stack that
will not work.

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

```bash
./update.sh --to ghcr.io/jothikaraja/cctv-stack:0.6.0
./update.sh --rollback     # the previous image, and the pre-upgrade database
./update.sh --dry-run      # print the plan, change nothing
```

`update.sh` runs the whole sequence in a fixed order, and the order is the safety:

1. **Back up first.** The state database is written to `data/backups/` and copied to
   `data/db-backups/pre-upgrade-<timestamp>.db`. If that fails, nothing else happens.
2. Point `.env` at the new image, keeping the old one as `CCTV_IMAGE_PREVIOUS`, and save the
   old `.env` as `.env.pre-upgrade-<timestamp>`.
3. Pull the new image.
4. Stop `clip-worker`, `uploader` and `cleanup` — the producer and the destroyer — then run the
   migrations with the **new** code.
5. Start everything and verify with `cctv-doctor`.

Frigate and the broker keep running the whole time, so recording continuity is never
interrupted. A failed migration stops and prints the rollback command; it does not roll back by
itself, because that decision is yours.

!!! warning "Upgrading from 0.3.x"
    `0.5.x` adds three services — `status` (the aggregate page), `gatus` and `ntfy` — so the
    expected `compose.yml` and `.env` changed as well as the image. Re-download both, then
    merge any values you had edited in `.env` into the new file (it gains `STATUS_PORT`,
    `GATUS_PORT`, `NTFY_PORT` and `CCTV_MONITORING_TOKEN`). `config/cameras.yml` is unchanged.

    The generated `config/host.yml` is written by the installer; existing sites keep working
    without it.

!!! note "Upgrading from 0.5.0"
    `0.5.1` moves the generated Gatus config from `config/gatus/` to `data/gatus/` and gives
    Gatus a named volume for its history, which is what stops a fresh install's Gatus from
    crash-looping on a root-owned directory. Re-download `compose.yml`; `docker compose up -d`
    recreates the affected services. The old `config/gatus/` directory can be deleted.

pip:

```bash
python -m pip install --upgrade "cctv-stack==0.5.1"
```

Database migrations run automatically when a service starts, and `update.sh` runs them
between stopping the old workers and starting the new containers. They are forward-only:
rolling back the image after a schema migration needs the pre-upgrade database, which is why
`update.sh` always writes one. `./update.sh --rollback` restores it and repins the previous
image in one step.

## Removing the stack

```bash
./uninstall.sh            # stop and remove containers; keep data and config
./uninstall.sh --images   # also remove the images this stack used
./uninstall.sh --purge    # also remove config/ and the data directory
./uninstall.sh --dry-run  # print what would happen, change nothing
```

Nothing is deleted unless you ask for it. `data/`, `config/` and `.env` survive a plain
uninstall, so starting the stack again resumes where it left off.

`--purge` asks you to **type the location name** (`CCTV_SITE`, for example `parents-home`)
before it removes anything. A `--yes` flag is too easy to paste from a forum answer.

What a purge still does not do: delete a single object from Backblaze B2. The bucket is not
this script's business, and forgetting a bucket with no lifecycle rule attached is how a site
keeps billing after it is gone. Empty it, or set a lifecycle rule, on purpose.

If something is wrong but you are not ready to remove the stack, run `./scripts/doctor.sh`
instead — it diagnoses without changing anything. See [Diagnose problems](doctor.md).

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
