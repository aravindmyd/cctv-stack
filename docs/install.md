# Install

One command takes a fresh machine to a working CCTV system: `./install.sh`. This page is the
whole path — what to prepare, what the installer asks, the URLs to open when it finishes, and how
to prove it is recording rather than merely running.

If you read only one section, read [Verify it is working](#step-5-verify-it-is-actually-working).
An installation that is **up** and one that is **working** are different things, and the
difference is four commands.

---

## Step 0 — what you need

| | Requirement | Why |
|---|---|---|
| **A machine that stays on** | Mini-PC, NAS, Raspberry Pi 5, or a VM | It is the recorder. Sleep is data loss |
| **Docker** | Engine 24+ with the Compose v2 plugin | A bare `docker-compose` binary is not enough |
| **Disk** | An SSD. 160 GB is comfortable for two cameras | ~1 GB per camera per hour of continuous footage, and SD cards die |
| **Cameras** | RTSP URLs **including username and password** | ONVIF discovery is not used — you supply the URLs |
| **Cloud backup** (optional) | A Backblaze B2 bucket and application key | Without it nothing leaves the machine, and local retention never deletes a clip |
| **A phone** (optional) | The [ntfy app](https://ntfy.sh/#subscribe) | Where alerts arrive |

Two numbers decide how much disk you need, and both are per camera:

- Continuous footage is kept **2 days** (`record.keep_days`), motion footage **10 days**.
- Clips are kept **90 days locally** and **60 days in B2**, and a local clip is only deleted once
  its upload is confirmed.

At 2 Mbps that is roughly **21 GB per camera per day** of continuous footage. A camera at 4 Mbps
needs about 86 GB for the two days.

### Get the camera URL first

Check the camera's manual or its web interface. These are the common shapes:

```text
rtsp://admin:PASSWORD@192.168.1.50:554/Streaming/Channels/101                 Hikvision
rtsp://admin:PASSWORD@192.168.1.51:554/cam/realmonitor?channel=1&subtype=0     Dahua
rtsp://user:PASSWORD@192.168.1.52:554/stream1                         Reolink / Tapo / generic
```

Test it before installing. A URL that fails here fails at 3 a.m. too:

```bash
ffplay -rtsp_transport tcp "rtsp://admin:PASSWORD@192.168.1.50:554/Streaming/Channels/101"
# No ffplay? Any player works: VLC → Open Network Stream → paste the URL.
```

---

## Step 1 — install Docker

```bash
# Debian / Ubuntu / Raspberry Pi OS
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"    # then log out and back in

# Fedora
sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker

# macOS / Windows — Docker Desktop, with the daemon running
```

These are the same two checks `install.sh` runs before anything else:

```bash
docker compose version    # v2.x — if this errors, the Compose plugin is missing
docker info > /dev/null && echo "the daemon is up"
```

---

## Step 2 — get the files

### From the repository (works today)

```bash
git clone https://github.com/aravindmyd/cctv-stack.git
cd cctv-stack
```

### From a signed release bundle

Each release from the one that ships the bundle onward publishes a tarball, a checksum file, and
a signature over that file. **Verify before extracting** — everything after this step runs code:

```bash
VERSION=0.8.0   # the version you are installing

curl -fsSLO "https://github.com/aravindmyd/cctv-stack/releases/download/v$VERSION/cctv-stack-$VERSION.tar.gz"
curl -fsSLO "https://github.com/aravindmyd/cctv-stack/releases/download/v$VERSION/SHA256SUMS"
curl -fsSLO "https://github.com/aravindmyd/cctv-stack/releases/download/v$VERSION/SHA256SUMS.sig"
curl -fsSLO "https://github.com/aravindmyd/cctv-stack/releases/download/v$VERSION/SHA256SUMS.pem"

sha256sum -c SHA256SUMS --ignore-missing

cosign verify-blob \
  --certificate SHA256SUMS.pem \
  --signature SHA256SUMS.sig \
  --certificate-identity-regexp \
    'https://github.com/aravindmyd/cctv-stack/.github/workflows/publish.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS

sudo mkdir -p /opt/cctv
sudo tar -xzf "cctv-stack-$VERSION.tar.gz" -C /opt/cctv --strip-components=1
cd /opt/cctv
```

The two checks answer different questions. `sha256sum -c` proves the file matches the checksum
you were given. `cosign verify-blob` proves **who** gave you that checksum. Without the second, a
tampered download and a tampered checksum agree with each other perfectly.

> **Status.** The bundle is built, verified and signed by the release workflow, but the newest
> published release (`0.7.0`) predates it. Until a newer tag exists, use the repository path —
> it produces the same stack, and `install.sh` notices the difference and builds instead of
> pulling.

---

## Step 3 — run the installer

```bash
./install.sh
```

It asks four sets of questions, every one with a default, so the common path is pressing Enter.

```text
==> Configuring this site

The location name appears in every clip path and cloud object key.
Location [mini-pc]: parents-home

The timezone decides which directory a clip is filed under. It is never
guessed from the container, so confirm it.
Timezone [Europe/London]: Asia/Kolkata

Cameras. The RTSP URL is written to .env and referenced from cameras.yml as
${CAMERA_FRONT_DOOR_RTSP}; it is never printed back with its password.

  Camera 1
    Name [front-door]:
    Main RTSP URL: rtsp://admin:secret@192.168.1.50:554/Streaming/Channels/101
    Is there a sub-stream URL (cheaper detection)? [y/N]: y
    Sub RTSP URL: rtsp://admin:secret@192.168.1.50:554/Streaming/Channels/102

Add another camera? [y/N]:

Cloud backup is optional. Without it the uploader runs and reports DISABLED,
and local retention never deletes a clip, because nothing is backed up.
Enable Backblaze B2 backup? [y/N]: y
  Bucket: cctv-parents-home
  Key ID: 0051a2b3c4d5e6f7000000001
  Application key (not echoed):
```

Rules the installer enforces so you do not have to remember them:

| Input | Rule | Why |
|---|---|---|
| Location | lowercase, 1–32 characters, interior hyphens | It appears in every clip path and every cloud object key |
| Camera name | lowercase kebab-case (`front-door`) | Same reason — it becomes a directory name |
| Timezone | an IANA name (`Asia/Kolkata`) | Decides which date directory a clip is filed under |
| RTSP URL | must start `rtsp://` and name a host | A placeholder produces a camera that looks configured and never connects |

**Passwords are never echoed and never printed back.** The camera URL goes into `.env` and is
referenced from `cameras.yml` as `${CAMERA_FRONT_DOOR_RTSP}`, so the file you show someone does
not contain the password.

### What it does next, in order

```text
1. Builds the image — or, in a bundle, pulls this release's pinned digests
2. Asks the questions above and writes config/cameras.yml and .env
3. Detects this host against the support matrix, and sizes Frigate's shared memory
4. Generates two internal tokens (admin endpoints, monitoring heartbeats)
5. Validates what it wrote, with the same loader the services use
6. Provisions the B2 bucket — create, versioning, expiry rule, probe object
7. Starts the stack, and waits up to 90 s for Frigate
8. Sends a real test message to every configured alert channel
```

Steps 6 and 8 are loud but not fatal: a wrong bucket key means clips are not uploaded yet, and a
broken alert channel means alerts do not arrive. Neither stops recording, and both print how to
re-run them.

`./install.sh` is safe to run again. It reconciles rather than replaces, and it will not
overwrite a `config/cameras.yml` you have edited unless you ask it to.

---

## Step 4 — the URLs

Replace `192.168.1.10` with this machine's address:

```bash
hostname -I | awk '{print $1}'     # Linux
ipconfig getifaddr en0             # macOS
```

| What | URL | Reachable from |
|---|---|---|
| **Status page** — *is my CCTV working?* | `http://192.168.1.10:8084/status` | Your LAN |
| **Gatus** — dashboard and alert history | `http://192.168.1.10:3001` | Your LAN |
| **ntfy** — the alert channel | `http://192.168.1.10:8085` | Your LAN |
| **Frigate UI** — live views, recordings, zones | `https://127.0.0.1:8971` | **Only this machine** |
| Uploader health | `http://127.0.0.1:8082/health` | Only this machine |
| Cleanup health | `http://127.0.0.1:8083/health` | Only this machine |
| Metrics | `http://127.0.0.1:8082/metrics`, `:8083`, `:8084` | Only this machine |

Open the status page first. A browser gets the page; a script gets JSON:

```bash
curl -s http://192.168.1.10:8084/status | jq
```

**Why the Frigate UI is loopback-only, and how to reach it from your laptop.** Port 8971 binds to
`127.0.0.1` on purpose: it is the one surface that shows every camera and changes every setting.
Forward it over SSH instead of publishing it:

```bash
ssh -N -L 8971:127.0.0.1:8971 user@192.168.1.10
# then open https://127.0.0.1:8971 in your laptop's browser
```

It is **HTTPS with a self-signed certificate** Frigate generates on first start, so the browser
warns once. That warning is expected on a loopback port. If you see *"plain HTTP request sent to
HTTPS port"*, you opened `http://` by mistake.

**Port 5000 is never published, by design.** Frigate treats requests arriving on it as anonymous
admin, so it is reachable only from inside the Docker network.

Two secrets are generated for you on first install and left alone afterwards:
`INTERNAL_API_TOKEN` (without it the uploader's admin endpoints are not mounted at all) and
`CCTV_MONITORING_TOKEN` (without it Gatus's heartbeat endpoints are omitted from its config, so
monitors would exist but nothing would report into them).

---

## Step 5 — verify it is actually working

Running is not working. Four checks, each answering a different question.

### 1. The machine agrees it is healthy

```bash
./scripts/doctor.sh
```

Read-only — it starts nothing, stops nothing, changes nothing. It checks the host, storage,
configuration, the database, connectivity (including the ntfy server) and the pipeline. Fix
anything it prints as failing before going further.

### 2. Every camera is delivering frames

```bash
curl -s http://192.168.1.10:8084/status | jq '.cameras'
```

Each camera should be online with a frames-per-second value above zero. A camera that is listed
but `QUIET` connects and sends nothing — usually a wrong stream path, or a stream the camera only
offers on another channel.

Frigate's own answer is the authoritative one:

```bash
docker compose exec -T clip-worker python -c \
  "import json,urllib.request as u; print(json.dumps(json.load(u.urlopen('http://frigate:5000/api/stats'))['cameras'], indent=2))"
```

A camera with `camera_fps: 0` has never delivered a frame.

### 3. Footage is landing on disk

```bash
ls -lt data/media/recordings | head
docker compose logs --tail=200 frigate | grep -ciE 'not valid|safe mode'   # must be 0
```

A count above zero on the second command means Frigate rejected its config and started in **safe
mode** — with zero cameras and no error where you would look. It is the most common cause of "it
installed fine but nothing records".

### 4. A clip exists, which is the entire point

Walk in front of a camera, wait about 30 seconds past the end of the activity, then:

```bash
find data/clips -name '*.mp4' -newermt '-10 minutes' | head    # GNU find
ls -t data/clips/*/*/*/*/*.mp4 | head                          # portable
```

You want a path shaped like `data/clips/front-door/2026/10/20/14-32-05.mp4`, with a matching
`.mp4.json` beside it. That file and the row behind it are what everything else protects.

If you configured B2, confirm the upload rather than assuming it:

```bash
curl -s http://127.0.0.1:8082/health | jq '.state'               # HEALTHY
curl -s http://127.0.0.1:8083/health | jq '.state'               # HEALTHY
curl -s http://127.0.0.1:8082/admin/queue | jq '.uploaded_24h'   # above 0
```

---

## Step 6 — put alerts on your phone

The stack ships its own alert path: **Gatus** watches everything and **ntfy** delivers to a
phone. Both are already running, and the installer sent a test message during step 3.

1. Install the **ntfy** app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) ·
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)), or open
   `http://192.168.1.10:8085` in a browser.
2. Subscribe to server `http://192.168.1.10:8085`, topic `cctv`.
3. Open `http://192.168.1.10:3001` — Gatus shows every monitor and its history.

If the test message did not arrive, ask again rather than guessing:

```bash
docker compose exec -T status cctv-notify test
```

| Output | Meaning | Fix |
|---|---|---|
| `ntfy: delivered` | The channel works | Check the phone subscribes to the same topic |
| `ntfy: FAILED — cannot reach …` | The ntfy container is not up | `docker compose up -d ntfy` |
| `telegram: skipped (not configured)` | No Telegram token set | Expected unless you configured one |

To use a topic other than `cctv` — worth doing when you operate more than one installation — set
`CCTV_STATUS_NTFY_TOPIC` in `.env` and restart `status` and `gatus`.

---

## Step 7 — add a camera later

Cameras are data. Nothing needs redeploying, and no code refers to a camera by name.

1. Put the credentials in `.env`, named after the camera:

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

3. Prove the URL works before applying anything — inside the Frigate container, which already
   has the network access the cameras need:

   ```bash
   docker compose exec -T frigate ffprobe -v error -rtsp_transport tcp "$CAMERA_GARAGE_SUB"
   ```

   No output means the URL or the credentials are wrong. A wall of errors means the same thing.

4. Validate, then apply:

   ```bash
   docker compose run --rm --no-deps config \
     cctv-config validate --cameras /config/cameras.yml
   docker compose up -d
   ```

   `up -d` re-runs the one-shot `config` service, which rewrites `config/frigate/config.yml`,
   and recreates Frigate because the generated file changed.

Every field is documented in [Configure](configure.md), including the per-camera retention
windows. The one rule that matters: the name is lowercase kebab-case, because it becomes a
directory name and a cloud object key.

---

## Day two

| Task | Command | Details |
|---|---|---|
| Is anything wrong right now? | `http://<host>:8084/status` | [Health](health.md) |
| Something is wrong — why? | `./scripts/doctor.sh` | [Doctor](doctor.md) |
| Back up config and database | `./backup.sh`, then `./backup.sh --verify` | [Disaster recovery](disaster-recovery.md) |
| Upgrade, or roll back | `./update.sh`, `./update.sh --rollback` | [Upgrading](#upgrading) |
| A clip will not upload | — | [Recover uploads](recover-uploads.md) |
| The machine is gone | `./restore.sh config`, `state`, `clips`, `rebuild` | [Disaster recovery](disaster-recovery.md) |
| Rotate a password or B2 key | — | [Rotate credentials](rotate-credentials.md) |

**Take a backup now, not after the first incident.** `./backup.sh` writes an encrypted config
bundle and a verified online copy of the database into `data/backups/`. `--verify` re-opens the
database and compares its row count against the live one, so a backup that has quietly stopped
being taken is reported instead of trusted.

---

## Install by hand, without the installer

The released image is public, so nothing in this guide needs the source repository.
Compose needs three files: `compose.yml`, an environment file, and `cameras.yml`.

```bash
mkdir cctv-stack && cd cctv-stack
mkdir -p config

curl -fsSLO https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/compose.yml
curl -fsSL https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/.env.example -o .env

chmod 600 .env
```

### 1. Answer the questions

The same interview the installer runs, against the files in this directory:

```bash
docker compose run --rm --no-deps init cctv-init
```

It writes `.env` (mode `0600`) and `config/cameras.yml`, and validates the generated
configuration before it replaces anything. `--force` replaces an existing `cameras.yml`;
without it, an existing file is left alone and the command says so.

No terminal, or automation? `--non-interactive` never guesses — it validates what is
already on disk and exits `78` naming the values that are still placeholders.

#### If you would rather write the files yourself

`.env`:

```ini
# The published image. Pin the release you are installing; `edge` follows the default
# branch and is not a stable target.
CCTV_IMAGE=ghcr.io/aravindmyd/cctv-stack:0.7.0

# RTSP URLs. Replace the placeholder user:password and host.
CAMERA_FRONT_DOOR_RTSP=rtsp://user:password@192.168.1.10:554/Streaming/Channels/101
CAMERA_FRONT_DOOR_SUB=rtsp://user:password@192.168.1.10:554/Streaming/Channels/102

# Must match `site.timezone` in config/cameras.yml.
TZ=Asia/Kolkata
```

Leave `CCTV_STORAGE_BUCKET` empty for a local-only install. The uploader will run and
report `DISABLED`; nothing is uploaded and nothing is hidden.

!!! note "Image tags"
    `0.7.0` is the current release and matches the configuration shown here. `:0.7` follows
    the newest 0.7.x patch, and `:edge` is rebuilt on every merge to `master` — useful for
    testing a change, not for a site you rely on.

`config/cameras.yml`, where each camera references the variable names from `.env`:

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

### 2. Validate before starting

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

### 3. Start

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

### 4. Check that it is running

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

---

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

---

## Upgrading

```bash
./update.sh --to ghcr.io/aravindmyd/cctv-stack:0.7.0
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

!!! warning "Upgrading from 0.6.0 — your disk will start filling"
    `0.7.0` changes the default recording mode to **two days of continuous footage** (was: three
    days of motion only). Existing sites gain it without editing anything, because their
    config omits `record:`. That is roughly **21 GB per camera per day at 2 Mbps**, so check
    free space before upgrading: a camera at 4 Mbps needs about 86 GB for the two days.

    The footage stays on this host — it is never uploaded, never backed up, and never billed.
    To keep the old behaviour, set `record: {mode: motion}` under `defaults` in
    `config/cameras.yml`. See [Configure](configure.md#record) for the trade-off.

!!! note "Upgrading from 0.5.0"
    `0.5.1` moves the generated Gatus config from `config/gatus/` to `data/gatus/` and gives
    Gatus a named volume for its history, which is what stops a fresh install's Gatus from
    crash-looping on a root-owned directory. Re-download `compose.yml`; `docker compose up -d`
    recreates the affected services. The old `config/gatus/` directory can be deleted.

pip:

```bash
python -m pip install --upgrade "cctv-stack==0.7.0"
```

Database migrations run automatically when a service starts, and `update.sh` runs them
between stopping the old workers and starting the new containers. They are forward-only:
rolling back the image after a schema migration needs the pre-upgrade database, which is why
`update.sh` always writes one. `./update.sh --rollback` restores it and repins the previous
image in one step.

---

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

---

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
