# ADR-0023: Two deletions, either order — and a `PURGED` state to end in

- **Status:** Accepted
- **Date:** 2026-10-20
- **Deciders:** project owner
- **Related:** [`database-schema.md`](../architecture/contracts/database-schema.md) §3,
  [`lld/05-cleanup.md`](../architecture/lld/05-cleanup.md) §§3–4, [INV-1](../architecture/invariants.md),
  migration `0005`, OQ-17

---

## Context

Local retention (J1) shipped alone and was correct: `UPLOADED → DELETED_LOCAL` ("local file
removed; cloud copy exists"), terminal.

Implementing cloud retention (J2) exposed a gap that only exists once **both** passes run. The
contract had two terminal states:

| State | Documented meaning |
|---|---|
| `DELETED_LOCAL` | local file removed; **cloud copy exists** |
| `EXPIRED_CLOUD` | **both copies gone** |

Both were reachable only from `UPLOADED`, and neither from the other. With the defaults
`local_days: 90`, `cloud_days: 60` — cloud first — the normal outcome at day 60 is *"cloud object
deleted, local file still on disk for another month"*, and **no state describes that row**.
Leaving it `UPLOADED` meant the cloud pass retried it forever; marking it `EXPIRED_CLOUD` made
the database claim both copies were gone while the file was still there.

The other direction was worse. `LOCAL_EXPIRY_STATES` was `{UPLOADED, SKIPPED_BY_POLICY}`, so a row
that reached `EXPIRED_CLOUD` could **never** have its local file deleted. Whichever pass ran
first, one of the two copies was stranded.

## Decision

**Delete in either order, and let the state say what *remains* rather than which pass ran.**

| State | Meaning |
|---|---|
| `UPLOADED` | both copies exist |
| `DELETED_LOCAL` | the local file is gone; the cloud object may exist |
| `EXPIRED_CLOUD` | the cloud object is gone; the local file may exist |
| `PURGED` | **neither** exists — terminal |
| `FORCE_DELETED` | operator removed an un-uploaded clip — terminal |

```
UPLOADED ──local retention──► DELETED_LOCAL ──cloud retention──► PURGED
   └────────cloud retention──► EXPIRED_CLOUD ──local retention──► PURGED
```

`SKIPPED_BY_POLICY → DELETED_LOCAL` stays: such a clip never had a cloud copy, so removing the
file removes the only one and there is nothing left to purge.

The eligibility sets follow:

| Set | States | Question it answers |
|---|---|---|
| `LOCAL_EXPIRY_STATES` | `UPLOADED`, `SKIPPED_BY_POLICY`, `EXPIRED_CLOUD` | may this file be deleted? |
| `CLOUD_EXPIRY_STATES` | `UPLOADED`, `DELETED_LOCAL` | may this object be deleted? |
| `HELD_STATES` | `PENDING`, `UPLOADING`, `RETRY_WAIT`, `FAILED_PERMANENT` | is INV-1 costing disk? |

`HELD_STATES` became an allow-list rather than "not terminal". The old negation was right while
`DELETED_LOCAL` and `EXPIRED_CLOUD` were terminal; once they describe clips waiting for their
**second** deletion, counting them as held would inflate the number that exists to show the cost
of the invariant.

## Consequences

- **`cloud_days` and `local_days` both mean what they say.** 60 days in the bucket, 90 on the
  SSD, whichever happens first, and the row records which copies are left.
- **`DELETED_LOCAL` and `EXPIRED_CLOUD` are no longer terminal**, so any code that treated them as
  an end state had to be revisited. Two places did: the local and cloud expiry queries, and the
  overdue-retention metric.
- **A migration was required** (`0005`): the `upload_state` CHECK is closed, so `PURGED` could not
  be written until the table was rebuilt. Forward-only, and the rebuild copies columns explicitly
  rather than `SELECT *`.
- **The state names now mislead if read as "what is left" in one word.** The contract's table is
  the authority, and `doctor`/`/status` should render the two timestamp columns rather than the
  state name when the distinction matters.
- **A clip is only `PURGED` when both deletions were provider-confirmed.** A hide-instead-of-delete
  keeps the row `UPLOADED` (or `DELETED_LOCAL`) and records a `cloud_delete_failed` finding, so
  "purged" never means "we asked and hoped".

## Triggers to reopen

- A third copy appears (a second provider, an offsite mirror): the model becomes a set of
  destinations with their own timestamps, not two states plus a terminal one.
- Anyone wants to keep the cloud copy **longer** than the local one as a deliberate archive: that
  is already expressible (`cloud_days > local_days`) and needs no change here.
- `FORCE_DELETED` is ever implemented (ADR-0021 is deferred): it removes a clip that was never
  uploaded, and its terminal status is unchanged by this ADR.
