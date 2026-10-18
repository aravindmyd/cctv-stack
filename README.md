# cctv-stack

[![PyPI](https://img.shields.io/pypi/v/cctv-stack.svg)](https://pypi.org/project/cctv-stack/)
[![Python versions](https://img.shields.io/pypi/pyversions/cctv-stack.svg)](https://pypi.org/project/cctv-stack/)
[![CI](https://github.com/aravindmyd/cctv-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/aravindmyd/cctv-stack/actions/workflows/ci.yml)

`cctv-stack` is a Python toolkit and container image for a self-hosted CCTV pipeline built
on **[Frigate](https://github.com/blakeblackshear/frigate)**.

Frigate does camera ingest, detection, recording and review segmentation. `cctv-stack`
adds the four pieces it leaves to the operator:

1. **Canonical clips** — one deterministic file per review item.
2. **Durable backup** — clips are uploaded to Backblaze B2 exactly once.
3. **Upload-aware retention** — local cleanup never deletes a clip that is not safely
   uploaded.
4. **Health surfaces** — the uploader and cleanup expose `/health` and `/metrics`.

It is not a replacement for Frigate. It is the missing layer around it.

**Current release: `0.5.0`.** Clips, B2 upload, local retention, the aggregate status page
and alerting through Gatus/ntfy are implemented. Cloud retention and the one-command
installer are not — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the exact state and roadmap.

---

## Install

```bash
pip install cctv-stack
```

Requires **Python 3.12+**. The distribution is `cctv-stack`; the import package is `cctv`.

Optional extras:

| Extra | Adds | Needed by |
|---|---|---|
| `mqtt` | `paho-mqtt` | `cctv-clip-worker` when subscribing to `frigate/reviews` |
| `storage` | `boto3` | `cctv-uploader` for B2/S3 |
| `dev` | pytest, ruff | Contributors |

```bash
pip install "cctv-stack[mqtt,storage]"
```

### Container image

```bash
docker pull ghcr.io/aravindmyd/cctv-stack:0.5.0
```

Tags published per release: `:0.5.0`, `:0.5`, and the commit tag `:sha-<short>`.
A moving `:edge` tag follows `master`. If the registry package is private, run
`docker login ghcr.io` first. The image includes `ffmpeg`/`ffprobe`.

## Commands

| Command | What it does |
|---|---|
| `cctv-config validate --cameras cameras.yml` | Validate the camera configuration. Exits 78 (`EX_CONFIG`) on an invalid config |
| `cctv-config generate --cameras cameras.yml --out frigate.yml` | Generate Frigate's configuration from `cameras.yml` |
| `cctv-clip-worker` | Turn completed Frigate reviews into canonical clips |
| `cctv-uploader` | Upload clips to B2 through the persisted retry state machine |
| `cctv-cleanup` | Enforce local retention after a successful upload |
| `cctv-status` | Serve the aggregate status page (`:8084/status`), health and metrics |
| `cctv-host` | Detect this machine's host profile and Frigate's `shm_size` |

The four services are long-running and configuration-driven; they take no command-line
arguments. Run them under systemd, Docker, or Compose.

With Compose, the answer to *is my CCTV working?* is at
`http://<host>:8084/status`; the dashboard and history are at `http://<host>:3001` (Gatus),
and alerts arrive through ntfy at `http://<host>:8085`.

## Quick start

The only required input is a `cameras.yml`. This minimal file is valid; `${VAR}` values
are read from the environment and an unset variable is a hard error.

```yaml
site:
  location: home
  timezone: UTC

cameras:
  front-door:
    display_name: Front Door
    rtsp:
      main: ${CAMERA_FRONT_DOOR_RTSP}
      sub: ${CAMERA_FRONT_DOOR_SUB}
```

Validate it and generate Frigate's config:

```bash
export CAMERA_FRONT_DOOR_RTSP='rtsp://user:pass@192.168.1.10:554/stream1'
export CAMERA_FRONT_DOOR_SUB='rtsp://user:pass@192.168.1.10:554/stream2'

cctv-config validate --cameras cameras.yml
cctv-config generate --cameras cameras.yml --out frigate.yml
```

`validate` checks the camera model and the environment substitution. `generate` writes a
Frigate config with a `GENERATED FILE — DO NOT EDIT` header.

### Running the services

```bash
export CCTV_CONFIG_DIR="$PWD"
export CCTV_DATA_DIR="$PWD/data"
export CCTV_FRIGATE_URL="http://127.0.0.1:5000"

cctv-clip-worker
```

Run each service in its own process. `cctv-clip-worker` works without MQTT (sweep-only);
set `CCTV_MQTT_HOST` to receive reviews immediately. `cctv-uploader` reports `DISABLED`
and uploads nothing when `CCTV_STORAGE_BUCKET` is empty; a bucket with no key exits `78`.

> `ffprobe` must be on `PATH` for the clip worker. The container image includes it; a
> `pip install` does not install ffmpeg.

## Configuration

There are two kinds of configuration, and they must not be mixed:

| File | Holds | Secret? |
|---|---|---|
| `cameras.yml` | Site name, timezone, cameras, clip policy, retention | No — but `${VAR}` values come from the environment |
| environment / `.env` | RTSP URLs, B2 keys, paths, ports, intervals | **Yes** |

The full template is [`.env.example`](.env.example). The settings most consumers need:

| Variable | Default | Meaning |
|---|---|---|
| `CCTV_CONFIG_DIR` | `/config` | Directory containing `cameras.yml` |
| `CCTV_DATA_DIR` | `/data` | Root for `clips/` and the state database |
| `CCTV_DB` | `$CCTV_DATA_DIR/cctv.db` | SQLite state store |
| `CCTV_FRIGATE_URL` | `http://frigate:5000` | Frigate API base URL. Port 5000 must stay on a private network |
| `CCTV_MQTT_HOST` | unset | Unset means sweep-only; no broker required |
| `CCTV_SWEEP_INTERVAL_S` | `60` | Worst-case delay before a missed review is noticed |
| `CCTV_STORAGE_BUCKET` | empty | Empty means cloud backup `DISABLED` |
| `CCTV_STORAGE_KEY_ID` | empty | B2 application key ID |
| `CCTV_STORAGE_APPLICATION_KEY` | empty | B2 application key |
| `CCTV_STORAGE_REGION` | `us-west-004` | B2 region |
| `CCTV_UPLOADER_HEALTH_PORT` | `8082` | Uploader `/health` and `/metrics` |
| `CCTV_CLEANUP_HEALTH_PORT` | `8083` | Cleanup `/health` and `/metrics` |
| `INTERNAL_API_TOKEN` | empty | Enables the uploader admin endpoints when set |

Keep `.env` out of version control. RTSP URLs and B2 keys are credentials.

Camera names must be lowercase kebab-case (`front-door`): they become directory names and
cloud object keys.

## What it produces

```text
$CCTV_DATA_DIR/
├── clips/
│   └── <camera>/YYYY/MM/DD/HH-MM-SS.mp4
│       <camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
└── cctv.db
```

The clips tree is rendered in the **site timezone**. The sidecar records the review id,
event ids, requested boundaries, measured coverage, codecs, size and SHA-256. The cloud
object key is `<location>/clips/<path-inside-clips>`.

A clip appears **after** the visit ends: Frigate keeps the review open until its alert
cutoff passes. There is no partial clip by design.

### Health endpoints

| Service | Endpoint | Notes |
|---|---|---|
| `cctv-uploader` | `http://127.0.0.1:8082/health`, `/metrics` | `DISABLED` without a bucket |
| `cctv-cleanup` | `http://127.0.0.1:8083/health`, `/metrics` | Local retention state |
| Uploader admin | `/admin/queue`, `/admin/retry` | Mounted only when `INTERNAL_API_TOKEN` is set |

Bind these to loopback. Nothing in the package needs an inbound port from the LAN.

## Python API

The supported contract is the CLI, the environment variables, and the HTTP health
endpoints. The importable modules are tested, but pre-1.0 names may change between minor
versions.

```python
from cctv.config import ConfigError, load_cameras_config

try:
    config = load_cameras_config("cameras.yml")
except ConfigError as exc:
    raise SystemExit(f"invalid camera config: {exc}")

print(config.site.location)
for name, camera in config.enabled_cameras().items():
    print(name, camera.display_name, camera.record.mode)
```

Storage is available behind the `storage` extra:

```python
import os
from cctv.storage import S3StorageAdapter

adapter = S3StorageAdapter(
    bucket=os.environ["CCTV_STORAGE_BUCKET"],
    access_key_id=os.environ["CCTV_STORAGE_KEY_ID"],
    secret_access_key=os.environ["CCTV_STORAGE_APPLICATION_KEY"],
    region=os.environ.get("CCTV_STORAGE_REGION", "us-west-004"),
)
```

`cctv.__version__` reports the installed package version.

## Complete installation

For a full CCTV site — Frigate, MQTT, the worker, uploader and cleanup — use the Compose
deployment in this repository:

```bash
git clone https://github.com/aravindmyd/cctv-stack.git
cd cctv-stack
./install.sh
```

The first run creates `.env` and `config/cameras.yml`; edit both and run it again. See
[`docs/install.md`](docs/install.md) for the operator procedure and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for how releases are built and delivered.

## Compatibility

| | |
|---|---|
| Python | 3.12 or newer |
| Frigate | 0.18 API and MQTT contract (verified) |
| Platforms | Linux x86_64/arm64; macOS for development |
| Cloud backup | Backblaze B2 through its S3-compatible endpoint; optional |
| Inbound network | None required |

## Documentation

Full documentation: **https://aravindmyd.github.io/cctv-stack/**

- [Install](docs/install.md) — Docker Compose from the published image, or pip
- [Configure](docs/configure.md) — `cameras.yml`, `.env`, adding a camera
- [Frigate UI login](docs/frigate-ui.md)
- [Health](docs/health.md) — endpoints and what to check first
- [Recover failed uploads](docs/recover-uploads.md)
- [Glossary](docs/glossary.md) — the vocabulary these docs use
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development, CI and release process

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
