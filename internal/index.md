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
| Implementation | ✅ **M1–M3 built, M4 J1 built** — detect → materialise → durable upload → local retention |
| Release | ✅ **0.3.0** — published to PyPI and GHCR |
| Client-site packaging | 📐 |
| Current problems | 📐 — [`now.md`](now.md) |

The design set is complete: 4 entry points, 1 HLD, 1 invariant list, 6 contracts,
9 LLDs, 20 ADRs, and 7 runbooks.

**What is built** (`src/cctv/`, entry points `cctv-clip-worker`, `cctv-config`,
`cctv-uploader`, `cctv-cleanup`): a review becomes a file at the canonical path, the
uploader pushes it to Backblaze B2 exactly once through a storage adapter, and local
retention deletes a clip only after its upload is terminal-success. Each service exposes
`/health` and `/metrics` per
[`service-contracts.md`](architecture/contracts/service-contracts.md).

**What is not built:** M4's cloud retention, reconciliation, housekeeping and force-delete;
the aggregate `/status` page and alerting (M5); the installer (M6); and
backup/restore/upgrade (M7). Nothing is deleted without a successful upload, so
[INV-1](architecture/invariants.md) holds.

spike/ in the repo root is still a throwaway verification harness for M1, not product code,
and goes once M1's `VERIFY` items are replaced with measurements.

Only two open questions touch M1, and both are answered by running the probe: **OQ-10**
(recording retention budget) and **OQ-16** (pre/post-roll defaults). The rest — name
(OQ-1), licence (OQ-2), monitoring tool (OQ-3) — block later milestones only.

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

8. runbooks/ — what operating this actually looks like

### If you are the operator

1. [`runbooks/install.md`](../docs/runbooks/install.md)
2. [`runbooks/add-a-camera.md`](../docs/runbooks/add-a-camera.md)
3. [`runbooks/frigate-authentication.md`](../docs/runbooks/frigate-authentication.md) — turn on, recover, or turn off the Frigate UI login
4. [`runbooks/recover-failed-uploads.md`](../docs/runbooks/recover-failed-uploads.md)
5. [`runbooks/rotate-credentials.md`](../docs/runbooks/rotate-credentials.md)
6. [`runbooks/disaster-recovery.md`](../docs/runbooks/disaster-recovery.md)
7. [`runbooks/clip-boundaries.md`](../docs/runbooks/clip-boundaries.md) — why a clip can look fine and still be wrong

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

### Runbooks

| Doc | Purpose | Status |
|---|---|---|
| [`runbooks/install.md`](../docs/runbooks/install.md) | Install and verify | 📐 |
| [`runbooks/add-a-camera.md`](../docs/runbooks/add-a-camera.md) | Add or change a camera | 📐 |
| [`runbooks/frigate-authentication.md`](../docs/runbooks/frigate-authentication.md) | Enable, recover, or disable the Frigate UI login | ✅ |
| [`runbooks/recover-failed-uploads.md`](../docs/runbooks/recover-failed-uploads.md) | Diagnose and drain a stuck upload queue | 📐 |
| [`runbooks/rotate-credentials.md`](../docs/runbooks/rotate-credentials.md) | B2 keys, camera passwords, Frigate auth | 📐 |
| [`runbooks/disaster-recovery.md`](../docs/runbooks/disaster-recovery.md) | Rebuild on new hardware | 📐 |
| [`runbooks/clip-boundaries.md`](../docs/runbooks/clip-boundaries.md) | Diagnose clips that are short, holed, or misaligned | 📐 |
