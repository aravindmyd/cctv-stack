"""Deciding *when* to fetch a clip, and whether what came back is good enough.

Two separate responsibilities, both pure, both here so that the clip-worker's
orchestration has nothing subtle left in it:

1. **When.** A clip must not be fetched until the last recording segment covering the
   review has been flushed to disk. Fetching earlier does not fail loudly — it returns
   `200` with a *short* clip, which is the worst kind of bug because the MP4 is valid and
   plays fine. Measured on the real stack: at 5 s after the review ended, 5 of 6 samples
   came back silently truncated; the first full-length result was at 10 s. That is why the
   default grace is 15 s and why [`FetchPlan.not_before`][cctv.clip.FetchPlan.not_before]
   is a value that must be waited for rather than a hint.

2. **Whether it is good enough.** A truncated clip, a clip with a hole, and a perfect clip
   are all valid MP4s that upload successfully. Telling them apart is what
   [INV-14](../../../internal/architecture/invariants.md) requires, and it is why these
   decisions are pure functions with names rather than a chain of `if`s in a worker loop.

**A short clip is never discarded.** Retrying improves the outcome; discarding cannot. When
attempts run out the best result obtained is kept and marked degraded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .timeutil import Instant

__all__ = [
    "DEFAULT_COVERAGE_OK",
    "DEFAULT_COVERAGE_POLICY",
    "DEFAULT_COVERAGE_RETRY",
    "DEFAULT_FETCH_ATTEMPTS",
    "DEFAULT_FETCH_TIMEOUT_BASE_S",
    "DEFAULT_FETCH_TIMEOUT_CEILING_S",
    "DEFAULT_LONG_CLIP_WARN_S",
    "DEFAULT_POST_ROLL_S",
    "DEFAULT_PRE_ROLL_S",
    "DEFAULT_SEGMENT_FLUSH_GRACE_S",
    "ClipPlanError",
    "CoveragePolicy",
    "FetchPlan",
    "Verdict",
    "fetch_timeout_s",
    "judge_coverage",
    "pick_best",
    "plan_fetch",
]

DEFAULT_PRE_ROLL_S = 5.0
DEFAULT_POST_ROLL_S = 10.0

#: Measured, not guessed. The floor for a reliable fetch was 10 s after the review ended;
#: 5 s raced Frigate's segment flush. See `spike/RESULTS.md`.
DEFAULT_SEGMENT_FLUSH_GRACE_S = 15.0

#: `actual_duration / requested_duration` at or above this is a good clip.
DEFAULT_COVERAGE_OK = 0.95

#: Below this, the clip is short enough that another attempt is worth making. Note the gap
#: between the two thresholds is the *degraded* band: kept, flagged, not retried forever.
DEFAULT_COVERAGE_RETRY = 0.80

DEFAULT_FETCH_ATTEMPTS = 3

#: A fetch is a range read plus an `ffmpeg` cut, so its cost is roughly linear in the
#: clip's duration. A fixed timeout is therefore not a safety net but a silent failure
#: mode: at 120 s, every review longer than about two minutes fails, and with a generous
#: `visit_gap_s` those are precisely the interesting ones.
DEFAULT_FETCH_TIMEOUT_BASE_S = 120.0

#: Two hours. The ceiling exists to bound a *hang* rather than a slow-but-working fetch, and
#: it is only defensible if footage-length plus headroom fits underneath it — otherwise the
#: timeout is shorter than the clip it is fetching, which is a guaranteed failure dressed up
#: as a bound. At the previous 1800 s the cliff was a 28-minute visit: past that the timeout
#: was below the clip's own length, so one long review became three doomed attempts and then
#: a `FAILED_PERMANENT` — the worst possible clip to lose, because a long visit is the
#: interesting one.
#:
#: What remains beyond two hours is a *review* that long, which is Frigate's decision rather
#: than ours, and the answer to it is measurement (backlog §1.3) rather than a bigger number
#: here. A review kept open for a whole day has to be segmented, not fetched with a 24-hour
#: timeout that occupies a fetch slot for a day.
DEFAULT_FETCH_TIMEOUT_CEILING_S = 7200.0

#: Flag only. A long review is stored in full, never truncated and never split.
#:
#: Deliberately well below the ceiling: it is the leading indicator, so a visit heading toward
#: the two-hour cliff is visible in the log long before it can fail. At 600 s it fired on
#: ordinary long visits instead, and a warning that always fires is a warning nobody reads.
DEFAULT_LONG_CLIP_WARN_S = 1800.0


class ClipPlanError(ValueError):
    """A fetch that cannot be planned as asked."""


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """The range to request, and the earliest instant it may be requested.

    Asymmetric on purpose: `pre_roll_s` and `post_roll_s` differ, so the symmetric
    `?padding=` on Frigate's review endpoint cannot express this — hence the range
    endpoint ([ADR-0015](../../../internal/adr/0015-clip-boundaries-and-padding.md)).
    """

    review_id: str
    requested_start: Instant
    requested_end: Instant
    #: `end_time + post_roll_s + segment_flush_grace_s`. Waiting for this is what stops the
    #: silent truncation the spike measured.
    not_before: Instant
    timeout_s: float

    @property
    def requested_s(self) -> float:
        return self.requested_end - self.requested_start

    def seconds_until_ready(self, now: Instant) -> float:
        """How long to wait before fetching. Negative means it is already safe."""
        return self.not_before - now

    def is_ready(self, now: Instant) -> bool:
        return now >= self.not_before


def fetch_timeout_s(
    requested_s: float,
    *,
    base_s: float = DEFAULT_FETCH_TIMEOUT_BASE_S,
    ceiling_s: float = DEFAULT_FETCH_TIMEOUT_CEILING_S,
) -> float:
    """A timeout proportional to the clip's length, clamped.

    Proportionality matters because the alternative is worse than it looks: a clip that
    times out *is* retried, so a fixed timeout turns one long review into three doomed
    attempts and then a permanent failure — losing the clip entirely rather than taking
    longer over it. At the original 120 s, every review longer than about two minutes
    failed that way, and with a generous `visit_gap_s` those are the interesting ones.

    The ceiling exists for a different job: bounding a *hang* rather than a slow-but-working
    fetch. A generous timeout is right for a large clip, but an unbounded one lets a stuck
    connection occupy a fetch slot for as long as the clip is long, which is how a queue
    quietly stops draining.

    The two conflict only for a clip longer than the ceiling, and the arithmetic of the
    compromise is worth stating rather than hiding: at the ceiling the timeout stops growing,
    so a review whose footage is longer than `ceiling - base` gets a timeout shorter than its
    own length. Raising the ceiling moves that point instead of removing it, and removing it
    entirely would mean no bound at all — so the ceiling was set from the longest visit worth
    supporting rather than from the largest number that looked safe. Past two hours a *review*
    that long is Frigate's decision, and the answer to it is measurement (`lld/02` §3.5), not a
    longer timeout.
    """
    if requested_s < 0:
        raise ClipPlanError(f"requested duration cannot be negative, got {requested_s}.")
    return min(base_s + requested_s, ceiling_s)


def plan_fetch(
    review_id: str,
    *,
    start_time: Instant,
    end_time: Instant,
    pre_roll_s: float = DEFAULT_PRE_ROLL_S,
    post_roll_s: float = DEFAULT_POST_ROLL_S,
    segment_flush_grace_s: float = DEFAULT_SEGMENT_FLUSH_GRACE_S,
    timeout_base_s: float = DEFAULT_FETCH_TIMEOUT_BASE_S,
    timeout_ceiling_s: float = DEFAULT_FETCH_TIMEOUT_CEILING_S,
) -> FetchPlan:
    """Build the fetch plan for one review.

    Raises:
        ClipPlanError: the review has no end, ends before it starts, or a roll is negative.
    """
    if not review_id:
        raise ClipPlanError("A fetch plan needs the review id it belongs to.")
    if end_time is None:  # type: ignore[comparison-overlap]
        raise ClipPlanError(
            f"Review {review_id!r} has no end_time, so it is still open. Fetching now would "
            f"capture a partial visit, and the clip could never be extended afterwards."
        )
    if end_time < start_time:
        raise ClipPlanError(
            f"Review {review_id!r} ends ({end_time}) before it starts ({start_time})."
        )
    for name, value in (
        ("pre_roll_s", pre_roll_s),
        ("post_roll_s", post_roll_s),
        ("segment_flush_grace_s", segment_flush_grace_s),
    ):
        if value < 0:
            raise ClipPlanError(f"{name} cannot be negative, got {value}.")

    requested_start = start_time - pre_roll_s
    requested_end = end_time + post_roll_s

    return FetchPlan(
        review_id=review_id,
        requested_start=requested_start,
        requested_end=requested_end,
        not_before=requested_end + segment_flush_grace_s,
        timeout_s=fetch_timeout_s(
            requested_end - requested_start, base_s=timeout_base_s, ceiling_s=timeout_ceiling_s
        ),
    )


# ---------------------------------------------------------------------------------------
# Is it good enough?
# ---------------------------------------------------------------------------------------


class Verdict(StrEnum):
    """What to do with a fetched clip."""

    #: Accept it. `coverage_ratio` at or above `coverage_ok`.
    ACCEPT = "accept"
    #: Short, and another attempt is affordable. Keep it in hand first.
    RETRY = "retry"
    #: Short, and attempts are exhausted. Store it, mark it degraded, alert.
    ACCEPT_DEGRADED = "accept_degraded"


@dataclass(frozen=True, slots=True)
class CoveragePolicy:
    ok: float = DEFAULT_COVERAGE_OK
    retry: float = DEFAULT_COVERAGE_RETRY
    max_attempts: int = DEFAULT_FETCH_ATTEMPTS

    def __post_init__(self) -> None:
        if not 0 < self.retry <= self.ok <= 2.0:
            raise ClipPlanError(
                f"Coverage thresholds must satisfy 0 < retry ({self.retry}) <= ok "
                f"({self.ok}). A `retry` above `ok` would mean a clip that was already "
                f"acceptable still gets re-fetched."
            )
        if self.max_attempts < 1:
            raise ClipPlanError("max_attempts must be at least 1.")

    @property
    def degraded_band(self) -> tuple[float, float]:
        """The range that is stored and flagged rather than retried further."""
        return (self.retry, self.ok)


#: A module singleton, not a call in a signature. `CoveragePolicy` is frozen, so this
#: cannot be mutated by a caller that forgets to pass its own.
DEFAULT_COVERAGE_POLICY = CoveragePolicy()


def judge_coverage(
    coverage_ratio: float | None,
    *,
    attempts_used: int,
    policy: CoveragePolicy = DEFAULT_COVERAGE_POLICY,
) -> Verdict:
    """Classify a fetch result.

    `coverage_ratio` is `actual_duration / requested_duration`, and it is **expected to
    exceed 1.0 sometimes**. `-c copy` can only cut at keyframes, and the spike measured it
    erring *outward* by roughly 1.9 s per 10 s of clip. Over-running is the intended
    behaviour ([INV-14]): overshooting costs storage, undershooting loses evidence. So a
    ratio above 1.0 is a success, not an anomaly to correct.

    `None` means the duration could not be measured at all, which is treated as a failed
    attempt rather than a perfect one.
    """
    if attempts_used < 1:
        raise ClipPlanError("attempts_used must be at least 1 after a fetch has happened.")

    if coverage_ratio is None:
        return Verdict.RETRY if attempts_used < policy.max_attempts else Verdict.ACCEPT_DEGRADED

    if coverage_ratio >= policy.ok:
        return Verdict.ACCEPT

    if attempts_used < policy.max_attempts:
        return Verdict.RETRY

    return Verdict.ACCEPT_DEGRADED


def pick_best(candidates: Sequence[object]) -> object | None:
    """The best of several attempts. Never discard a result in favour of nothing.

    Ranked by coverage first, then by duration. Ties broken by duration because when two
    attempts cover the same fraction of the request, the longer one contains more footage
    — coverage alone can tie while the clips differ materially, which happens when the
    second attempt was cut against different keyframes.
    """
    scored = [c for c in candidates if getattr(c, "coverage_ratio", None) is not None]
    if not scored:
        # Nothing measurable. Return the longest if we can, else the last attempt.
        with_duration = [c for c in candidates if getattr(c, "duration_s", None) is not None]
        if with_duration:
            return max(with_duration, key=lambda c: c.duration_s)  # type: ignore[attr-defined]
        return candidates[-1] if candidates else None
    return max(
        scored,
        key=lambda c: (c.coverage_ratio, c.duration_s or 0.0),  # type: ignore[attr-defined]
    )
