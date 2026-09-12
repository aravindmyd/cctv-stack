# ADR-0001: Use Frigate as the detection and recording engine

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [HLD §2](../architecture/hld.md), [`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md)

---

## Context

The PRD describes a CCTV system with object detection over RTSP camera streams,
local recording with retention, event clips, and a review UI. It also describes
packaging, cloud backup, and monitoring — which is where the actual differentiation
lives. PRD §1 explicitly asks for the project to be treated as "a single deployable
application", and §15 states the goal is that deployment be "a configuration
exercise, not a software-engineering exercise".

Building detection, recording, and a review interface ourselves would mean
implementing RTSP ingest, hardware-accelerated decode, a detection pipeline across
CPU/TPU/GPU/NPU backends, object tracking, zone logic, a recording segmenter with
retention, and a review UI. That is years of work with a deep hardware-support
surface, and none of it is what makes this project worth building.

Frigate is a mature open-source NVR that already solves exactly that, with broad
hardware support: Coral Edge TPU, Hailo, OpenVINO on Intel CPU/GPU/NPU, ONNX on
ROCm/TensorRT, RKNN on Rockchip, and Apple Silicon via an external detector client.
It exposes an HTTP API, MQTT event streams, and its own UI.

## Decision

**We will use Frigate as the detection, recording, review, and live-view subsystem,
behind a documented contract, and we will not reimplement any of it.**

Our services provide the guarantees Frigate deliberately does not:

| Our concern | Why Frigate does not solve it |
|---|---|
| Canonical, deterministic clip layout | Frigate does not store event clips as files at all |
| Durable cloud backup with a state machine | Out of scope for Frigate |
| Independent local and cloud retention | Frigate manages its own recordings only |
| Aggregate health across the whole stack | Frigate reports on itself |
| Multi-architecture, one-command packaging | Frigate ships a container, not an installation |

The integration surface is confined to one document
([`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md)),
one pinned version, and one adapter module. No other file may assume Frigate
behaviour.

## Consequences

### Positive

- Years of development avoided, and a hardware-support matrix we could not match
- Frigate's own web UI covers live view, playback, review browsing, and exports, so
  we do not build a UI
- Active upstream project: improvements to detection quality arrive for free
- A well-understood, widely deployed dependency that operators may already know

### Negative / accepted costs

- **A hard external dependency with its own release cadence.** A Frigate change can
  break us, so its version is pinned by digest and every bump requires re-running the
  integration checklist and shipping a MINOR release ([ADR-0014](0014-pinned-image-digests.md))
- **We inherit Frigate's failure modes**, including the fact that event clips are cut
  on demand from recordings, which means its recording retention can expire footage
  we have not yet copied ([INV-9](../architecture/invariants.md))
- **Deployment size.** Frigate is a large image with its own model cache; this is the
  dominant container on the host
- **Constrained by upstream design.** When Frigate's data model does not fit (for
  example, no stored clips), we work around it rather than changing it
- We must maintain the contract document as Frigate's API evolves

### Neutral

- Frigate's own recordings and snapshots are its property; we never read its storage
  directly, only its API. This keeps us insulated from its on-disk layout, which has
  changed across versions

## Alternatives considered

| Alternative | Why not |
|---|---|
| Build detection and recording ourselves | Months to years of work to reach parity on hardware support alone, with none of the differentiation the PRD asks for |
| Use a different NVR (ZoneMinder, Shinobi, Blue Iris) | Weaker object detection, less hardware acceleration, or Windows-only. Frigate's API and MQTT event stream are the best integration surface available |
| Use Frigate *only* as a detector, with our own recording | We would reimplement the segmenter and review logic, and lose the UI. The one genuinely awkward part (no stored clips) is manageable via the API |
| Wrap Frigate in a thin Compose file and stop there | Does not satisfy PRD §7 (canonical clips), §9 (durable upload), §10 (independent retention), or §11 (aggregate health) — i.e. most of what the PRD asks for |

## Revisit if

- Frigate is abandoned or its licence changes
- Frigate begins storing clips as files with a configurable layout, at which point the
  clip-worker's download step may become unnecessary
- Frigate's API changes in a way the adapter cannot absorb
- The project's goals shift toward self-contained detection (for example, a
  requirement to run without a large container)
