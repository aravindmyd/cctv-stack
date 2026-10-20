# Contributing to cctv-stack

This file is for people **changing** the project. If you want to install or use the
package, read [`README.md`](README.md) first.

---

## Project status

**Current release: `0.7.0`.** This is the state the code is actually in, not the target
design.

| Capability | State |
|---|---|
| Detect → canonical clip | ✅ built |
| Upload to Backblaze B2, exactly once | ✅ built |
| Local retention (upload-aware) | ✅ built |
| Cloud retention (J2) | ✅ built — provider-confirmed deletes |
| Per-service health and metrics | ✅ built |
| Aggregate status page and alerting | ✅ built — Gatus + ntfy |
| Guided install (`./install.sh`) | ✅ built — interview, host detection, shm sizing |
| Diagnosis (`./scripts/doctor.sh`) | ✅ built |
| Backup and restore (`./backup.sh`) | ✅ built — config, database, manifest |
| Upgrade and rollback (`./update.sh`) | ✅ built — backup, migrate, verify |
| Reconciliation (J3) | ✅ built — reports drift, repairs nothing |
| Recovery from the bucket (`./restore.sh clips\|rebuild`) | ✅ built — footage back, `state.db` rebuilt |
| Housekeeping (J4) | ✅ built — directories, scratch files, old rows |
| Test notifications and signed bundles | ⬜ deferred (M6) |

What is deferred, and why, is recorded in
[`.private/plans/backlog.md`](.private/plans/backlog.md). The client-site delivery plan is
[`.private/plans/packaging.md`](.private/plans/packaging.md).

## How it fits together

```text
 cameras ──RTSP──> Frigate ──review──> clip-worker ──> clips/... ──> uploader ──> B2
                      │                                  │
                      └── recordings (recovery window)    └── cleanup (after upload)
```

Frigate does ingest, detection, recording and review segmentation. `clip-worker` cuts one
canonical clip per **review item**. The uploader pushes it once through a persisted state
machine. Cleanup deletes a local clip only after its upload is terminal-success (INV-1).

There are two inputs to the worker:

```text
frigate/reviews (MQTT)  ──→  bounded queue  ──┐
                                              ├──→  decide()  ──→  canonical clip
GET /api/review (sweep)  ─────────────────────┘
```

MQTT is the latency optimisation; the sweep is the guarantee. Frigate can close a review
on restart without publishing `end`, so the sweep is what prevents silent clip loss.

## Repository map

```text
src/cctv/                 shared library and five services
  worker.py               cctv-clip-worker
  uploader.py             cctv-uploader
  cleanup.py              cctv-cleanup
  status.py               status derivation: answer, overall state, projection, HTML
  status_service.py       cctv-status
  monitoring.py           monitoring adapter: Gatus config, monitor inventory, heartbeats
  host.py                 host detection, support matrix, shm sizing
  host_cli.py             cctv-host
  backup.py               cctv-backup: encrypted config bundle, state backup, verification
  recover.py              cctv-recover: clips back from B2, state.db rebuilt from what is left
  doctor.py               cctv-doctor: read-only diagnosis of the whole installation
  interview.py            cctv-init: the install interview (cameras.yml + .env)
  provision.py            cctv-provision: bucket, versioning, lifecycle rule, probe
  config_cli.py           cctv-config
  materialise.py          review → canonical clip
  db.py                   SQLite state machine and migrations
  storage.py              StorageAdapter + S3/B2 implementation
  health.py               /health, /metrics, /status, admin endpoints
  migrations/             schema migrations
tests/                    unit suite
tests/integration/        live S3 suite against RustFS
tests/e2e/                synthetic camera → Frigate → canonical clip
dev-synthetic/            camera-free harness used by e2e and manual experiments
docs/                     the published user site (install, operate, glossary)
internal/                 design authority: PRD, contracts, LLDs, ADRs, status
compose.yml               deployment boundary
Dockerfile                one image, several entry points
pyproject.toml            packaging, dependencies and tool configuration
```

## Where the truth lives

**The design documents in `internal/` are authoritative. Code follows docs, not the other
way round.** `docs/` is the published site and is not authoritative.

1. [`internal/index.md`](internal/index.md) — reading order and map
2. [`docs/glossary.md`](docs/glossary.md) — vocabulary
3. [`internal/architecture/hld.md`](internal/architecture/hld.md) — architecture
4. [`internal/architecture/invariants.md`](internal/architecture/invariants.md) — non-negotiables
5. [`internal/architecture/contracts/`](internal/architecture/contracts/) — exact interfaces
6. The relevant LLD in [`internal/architecture/lld/`](internal/architecture/lld/)
7. Relevant ADRs in [`internal/adr/`](internal/adr/)

[`AGENTS.md`](AGENTS.md) contains the hard rules and the change process. If a request
contradicts an invariant, stop and raise it in
[`.private/plans/open-questions.md`](.private/plans/open-questions.md) instead of coding around it.

Every non-obvious decision gets an ADR. Docs are updated in the same change as the code.

## Development

Requirements: `uv` 0.11+, Python 3.12 or 3.13, and Docker for the live suites.

```bash
uv sync
uv run pytest                    # unit suite; live tests skip without Docker/S3
uv run ruff check .
uv run ruff format --check .

./tests/integration/run.sh       # live S3 suite against RustFS; needs Docker
./tests/e2e/run.sh               # synthetic camera → real clip; needs Docker + Frigate
```

The unit suite is **717 passed, 19 skipped** without a configured S3 endpoint or a running
Docker daemon. The skips are the 18 live-storage tests and the read-only-mount check.

The e2e test is deliberately a real pipeline, not a mock: it starts `dev-synthetic`, runs
the built image as a sweep-only worker, drives one manual event through Frigate, and
asserts the canonical path and sidecar. It takes about five minutes because it pulls
Frigate and records real footage.

### Pre-commit

```bash
uvx pre-commit install
uvx pre-commit run --all-files
```

The hooks pin ruff to the lockfile version and use `python3.12` for their environment.
Without that pin, pre-commit can pick an unsupported system Python and fail to install
ruff.

### CI

| Workflow | Runs | What it proves |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | every push and PR | pre-commit, unit tests on 3.12/3.13, pip fallback, live S3 integration |
| [`e2e.yml`](.github/workflows/e2e.yml) | `main`/`master`, path-filtered PRs, manual | synthetic camera → canonical clip |
| [`publish.yml`](.github/workflows/publish.yml) | `main`/`master` and `v*` tags | image, wheel/sdist, PyPI, GitHub Release |

## Packaging and release

The container image is the runtime artefact:

```bash
ghcr.io/aravindmyd/cctv-stack:edge          # every merge to main/master
ghcr.io/aravindmyd/cctv-stack:sha-<short>   # immutable per commit
ghcr.io/aravindmyd/cctv-stack:<version>     # from a v* tag
```

`latest` is deliberately not published (ADR-0020): a site always names its version.

The Python distribution is published to PyPI from a `v*` tag only. PyPI versions are
immutable, so publishing on every merge would fail on the second merge. Per-merge builds
are available as a workflow artefact.

### Release checklist

1. Bump `version` in `pyproject.toml` and update the changelog/release notes.
2. Run `uv lock` and commit the lockfile if dependencies changed.
3. Commit to `master`; wait for `CI` and `E2E`.
4. Create an annotated tag matching the version: `v<version>`.
5. Push the tag. `publish.yml` checks that the tag equals `pyproject.toml`'s version.
6. Confirm the image, PyPI upload, and GitHub Release all succeeded.

### Two packaging traps

Both have caused a failed release before:

1. **The Docker build stage must copy `README.md` and `LICENSE`.** `pyproject.toml`
   declares both, and hatchling refuses to build the wheel without them. `.dockerignore`
   excludes `*.md`, so `README.md` must be re-included explicitly.
2. **The sdist is allowlisted.** Hatchling's default walk includes git-ignored files; the
   first build shipped 22 MB of `dev-synthetic/fixture` and would have included `.env`
   and `config/cameras.yml` on a real machine. Only `src/`, `pyproject.toml`, `README.md`
   and `LICENSE` are in the sdist.

PyPI publishing uses trusted publishing, configured in the PyPI UI with owner
`aravindmyd`, repository `cctv-stack`, workflow `publish.yml`, and environment `pypi`.
No API token is stored. The GHCR package has its own visibility setting; making the
repository public does not make the package public.

## Conventions

- Conventional Commits, signed off (`git commit -s`), subject under 100 characters.
- Camera names are lowercase kebab-case; they become paths and object keys.
- Timestamps are stored as instants; presentation uses the site timezone (ADR-0017).
- Application config lives in `cameras.yml`; location config and secrets live in `.env`.
- No secret in the repository, in history, in logs, or in an image layer.
- A change that alters behaviour updates the docs and, if non-obvious, adds an ADR.

## License

Contributions are accepted under Apache-2.0. See [`LICENSE`](LICENSE).
