"""Tests for fetch planning and clip-quality judgement. INV-14."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cctv import clip
from cctv.clip import ClipPlanError, CoveragePolicy, Verdict

NOW = 1_700_000_000.0


# --------------------------------------------------------------------------------------
# When to fetch — the race the spike measured
# --------------------------------------------------------------------------------------


def test_plan_uses_asymmetric_rolls() -> None:
    plan = clip.plan_fetch("rev-1", start_time=NOW, end_time=NOW + 30)
    assert plan.requested_start == NOW - 5
    assert plan.requested_end == NOW + 40
    assert plan.requested_s == 45


def test_plan_honours_a_longer_post_roll() -> None:
    plan = clip.plan_fetch("rev-1", start_time=NOW, end_time=NOW + 30, post_roll_s=20)
    assert plan.requested_start == NOW - 5
    assert plan.requested_end == NOW + 50


def test_not_before_includes_the_flush_grace() -> None:
    """The 5 s default raced Frigate's segment flush and returned short clips."""
    plan = clip.plan_fetch("rev-1", start_time=NOW, end_time=NOW + 30)
    assert plan.not_before == NOW + 40 + clip.DEFAULT_SEGMENT_FLUSH_GRACE_S
    assert plan.not_before > plan.requested_end


def test_readiness_helpers_agree_with_not_before() -> None:
    plan = clip.plan_fetch("rev-1", start_time=NOW, end_time=NOW + 30)
    assert plan.is_ready(plan.not_before) is True
    assert plan.is_ready(plan.not_before - 0.001) is False
    assert plan.seconds_until_ready(plan.not_before - 10) == pytest.approx(10)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"end_time": None}, "still open"),
        ({"end_time": NOW - 1}, "ends .* before it starts"),
        ({"pre_roll_s": -1}, "pre_roll_s cannot be negative"),
        ({"post_roll_s": -1}, "post_roll_s cannot be negative"),
        ({"segment_flush_grace_s": -1}, "segment_flush_grace_s cannot be negative"),
    ],
)
def test_impossible_plans_are_refused(kwargs: dict, match: str) -> None:
    base = {"start_time": NOW, "end_time": NOW + 30}
    base.update(kwargs)
    with pytest.raises(ClipPlanError, match=match):
        clip.plan_fetch("rev-1", **base)  # type: ignore[arg-type]


def test_a_plan_without_a_review_id_is_refused() -> None:
    with pytest.raises(ClipPlanError, match="review id"):
        clip.plan_fetch("", start_time=NOW, end_time=NOW + 30)


def test_a_zero_length_fetch_is_allowed() -> None:
    """A review can be very short; that is not a planning error."""
    plan = clip.plan_fetch("rev-1", start_time=NOW, end_time=NOW, pre_roll_s=0, post_roll_s=0)
    assert plan.requested_s == 0
    assert plan.not_before == NOW + clip.DEFAULT_SEGMENT_FLUSH_GRACE_S


# --------------------------------------------------------------------------------------
# Timeout — the silent failure mode for long reviews
# --------------------------------------------------------------------------------------


def test_timeout_grows_with_the_clip() -> None:
    assert clip.fetch_timeout_s(0) == clip.DEFAULT_FETCH_TIMEOUT_BASE_S
    assert clip.fetch_timeout_s(600) == clip.DEFAULT_FETCH_TIMEOUT_BASE_S + 600


def test_timeout_is_capped() -> None:
    assert clip.fetch_timeout_s(10_000) == clip.DEFAULT_FETCH_TIMEOUT_CEILING_S


def test_a_two_hour_review_gets_a_timeout_far_larger_than_the_base() -> None:
    """At a fixed 120 s this review would fail three times and be lost entirely.

    The ceiling still applies — it bounds a hang, not a slow fetch — so the assertion is
    that the change is material, not that the timeout is unbounded.
    """
    plan = clip.plan_fetch("rev-long", start_time=NOW, end_time=NOW + 7200)
    assert plan.timeout_s == clip.DEFAULT_FETCH_TIMEOUT_CEILING_S
    assert plan.timeout_s > clip.DEFAULT_FETCH_TIMEOUT_BASE_S * 10


def test_negative_duration_is_refused() -> None:
    with pytest.raises(ClipPlanError, match="negative"):
        clip.fetch_timeout_s(-1)


# --------------------------------------------------------------------------------------
# Coverage — INV-14 as a decision
# --------------------------------------------------------------------------------------


def test_a_full_clip_is_accepted() -> None:
    assert clip.judge_coverage(1.0, attempts_used=1) is Verdict.ACCEPT


def test_erring_outward_is_a_success_not_an_anomaly() -> None:
    """`-c copy` cuts at keyframes only and measured ~1.9 s over per 10 s of clip.

    Overshooting costs storage; undershooting loses evidence. So a ratio above 1.0 must
    not be treated as needing correction — which is the tempting mistake.
    """
    assert clip.judge_coverage(1.19, attempts_used=1) is Verdict.ACCEPT


def test_a_slightly_short_clip_is_retried_while_attempts_remain() -> None:
    assert clip.judge_coverage(0.90, attempts_used=1) is Verdict.RETRY


def test_a_very_short_clip_is_retried_while_attempts_remain() -> None:
    assert clip.judge_coverage(0.40, attempts_used=1) is Verdict.RETRY


def test_a_short_clip_is_stored_rather_than_discarded() -> None:
    """INV-14: retrying improves the outcome, discarding cannot. So the floor is 'keep'."""
    assert clip.judge_coverage(0.40, attempts_used=3) is Verdict.ACCEPT_DEGRADED


def test_an_unmeasurable_clip_is_never_treated_as_perfect() -> None:
    assert clip.judge_coverage(None, attempts_used=1) is Verdict.RETRY
    assert clip.judge_coverage(None, attempts_used=3) is Verdict.ACCEPT_DEGRADED


def test_the_degraded_band_bounds() -> None:
    policy = CoveragePolicy()
    assert policy.degraded_band == (0.80, 0.95)
    assert clip.judge_coverage(0.95, attempts_used=3, policy=policy) is Verdict.ACCEPT
    assert clip.judge_coverage(0.9499, attempts_used=3, policy=policy) is Verdict.ACCEPT_DEGRADED


def test_judging_without_an_attempt_is_a_programming_error() -> None:
    with pytest.raises(ClipPlanError, match="at least 1"):
        clip.judge_coverage(1.0, attempts_used=0)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: CoveragePolicy(ok=0.5, retry=0.9), id="retry-above-ok"),
        pytest.param(lambda: CoveragePolicy(ok=0.95, retry=0.0), id="retry-not-positive"),
        pytest.param(lambda: CoveragePolicy(ok=3.0, retry=0.5), id="ok-over-2"),
        pytest.param(lambda: CoveragePolicy(max_attempts=0), id="no-attempts"),
    ],
)
def test_nonsensical_policies_are_refused(factory) -> None:
    """Validation happens at construction, so an invalid policy cannot exist to be used.

    The factories are deferred for exactly that reason: building these values at collection
    time would raise while pytest was still importing the module.
    """
    with pytest.raises(ClipPlanError):
        factory()


# --------------------------------------------------------------------------------------
# pick_best — a retry must never lose ground
# --------------------------------------------------------------------------------------


@dataclass
class Result:
    coverage_ratio: float | None
    duration_s: float | None


def test_pick_best_takes_the_highest_coverage() -> None:
    results = [Result(0.6, 27.0), Result(0.99, 44.0), Result(0.8, 36.0)]
    assert clip.pick_best(results) is results[1]


def test_pick_best_breaks_ties_on_duration() -> None:
    """Equal coverage can hide different footage when attempts cut at different keyframes."""
    results = [Result(0.95, 42.0), Result(0.95, 45.5)]
    assert clip.pick_best(results) is results[1]


def test_pick_best_ignores_unmeasurable_attempts_when_a_measurable_one_exists() -> None:
    results = [Result(None, 40.0), Result(0.7, 31.0)]
    assert clip.pick_best(results) is results[1]


def test_pick_best_falls_back_to_the_longest_unmeasurable_attempt() -> None:
    results = [Result(None, 10.0), Result(None, 25.0)]
    assert clip.pick_best(results) is results[1]


def test_pick_best_of_nothing_is_nothing() -> None:
    assert clip.pick_best([]) is None


def test_two_failed_attempts_do_not_beat_one_good_one() -> None:
    """The property that makes retry-then-keep-best safe: keeping is monotonic."""
    attempts = [Result(0.55, 25.0), Result(0.61, 27.0), Result(0.58, 26.0)]
    best = clip.pick_best(attempts)
    assert best is attempts[1]  # type: ignore[comparison-overlap]
    assert best.coverage_ratio >= max(a.coverage_ratio for a in attempts)  # type: ignore[union-attr]
