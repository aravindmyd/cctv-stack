# ADR-0012: The installer detects host capability and asks the operator to confirm

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`lld/07`](../architecture/lld/07-installer-and-host-profiles.md), PRD §3, PRD §15

---

## Context

PRD §3 asks for an extremely simple installation; PRD §15's end state is *"Install
Docker → clone → create .env → ./install.sh"*.

But the machines in play are genuinely different. The owner's own hardware: a
Raspberry Pi, a Windows machine, and an ARM Mac. Across supported deployments the
differences that matter are substantial:

| Difference | Consequence if got wrong |
|---|---|
| Architecture (amd64 / arm64) | The image will not start |
| Accelerator (Coral, Hailo, Intel iGPU/NPU, Rockchip NPU, none) | Frigate needs a different `detectors:` block; a wrong choice fails explicitly or, worse, silently falls back to CPU |
| Video decode acceleration (VAAPI/QSV/NVENC) | Without it, CPU is consumed decoding, starving detection |
| Available RAM | `/dev/shm` sizing; Frigate frame buffers live in shared memory |
| Filesystem and free space | Retention feasibility; whether atomic rename and locking work at all |
| Docker runtime (native, WSL2) | Device passthrough, path semantics, performance |

Requiring the operator to know these is incompatible with PRD §15. Guessing from a
template produces a system that starts, looks configured, and quietly performs badly —
the worst possible outcome, because nothing indicates a problem.

## Decision

**The installer detects the host, states what it found in plain language, recommends a
profile, and asks the operator to confirm before proceeding. It never silently assumes,
and it never requires the operator to understand the internals.**

```text
Detect → propose → confirm → apply profile → generate config
```

Supporting decisions:

1. **Detection is a separate script producing JSON** (`scripts/detect-host.sh`), so it
   is testable against recorded fixtures and reusable by `doctor.sh`
2. **Detection degrades to `unknown`, never to a confident wrong answer.** If
   support for a capability cannot be confirmed, the profile does not claim it
3. **The proposal is always shown, always with reasoning**, including what the profile
   limits (camera count, resolution, framerate)
4. **Warnings accompany the mode, not just the choice.** A Raspberry Pi with no
   accelerator gets an explicit statement that CPU detection will be slow — so the
   operator's expectation is set before they are disappointed
5. **The operator can always override**, and the override plus a reason is recorded in
   `system_events`
6. **Unsupported platforms are refused with an explanation**, not attempted
7. **The profile is stored** in `config/host.yml` and consumed by the config generator,
   so detection happens once and is reviewable
8. **A non-interactive mode** (`--yes`) exists for automation, defaulting to the
   recommended profile

### Profile shape

```yaml
# config/host.yml (generated)
arch: arm64
profile: pi-cpu
detected:
  model: "Raspberry Pi 5 Model B Rev 1.0"
  memory_bytes: 8589934592
  accelerators: []
  dri_present: true
detector:
  type: cpu
  num_threads: 2
hwaccel_args: null
shm_size: 256mb
limits:
  max_cameras: 2
  detect_width: 640
  detect_height: 360
  detect_fps: 3
```

## Consequences

### Positive

- **One command works on genuinely different hardware**, which is the PRD's core goal
- **Bad configurations become visible at install time** rather than as mysterious
  slowness weeks later
- **The operator learns something about their machine**, in language they can act on
- **The profile is an artefact**, so "why is this machine configured this way?" has a
  file to read rather than a memory to trust
- **Testable**: detection against recorded `/proc`, `lsusb`, and `lspci` fixtures covers
  hardware we do not own, which matters for a project supporting a Pi, Windows, and a
  Mac
- **The refusal path is honest.** An unsupported platform gets a clear answer instead
  of a partial install

### Negative / accepted costs

- **Detection is imperfect and will be wrong on unfamiliar hardware.** Mitigated by the
  confirmation step, which is what makes imperfection acceptable — the operator is the
  backstop
- **An extra confirmation step** for an operator who wants a one-liner. Mitigated by
  `--yes`, and by the prompt defaulting to the recommended profile so Enter proceeds
- **A profile matrix to maintain.** Each new accelerator needs detection logic,
  a profile, a Frigate config fragment, and tests. This is ongoing cost proportional to
  the hardware we claim to support
- **The Pi profile is genuinely limited**, and the installer must say so rather than
  pretend a 4-core ARM CPU can do what a Coral does. Some operators will find the
  answer unwelcome
- **Detection can be slow** (`lsusb`, `lspci`, `vainfo` on constrained hardware).
  Bounded by per-command timeouts
- **WSL2 and macOS need special-casing** that is documented rather than engineered:
  Windows via Docker Desktop is best-effort, macOS is development only

### Neutral

- Detection results are included in the diagnostics bundle, which helps support and
  helps us improve detection over time
- The profile is regenerable: `./install.sh --force` re-detects

## Alternatives considered

| Alternative | Why not |
|---|---|
| **One universal config, no detection** | Simplest installer, and it would start on everything. Rejected because the "universal" config is the CPU-detection one: an operator with a Coral would get a system performing at a fraction of its capability, with no indication that anything was wrong |
| **Ask the operator to choose a profile from a list, without detecting** | Removes the detection bugs, but requires the operator to know what a Hailo-8 is and whether they have one. Contradicts PRD §15 directly |
| **Detect silently, no confirmation** | Fastest path, and it would work most of the time. Rejected because a wrong silent choice produces a system that looks fine and performs badly — the exact failure mode this ADR exists to prevent |
| **Compose files per hardware combination** | Would replace one detection problem with a combinatorial maintenance problem, and would put the choice outside version control in the operator's head |
| **A web-based setup wizard** | Nicer interaction, but requires a running web server to configure the system that is not yet running, plus a UI to build and secure. Contradicts the "reuse, don't build UI" decision ([ADR-0010](0010-external-monitoring-tool.md)) |
| **Docker Compose `deploy.resources` and runtime auto-detection only** | Docker cannot detect a Coral or a Hailo, and image variant selection (`-tensorrt`, `-rocm`, `-standard-arm64`) is not expressible in compose alone |

## Revisit if

- Detection proves wrong often enough to erode trust, which would argue for a more
  conservative default and a more prominent override
- A much simpler universal configuration becomes viable — for example if hardware
  acceleration becomes unnecessary for typical camera counts
- The supported hardware matrix grows large enough that a community-maintained profile
  repository becomes preferable to shipping profiles in-tree
