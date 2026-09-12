# ADR-0011: Custom services are Python, shipped as one image with four entrypoints

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`lld/00`](../architecture/lld/00-overview.md)

---

## Context

Four services must be built: clip-worker, uploader, cleanup, and status-collector.
They share an internal library — configuration, database access, the clip path
function, time handling, health, logging, the storage adapter, and the Frigate clients.

Their characteristics:

- **I/O-bound.** HTTP calls to Frigate, MQTT messages, S3 uploads, SQLite queries.
  Very little CPU work, and the only genuinely CPU-heavy operation (`ffmpeg` remux) is
  a subprocess.
- **Low throughput.** A few hundred operations a day on a busy site.
- **Long-running daemons** with small HTTP endpoints for health and admin.
- **Deployed on a Raspberry Pi as well as x86.** Memory and image size matter.
- **Must be maintainable by an open-source contributor**, which makes ecosystem and
  familiarity significant considerations.

The owner's guidance: *"You can use Python, most reliable, which is close to latest
versions."*

## Decision

**All four services are Python 3.12+, share one internal `cctv` library, and ship as a
single container image with four entrypoints.**

```text
ghcr.io/<owner>/cctv-stack:<version>
  /usr/local/bin/cctv-clip-worker
  /usr/local/bin/cctv-uploader
  /usr/local/bin/cctv-cleanup
  /usr/local/bin/cctv-status
```

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Best fit for the I/O-bound, integration-heavy work; the ecosystem for MQTT, S3, HTTP, and YAML is mature |
| HTTP framework | FastAPI + uvicorn | Typed request/response models; automatic OpenAPI for the admin endpoints; the standard choice |
| Async | `asyncio` with `asyncio.to_thread` for blocking work | One process per service; no GIL contention concern at this I/O profile |
| Media inspection | `ffprobe` / `ffmpeg` as subprocesses | Never reimplement media handling; never link a media library |
| Packaging | One image, four entrypoints | See below |
| Dependency pinning | Locked with hashes | Reproducible builds; supply-chain hygiene |
| Style | `ruff` for lint and format, `mypy` in strict mode for the library | Type errors in the shared library affect four services |

### Why one image and not four

| | One image, four entrypoints | Four images |
|---|---|---|
| Shared library | Bundled once | Bundled four times |
| Build time | One build (multi-arch: two) | Four builds (multi-arch: eight) |
| Image scanning | One image | Four |
| Digest pinning | One digest | Four |
| Arm64 CI cost | One build | Four |
| Isolation | Still four processes, four mounts, four health endpoints, four resource limits | Same |

Isolation is provided by **compose**, not by the image: separate services, separate
UIDs where possible, separate read-only mounts, separate health endpoints, separate
CPU and memory limits. Sharing an image costs nothing in isolation and saves
substantially in build, scan, and release overhead — which matters most for a project
intended to be open-sourced and released frequently.

### Why one shared library and not four implementations

Because two of the invariants are only enforceable with a single implementation:

- [INV-5](../architecture/invariants.md) — one clip path function. The moment
  clip-worker and cleanup have their own, they diverge, and divergence means cleanup
  cannot find clips it should be deleting.
- [INV-12](../architecture/invariants.md) — one time policy.

The same argument applies to configuration loading, the database policy, health
derivation, and log redaction.

## Consequences

### Positive

- **Fastest path to a correct system.** MQTT, S3, HTTP, SQLite, YAML, and subprocess
  handling are all one import away, with no plumbing to write
- **The library holds the invariants in one place**, so they are enforced rather than
  documented
- **Contributor-friendly.** Python is the most widely understood language among people
  likely to contribute to a self-hosted CCTV project, and it matches Frigate's own
  ecosystem, so the mental model transfers
- **`ffmpeg` and `ffprobe` are already present** in any image derived from Frigate's
  base, and are trivially available otherwise
- **One image means one digest to pin, one SBOM, one CVE scan, one multi-arch build**
- **Rich typing** with FastAPI, Pydantic, and `mypy` gives schema-level guarantees on
  the health and status contracts

### Negative / accepted costs

- **Larger image than a static Go binary**, and a higher memory floor. On a Raspberry
  Pi this is measured in tens of megabytes, which matters but is not decisive at 8 GB
- **No single-binary distribution.** Deployment is Docker-only, which is already the
  constraint from PRD §4
- **The GIL.** Irrelevant for I/O-bound work with subprocesses, but it does mean that a
  future CPU-heavy in-process task would need `multiprocessing`
- **Dependency surface.** Python's dependency ecosystem is large and needs pinning,
  scanning, and periodic updates. Mitigated by hash-locked requirements and Dependabot
- **Slower startup** than a compiled binary, which slightly lengthens the installer's
  verification phase
- **Three of four services share a process image**, so a vulnerability in the shared
  dependency set affects all four. It would affect four images built from the same
  library anyway

### Neutral

- The four entrypoints are thin: argument parsing, then the shared service skeleton
- A service could later be extracted into its own image without changing the library

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Go** | Better fit for a single static binary, lower memory, and genuine concurrency. Rejected because the work is integration glue — MQTT, S3, YAML, HTTP, subprocesses — where Python's ecosystem and contributor familiarity are worth more than binary size and a few tens of MB of RSS on a machine that has gigabytes |
| **Node / TypeScript** | Comparable ecosystem quality, and excellent for HTTP services. Rejected because MQTT and S3 clients are less mature than Python's, and because the media and YAML handling we need is better in Python |
| **Rust** | Best runtime characteristics by a distance. Rejected on development velocity for a project with this much integration surface, and on contributor accessibility for an open-source project |
| **Four separate images** | More isolation in theory, four times the build, scan, and pinning cost in practice, with no isolation benefit that compose does not already provide |
| **One process running all four services** | Fewer moving parts and no shared-database concurrency at all. Rejected because it collapses the failure domains: a crash in the uploader would stop clip materialisation, and a cleanup bug would take down ingestion. The four-service split exists so that any one of them can be down without permanent loss |
| **A shell-script and cron implementation** | Closest to the PRD's "no cron jobs" complaint, which is what this project is trying to eliminate. Cron gives no state machine, no health, no retries, and no observability |

## Revisit if

- Memory or image size proves genuinely problematic on constrained hardware, which
  would argue for a Go or Rust rewrite of the highest-value service first
- A service becomes CPU-bound in-process, requiring `multiprocessing` or a rewrite
- Contributor feedback shows Python is a barrier rather than an asset
