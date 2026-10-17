"""The reconciliation sweep: find reviews we have not turned into clips, and turn them.

MQTT is the fast path and it is lossy — at-most-once from our side, and worse than that:
Frigate closes a review at start-up **without publishing `end` at all**. Measured, with
the subscriber on a broker that never restarted. A deployment trusting `end` loses that
clip silently. So the sweep is not a backstop for a rare failure; it is the mechanism
that makes materialisation complete, and MQTT is only the latency optimisation.

## `after` filters on `end_time`, not `start_time` — and the watermark depends on it

Frigate's `/api/review` applies the window as:

```python
clauses = [(start_time < before) & ((end_time.is_null()) | (end_time > after))]
```

So a review that *started* before the watermark but *ended* after it is still returned.
That is why the watermark is the highest `end_time` fully processed rather than a
`start_time`: advancing past a start time would skip any review that was still open when
we last looked, which is exactly when a restart happens.

## Progressing without losing work

If a review defers — the flush race, a transient Frigate failure — the watermark must not
move past it, or the next sweep never sees it again and the clip is lost by *retrying*.
But it also cannot stay put, or one persistently failing review blocks the sweep forever.

So the watermark advances to the highest `end_time` terminally decided, and then retreats
to just before the earliest *deferred* review. Deferrals are therefore revisited, and
everything else progresses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from . import db
from .config import CameraConfig, CamerasConfig
from .frigate import FrigateError
from .materialise import CREATED, DEFERRED, FAILED, SKIPPED, MaterialiseOutcome
from .reviews import ReviewCandidate, candidates_from_reviews, decide
from .timeutil import Instant
from .timeutil import now as _now

__all__ = [
    "DEFAULT_LOOKBACK_S",
    "WATERMARK_EPSILON",
    "WATERMARK_NAME",
    "ReviewLister",
    "ReviewSweep",
    "SweepResult",
]

#: Where the watermark lives in `sweep_state`. Named for reviews rather than events,
#: since ADR-0016.
WATERMARK_NAME = "reviews_sweep"

#: On a first run there is no watermark, and Frigate may hold months of history whose
#: footage has long expired. Sweeping all of it would produce a burst of doomed fetches, so
#: a fresh install starts from a bounded lookback instead.
DEFAULT_LOOKBACK_S = 3600.0

#: The `after` filter is strictly `end_time > after`, so a deferred review is only
#: re-fetched if the watermark sits *just* before it.
WATERMARK_EPSILON = 0.001


class ReviewLister(Protocol):
    def list_reviews(
        self, *, after: Instant | None = None, before: Instant | None = None, limit: int = ...
    ) -> list[dict]: ...


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep did. Counts rather than a boolean, because "how far behind am I" is
    the question the health check and the dashboard both need answered.
    """

    considered: int = 0
    materialised: int = 0
    skipped: int = 0
    deferred: int = 0
    failed: int = 0
    unknown_camera: int = 0
    watermark: float | None = None
    #: Set only when the sweep itself could not run — Frigate unreachable, for instance.
    #: Not for per-review failures, which are counted instead.
    failure: str | None = None
    outcomes: tuple[MaterialiseOutcome, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def needs_another_run(self) -> bool:
        """True when something is waiting to be retried."""
        return self.deferred > 0


@dataclass(frozen=True, slots=True)
class _Decision:
    candidate: ReviewCandidate
    camera: CameraConfig | None
    outcome: MaterialiseOutcome | None = None
    skip_reason: str | None = None


class ReviewSweep:
    """Walks the reviews Frigate holds and materialises the ones we are missing."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        client: ReviewLister,
        materialiser,
        clock: Callable[[], Instant] = _now,
        lookback_s: float = DEFAULT_LOOKBACK_S,
        limit: int = 200,
        watermark_name: str = WATERMARK_NAME,
    ) -> None:
        self.conn = conn
        self.client = client
        self.materialiser = materialiser
        self._clock = clock
        self.lookback_s = lookback_s
        self.limit = limit
        self.watermark_name = watermark_name

    # -----------------------------------------------------------------------------------

    def run(self, *, config: CamerasConfig) -> SweepResult:
        """One pass. Intended to be called on a timer, not in a tight loop."""
        now = self._clock()
        after = self._resume_point(now)

        try:
            raw = self.client.list_reviews(after=after, before=now + 1, limit=self.limit)
        except FrigateError as exc:
            # Frigate unreachable. The watermark does not move and the failure count rises,
            # so health can tell "idle" apart from "failing for an hour".
            db.set_watermark(
                self.conn,
                name=self.watermark_name,
                watermark=after,
                succeeded=False,
                note=str(exc)[:200],
                now=now,
            )
            return SweepResult(watermark=after, failure=f"could not list reviews: {exc}")

        candidates = candidates_from_reviews(raw)
        if not candidates:
            db.set_watermark(
                self.conn, name=self.watermark_name, watermark=after, succeeded=True, now=now
            )
            return SweepResult(watermark=after)

        known = db.known_review_ids(self.conn, [c.review_id for c in candidates])
        decisions = [
            self._handle(candidate, config=config, known=known) for candidate in candidates
        ]

        watermark = self._advance(decisions, resume_point=after)
        db.set_watermark(
            self.conn, name=self.watermark_name, watermark=watermark, succeeded=True, now=now
        )

        outcomes = tuple(d.outcome for d in decisions if d.outcome is not None)
        return SweepResult(
            considered=len(decisions),
            materialised=sum(1 for o in outcomes if o.status == CREATED),
            skipped=sum(1 for d in decisions if d.skip_reason is not None)
            + sum(1 for o in outcomes if o.status == SKIPPED),
            deferred=sum(1 for o in outcomes if o.status == DEFERRED),
            failed=sum(1 for o in outcomes if o.status == FAILED),
            unknown_camera=sum(1 for d in decisions if d.skip_reason == "unknown camera"),
            watermark=watermark,
            outcomes=outcomes,
        )

    # -----------------------------------------------------------------------------------

    def _handle(
        self,
        candidate: ReviewCandidate,
        *,
        config: CamerasConfig,
        known: set[str],
    ) -> _Decision:
        camera = config.cameras.get(candidate.camera)
        if camera is None or not camera.enabled:
            # A review for a camera that is not in `cameras.yml`, or was retired. Not an
            # error: Frigate may still hold history for a camera we no longer manage.
            return _Decision(candidate, None, skip_reason="unknown camera")

        verdict = decide(
            candidate,
            severities=camera.clip.severities,
            objects=camera.clip.objects,
            known_review_ids=known,
        )
        if not verdict.materialise:
            return _Decision(candidate, camera, skip_reason=verdict.reason)

        outcome = self.materialiser.materialise(
            candidate,
            pre_roll_s=camera.clip.pre_roll_s,
            post_roll_s=camera.clip.post_roll_s,
        )
        return _Decision(candidate, camera, outcome=outcome)

    def _resume_point(self, now: Instant) -> Instant:
        state = db.get_watermark(self.conn, self.watermark_name)
        if state is None or state["watermark"] is None:
            return now - self.lookback_s
        return float(state["watermark"])

    def _advance(self, decisions: list[_Decision], *, resume_point: Instant) -> Instant:
        """The new watermark: past everything settled, but not past anything deferred."""
        settled: list[float] = []
        deferred: list[float] = []

        for decision in decisions:
            end = decision.candidate.end_time
            if end is None:
                # An open review has no end to advance to, and it will come back anyway:
                # the listing treats a null `end_time` as "inside the window" always.
                continue
            if decision.outcome is not None and decision.outcome.status == DEFERRED:
                deferred.append(end)
            else:
                # Created, skipped or permanently failed — all terminal. A failure does not
                # hold the watermark back, or one un-fetchable review would freeze the
                # sweep forever; it is alerted on instead.
                settled.append(end)

        watermark = max(settled) if settled else resume_point
        if deferred:
            # Retreat to just before the earliest deferral, because the listing's filter is
            # strictly `end_time > after` — sitting exactly on it would hide it.
            watermark = min(watermark, min(deferred) - WATERMARK_EPSILON)
        # Never backwards: redoing work is safe, skipping it is not.
        return max(watermark, resume_point)


def cameras_for(config: CamerasConfig) -> Mapping[str, CameraConfig]:
    """Convenience for callers that want the enabled cameras only."""
    return config.enabled_cameras()
