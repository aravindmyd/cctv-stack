# cctv-stack

A packaging-first, self-hosted CCTV system. Clone it, configure it, run one
script, and you have a working system with local recording, event clips, cloud
backup, retention, and health monitoring.

> **Status: 📐 Planning.** The design is being written before any application
> code. Start with [`AGENTS.md`](AGENTS.md), then [`docs/index.md`](docs/index.md).
>
> Nothing in this README is implemented yet. It describes the target experience.

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

## Target quickstart

```bash
git clone <repository> cctv-stack
cd cctv-stack

./install.sh          # guided setup: detects host, asks for cameras, writes config
```

`install.sh` validates prerequisites, detects the host profile (architecture,
GPU, accelerator, storage), interviews you for cameras and credentials, generates
all configuration, starts the stack, waits for health, and prints the URLs.

## What you get

| Component | Responsibility |
|---|---|
| **Frigate** | Camera ingest, object detection, recording, review. Its own web UI is kept for live view and playback. |
| **clip-worker** | Materialises completed events into the canonical `clips/` tree with metadata. |
| **state-store** | Durable SQLite state: clips, upload queue, retries, health snapshots. |
| **uploader** | Uploads clips to Backblaze B2 via a storage adapter, with a persisted retry state machine. |
| **cleanup** | Enforces local (90d) and cloud (60d) retention. Never deletes un-uploaded clips. |
| **status-collector** | Aggregates component + camera health into one answer to *"is my CCTV working?"* |
| **Uptime Kuma** | The monitoring dashboard and alerting (ntfy, Telegram). We do not build a UI for this. |

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
- [`docs/plans/open-questions.md`](docs/plans/open-questions.md) — undecided items

## License

Not yet chosen — see [`docs/plans/opensourcing.md`](docs/plans/opensourcing.md).
