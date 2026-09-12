# ADR-0004: SQLite is the state store

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [ADR-0002](0002-independent-per-site-deployments.md), [`contracts/database-schema.md`](../architecture/contracts/database-schema.md), [`lld/03`](../architecture/lld/03-state-store.md)

---

## Context

The system needs durable state: the clip inventory, the upload queue with retry
counters and error history, sweep watermarks, held pins, health snapshots, and
reconciliation findings.

Four services need it: clip-worker (writes), uploader (reads and writes), cleanup
(reads and writes), status-collector (reads). They coordinate **only** through this
state — they never call each other — so it is the central integration point of the
design, not an implementation detail.

Expected scale, at the upper end of a family or small-business site: ~200 events per
day, so ~73 000 clips per year, each with a handful of upload attempts. Over a decade
that is well under a million rows, with a few writes per minute at peak.

Because sites are independent ([ADR-0002](0002-independent-per-site-deployments.md)),
there is no multi-host requirement.

## Decision

**Use a single SQLite database at `${DATA_DIR}/data/state/state.db`, in WAL mode,
with a bounded concurrency discipline: short transactions, `BEGIN IMMEDIATE` for
writes, one connection per thread, and no network I/O inside a transaction.**

- Schema versioning with a numbered, forward-only, transactional migration chain
- `PRAGMA synchronous = FULL` — durability over throughput, because we are a low-rate
  workload where losing a committed upload record to a power cut is unacceptable
- Read-only connections for the status-collector and for backups, so the observer
  structurally cannot mutate
- Online backup API for backups; never `cp` a live database

## Consequences

### Positive

- **No server to operate.** No container, no port, no credentials, no connection pool,
  no version negotiation. Nothing to misconfigure and nothing to fail independently
- **Backup is a file copy**, and restore is a file copy. This makes the backup story
  in PRD §14 tractable rather than a project in itself
- **Transactional state transitions**, which is exactly what the upload state machine
  needs. A claim is atomic; a crash cannot leave a half-updated row
- **`UNIQUE(event_id)` enforces idempotency in the schema**, not in application logic
  ([INV-7](../architecture/invariants.md))
- **Concurrent readers with one writer** matches the actual access pattern: three
  readers and one writer at a time, most of the time
- **The whole database fits in memory cache** at this scale, so queries are effectively
  free
- Zero cost, zero licence, works on every host we support including a Raspberry Pi
- A future restore can be verified by opening a file, which makes the verification
  steps in `backup.sh --verify` genuinely simple

### Negative / accepted costs

- **One writer at a time, per database.** Any write held open for seconds blocks all
  other writes. This forces a discipline — short transactions, no I/O inside them —
  that must be documented, reviewed, and linted. It is the main cost of this decision
- **No multi-host.** Acceptable only because of [ADR-0002](0002-independent-per-site-deployments.md).
  If multi-host is ever needed, this decision must be revisited first
- **`SQLITE_BUSY` is a real failure mode** under misuse. Mitigated by `busy_timeout`,
  `BEGIN IMMEDIATE`, and by keeping transactions short
- **Limited analytical capability.** Complex reporting would want window functions and
  better tooling. Our reporting needs are simple aggregates
- **Schema migrations need care** around the compatibility window during upgrades
  ([ADR-0009 of the upgrade design](../architecture/lld/09-upgrade-and-versioning.md))
- Type affinity is looser than a strict database, so the access layer must validate
  types on read

### Neutral

- WAL files (`-wal`, `-shm`) must be handled correctly during backup and restore —
  documented and scripted, so operators never need to know
- Database size grows slowly and predictably; `VACUUM` is unnecessary for an
  append-and-update workload

## Alternatives considered

| Alternative | Why not |
|---|---|
| PostgreSQL | A second container to operate, back up, upgrade, and secure, plus connection management and a migration tool — for a workload a single file handles comfortably. The operational cost is the whole objection, not capability |
| MariaDB / MySQL | Same objection as Postgres, with a heavier image |
| DuckDB | Excellent for analytics, not designed as a transactional multi-process store. Wrong tool for a queue |
| JSON files / a directory of state | No atomicity, no transactions, no indexing, and corruption on crash. Would require reimplementing a database badly |
| Redis with persistence | Another service; weaker durability guarantees than we need for upload state; poor fit for queryable history |
| The filesystem as the state (file existence implies uploaded) | This is precisely the naive design PRD §9 rejects. It cannot represent retry counts, error history, or a lease, and it makes [INV-1](../architecture/invariants.md) unenforceable |
| Frigate's own database | It is Frigate's private cache, it is not our schema, and coupling to it would breach the integration contract |

## Revisit if

- Multi-host coordination becomes a requirement (this would supersede
  [ADR-0002](0002-independent-per-site-deployments.md) as well)
- Write volume grows by orders of magnitude — for example, per-segment rather than
  per-clip bookkeeping
- Reporting requirements grow beyond simple aggregates
- A concrete, demonstrated need for concurrent writers beyond what short transactions
  allow
