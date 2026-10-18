# ADR-0014: Container images are pinned by digest

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`lld/09`](../architecture/lld/09-upgrade-and-versioning.md), PRD §13

---

## Context

PRD §13 requires that updating a machine be `./update.sh` and that the package be
versioned. That implies an operator can move from one known-good state to another known
state, deliberately, with a supported migration path.

Two ways to reference an image conflict with that:

```yaml
image: ghcr.io/blakeblackshear/frigate:stable     # floating tag
image: ghcr.io/blakeblackshear/frigate:0.16.1     # pinned tag
```

A floating tag means a `docker compose pull` can change the Frigate version without a
`cctv-stack` release, without the integration-contract checklist being re-run, and
without any record of what changed. The stack would then be running an untested
combination, and `./update.sh` would not be the thing that caused it.

A pinned tag is better, but tags are mutable. A registry compromise or an upstream
re-tag means the same tag resolves to different bytes, and the operator has no way to
know.

There is also a compatibility dimension specific to this project: the integration
contract documents exact Frigate API and MQTT behaviour that our pipeline depends on.
Running an unreviewed Frigate version means running against an unverified contract —
which is the source of the most severe possible failure, silent clip loss.

## Decision

**Every container image is pinned by digest in `versions.yml`, resolved for each
architecture we support. `compose.yml` consumes those digests. Tags are never used at
runtime.**

```yaml
frigate:
  version: "0.16.1"
  api_contract_verified: "0.16.1"
  images:
    amd64: ghcr.io/blakeblackshear/frigate@sha256:…
    arm64: ghcr.io/blakeblackshear/frigate@sha256:…
    tensorrt: ghcr.io/blakeblackshear/frigate@sha256:…
```

Supporting decisions:

1. **`api_contract_verified` must equal `version`** or CI fails. A Frigate bump without
   re-running the integration checklist cannot be released
2. **`update.sh` verifies digests after pulling.** If a pulled digest does not match
   `versions.yml`, the upgrade aborts. A tag that moved is not our version
3. **The running Frigate version is checked at runtime** against `versions.yml` and
   reported as `DEGRADED` (`FRIGATE_VERSION_MISMATCH`) if it differs — visible, not
   fatal, so an operator who deliberately pinned something old sees a notice
4. **Multi-arch digests are recorded per architecture**, since an amd64 digest is not
   the digest of the arm64 image
5. **Only our own image and first-party dependencies are pinned.** Base images inside
   our own Dockerfile are pinned too, so our image is reproducible
6. **Dependency versions inside our image are hash-locked**, the same principle applied
   one level down

## Consequences

### Positive

- **An upgrade changes exactly what it says it changes.** What `./update.sh` will do is
  knowable in advance, from a file, without pulling anything
- **Supply-chain drift is detected, not absorbed.** A moved tag or a substituted image
  fails the upgrade instead of silently running
- **Untested Frigate versions cannot reach an operator** through our tooling
- **Rollback becomes possible**, because the previous digest is a known value
  ([`lld/09`](../architecture/lld/09-upgrade-and-versioning.md))
- **Reproducible installs.** Two machines installed from the same `versions.yml` run
  identical bytes, which makes bug reports meaningful
- **The integration contract has a version it was verified against**, recorded in the
  same file that pins the image — so the two cannot drift apart silently
- **SBOM and scanning are meaningful**, because the artefact identity is stable

### Negative / accepted costs

- **Digests are opaque.** `sha256:9a3f…` tells a human nothing. Mitigated by
  `versions.yml` pairing each digest with a readable version, and by an update command
  that reports "Frigate 0.16.1 → 0.16.2"
- **A release is required to pick up an upstream security patch.** This is the sharpest
  cost: a CVE fix in Frigate or in a base image cannot reach operators without a
  `cctv-stack` release. Mitigated by making patch releases cheap and automated, and by
  a documented emergency path for operators who must move faster than our release
  cadence
- **Multi-arch multiplies the bookkeeping.** Every image needs a digest per
  architecture and per Frigate variant (`-tensorrt`, `-rocm`, `-standard-arm64`, …)
- **Digests must be refreshed** whenever upstream publishes. This is release-process
  work, and it is automated in CI or it will not happen
- **Operators cannot easily pin their own version** without understanding digests. The
  documented path is to pin a `cctv-stack` version, not an individual image
- **A registry outage with digest pinning is no worse than with tags**, but a registry
  that garbage-collects an old digest breaks old versions permanently. Mitigated by
  pinning to released versions and by documenting a local image archive for long-lived
  offline sites

### Neutral

- The unmodified `compose.yml` is not directly runnable without `versions.yml` being
  resolved; the installer resolves digests into the compose environment at install and
  update time. A developer can still run a tag-based override for local work
- Digest pinning is orthogonal to *what* is pinned; our own image is pinned the same
  way as third-party ones

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Floating tags (`:stable`, `:latest`)** | Simplest, and it means operators get updates automatically. Rejected because it makes the running version unknowable without asking the daemon, and because it silently changes a component our contract depends on. `latest` in particular makes an accidental `pull` a version bump |
| **Pinned tags (`:0.16.1`)** | Readable, and it is what most projects do. Rejected because tags are mutable: a re-tag or a registry compromise silently changes the bytes behind a version the operator believes they are running. Digests are strictly stronger for the same bookkeeping cost |
| **Pinned tags plus a checksum check** | Nearly equivalent to digests, with readable identifiers. Rejected as equivalent work for a weaker guarantee, and Compose supports digests natively |
| **Let the operator choose** | Some flexibility. Rejected because the default must be safe, and because an operator who has changed the pin has left the tested configuration — which should be a deliberate, visible act rather than a default |
| **A private registry mirror with immutable tags** | Strong guarantees and local caching for offline sites. Rejected as disproportionate infrastructure for a self-hosted project; noted as a sensible operator-side option for large deployments |
| **Vendor the images into the repository** | Most reproducible. Rejected: hundreds of megabytes in git, and it makes upstream security patches even harder to deliver |

## Revisit if

- The release overhead makes security patches too slow to deliver, which would argue
  for automated digest-refresh releases on a schedule
- Upstream becomes unreliable enough that vendoring or a local mirror becomes the
  responsible default
- Compose or the registry ecosystem gains a mechanism that provides immutability with
  readable identifiers and no bookkeeping cost
