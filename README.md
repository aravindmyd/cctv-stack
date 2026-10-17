# cctv-stack

[![CI](https://github.com/aravindmyd/cctv-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/aravindmyd/cctv-stack/actions/workflows/ci.yml)
[![Publish](https://github.com/aravindmyd/cctv-stack/actions/workflows/publish.yml/badge.svg)](https://github.com/aravindmyd/cctv-stack/actions/workflows/publish.yml)

Self-hosted CCTV built on **Frigate**, adding the four things Frigate deliberately leaves
to the operator:

1. **Canonical clips** — every review becomes one file at a deterministic path.
2. **Durable cloud backup** — clips are uploaded to Backblaze B2 exactly once.
3. **Independent retention** — local cleanup that never deletes an un-uploaded clip.
4. **Health surfaces** — each service reports whether it is actually working.

Every installation is independent: no central hub, no cross-site data, no inbound
connectivity requirement.

---

## Status

| Capability | State |
|---|---|
| Detect → canonical clip | ✅ |
| Upload to Backblaze B2, exactly once | ✅ |
| Local retention (upload-aware) | ✅ |
| Cloud retention and reconciliation | ⬜ |
| Aggregate status page and alerting | ⬜ |
| One-command client installer and signed bundle | ⬜ |
| Backup, restore, upgrade | ⬜ |

v0.3 is usable for a local-only installation or a B2-backed one. The deferred work, and
why it is deferred, is in [`docs/plans/backlog.md`](docs/plans/backlog.md). The client-site
delivery plan is [`docs/plans/packaging.md`](docs/plans/packaging.md).

## How it fits together

```text
 cameras ──RTSP──> Frigate ──review──> clip-worker ──> data/clips/... ──> uploader ──> B2
                      │                                    │
                      └── recordings (recovery window)      └── cleanup (only after upload)
```

Frigate does camera ingest, detection, recording and review segmentation. The
`clip-worker` cuts **one canonical clip per review item**. The uploader pushes it once,
and cleanup deletes a local clip only after its upload is terminal-success (INV-1).

MQTT is the low-latency path. The Frigate review sweep is the guarantee: if the broker or
the worker was down, the next sweep still materialises the clip. A review arriving twice
produces one clip, not two.

## Quick start

**Requirements:** `git`, Linux x86_64 or arm64 (macOS is supported for development),
Docker Engine with the Compose v2 plugin, and disk space for Frigate's recordings. The
first real run pulls the Frigate image and builds the CCTV image, so expect several
minutes.

```bash
git clone https://github.com/aravindmyd/cctv-stack.git
cd cctv-stack
./install.sh
```

The first run creates the two files that only you can fill in, then exits:

| File | Contains | Committed? |
|---|---|---|
| `.env` | Camera RTSP URLs, timezone, optional B2 credentials | **No** — gitignored |
| `config/cameras.yml` | Camera names, clip padding, retention policy | **No** — gitignored |

Edit both, then run `./install.sh` again. It validates the configuration, generates
Frigate's config, builds the image, starts the stack, and waits until Frigate answers.

Frigate's UI is HTTPS on `https://127.0.0.1:8971` (loopback only). The certificate is
self-signed, so the browser warns once. Port 5000 and MQTT are never published.

Clips land at:

```text
data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
```

The sidecar records the review, event ids, requested boundaries, coverage, size and
SHA-256. The clips tree uses the **site timezone**; Frigate's own recordings tree uses
**UTC** — both are deliberate ([ADR-0017](docs/adr/0017-time-handling.md)).

A clip appears **after** the visit ends, not during it: Frigate keeps the review open
until the alert cutoff passes. There is no partial clip by design.

## What runs

| Service | Role |
|---|---|
| **Frigate** | Camera ingest, detection, recording and review segmentation |
| **clip-worker** | Materialises completed reviews at the canonical path, with a sidecar |
| **uploader** | Uploads to B2 through a storage adapter, with a persisted retry state machine. Reports `DISABLED` when no bucket is configured and exits `78` on a half-configuration |
| **cleanup** | Enforces local retention; refuses to delete a clip whose upload is not terminal-success |
| **config** | One-shot generator: `cameras.yml` → Frigate's config (INV-3) |
| **mosquitto** | MQTT broker, internal only |
| **status-collector** | ⬜ not built yet |

### Health surfaces

| Surface | Address | Notes |
|---|---|---|
| Frigate UI | `https://127.0.0.1:8971` | HTTPS, self-signed, loopback only |
| Uploader | `http://127.0.0.1:8082/health` | `DISABLED` without a B2 bucket |
| Cleanup | `http://127.0.0.1:8083/health` | Local retention state |
| Frigate API, MQTT | not published | Reachable only on the compose network |

## Configuration

The split is deliberate and load-bearing:

- **`config/cameras.yml`** is the single source of truth for cameras (INV-3). Frigate's
  config is generated from it and must never be hand-edited.
- **`.env`** holds location values and secrets: RTSP URLs, B2 keys, ports, data path.

Both real files are gitignored. `.env.example` and `config/cameras.example.yml` are the
templates. Never commit the filled-in versions, and never paste an RTSP URL or B2 key into
an issue. If a credential is exposed, follow
[`docs/runbooks/rotate-credentials.md`](docs/runbooks/rotate-credentials.md).

Camera names are lowercase kebab-case because they become filesystem paths and cloud
object keys.

## Published artefacts

| Artefact | Where | Published when |
|---|---|---|
| Branch image | `ghcr.io/aravindmyd/cctv-stack:edge` | Every merge to `main`/`master` |
| Commit image | `ghcr.io/aravindmyd/cctv-stack:sha-<short>` | Every merge |
| Version image | `ghcr.io/aravindmyd/cctv-stack:<version>` | A `v*` tag |
| Python package | `pypi.org/project/cctv-stack` | A `v*` tag |
| Wheel and sdist | Workflow artefact, and GitHub Release on tags | Every merge / tag |

```bash
docker pull ghcr.io/aravindmyd/cctv-stack:edge
pip install cctv-stack
```

The image is the runtime. `pip install cctv-stack` installs the four CLI entry points
(`cctv-clip-worker`, `cctv-config`, `cctv-uploader`, `cctv-cleanup`) but **not** Frigate,
Compose, or site configuration. The complete client-site install is the signed release
bundle in [`docs/plans/packaging.md`](docs/plans/packaging.md).

## Development

```bash
uv sync
uv run pytest                    # unit suite; live tests skip without Docker/S3
uv run ruff check .
uv run ruff format --check .

./tests/integration/run.sh       # live S3 suite against RustFS; needs Docker
./tests/e2e/run.sh               # synthetic camera → real clip; needs Docker + Frigate
```

The unit suite is **717 passed, 19 skipped** without a configured S3 endpoint or a running
Docker daemon. CI runs pre-commit, unit tests on Python 3.12 and 3.13, the pip-install
fallback, live integration, and the synthetic e2e pipeline. The publish workflow builds
the multi-arch image and the Python distributions.

## Documentation

- [`docs/index.md`](docs/index.md) — documentation map and reading order
- [`docs/architecture/hld.md`](docs/architecture/hld.md) — how the system fits together
- [`docs/architecture/invariants.md`](docs/architecture/invariants.md) — rules that must never break
- [`docs/runbooks/install.md`](docs/runbooks/install.md) — the target operator runbook (M6; the current installer is the two-run flow above)
- [`docs/plans/roadmap.md`](docs/plans/roadmap.md) — what gets built, in what order
- [`docs/plans/backlog.md`](docs/plans/backlog.md) — deliberately deferred work
- [`docs/plans/packaging.md`](docs/plans/packaging.md) — client-site delivery and release artefacts

## License

Apache-2.0 — see [`LICENSE`](LICENSE). The reasoning is in
[`docs/plans/opensourcing.md`](docs/plans/opensourcing.md).
