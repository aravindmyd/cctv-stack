# ADR-0022: Continuous recording for two days, locally only

- **Status:** Accepted
- **Date:** 2026-10-20
- **Deciders:** project owner
- **Related:** [`cameras-config-schema.md`](../architecture/contracts/cameras-config-schema.md) §4.1,
  [`lld/05-cleanup.md`](../architecture/lld/05-cleanup.md), [INV-1](../architecture/invariants.md),
  [INV-9](../architecture/invariants.md), [`ADR-0015`](0015-clip-boundaries-and-padding.md)

---

## Context

The generated default was `record.mode: motion`: Frigate kept three days of footage around
motion, thirty days of alert events, fourteen of detection events, and **no continuous
recording at all**. That default was chosen to protect the disk, and it does — but it has a
cost that only becomes obvious once the system is in use:

**An incident with no motion leaves no footage.** A parcel left at a gate, a vehicle idling in
a corner of the frame, someone walking a line the detector does not cover: none of these
produce a review, so none produce a clip, and with motion-only recording there is no
Frigate-side footage to go back to either. The one thing a camera is for — *what happened
here at 02:14?* — has no answer in that case.

The second reason is the clip worker's recovery budget ([INV-9](../architecture/invariants.md)).
That budget is the Frigate-side window, so at three days of motion-only footage it is three
days of *motion*, not three days of time.

Frigate's retention is also the only retention in this system that is never billed: those
recordings are not uploaded, not listed in the database, and not backed up.

## Decision

**Default to `record.mode: continuous` with two days of continuous footage. Keep the other
windows where they are: `motion` seven days, alerts thirty, detections fourteen. Continuous
recordings stay local and are never uploaded to B2.**

| Block | Days | Why |
|---|---|---|
| `continuous` | **2** | Bounded, predictable disk. Satisfies `min_headroom_days: 2` exactly |
| `motion` | 7 | Footage attached to activity outlives the 24/7 churn by five days |
| `alerts` | 30 | Frigate's own alert events, unchanged |
| `detections` | 14 | Frigate's own detection events, unchanged |

Three properties make two days safe to be the default rather than an opt-in:

1. **It is bounded and known.** Continuous retention is roughly `bitrate × 86400 / 8` per
   camera per day — about 21 GB per camera per day at 2 Mbps, so about 43 GB per camera for
   two days. That is a number an operator can size a disk against, unlike "keep everything".
2. **It cannot grow into the bucket.** The uploader only ever uploads rows from the `clips`
   table, whose paths are built by `paths.primary_relpath` under `clips/`. Frigate's
   recordings live under `data/media/`, are not in the database, and no code path enumerates
   them. `cleanup` explicitly refuses to touch them
   (`tests/test_cleanup.py::test_frigates_footage_is_byte_identical_after_a_full_cycle`), and
   `cctv-backup` writes only configuration and the database.
3. **Expiry is Frigate's job.** We generate the retention blocks and never delete inside
   `data/media/`. If our cleanup service is broken, continuous footage still expires.

`min_headroom_days` (default 2) is now satisfied exactly rather than comfortably. That is
intentional: it makes the relationship visible. Raising `min_headroom_days` above 2 without
raising `continuous.days` is refused at generation time with the reason — the guard is
working, not broken.

## Consequences

- **The disk now grows between visits.** A site with no activity still writes ~21 GB per
  camera per day. The installer does not yet project this, and `doctor` warns only below
  5 GiB free; both should learn about continuous footage (recorded in the backlog).
- **A two-day outage is now the exact limit**, not a comfortable margin, for recovering a
  review whose footage has not been materialised yet. This is a deliberate trade: 48 hours
  of *all* footage, versus 72 hours of only-motion footage.
- **Upgrading a site changes its disk profile without changing its configuration.** Existing
  sites that relied on the old default gain continuous recording. That is a MINOR bump with a
  loud release note (lld/09 §1.1), not a silent change: 0.7.0 documents it, and
  `record.mode: motion` restores the old behaviour for anyone who wants it back.
- **The HLD's capacity narrative changes.** "Continuous disabled by default" was one of the
  reasons the design was cheap; that reason is now "continuous is bounded to two days and
  never leaves the host".

## Triggers to reopen

- A site reports filling its disk because of continuous footage (raise the projection work,
  or drop to 1 day).
- Cloud backup of *continuous* footage is ever proposed: that is a different decision with a
  different cost, and it must not happen by relaxing this one.
- Frigate's `record` semantics change such that `continuous.days: 2` no longer means two days
  of wall-clock footage.
