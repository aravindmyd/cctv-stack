# Internal documentation index

The map of everything we know and everything we have decided. These documents are for
people changing the code. The published site is built from `docs/` only and includes
nothing from this directory. A new agent should start with [`../AGENTS.md`](../AGENTS.md).

**Status legend**

| Symbol | Meaning |
|---|---|
| ✅ | Agreed, ready to build against |
| 📐 | Draft, under review |
| ❓ | Open question, needs a decision |
| 🚧 | Planned, not written or not implemented |

---

## Current state

| Phase | State |
|---|---|
| Product requirements | ✅ Complete — [`prd.md`](prd.md) |
| Architecture and design | 📐 **Written, awaiting owner review** |
| Implementation | ✅ **M1–M3 built, M4 J1–J3 built, M5 and M6 built, M7 built** — detect → materialise → durable upload → local and cloud retention → aggregate status → guided install → backup, recovery and upgrade |
| Release | ✅ **0.7.0** — published to PyPI and GHCR |
| Client-site packaging | 📐 |
| Current problems | 📐 — [`now.md`](now.md) |

The design set is complete: 4 entry points, 1 HLD, 1 invariant list, 6 contracts,
10 LLDs, and 23 ADRs. The runbooks are published pages under `docs/`, not internal
documents.

**What is built** (`src/cctv/`, entry points `cctv-clip-worker`, `cctv-config`,
`cctv-uploader`, `cctv-cleanup`, `cctv-status`, `cctv-host`, `cctv-backup`, `cctv-recover`,
`cctv-doctor`, `cctv-migrate`, `cctv-init`, `cctv-provision`): a review becomes a
file at the canonical path, the uploader pushes it to Backblaze B2 exactly once through a
storage adapter, local and cloud retention delete a copy only after the other one is safe, a
daily reconciliation pass reports drift between the bucket and the database and repairs
nothing, a daily housekeeping pass empties directories that lost their clips and keeps the
database bounded, the status collector aggregates all of it into `/status` (JSON and HTML) on 8084 and
drives Gatus/ntfy, the installer detects the host profile and Frigate's shared-memory size,
`cctv-backup` writes an encrypted config bundle and a verified database backup, `cctv-recover`
downloads the footage back from the bucket and rebuilds the database when there is no backup,
and `cctv-doctor` diagnoses an installation read-only. Each service
exposes `/health` and `/metrics` per
[`service-contracts.md`](architecture/contracts/service-contracts.md).

**What is not built:** J5's full retention report; `force-delete` and the cleanup lock, both
deliberately deferred by [ADR-0021](adr/0021-defer-force-delete-and-advisory-lock.md); a live
end-to-end alert run (M5.13, manual); the `KumaAdapter` fallback; and, from M6, test
notifications and signed release bundles. Nothing is deleted while the other copy is still
needed, so [INV-1](architecture/invariants.md) holds.

spike/ in the repo root is still a throwaway verification harness for M1, not product code,
and goes once M1's `VERIFY` items are replaced with measurements.

No open question blocks implementation today. Only **OQ-3** (monitoring tool) is still
live, and it affects M5 alone; the name (OQ-1), licence (OQ-2), recording-retention
budget (OQ-10) and pre/post-roll defaults (OQ-16) are settled.

---

## Reading order by role

### If you are an AI agent starting fresh

1. [`glossary.md`](../docs/glossary.md) — vocabulary, **read this before writing anything**
4. [`architecture/hld.md`](architecture/hld.md)
5. [`architecture/invariants.md`](architecture/invariants.md)
6. architecture/contracts/ — the interfaces you must honour
7. The LLD for the component you are changing
8. Relevant ADRs for *why*

### If you are a new human contributor

Same as above, then:

8. [`../docs/`](../docs/index.md) — the published site, which is what operators read

### If you are the operator

1. [`install.md`](../docs/install.md)
2. [`configure.md`](../docs/configure.md) — cameras, padding, retention
3. [`frigate-ui.md`](../docs/frigate-ui.md) — put the Frigate web UI behind a login
4. [`health.md`](../docs/health.md)
5. [`recover-uploads.md`](../docs/recover-uploads.md)
6. [`rotate-credentials.md`](../docs/rotate-credentials.md)
7. [`disaster-recovery.md`](../docs/disaster-recovery.md)
8. [`clip-boundaries.md`](../docs/clip-boundaries.md) — why a clip can look fine and still be wrong

---

## Inventory

### Product

| Doc | Purpose | Status |
|---|---|---|
| [`prd.md`](prd.md) | Original product requirements, authored by the owner | ✅ input |

### Architecture

| Doc | Purpose | Status |
|---|---|---|
| [`architecture/hld.md`](architecture/hld.md) | System context, containers, data flow, failure modes | 📐 |
| [`architecture/invariants.md`](architecture/invariants.md) | Rules that must never break, and how each is enforced | 📐 |
| [`architecture/contracts/frigate-integration.md`](architecture/contracts/frigate-integration.md) | Exactly how we talk to Frigate | 📐 |
| [`architecture/contracts/on-disk-layout.md`](architecture/contracts/on-disk-layout.md) | Host directories, mount points, volume boundaries | 📐 |
| [`architecture/contracts/cameras-config-schema.md`](architecture/contracts/cameras-config-schema.md) | The camera schema and the generated Frigate config | 📐 |
| [`architecture/contracts/database-schema.md`](architecture/contracts/database-schema.md) | Tables, state machines, migrations | 📐 |
| [`architecture/contracts/service-contracts.md`](architecture/contracts/service-contracts.md) | Health/status HTTP contract, logging, exit codes | 📐 |
| [`architecture/contracts/cloud-object-layout.md`](architecture/contracts/cloud-object-layout.md) | Object keys, checksums, lifecycle rules | 📐 |

### Low-level design

| Doc | Component | Status |
|---|---|---|
| [`architecture/lld/00-overview.md`](architecture/lld/00-overview.md) | How the LLDs fit together, dependency order | 📐 |
| [`architecture/lld/01-config-model.md`](architecture/lld/01-config-model.md) | Config loading, validation, generation | 📐 |
| [`architecture/lld/02-clip-worker.md`](architecture/lld/02-clip-worker.md) | Event → canonical clip materialisation | 📐 |
| [`architecture/lld/03-state-store.md`](architecture/lld/03-state-store.md) | SQLite schema, access patterns, migrations | 📐 |
| [`architecture/lld/04-uploader.md`](architecture/lld/04-uploader.md) | Upload state machine, retry, backoff | 📐 |
| [`architecture/lld/05-cleanup.md`](architecture/lld/05-cleanup.md) | Local and cloud retention enforcement | 📐 |
| [`architecture/lld/06-health-and-monitoring.md`](architecture/lld/06-health-and-monitoring.md) | Health model, status-collector, Uptime Kuma, alerting | 📐 |
| [`architecture/lld/07-installer-and-host-profiles.md`](architecture/lld/07-installer-and-host-profiles.md) | Detect-and-confirm installer, compose profiles | 📐 |
| [`architecture/lld/08-backup-and-restore.md`](architecture/lld/08-backup-and-restore.md) | What is backed up and how it is restored | 📐 |
| [`architecture/lld/09-upgrade-and-versioning.md`](architecture/lld/09-upgrade-and-versioning.md) | Version scheme, image pinning, migration order | 📐 |

### Decisions

| Doc | Decision | Status |
|---|---|---|
| (Template removed from nav) | | |
| [`adr/0001-frigate-as-detection-engine.md`](adr/0001-frigate-as-detection-engine.md) | Use Frigate rather than build detection | 📐 |
| [`adr/0002-independent-per-site-deployments.md`](adr/0002-independent-per-site-deployments.md) | No central hub | 📐 |
| [`adr/0003-cameras-yml-single-source-of-truth.md`](adr/0003-cameras-yml-single-source-of-truth.md) | `cameras.yml` is canon, Frigate config is generated | 📐 |
| [`adr/0004-sqlite-for-state.md`](adr/0004-sqlite-for-state.md) | SQLite over Postgres | 📐 |
| [`adr/0005-b2-behind-storage-adapter.md`](adr/0005-b2-behind-storage-adapter.md) | B2-only implementation, adapter interface | 📐 |
| [`adr/0006-canonical-clip-layout.md`](adr/0006-canonical-clip-layout.md) | Deterministic clip paths | 📐 |
| [`adr/0007-mqtt-api-over-filesystem-watching.md`](adr/0007-mqtt-api-over-filesystem-watching.md) | Event-driven, not inotify | 📐 |
| [`adr/0008-upload-state-machine.md`](adr/0008-upload-state-machine.md) | Persisted upload states | 📐 |
| [`adr/0009-cleanup-safety-invariant.md`](adr/0009-cleanup-safety-invariant.md) | Cleanup respects upload state | 📐 |
| [`adr/0010-external-monitoring-tool.md`](adr/0010-external-monitoring-tool.md) | Reuse an external tool for dashboard and alerting | 📐 |
| [`adr/0011-python-fastapi-services.md`](adr/0011-python-fastapi-services.md) | Python for custom services | 📐 |
| [`adr/0012-host-profile-detection.md`](adr/0012-host-profile-detection.md) | Detect and confirm host capabilities | 📐 |
| [`adr/0013-compose-profiles.md`](adr/0013-compose-profiles.md) | One compose file with profiles, not many files | 📐 |
| [`adr/0014-pinned-image-digests.md`](adr/0014-pinned-image-digests.md) | Pin image digests, not tags | 📐 |
| [`adr/0015-clip-boundaries-and-padding.md`](adr/0015-clip-boundaries-and-padding.md) | Clip boundaries err outward; pre-roll, post-roll, and the flush-aware fetch | 📐 |
| [`adr/0016-clip-per-review-item.md`](adr/0016-clip-per-review-item.md) | One canonical clip per Frigate **review item**, not per event | 📐 |
| [`adr/0017-time-handling.md`](adr/0017-time-handling.md) | Time is stored as a timezone-free instant; rendered in the site timezone | 📐 |
| [`adr/0018-uv-as-toolchain.md`](adr/0018-uv-as-toolchain.md) | uv for development, CI and the build stage — never at runtime | 🛠 |
| [`adr/0019-rustfs-as-s3-test-double.md`](adr/0019-rustfs-as-s3-test-double.md) | RustFS as the S3 test double for integration testing | 🛠 |
| [`adr/0020-release-artifacts-and-delivery-modes.md`](adr/0020-release-artifacts-and-delivery-modes.md) | A signed, digest-pinned release bundle; connected, mirrored and air-gapped delivery | 📐 |
| [`adr/0021-defer-force-delete-and-advisory-lock.md`](adr/0021-defer-force-delete-and-advisory-lock.md) | Defer force-delete (M4.13) and the cleanup advisory lock (M4.14) | 🛠 |

### Published site

| Page | Covers |
|---|---|
| [`index.md`](../docs/index.md) | Landing page |
| [`install.md`](../docs/install.md) | Compose from GHCR, pip, upgrades |
| [`configure.md`](../docs/configure.md) | `cameras.yml`, `.env`, camera changes |
| [`frigate-ui.md`](../docs/frigate-ui.md) | Frigate UI login |
| [`health.md`](../docs/health.md) | Health reports and admin endpoints |
| [`recover-uploads.md`](../docs/recover-uploads.md) | Diagnose a stuck upload queue |
| [`rotate-credentials.md`](../docs/rotate-credentials.md) | B2 keys and camera passwords |
| [`disaster-recovery.md`](../docs/disaster-recovery.md) | What to back up and how to rebuild |
| [`clip-boundaries.md`](../docs/clip-boundaries.md) | Clips that are short or misaligned |
| [`glossary.md`](../docs/glossary.md) | Vocabulary |
