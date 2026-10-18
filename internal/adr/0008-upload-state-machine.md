# ADR-0008: Uploads are governed by a persisted state machine, not file existence

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-1](../architecture/invariants.md), [INV-7](../architecture/invariants.md), PRD §9, [`contracts/database-schema.md`](../architecture/contracts/database-schema.md)

---

## Context

PRD §9 rejects the naive pipeline explicitly:

> "The uploader should not simply do: file appears → upload → delete. Instead… the
> system should maintain upload state… This means a machine restart doesn't lose track
> of what has already been uploaded."

The naive design — a directory of pending files, upload, then delete — fails in
several distinct ways:

| Failure | Naive behaviour | Consequence |
|---|---|---|
| Restart mid-upload | No memory of in-flight work | Re-upload (wasteful but survivable) or, worse, the file is deleted and never uploaded |
| Upload fails | File stays or is deleted depending on the code path | Silent loss if deleted; unbounded growth if kept |
| Permanent failure | Indistinguishable from transient | Infinite retry, or silent abandonment |
| Cleanup runs | Sees a file, deletes by age | **Deletes un-uploaded footage** |
| Need to answer "what is stuck?" | Requires correlating logs | Unanswerable |

The third row is the one that matters most: retention and durability are different
policies, and conflating them is how a 90-day cleanup window turns into permanent data
loss. Explicit state is what keeps the two separable.

## Decision

**Every clip carries an explicit, persisted upload state. All upload behaviour —
claiming, retrying, giving up, and permission to delete — is derived from that state,
never from the presence or absence of a file.**

States: `PENDING`, `UPLOADING`, `RETRY_WAIT`, `UPLOADED`, `FAILED_PERMANENT`,
`SKIPPED_BY_POLICY`, `DELETED_LOCAL`, `EXPIRED_CLOUD`, `FORCE_DELETED`.

Supporting decisions:

| Concern | Decision |
|---|---|
| Claiming | Atomic `UPDATE … RETURNING` inside a write transaction, setting a **lease** |
| Crash recovery | Expired leases return rows to `PENDING`; no attempt is refunded |
| Retry | Exponential backoff with jitter, from `attempts` stored on the row |
| Termination | `attempts >= max_attempts` → `FAILED_PERMANENT`, alerted, **file retained** |
| Deletion permission | Local retention enumerates `UPLOADED` and `SKIPPED_BY_POLICY` **positively** ([INV-1](../architecture/invariants.md)) |
| Idempotency | `UNIQUE(review_id)` plus a deterministic cloud key plus checksum verification |
| Illegal transitions | Rejected in code and logged `CRITICAL` |
| Legitimate local-only mode | `SKIPPED_BY_POLICY` — an explicit, non-alerting state, so a deliberate choice is distinguishable from a failure |
| Manual intervention | `FORCE_DELETED` is terminal and permanent in the record, so giving up on a clip is not indistinguishable from success |

## Consequences

### Positive

- **Restarts are safe by construction.** All queue state is in the database; the only
  in-memory state is a lease, which expires
- **Retention can never delete an un-uploaded clip.** The permission query enumerates
  permitted states, so a newly added state is safe by default
- **"What is stuck?" is directly queryable**: counts by state, oldest pending age,
  error history per clip. This becomes the health model's upload section
- **Failure classes are explicit**, so a revoked credential fast-fails instead of
  hammering an endpoint that has already said no
- **The backlog is bounded and observable**, and its age is the metric that
  distinguishes "working" from "running"
- **Local-only deployments are supported honestly**, without pretending to be broken
- Idempotency is enforced by the schema, not by convention

### Negative / accepted costs

- **More machinery than a directory of files.** A schema, a state machine, a lease
  protocol, transition validation, and their tests
- **Illegal transitions are possible in principle** and must be actively rejected;
  the state machine is a discipline, not a compiler guarantee
- **Terminal failure retains data forever.** `FAILED_PERMANENT` clips grow the disk
  until an operator acts. This is deliberate — holding data beats deleting it — but it
  must be alerted and reported, and `clips_overdue_retention` exists for exactly that
- **The database becomes critical infrastructure.** Losing `state.db` loses upload
  state, though not the clips. This is why it is the centre of the backup strategy
  (PRD §14)
- **Nine states is a lot to learn.** Mitigated by documenting the table with its
  deletion permissions inline
- A retry that was killed mid-flight counts as an attempt even if no bytes were sent,
  so `max_attempts` can be reached slightly early. Conservative, and accepted

### Neutral

- Upload state is deliberately separate from *materialisation* state. A clip can fail
  to materialise (no file, no row) or fail to upload (file, row, `FAILED_PERMANENT`).
  Conflating them would put clips with no file into the upload queue

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Filesystem as state**: `pending/`, `uploaded/`, `failed/` directories | The design PRD §9 explicitly rejects. Cannot express lease, attempt count, error history, or next-attempt time; makes cleanup unsafe; breaks on crash between rename steps; and makes "what is stuck?" unanswerable |
| **In-memory queue with a journal** | Fast, but recovery requires replaying the journal, and the journal is a worse database than the database we already have |
| **Boolean `uploaded` flag** | Simplest thing that could work, and it fails on the first question anyone asks: why has this one not uploaded? No error history, no backoff, no distinction between transient and permanent, no lease, no way to detect a stuck row beyond age |
| **Fewer states** (merge `RETRY_WAIT` into `PENDING` with a `next_attempt_at`) | This is a genuine simplification and was considered seriously. Rejected because the distinction is operationally useful: "3 pending, 0 retrying" and "0 pending, 3 retrying with the provider returning 503" are different situations, and the dashboard should show them differently. The cost is one extra state |
| **A separate queue service** (Redis, RabbitMQ) | Another container to operate and persist, for a workload of a few hundred messages a day, and it still needs the database for the durable record. The queue would be a cache in front of the thing that actually matters |
| **Retry forever, never `FAILED_PERMANENT`** | Tempting, since the file is retained either way. Rejected because an unbounded retry state is indistinguishable from a working system, and a permanently broken configuration (a revoked key) would retry silently forever instead of demanding attention |

## Revisit if

- Upload volume grows enough that the database becomes a bottleneck (very unlikely at
  the documented scale)
- A requirement appears for coordinated multi-host upload (which would also supersede
  [ADR-0002](0002-independent-per-site-deployments.md) and
  [ADR-0004](0004-sqlite-for-state.md))
- Provider behaviour changes such that retry classification needs finer granularity
