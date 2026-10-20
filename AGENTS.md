# AGENTS.md

Instructions for AI agents **and** humans who are new to this repository.
Read this file first, then [`internal/index.md`](internal/index.md) — the internal map and reading order.

---

## 1. Project status

| | |
|---|---|
| **Phase** | **BUILDING — v0.3** |
| **Implemented** | Detect → materialise at the canonical clip path → **upload to Backblaze B2, exactly once** → installable. M3 is certified against a live S3-compatible server. M4's **J1 local retention** runs as the `cleanup` service. M5 is built: the `status` service serves `/status` (JSON and HTML), `/health` and `/metrics` on 8084, generates Gatus's config and pushes heartbeats to it; Gatus and ntfy run in the default stack. M6 has started: `cctv-host` detects the host against the support matrix and computes `FRIGATE_SHM_SIZE`, and `install.sh` runs it before anything starts. M7 has started: `cctv-backup` (with `./backup.sh` and `./restore.sh`) writes an encrypted config bundle, an online database backup and a manifest, and verifies staleness. Lint clean; 836 tests green, 18 skipped (they need a live S3 endpoint or the Docker daemon). |
| **Not implemented** | M4's cloud retention (J2), reconciliation (J3) and housekeeping (J4); `force-delete` and the single-instance lock, both **deliberately deferred** by [`internal/adr/0021`](internal/adr/0021-defer-force-delete-and-advisory-lock.md); M5's live end-to-end alert run (M5.13, manual) and the `KumaAdapter` fallback; the rest of M6 — the interview, B2 provisioning, `doctor.sh`, `uninstall.sh`, signed bundles; M7's `update.sh`, cloud clip recovery and database reconstruction. One thing is deleted today — a clip whose upload is terminal-success and that is past `retention.local_days` — see [`.private/plans/backlog.md`](.private/plans/backlog.md) for exactly what is deferred and why. |
| **Authority** | Design docs in `internal/` are the source of truth. Code follows docs, not the other way round. `docs/` is the published user site and is not authoritative. |

> **Where things stand.** `src/cctv/` is the shared library plus seven entry points:
> `cctv-clip-worker` (reviews → clips), `cctv-config` (`cameras.yml` → Frigate's config),
> `cctv-uploader` (clips → B2, with a persisted retry state machine), `cctv-cleanup` (local
> retention), `cctv-status` (the aggregate answer to "is my CCTV working?"), `cctv-host`
> (which machine is this, and how much shared memory does Frigate need), `cctv-backup`
> (configuration, state and manifest, with verification) and `cctv-doctor` (read-only
> diagnosis of a whole installation, published as `./scripts/doctor.sh`).
> `dev-synthetic/` is the camera-free harness and is **kept** — it is how the pipeline gets
> exercised without a camera, and how it will run in CI. `spike/` is a throwaway measurement
> harness and still goes once M1's exit criteria are met.
>
> **Cloud backup is opt-in and honest about it.** With `CCTV_STORAGE_BUCKET` unset the
> uploader runs, uploads nothing, and reports `DISABLED` — a supported state rather than a
> silent one. A *half*-configured bucket (a bucket with no key) exits `78` instead, because
> that is a mistake rather than a choice.

This repository is being designed as a *packaging-first* CCTV stack: a single
deployable application that can be installed at any location by changing
configuration only. See [`internal/prd.md`](internal/prd.md) for the original product
requirements.

> **Rule:** No application code is written until the plan in `internal/` is approved.
> If you are an agent asked to implement something that contradicts `internal/architecture/invariants.md`,
> stop and raise it as an open question in `.private/plans/open-questions.md` instead.

---

## 2. What this project is

A self-hosted CCTV system built on top of **Frigate** (detection + recording),
adding four things Frigate deliberately does not do:

1. **Canonical clip layout** — every event clip is materialised into a
   deterministic `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4` tree.
2. **Durable cloud backup** — clips are uploaded to Backblaze B2 through a
   storage adapter, driven by a persisted retry state machine.
3. **Independent retention** — local 90-day and cloud 60-day retention enforced
   by a dedicated cleanup service, plus B2 lifecycle rules as a second belt.
4. **Health as a first-class feature** — every component and camera exposes a
   health state that answers *"is my CCTV actually working?"*

Deployments are **fully independent per site**. There is no central hub, no
cross-site data sharing, and no inbound connectivity requirement.

---

## 3. Repository map

```text
cctv-stack/
├── AGENTS.md                  # ← you are here
├── README.md                  # human-facing entry point
├── docs/                      # PUBLISHED site — user-facing only
│   ├── index.md               # landing page
│   ├── install.md             # Compose from GHCR, pip, upgrades
│   ├── configure.md           # cameras.yml, .env, adding cameras
│   ├── frigate-ui.md          # Frigate web login
│   ├── health.md              # health reports and admin endpoints
│   ├── doctor.md              # diagnosing a broken installation
│   ├── recover-uploads.md     # a stuck upload queue
│   ├── rotate-credentials.md  # B2 keys and camera passwords
│   ├── disaster-recovery.md   # what to back up, how to rebuild
│   ├── clip-boundaries.md     # clips that are short or misaligned
│   └── glossary.md            # shared vocabulary
├── internal/                  # design authority — never published
│   ├── index.md               # internal map + reading order
│   ├── prd.md                 # product requirements (input, immutable)
│   ├── now.md                 # current problems and known gaps
│   ├── architecture/
│   │   ├── hld.md             # high-level design
│   │   ├── invariants.md      # hard rules that must never be broken
│   │   ├── contracts/         # interfaces between components
│   │   └── lld/               # low-level design, one file per component
│   └── adr/                   # architecture decision records
├── .private/                  # gitignored planning notes (plans, backlog)
├── compose.yml                # (planned) deployment boundary
├── .env.example               # (planned) location configuration template
├── spike/                     # ⚠️ TEMPORARY — M1 verification harness, not product code
│   ├── README.md              #    how to run it, and why it is throwaway
│   ├── probe/probe.py         #    measures the contract's VERIFY items
│   └── RESULTS.md             #    fill in, then delete the whole directory
└── config/  services/  data/  # (planned) see internal/architecture/contracts/on-disk-layout.md
```

Paths marked *(planned)* do not exist yet. Do not create them speculatively.

---

## 4. Required reading order

1. [`internal/prd.md`](internal/prd.md) — what we were asked to build
2. [`docs/glossary.md`](docs/glossary.md) — vocabulary (Frigate's terms are subtle)
3. [`internal/architecture/hld.md`](internal/architecture/hld.md) — how the system fits together
4. [`internal/architecture/invariants.md`](internal/architecture/invariants.md) — what must never break
5. [`internal/architecture/contracts/`](internal/architecture/contracts/) — exact interfaces
6. The specific `internal/architecture/lld/*.md` for the component you are touching
7. Relevant ADRs in [`internal/adr/`](internal/adr/) — *why* it is this way

---

## 5. Hard invariants

Full list and rationale: [`internal/architecture/invariants.md`](internal/architecture/invariants.md).

Summary of the non-negotiables:

| ID | Invariant |
|---|---|
| INV-1 | A clip is never deleted locally until its upload state is terminal-success or the operator explicitly forces it. |
| INV-2 | No secret ever appears in the repository, in git history, in logs, or in a Docker image layer. |
| INV-3 | Camera configuration exists in exactly one place (`config/cameras.yml`). Everything else is generated. |
| INV-4 | Containers are disposable: all state lives under the host data directory. |
| INV-5 | Every clip path is deterministic and derivable from its metadata. |
| INV-6 | Every component exposes a health state; unhealthy is never silently healthy. |
| INV-7 | Uploads are idempotent — re-running an upload never creates a duplicate cloud object. |
| INV-8 | Upgrades never destroy state; schema changes are forward-migrated. |
| INV-9 | A clip is materialised before Frigate's own retention can expire its source footage. |
| INV-14 | Clip boundaries err outward; a short clip is retried and kept, never truncated or discarded. |

---

## 6. Conventions

- **Naming:** camera names are lowercase kebab-case (`front-door`, not `FrontDoor`).
  They appear in filesystem paths and cloud object keys, so this is load-bearing.
- **Time:** timestamps are stored in **UTC** everywhere. Local time is a
  presentation concern only.
- **Configuration:** application config (same everywhere) lives in
  `config/*.yml` with defaults. Location config (differs per site) lives in `.env`.
  Never mix the two.
- **Cameras are data, never code.** No `if camera == "..."` branching. Ever.
- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`).
  100-character signed-off provenance (`git commit -s --signoff`).
- **Every non-obvious decision gets an ADR.** If you cannot find one, write one
  using any accepted ADR as the shape — context, decision, consequences, and (where it
  matters) the triggers that would reopen it.
- **Docs are updated in the same change as the code.** A PR that changes behaviour
  without changing docs is incomplete.

---

## 7. Commands

Nothing is implemented yet, so these are the *intended* commands. Each is
specified in the linked design doc.

| Command | Purpose | Status | Spec |
|---|---|---|---|
| `./install.sh` | Create the site config, validate it, start the stack, wait for Frigate | **built** (development version; M6 adds detection, the interview and full verification) | [`lld/07`](internal/architecture/lld/07-installer-and-host-profiles.md) |
| `./update.sh` | Pull new version, migrate, recreate containers, verify | planned | [`lld/09`](internal/architecture/lld/09-upgrade-and-versioning.md) |
| `./uninstall.sh` | Stop and remove containers, keep data | planned | [`lld/07`](internal/architecture/lld/07-installer-and-host-profiles.md) |
| `./backup.sh` | Back up config + database + upload state | planned | [`lld/08`](internal/architecture/lld/08-backup-and-restore.md) |
| `docker compose up -d` | Start the stack directly | planned | [`hld.md`](internal/architecture/hld.md) |
| `tests/integration/run.sh` | Run the live integration suite against RustFS (starts and stops it) | **built** | [`tests/integration/README.md`](tests/integration/README.md) |
| `tests/e2e/run.sh` | Run the synthetic camera → Frigate review → canonical clip pipeline | **built** | [`tests/e2e/run.sh`](tests/e2e/run.sh) |
| `scripts/doctor.sh` | Diagnose a broken installation | **built** | [`lld/07`](internal/architecture/lld/07-installer-and-host-profiles.md) §10 |

Client-site packaging is specified in [`.private/plans/packaging.md`](.private/plans/packaging.md):
`install.sh` remains the on-host entry point, but a client receives a signed release
bundle and a digest-pinned image rather than a git checkout. CI lives in
[`.github/workflows/`](.github/workflows/): pre-commit, unit, live integration, the
synthetic e2e pipeline, and `publish.yml` (GHCR on every merge, PyPI on `v*` tags).

---

## 8. How to change the design

1. Check [`.private/plans/open-questions.md`](.private/plans/open-questions.md) — it may already be an open question.
2. If it is a real decision with trade-offs, write an ADR **first**.
3. Update the affected contract in `internal/architecture/contracts/`.
4. Update the affected LLD.
5. Only then write code.

Do not silently diverge from a documented contract. If reality disagrees with a
doc, the doc is a bug — fix it in the same change.

---

## 9. Things that will get a change rejected

- A secret committed to the repository.
- A camera name or RTSP URL hardcoded in application code.
- A retention/cleanup path that can delete an un-uploaded clip.
- A health check that returns `healthy` when the underlying dependency is down.
- A new configuration key with no entry in `.env.example` and no docs.
- A component that writes state inside its container filesystem.
