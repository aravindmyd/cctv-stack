# ADR-0009: Cleanup never deletes a clip whose upload has not succeeded

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-1](../architecture/invariants.md), PRD §10, [`lld/05`](../architecture/lld/05-cleanup.md)

---

## Context

PRD §10 asks for retention to be its own service:

```text
Local:     Age > 90 days  → DELETE
Cloud:     Age > 60 days  → DELETE
```

Read literally, that is a rule about *age*. But age is not the only condition that
should govern deletion, because there is a second, competing policy in the same system
(PRD §9): clips must be uploaded reliably, with retries, and a machine restart must not
lose track of what has been uploaded.

These two policies collide whenever an upload has not succeeded. Consider a B2 outage,
a revoked key, or a bug — lasting longer than the local retention window. Two rules
now disagree:

| Policy | Answer |
|---|---|
| "Delete anything older than 90 days" | Delete |
| "Never lose a clip" | Keep |

The default outcome of that collision, if nobody decides, is determined by
implementation accident — whichever code path runs first. And the consequence is
asymmetric and irreversible: deleting an un-uploaded clip that is 91 days old
permanently destroys the only copy of that footage.

It is worth being explicit about how this failure presents. It is not an error
message. It is a system that reports `HEALTHY`, has a clean dashboard, and is missing
footage from a three-week period — discovered months later, when someone tries to look
at it.

## Decision

**Local retention only ever deletes clips whose upload has reached a terminal-success
state. An un-uploaded clip is held indefinitely, reported, and alerted — never deleted
automaticically.**

| Upload state | May local cleanup delete it? |
|---|---|
| `UPLOADED` | ✅ Yes |
| `SKIPPED_BY_POLICY` | ✅ Yes (the operator chose local-only) |
| `PENDING` | ❌ **Never** |
| `UPLOADING` | ❌ **Never** |
| `RETRY_WAIT` | ❌ **Never** |
| `FAILED_PERMANENT` | ❌ **Never** |
| `DELETED_LOCAL`, `EXPIRED_CLOUD`, `FORCE_DELETED` | n/a (already gone) |

Supporting decisions that make this structural rather than aspirational:

1. **The query enumerates permitted states positively** (`upload_state IN ('UPLOADED',
   'SKIPPED_BY_POLICY')`), never by exclusion. A state added in future is therefore
   undeletable by default — the failure mode of forgetting to update the query is
   *holding data too long*, not *deleting data too early*
2. **Cleanup never walks the filesystem** to decide what to delete. It is driven
   entirely by database rows, so a file the database does not know about is never
   deleted by policy
3. **A per-clip assertion re-checks the state** immediately before the unlink, catching
   a state change between the query and the action
4. **An `UPLOADED` clip must also have `cloud_key` and `uploaded_at`.** A row claiming
   success without the evidence of it is not deletable
5. **Held clips are reported, not hidden.** `clips_overdue_retention` counts them and
   their total size, and the health state degrades
6. **Removing a clip is only otherwise possible through an explicit, audited admin
   action** requiring a specific clip id and a written reason, resulting in the
   permanent, visible state `FORCE_DELETED`

## Consequences

### Positive

- **The single worst failure this system could have is prevented structurally**, not by
  review or by care
- **The trade is explicit and visible.** We will hold your data rather than lose it,
  and `/status` shows exactly how much is held and why
- **Transient outages are fully absorbed.** A B2 outage of any length costs disk, not
  footage
- **A misconfigured credential becomes a disk-growth problem with a clear alert**,
  rather than a silent data-loss problem
- **Cleanup becomes simple to reason about**, because its only input is database state
  and its only permission is a small enumerated set

### Negative / accepted costs

- **Disk can fill.** An unresolved upload failure means unbounded local growth. The
  90-day retention promise is therefore conditional: *"90 days, and longer if we could
  not back it up"*
- **The retention guarantee is no longer a simple statement.** PRD §10 reads as an
  absolute age rule; this ADR makes it conditional. Documented in the README, the
  status page, and the retention code
- **Operators can end up with a full disk and a system that refuses to delete.** The
  correct answer is to fix the upload; the escape hatch (`FORCE_DELETED`) exists but is
  deliberately tedious. This friction is intentional and must be explained, or it will
  feel like a bug
- **Full-disk behaviour is now coupled to upload health.** Frigate's own emergency
  cleanup deletes recordings when storage is nearly exhausted, which does not protect
  clips. So a stalled uploader can indirectly cost recent *recordings* while protecting
  *clips*. Documented, and monitored via projected days to full
- **A `FAILED_PERMANENT` clip is held forever** until someone acts, and it is not
  automatically re-tried. Requires an operator to read the runbook

### Neutral

- Cloud retention has no equivalent dilemma: an object is only expired after a
  successful upload by definition
- `FORCE_DELETED` exists so that giving up is a recorded decision rather than an
  absence of information

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Delete by age, unconditionally** (the literal PRD reading) | Simple, matches the PRD text, and destroys footage whenever uploads fail for longer than the retention window. Rejected outright |
| **Delete un-uploaded clips after a large grace period** (for example 180 days) | Sounds like a reasonable compromise, and it is exactly how silent data loss happens. There is no age at which destroying the only copy of footage becomes correct. If a disk is genuinely full, the operator should decide which clips to sacrifice |
| **Upload first, then apply age** — treat local retention as "age since upload" | Closer, and it is what cloud retention does. Rejected for local storage because local disk is the constrained resource: deferring deletion by the full upload latency would let a large backlog consume unbounded space even in the healthy case |
| **Alert and auto-delete the oldest un-uploaded clips when disk is critical** | The most tempting of the rejected options, since it keeps the system running. Rejected because it makes data loss a background process. Instead, disk pressure is alerted well in advance (`DISK_PRESSURE`, `DISK_CRITICAL`) and the operator chooses — including choosing to use `FORCE_DELETED` or to attach more storage |
| **Never delete anything; retention is advisory** | Would make the system fail its actual purpose on any finite disk |
| **Rely on B2 lifecycle rules alone** | Irrelevant to local retention, and they operate on a different clock (`daysFromUploadingToHiding`) |

## Revisit if

- A genuinely bounded-storage deployment appears (an appliance with a fixed small disk)
  where unbounded local growth is not survivable — which would require an explicit,
  documented policy for which clips to sacrifice, and probably an operator-set cap
- Storage policy changes such that local disk is no longer the constrained resource
- A requirement appears to guarantee a maximum disk footprint, which would require
  inverting this decision and accepting a bounded-loss policy instead

**If revisited:** any replacement must be operator-configured, loudly reported, and
must never be the silent default.
