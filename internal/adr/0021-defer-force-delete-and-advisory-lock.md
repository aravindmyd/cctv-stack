# ADR-0021: Force-delete and the cleanup advisory lock are deferred

- **Status:** Accepted
- **Date:** 2026-10-18
- **Deciders:** project owner
- **Related:** [`lld/05-cleanup.md`](../architecture/lld/05-cleanup.md) §8, §10, [`ADR-0009`](0009-cleanup-safety-invariant.md), [INV-1](../architecture/invariants.md), M4.13 and M4.14 in `.private/plans/milestones.md`

---

## Context

M4's local retention (J1) is built and runs as the `cleanup` service. Two of M4's exit criteria
remain unmet:

| # | Criterion | What it protects against |
|---|---|---|
| M4.13 | `force-delete` requires a clip id and a non-empty reason, and records `FORCE_DELETED` | A clip that will never upload is otherwise undeletable by design, so it holds disk forever and `/health` reports `clips_overdue_retention` indefinitely |
| M4.14 | Two cleanup instances cannot run concurrently | Someone scaling the service, or a recreate overlapping a slow pass, runs two retention passes over one database |

Both are specified in LLD 05 (§10 and §8 respectively) and neither is implemented. The
remaining work is not small enough to leave implicit: M4.13 needs the admin routing layer
(`health.AdminRoutes` is currently uploader-shaped with two fixed callables) generalised before
a third route fits, and M4.14 needs lease columns on `sweep_state`, which means migration
`0003`.

## Decision

**Defer both M4.13 and M4.14 until a deployment needs them. Build neither now.**

The reason is the deployment shape rather than the difficulty. Sites are single-host,
single-compose-stack installations that one person operates by hand:

- **The lock guards a scenario that cannot occur.** `cleanup` is one service in one compose
  file. There is no autoscaling, no multiple hosts, and no operator workflow that legitimately
  starts a second instance. The narrow residue — `docker compose up -d` recreating the
  container while the previous one is still finishing a pass — is survivable: the
  enumerated-state query and the four further guards in `src/cctv/cleanup.py` mean a double run
  re-checks each row before deleting and cannot turn a granted clip into a lost one.
- **No operator has needed the force-delete path yet.** It exists for a clip that will never
  upload, and no site has reported one. Building an audited destructive endpoint before the
  need is demonstrated adds a permanent security surface to buy a convenience nobody has
  asked for; the hand procedure below remains available in the meantime.

Writing this down is the point: `milestones.md` marks M4.13/M4.14 as deferred rather than
missing, and LLD 05 §8/§10 carry a note saying the described mechanism is not implemented.
Until this ADR is superseded, no document may claim the lock or the endpoint exists.

## Consequences

### Positive

- No migration `0003`, no new destructive HTTP route, and no untested operator path shipped.
- The retention guarantees that matter — INV-1, the enumerated states, and the guards — are
  already built, tested and running; nothing here weakens them.

### Negative

- **M4 cannot be closed as written.** M4.13 and M4.14 stay ⬜, so "M4 complete" is not a claim
  this project can make. `milestones.md` says so explicitly.
- A clip that genuinely will never upload still has no supported removal path. Today's
  procedure is the one in [`docs/recover-uploads.md`](../../docs/recover-uploads.md) §6:
  delete the file and the row by hand and record why. That is auditable only by what the
  operator writes down.
- Two concurrent cleanup instances are survivable rather than prevented. They will not delete
  an ineligible clip, but they can produce confusing duplicate log lines and a doubled
  `system_events` entry.

## Triggers to revisit

Any one of these makes the deferral wrong and should reopen this ADR:

1. A real clip reaches `FAILED_PERMANENT` and the operator needs it gone without hand-editing
   the database.
2. Cleanup stops being a single container — multiple hosts, autoscaling, or a second scheduled
   runner.
3. J2 cloud retention is built. Cloud deletes are the operation where a duplicated pass is
   most expensive (egress, hide-versus-delete), so the lock should arrive with J2 at the
   latest.
