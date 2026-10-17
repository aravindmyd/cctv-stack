# ADR-0020: Ship a signed release bundle around a digest-pinned image

- **Status:** Proposed
- **Date:** 2026-10-17
- **Deciders:** project owner
- **Related:** [`lld/07`](../architecture/lld/07-installer-and-host-profiles.md),
  [`lld/09`](../architecture/lld/09-upgrade-and-versioning.md),
  [ADR-0014](0014-pinned-image-digests.md), [ADR-0018](0018-uv-as-toolchain.md),
  [`plans/packaging.md`](../plans/packaging.md)

---

## Context

The PRD's install story is `git clone && ./install.sh`. That is the right path for a
developer and the wrong path for a client site. At a client site:

- there is no source checkout to keep, and no reason for one;
- the operator may not have git, a C compiler, or Python on the host;
- outbound access may be limited to a registry mirror, or absent entirely;
- "which version is installed?" must be answerable without reading a git log;
- a tarball downloaded over the internet must be verifiable before it runs as root;
- the same release must run on an amd64 mini-PC and an arm64 Raspberry Pi.

The design already fixes the hard parts elsewhere: one image with several entrypoints
([`lld/00`](../architecture/lld/00-overview.md)), images pinned by digest
([ADR-0014](0014-pinned-image-digests.md)), and a versioned upgrade path
([`lld/09`](../architecture/lld/09-upgrade-and-versioning.md)). What is missing is the
**unit that is handed to a site** and the decision of what is allowed inside it.

## Decision

**A release consists of two artefacts, both produced only by CI from a tagged commit:**

1. **The image**, `ghcr.io/<owner>/cctv-stack:<version>`, for `linux/amd64` and
   `linux/arm64`, published with build provenance and an SBOM. It is referenced at
   runtime by digest, never by tag ([ADR-0014](0014-pinned-image-digests.md)).
2. **The release bundle**, `cctv-stack-<version>.tar.gz`, about a hundred kilobytes,
   containing `compose.yml`, the installer and lifecycle scripts, `versions.yml`, the
   example configuration, the operator docs, `LICENSE`, and `SHA256SUMS`.

Supporting decisions:

1. **The bundle contains no site configuration and no secrets.** `.env` and
   `cameras.yml` are created at the site by the installer, from `.env.example` and
   `cameras.example.yml`. A release artefact that carries a customer's camera password
   is a leak waiting for a support ticket.
2. **The bundle is signed and published with checksums.** `SHA256SUMS` plus a
   Sigstore/cosign signature over it (keyless OIDC in CI). The install runbook says
   verify, then extract, then execute — in that order.
3. **`versions.yml` travels in the bundle and is the manifest of the running system.**
   It records the cctv image digest per architecture, the Frigate digest per
   architecture, the other dependency digests, and the schema versions. `install.sh`
   and `update.sh` read it; they never resolve a tag themselves.
4. **Three delivery modes are supported, selected by the site's network, not by a
   different build:**
   - **connected** — pull the image from GHCR by digest;
   - **mirrored** — pull the same digest (or a verified copy of it) from a site-local
     registry;
   - **air-gapped** — `cctv-stack-<version>-offline-<arch>.tar.zst`, an image archive
     loaded with `docker load`, still verified against `versions.yml`.
5. **`git clone` remains supported for development and contribution**, but it is not
   the documented client-site path. Development may move ahead of a release; a site
   must not.
6. **One tag, one artefact set.** A release is cut by pushing a `v<semver>` tag that
   points at a commit whose CI is green. Nothing is uploaded from a workstation.

## Consequences

### Positive

- **The site installs a known set of bytes**, and can answer "what is running?" from
  `versions.yml` without network access.
- **Verification happens before execution**, so a corrupted or substituted download
  fails closed rather than installing.
- **Air-gapped and mirrored sites are first-class**, not an afterthought bolted on when
  the first such client appears.
- **No secrets, paths, or hostnames enter the distribution path**, which keeps
  [INV-2](../architecture/invariants.md) true for the artefact as well as the repo.
- **The same image is tested and shipped**, so packaging bugs (a missing migration,
  a missing entrypoint) are caught by the unit/e2e jobs rather than by a customer.

### Negative / accepted costs

- **A second artefact to test.** The bundle can be wrong while the image is right
  (missing file, stale `compose.yml`), so installation from the bundle needs its own
  test on a clean host.
- **Signing adds CI complexity** and depends on the identity provider being available.
  A failed signing step blocks a release even when the code is fine.
- **Offline archives are large** — dominated by Frigate, in the gigabyte range. A
  site that wants offline delivery pays for that download once.
- **Two architectures double the build and the verification surface.** arm64 is not a
  cross-compile; it is a real build that must be smoke-tested.
- **The bundle's scripts are executed as root by an operator**, so their correctness and
  their verification chain matter more than their size suggests.

### Neutral

- GHCR is the default registry because it is already where the repository lives and
  supports digest references and OIDC signing. The design does not depend on GitHub
  beyond the release step.
- `docker save` / `docker load` is sufficient for an offline site because the runtime
  already requires Docker Compose v2.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **`git clone` only** | Requires git and a build toolchain on the host, has no signed artefact, and makes "which version is this?" a git question. Fine for development, wrong for a handover. |
| **Image only, no bundle** | The image cannot carry `compose.yml`, the installer, or `versions.yml` in a form an operator can read before running it, and publishing those separately invites version skew. |
| **Distro packages (`deb`/`rpm`)** | Two packaging formats, neither of which solves the container image pull, and both couple the release to a distribution's Python/Docker versions. |
| **Helm / Kubernetes manifests** | The target is a single host with Docker Compose, not a cluster. It adds a control plane the site does not have. |
| **Ship a pre-installed appliance image** | Requires owning hardware, updates, and returns. It is a business decision, not a packaging shortcut, and it does not serve the sites that already have a mini-PC. |
| **A `curl \| bash` one-liner as the only path** | Convenient, but it executes whatever the server sends and hides the verification step. A downloadable, verifiable bundle is the honest version. |

## Revisit if

- GHCR stops supporting digest references or keyless signing, or becomes unavailable in
  a target market.
- A third architecture (or Windows containers) becomes a real requirement.
- The offline archive becomes the common case rather than the exception — then a
  first-class mirror/sync tool may be worth building.
- The bundle grows beyond a few megabytes, which would mean something other than
  scripts and docs has been put inside it.
