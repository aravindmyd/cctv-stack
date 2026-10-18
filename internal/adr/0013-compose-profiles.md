# ADR-0013: One `compose.yml` with profiles, not a compose file per configuration

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`lld/07`](../architecture/lld/07-installer-and-host-profiles.md), PRD §4

---

## Context

PRD §4 makes Docker Compose the deployment boundary: `docker compose up -d` should
bring up the entire application, and `docker compose down` should stop it cleanly.

But the stack is genuinely variable:

- Monitoring (a dashboard container) is optional but on by default
- Hardware acceleration differs per host: Intel iGPU, Coral USB, Coral PCIe, Hailo,
  Rockchip NPU, Nvidia — each needing different device mappings and environment
- Development and CI need a synthetic camera source instead of real cameras
- Some deployments want an RTSP restream or WebRTC; most do not
- Some deployments want no cloud backup and therefore arguably no uploader

The naive approaches to variability are:

1. **One compose file per combination** — `compose.intel-coral.monitoring.yml`. The
   combination count is multiplicative and the files drift.
2. **A single compose file with everything enabled** — device mappings for hardware
   that is not present cause hard failures on start.
3. **Generate the compose file** — maximum flexibility, but then compose is a build
   artefact rather than a readable, reviewable, hand-editable document.

## Decision

**Use a single tracked `compose.yml` with Compose profiles. The installer sets
`COMPOSE_PROFILES` in `.env` based on the confirmed host profile. No compose file is
ever generated.**

| Profile | Adds | Default |
|---|---|---|
| `core` | frigate, mosquitto, clip-worker, uploader, cleanup, status-collector | **always on** |
| `monitoring` | the monitoring tool | on |
| `hwaccel-intel` | `devices: [/dev/dri]`, VAAPI/OpenVINO env | per host |
| `hwaccel-coral` | Coral USB/PCIe passthrough, `apex` device | per host |
| `hwaccel-hailo` | `/dev/hailo0`, Hailo group access | per host |
| `hwaccel-rknn` | `/dev/rknpu` | per host |
| `hwaccel-nvidia` | `runtime: nvidia`, capability mappings | per host |
| `dev-synthetic` | synthetic RTSP camera source | off |
| `restream` | Frigate RTSP/WebRTC published ports | off |

`.env` ends up with a single line, generated from the host profile:

```env
COMPOSE_PROFILES=core,monitoring,hwaccel-intel
```

## Consequences

### Positive

- **The deployment boundary stays one readable file**, which is what PRD §4 asks for.
  A reviewer or contributor can read the whole topology in one place
- **No generated compose file**, so there is no third artefact to keep in sync and no
  class of bug where the generator and the file disagree
- **The operator can hand-edit `compose.yml`** for a one-off need, and the change is
  visible in git, reviewable, and not overwritten by the next update
- **Hardware profiles are additive and independent.** Enabling the Coral profile does
  not require reasoning about the Intel profile
- **`COMPOSE_PROFILES` is one generated line**, so the variability lives in
  configuration rather than in structure
- **Development and CI use the same file as production**, with `dev-synthetic` instead
  of real cameras — so tests exercise the real topology
- **Compose v2 profiles are native**, require no tooling, and are understood by anyone
  who knows Compose

### Negative / accepted costs

- **All profiles live in one file, so the file grows.** Every hardware variant's
  service-level overrides are visible even when not enabled. Mitigated by grouping
  profile-specific blocks with comments
- **Some duplication across hardware profiles**, since each needs similar
  device-mapping overrides
- **Profiles cannot express conditional logic.** A "profile that requires another" is
  not a thing; the installer must select a coherent set. A malformed
  `COMPOSE_PROFILES` value is validated by the installer rather than by compose
- **Compose profile semantics can surprise**: a service with no profile is always
  started, and `depends_on` across profile boundaries behaves in ways that need care.
  Documented and tested
- **The optional-uploader case is awkward.** A local-only deployment still runs the
  uploader, which reports `SKIPPED_BY_POLICY` and does nothing. Accepted: one idle
  container is cheaper than a profile that changes the pipeline's shape and therefore
  its failure modes

### Neutral

- `dev-synthetic` is a real profile, not a special case, so CI exercises the same
  topology as production
- The installer validates that the selected profiles are coherent for the detected
  host before starting anything

## Alternatives considered

| Alternative | Why not |
|---|---|
| **One compose file per hardware combination** | Combinatorial: hardware × monitoring × restream × dev, and every combination must be maintained and tested. Files inevitably drift, and PRD §4's "one boundary" is lost |
| **A single compose file with all hardware devices mapped** | Simplest to write, and it fails hard on start when a device is absent — turning a missing Coral into a stack that will not boot. Unacceptable for a product meant to install cleanly on unknown hardware |
| **Generate `compose.yml` from a template** | Maximum flexibility, and it makes compose a build artefact. Rejected because compose is the one artefact PRD §4 elevates to the deployment boundary, and a boundary that is generated is a boundary nobody reads or reviews |
| **`docker-compose.override.yml` per host** | The idiomatic Compose way to layer local changes, and genuinely reasonable. Rejected as the primary mechanism because overrides merge implicitly and invisibly — debugging "why is this container different" becomes a merge-resolution exercise. Profiles are explicit and declarative in one file |
| **Separate compose projects per component** | More isolation, but `docker compose up -d` would no longer start the whole application, breaking PRD §4 and §15 |
| **Kubernetes or Nomad** | Explicitly out of scope in PRD §4. Enormous operational cost for a single-host deployment |
| **Systemd units instead of Compose** | Removes a dependency on Docker, and gives native hardware access and restart semantics. Rejected because Compose is what PRD §4 specifies, and because the container boundary is what makes the stack portable across hosts and architectures |

## Revisit if

- Compose profiles prove insufficient — for example if a future service genuinely
  cannot be expressed as always-on or profile-gated
- The single file grows large enough that its readability advantage is lost, which
  would argue for a documented split along clear boundaries (for example hardware
  profiles in a second file included explicitly)
- Compose v2 gains a feature (conditional includes, profile dependencies) that makes a
  different structure strictly better
