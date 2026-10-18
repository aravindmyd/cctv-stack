# ADR-0016: A canonical clip is one Frigate review item, not one event

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [ADR-0006](0006-canonical-clip-layout.md), [ADR-0007](0007-mqtt-api-over-filesystem-watching.md), [INV-5](../architecture/invariants.md), [`lld/02`](../architecture/lld/02-clip-worker.md), [`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md)

---

## Context

Frigate tracks *objects*. One person walking up a driveway, waiting at a door, and
leaving is three tracked objects and therefore **three Frigate events** — and clipping
per event produces three overlapping files containing largely the same footage.

```text
18:32:07   event A  ████████              ┐
18:32:18   event B           ███████████  │  one review item
18:32:34   event C                        ▘ 18:32:07 → 18:32:41
```

| | Per event | Per visit |
|---|---|---|
| Files per visitor | 3 | **1** |
| Storage (this example) | ~73 s | **~49 s** |
| Browsing | three near-identical clips | **one clip of what happened** |

The owner had already solved this in a previous script, grouping events into **visit
windows** using a `GAP_TOLERANCE` and a `MAX_CLIP_DURATION` cap — independently
arriving at the same conclusion.

Critically, **Frigate already computes this grouping.** Its review items are activity
periods built from one or more events, with a `start_time`, an `end_time`, a
`severity` (`alert` or `detection`), a stable id, and the list of constituent event ids
in `data.detections`. They are published on `frigate/reviews` with a final `end`
message.

The original design ignored this and clipped per event, which would have produced
overlapping duplicates and roughly a third more storage than necessary.

## Decision

**The canonical clip is one Frigate review item. We consume Frigate's grouping rather
than implementing our own.**

| Concern | Decision |
|---|---|
| Trigger | `frigate/reviews`, evaluating the condition on **every** message regardless of `type` |
| Dedup key | **review id** (`UNIQUE(review_id)`), not event id |
| Fetch range | `[review.start_time − pre_roll_s, review.end_time + post_roll_s]` via the range endpoint |
| Clip path basis | **review `start_time`** (see [ADR-0006](0006-canonical-clip-layout.md), amended) |
| Event relationship | Event ids from `data.detections` are stored in the sidecar, preserving 1:1 searchability |
| Which reviews | Configurable per camera: `clip.severities` (default `[alert, detection]`) and `clip.objects` (default `[person]`), per the owner: *"99% it's just Person"* |
| Long reviews | **Never split** — the whole review is stored as one clip |
| Long-clip handling | `long_clip_warn_s` **flags** a clip and surfaces the count. It never truncates and never splits |

### Why not hand-roll the windowing

The owner's script implemented clustering, gap tolerance, a cap, a cooling period, and
window extension logic in ~200 lines of shell and embedded Python. It worked, but it had
to reimplement review grouping, and each of those parameters is a way to get it wrong.

Frigate's review items are the same concept with none of that surface area: computed
upstream, stable ids, severity included, and maintained by people who know the detection
pipeline.

### Why "never split" is more defensible here than in a hand-rolled window

The owner's `MAX_CLIP_DURATION=300` existed partly to bound a window that *they* were
keeping open. With review items, **Frigate decides when activity has ended** — it has
stationary-object handling and closes reviews when objects stop being tracked. The
pathological case (a parked car reviewed for six hours) is therefore rarer than it would
be with a hand-rolled window, and the owner has chosen to accept it rather than add a
split.

`long_clip_warn_s` (default 1800) remains as a **flag only**, so the case stays visible
without changing behaviour.

## Consequences

### Positive

- **One clip per visit.** Matches what a human wants to watch, and what the owner built
  before.
- **Roughly a third less storage** on the reference example, and roughly a third fewer
  uploads — which also means fewer Class A calls.
- **~200 lines of windowing logic not written**, not tested, and not maintained.
- **Severity filtering for free.** `alert` vs `detection` is already computed, so
  "alerts only" is a config value rather than a feature.
- **A stable, meaningful dedup key.** A review id identifies one period of activity,
  which is exactly the unit we produce.
- **Fewer materialisation events.** Higher latency tolerance than per-event clipping,
  because a review only ends once activity has genuinely stopped.
- The pin, the flush-grace schedule, the coverage check, and the atomic write all carry
  over unchanged — only the trigger and the key change.

### Negative / accepted costs

- **We lose the strict 1:1 file-to-event mapping.** Mitigated by storing every
  constituent event id in the sidecar, so event-level search still works.
- **A review's `end_time` can move.** A review is `update`d as activity continues, so we
  must not act until it is final. The trigger requires a non-null `end_time`, and the
  dedup key makes repeated `update` handling harmless.
- **A long review is one long fetch.** Materialisation happens once, at the end, so a
  six-hour review is a six-hour download in a single request — with a 60-second
  `fetch_timeout_s` much too small. `fetch_timeout_s` must scale with duration, and
  `fetch_concurrency` bounds how many such fetches run at once.
- **A long clip is an inconvenient artefact.** A multi-hour file is awkward to scrub
  through, and a partial download at that size is expensive to retry. Accepted by
  explicit owner decision; `long_clip_warn_s` keeps it visible.
- **Storage estimates change shape.** Fewer, longer files. Peak usage is less spiky but
  the tail is longer.
- **Review grouping is now a dependency we do not control.** If Frigate changes its gap
  tolerance or how it closes reviews, our clip boundaries move. Pinned version plus the
  integration contract contains this.
- **New unverified surface.** The `frigate/reviews` payload, whether `end` is always
  published, and the review gap tolerance are all unprobed. See the `VERIFY` note in
  [`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md).

### Neutral

- Clips remain keyed on a start time, so the path layout is unchanged in shape — only
  *which* start time. Clips are not re-keyed if a review is later extended, because we
  only act on a finalised review.
- Existing per-event materialisation logic is not deleted, it is re-pointed: the range,
  the pin, the flush grace, and the coverage check are all reused.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Per-event clips** (the original design) | Three overlapping files per visitor and ~⅓ more storage. Rejected once the owner's prior art made the intent clear |
| **Hand-rolled visit windows**, as the owner's script did | Works, and the owner proved it. Rejected because it reimplements review grouping and adds five tunable parameters, each a way to be wrong. Frigate maintains the same concept for free |
| **Both: per-event files *and* per-visit files** | Doubles storage to serve two audiences that are 95% the same. The sidecar's event ids give event-level search without a second file |
| **`GET /api/review/{id}/clip.mp4`** instead of the range endpoint | Cleaner in principle, but its `padding` is symmetric, so asymmetric pre-roll and post-roll would inflate every clip — the same reasoning as [ADR-0015](0015-clip-boundaries-and-padding.md). We read `start_time`/`end_time` from the review and request the exact range ourselves |
| **Split long reviews at a duration cap** as the owner's script did | Reasonable, and considered. Declined by owner decision, since review length is bounded by Frigate's own activity detection rather than by our windowing, and a split would need its own naming rule (defeating deterministic paths) |
| **Filter to `severity: alert` only** | Far fewer clips and much less cloud cost, and a legitimate configuration. Rejected as the *default* because it silently drops activity the operator may want, and detection-only reviews are exactly the near-misses worth keeping |

## Revisit if

- The `frigate/reviews` payload or `end` semantics prove unreliable, in which case the
  event-based trigger returns as the primary and reviews become the grouping hint
- Long reviews become a practical problem — large partial-download retries, or unwatchable
  files — at which point splitting needs a deterministic naming rule
- Storage exceeds projections because reviews group more aggressively than expected
- Frigate exposes a grouping guarantee strong enough that clip boundaries become stable
  before `end`, which would allow materialisation with lower latency
