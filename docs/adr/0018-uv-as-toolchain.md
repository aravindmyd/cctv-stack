# ADR-0018: uv is the development toolchain, and is never installed at runtime

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`lld/00-overview.md`](../architecture/lld/00-overview.md) §2.2, [`lld/07`](../architecture/lld/07-installer-and-host-profiles.md), [INV-4](../architecture/invariants.md)

---

## Context

The project has one Python package, four services, and — for now — a single runtime
dependency (`tzdata`). That is a small surface, so the toolchain choice is not about
resolving an awkward dependency graph. It is about three other things:

1. **Reproducibility.** This project pins container image digests in `versions.yml` and
   promises in [INV-8](../architecture/invariants.md) that upgrades never destroy state.
   A test environment that resolves differently on two machines undermines the confidence
   that claim rests on.
2. **Onboarding cost.** The project is intended to be open-sourced. Every contributor who
   has to work out which virtualenv is active, or that `python3` is 3.9 on their machine,
   is a contributor who bounces.
3. **The image must not depend on it.** [LLD 00](../architecture/lld/00-overview.md) §2.2
   settles on one image with four entrypoints, and [INV-4](../architecture/invariants.md)
   requires the image to be disposable and stateless. A tool that creates or resolves an
   environment at container start-up would violate that — and would put a network round
   trip in front of the first clip of the day.

## Decision

**uv is the package manager and task runner for development, CI, and the container build
stage. It is never present in the runtime image, and no entrypoint invokes it.**

| Concern | Tool |
|---|---|
| Creating and syncing the development environment | `uv sync --frozen` |
| Running tests and linters | `uv run pytest`, `uv run ruff check` |
| CI | `uv sync --frozen` then `uv run …` |
| Installing dependencies in the **build stage** of the Dockerfile | `uv sync --frozen --no-dev` |
| **Starting a service in the container** | `python -m cctv.<service>` — never `uv run` |

Supporting decisions:

1. **`uv.lock` is committed.** CI builds with `--frozen`, so a lock that disagrees with
   `pyproject.toml` is a *failure*, not a silent re-resolve.
2. **`pyproject.toml` stays plain PEP 621.** No uv-only syntax in the metadata, so pip,
   Poetry and PDM can still read it. The dev dependencies are declared twice — once under
   `[dependency-groups]` (PEP 735, what uv uses) and once under
   `[project.optional-dependencies]` — so that `pip install -e .[dev]` keeps working.
   Duplication is the price of a fallback that actually functions.
3. **The pip fallback is tested in CI**, on one job. A fallback that is never exercised is
   a fallback that is broken the day someone needs it.
4. **`required-version` is pinned in `[tool.uv]`.** uv moves quickly; a version floor makes
   an incompatible uv produce a clear error rather than a confusing lockfile diff.
5. **uv manages the interpreter.** `requires-python = ">=3.12"` is enforced by uv, which
   removes the "my system Python is too old" failure mode entirely. The runtime image
   pins its own Python independently — uv's choice is irrelevant there.

## Consequences

### Positive

- **One tool replaces four** (venv, pip, pip-tools, and a task runner), and `uv run`
  removes the "which environment am I in" class of onboarding failure.
- **Reproducible test environments** across machines and CI, from a committed lockfile.
- **Fast CI.** The install step is the part of a Python CI pipeline most worth shortening.
- **No runtime coupling.** The image's entrypoints are plain `python -m` invocations, so
  nothing about how the code is *developed* can affect how it *runs*.

### Negative / accepted costs

- **Contributors without uv need a documented fallback.** Accepted, and mitigated by
  keeping the metadata standard and testing the pip path in CI rather than merely
  documenting it.
- **uv is young and moves quickly.** Accepted, mitigated by the `required-version` floor,
  `--frozen` everywhere, and a standard `pyproject.toml` that a different tool could take
  over. The lockfile format is the least portable part, and it is one file.
- **Dev dependencies are declared twice.** Ugly. The alternative — declaring them once and
  letting the declared fallback rot — is worse.
- **A non-standard base image is added to the build stage.** Accepted: the stage is
  discarded, and the runtime image is unaffected. If the project later decides it wants a
  fully distro-based build, `pip install --require-hashes` against an exported
  `requirements.txt` is the escape hatch, and `uv export` produces one.

### Neutral

- `.venv/`, `.ruff_cache/` and `.pytest_cache/` are gitignored; `uv.lock` deliberately is
  not.
- Nothing in `docs/architecture/` other than LLD 00 §2.2 and LLD 07 mentions the toolchain,
  so this decision does not ripple. It should stay that way: no design document should
  depend on which tool authors happen to use.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **pip + venv only** | Zero new dependencies and every contributor already has it. Rejected because there is no lockfile and no interpreter management, so reproducibility depends on discipline — exactly the thing that decays once there is more than one contributor |
| **Poetry** | Mature and well known, and its `poetry.lock` is solid. Rejected on speed and on weight: resolution and environment management are slower, and it wants to own more of `pyproject.toml` than PEP 621 needs to give up |
| **PDM** | Capable, PEP-standard-first, and a reasonable answer. Rejected only on adoption — fewer contributors will already have it, which is the problem we are trying to solve |
| **uv everywhere, including at runtime** | Simplest to document: one tool, no distinction to explain. Rejected outright — resolving or syncing inside the container makes start-up depend on the network and writes to the image filesystem, which [INV-4](../architecture/invariants.md) forbids |
| **No dev tooling at all; Docker only** | Consistent with "the container is the product". Rejected because the fastest feedback loop is `pytest` on the host in under a second, and the foundation modules — `paths`, `timeutil` — need no Frigate, no Docker and no network to test |

## Revisit if

- uv is abandoned or its licence changes
- The project needs system packages that uv cannot install, at which point the build stage
  becomes distro-driven and uv's role narrows to the development environment
- A contributor's platform turns out not to be supported, which is the one failure mode the
  pip fallback does not fully cover
