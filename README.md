# cctv-stack

A packaging-first, self-hosted CCTV system. Clone it, configure it, run one
script, and you have a working system with local recording, event clips, cloud
backup, retention, and health monitoring.

> **Status: ✅ v0.3 — detect, materialise, upload, and local retention.** A camera sees a
> person, the clip is materialised at its canonical path, and the uploader pushes it to
> Backblaze B2 exactly once when storage is configured. The cleanup service deletes a local
> clip only after its upload is terminal-success, so INV-1 holds in code rather than by
> omission.
>
> **Not built yet: cloud retention and reconciliation, the aggregate status page and
> alerting, the one-command installer, and backup/upgrade.** What is deferred, and why, is
> tracked in [`docs/plans/backlog.md`](docs/plans/backlog.md). The client-site delivery
> plan — a signed release bundle around a digest-pinned image — is in
> [`docs/plans/packaging.md`](docs/plans/packaging.md).
>
> Start with [`AGENTS.md`](AGENTS.md), then [`docs/index.md`](docs/index.md).

---

## The idea

A CCTV installation should be a **configuration exercise, not a software
engineering exercise**. The same package must work at a primary home, a parents'
home, a business, or a future location — with only location-specific
configuration changing.

```text
New machine
    │
    ├── Docker
    ├── Repository
    └── Configuration
          │
          ▼
      ./install.sh
          │
          ▼
    Complete CCTV Stack
```

## Quickstart

```bash
git clone <repository> cctv-stack
cd cctv-stack
./install.sh
```

The first run stops and asks, because two things are values only you know: your camera RTSP
URLs (in `.env`) and your camera names (in `config/cameras.yml`). Run it again and it
validates, builds, generates Frigate's config from yours, starts the stack, and waits until
Frigate answers.

Clips land at `./data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`, each with a `.json` sidecar
carrying the review it came from, its event ids, coverage and hash.

A clip appears **after** the visit ends, not during it: Frigate keeps counting activity
until the person has been gone for the alert cutoff. There is no partial clip by design —
cutting an open review would capture half a visit with no way to extend it later.

## Published artefacts

Every merge to `main` or `master` publishes a multi-architecture image to GHCR:

| Tag | Meaning |
|---|---|
| `ghcr.io/aravindmyd/cctv-stack:edge` | The current branch build; moves on every merge |
| `ghcr.io/aravindmyd/cctv-stack:sha-<short>` | Immutable per commit |
| `ghcr.io/aravindmyd/cctv-stack:<version>` | Published from a `v*` tag |

The Python package is also published to PyPI on a `v*` tag:

```bash
pip install cctv-stack
```

That installs the `cctv-clip-worker`, `cctv-config`, `cctv-uploader` and `cctv-cleanup`
entry points. It does **not** install Frigate, Docker Compose, or a site configuration, so
it is a developer package rather than a complete site install. A merge build does not go
to PyPI (versions are immutable); it is available as a wheel artefact on the workflow run.
The supported client-site install is the signed release bundle described in
[`docs/plans/packaging.md`](docs/plans/packaging.md).

## What is here

| Component | State | Responsibility |
|---|---|---|
| **Frigate** | ✅ running | Camera ingest, object detection, recording, and review segmentation. Its own web UI is kept for live view and playback. |
| **clip-worker** | ✅ running | Materialises completed reviews into the canonical `clips/` tree, with metadata. |
| **state-store** | ✅ running | Durable SQLite state: cameras, clips, coverage, checksums, sweep watermark. |
| **config generator** | ✅ running | `cameras.yml` → Frigate's config. One source of truth (INV-3). |
| **uploader** | ✅ running | Uploads clips to Backblaze B2 via a storage adapter, with a persisted retry state machine. Reports `DISABLED` when no bucket is configured, and exits `78` on a half-configuration rather than pretending to work. |
| **cleanup** | ✅ local retention | Enforces local retention and never deletes a clip whose upload is not terminal-success. Cloud retention (J2) and reconciliation (J3) are not built yet. |
| **status-collector** | ⬜ not built | Aggregates component and camera health into one answer to *"is my CCTV working?"* |

### Two inputs, one worker

```text
frigate/reviews (MQTT)  ──→  bounded queue  ──┐
                                              ├──→  decide()  ──→  canonical clip
GET /api/review (sweep)  ─────────────────────┘
```

Neither alone is enough. MQTT alone loses clips whenever the broker or the worker is down,
and Frigate closes a review on restart **without publishing `end` at all**. The sweep alone
works, but delays every clip by its interval. So MQTT is the latency optimisation and **the
sweep is the guarantee** — the same review arriving twice produces one clip, not two.

## Principles

1. **The repository contains the application.** The host contains configuration.
2. **Cameras are data, not code.** Adding a camera is a config edit.
3. **Containers are disposable.** Data lives in predictable host directories.
4. **Never lose a clip.** Upload state is durable; cleanup respects it.
5. **Health is a feature, not an afterthought.**
6. **Secrets never enter the repository.**

## Documentation

- [`docs/index.md`](docs/index.md) — full documentation map and reading order
- [`docs/prd.md`](docs/prd.md) — product requirements
- [`docs/architecture/hld.md`](docs/architecture/hld.md) — high-level design
- [`docs/architecture/invariants.md`](docs/architecture/invariants.md) — rules that must never break
- [`docs/plans/roadmap.md`](docs/plans/roadmap.md) — what gets built, in what order
- [`docs/plans/backlog.md`](docs/plans/backlog.md) — what is deliberately deferred, and why
- [`docs/plans/open-questions.md`](docs/plans/open-questions.md) — undecided items

## License

Apache-2.0 — see [`LICENSE`](LICENSE) and the reasoning in
[`docs/plans/opensourcing.md`](docs/plans/opensourcing.md).
