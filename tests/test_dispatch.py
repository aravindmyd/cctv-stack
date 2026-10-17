"""Tests for the dispatcher — the hand-off between the broker and the work.

The two things worth proving: **a callback never raises and never blocks**, because either
one silently stops every future message; and an open review never occupies a queue slot,
because it would evict the closed reviews that can actually become clips.
"""

from __future__ import annotations

import time

import pytest

from cctv.dispatch import DROPPED, OPEN, QUEUED, UNPARSABLE, ReviewDispatcher

START = 1_700_000_000.0
END = START + 30

CLOSED = {
    "id": "1718987129.308396-fqk5ka",
    "camera": "front-door",
    "severity": "alert",
    "start_time": START,
    "end_time": END,
    "data": {"detections": ["ev-1"], "objects": ["person"]},
}
OPEN_REVIEW = {**CLOSED, "end_time": None}

MESSAGE = {"type": "end", "before": OPEN_REVIEW, "after": CLOSED}


class Collector:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, candidate) -> None:
        self.seen.append(candidate.review_id)


def build(*, handler=None, max_pending=1000, on_open=None) -> tuple[ReviewDispatcher, Collector]:
    collector = handler or Collector()
    dispatcher = ReviewDispatcher(handler=collector, max_pending=max_pending, on_open=on_open)
    return dispatcher, collector


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_a_closed_review_is_queued() -> None:
    dispatcher, _ = build()
    assert dispatcher.dispatch(MESSAGE) == QUEUED
    assert dispatcher.pending == 1
    assert dispatcher.stats.queued == 1


def test_the_worker_handles_what_the_callback_queued() -> None:
    dispatcher, collector = build()
    dispatcher.dispatch(MESSAGE)

    handled = dispatcher.work_once(timeout=0)

    assert handled is not None
    assert collector.seen == [CLOSED["id"]]
    assert dispatcher.stats.handle == 1
    assert dispatcher.pending == 0


def test_an_update_carrying_a_closed_review_is_queued() -> None:
    """The restart finding, at the dispatcher level. Frigate closes a review without
    publishing `end`, so the message type cannot be part of the condition."""
    dispatcher, _ = build()
    message = {"type": "update", "before": OPEN_REVIEW, "after": CLOSED}
    assert dispatcher.dispatch(message) == QUEUED


def test_a_bare_review_object_is_accepted() -> None:
    """The shape the API returns, and what a replay tool would hand over."""
    dispatcher, _ = build()
    assert dispatcher.dispatch(CLOSED) == QUEUED


def test_a_bare_open_review_is_filtered_the_same_way() -> None:
    dispatcher, _ = build()
    assert dispatcher.dispatch(OPEN_REVIEW) == OPEN


# --------------------------------------------------------------------------------------
# The pre-filter — queue hygiene
# --------------------------------------------------------------------------------------


def test_an_open_review_never_takes_a_queue_slot() -> None:
    """The most common message by far, and none of it can become a clip. Queueing it would
    evict the closed reviews that matter."""
    dispatcher, _ = build()
    message = {"type": "update", "before": OPEN_REVIEW, "after": OPEN_REVIEW}

    assert dispatcher.dispatch(message) == OPEN
    assert dispatcher.pending == 0
    assert dispatcher.stats.open_reviews == 1
    assert dispatcher.stats.queued == 0


def test_an_open_review_cannot_evict_a_closed_one() -> None:
    """The failure this prevents: a chatty camera filling the queue with updates about a
    visit in progress, dropping the message that said the visit had ended."""
    dispatcher, _ = build(max_pending=1)
    dispatcher.dispatch(MESSAGE)

    for _ in range(50):
        dispatcher.dispatch({"type": "update", "after": OPEN_REVIEW})

    assert dispatcher.stats.dropped == 0
    assert dispatcher.pending == 1


def test_an_open_hook_is_called_and_cannot_break_the_callback() -> None:
    calls: list[str] = []

    def exploding(candidate) -> None:
        calls.append(candidate.review_id)
        raise RuntimeError("the observer blew up")

    dispatcher, _ = build(on_open=exploding)
    assert dispatcher.dispatch({"type": "update", "after": OPEN_REVIEW}) == OPEN
    assert calls == [OPEN_REVIEW["id"]]


# --------------------------------------------------------------------------------------
# The callback never raises
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        None,
        [],
        "a string",
        42,
        {},
        {"type": "end"},
        {"type": "end", "after": None},
        {"after": {"id": ""}},
        {
            "after": {
                "id": "x",
                "camera": "c",
                "severity": "alert",
                "start_time": "soon",
            }
        },
        {"after": CLOSED, "unexpected": object()},
    ],
)
def test_the_callback_never_raises(message) -> None:
    """An exception escaping an MQTT callback is swallowed by the network loop, so one bad
    message would stop every future message from being seen — with no error anywhere."""
    dispatcher, _collector = build()

    outcome = dispatcher.dispatch(message)  # must not raise

    assert outcome in (UNPARSABLE, OPEN, QUEUED)
    if outcome == UNPARSABLE:
        assert dispatcher.stats.unparsable == 1


def test_an_unparsable_message_is_counted_with_a_reason() -> None:
    dispatcher, _ = build()
    dispatcher.dispatch({"type": "end"})
    assert dispatcher.stats.unparsable == 1
    assert dispatcher.stats.reasons  # a reason was recorded, for the log line


def test_the_callback_never_blocks_when_the_queue_is_full() -> None:
    """Dropping costs latency, because the sweep will find the review. Blocking costs the
    socket, and stalls every other topic with it."""
    dispatcher, _ = build(max_pending=2)
    outcomes = [
        dispatcher.dispatch({**MESSAGE, "after": {**CLOSED, "id": f"r-{i}"}}) for i in range(5)
    ]

    assert outcomes == [QUEUED, QUEUED, DROPPED, DROPPED, DROPPED]
    assert dispatcher.stats.dropped == 3
    assert dispatcher.stats.reasons.get("queue full") == 3


def test_lost_measures_what_only_the_sweep_can_recover() -> None:
    dispatcher, _ = build(max_pending=1)
    dispatcher.dispatch(MESSAGE)
    dispatcher.dispatch({**MESSAGE, "after": {**CLOSED, "id": "r-2"}})  # dropped
    dispatcher.dispatch("nonsense")  # unparsable

    assert dispatcher.stats.dropped == 1
    assert dispatcher.stats.unparsable == 1
    assert dispatcher.stats.lost == 2


# --------------------------------------------------------------------------------------
# The worker survives a bad handler
# --------------------------------------------------------------------------------------


def test_a_handler_failure_is_counted_and_the_worker_continues() -> None:
    """The review is still in Frigate, so the sweep offers it again — which is exactly why
    this can be forgiving instead of fatal."""

    class Exploding:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, candidate) -> None:
            self.calls += 1
            raise RuntimeError("fetch failed")

    handler = Exploding()
    dispatcher = ReviewDispatcher(handler=handler)

    dispatcher.dispatch(MESSAGE)
    assert dispatcher.work_once(timeout=0) is not None
    dispatcher.dispatch({**MESSAGE, "after": {**CLOSED, "id": "r-2"}})
    assert dispatcher.work_once(timeout=0) is not None

    assert handler.calls == 2
    assert dispatcher.stats.handler_errors == 2
    assert dispatcher.stats.handle == 0
    assert "fetch failed" in (dispatcher.stats.last_error or "")


def test_drain_empties_the_queue() -> None:
    dispatcher, collector = build()
    for index in range(4):
        dispatcher.dispatch({**MESSAGE, "after": {**CLOSED, "id": f"r-{index}"}})

    assert dispatcher.drain() == 4
    assert len(collector.seen) == 4
    assert dispatcher.pending == 0


def test_work_once_on_an_empty_queue_returns_nothing() -> None:
    dispatcher, _ = build()
    assert dispatcher.work_once(timeout=0) is None


# --------------------------------------------------------------------------------------
# Thread lifecycle
# --------------------------------------------------------------------------------------


def test_starting_twice_is_a_programming_error() -> None:
    dispatcher, _ = build()
    dispatcher.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            dispatcher.start()
    finally:
        dispatcher.stop()


def test_the_thread_drains_the_queue_and_stops() -> None:
    """The real lifecycle, as the service uses it."""
    dispatcher, collector = build()
    for index in range(5):
        dispatcher.dispatch({**MESSAGE, "after": {**CLOSED, "id": f"r-{index}"}})

    dispatcher.start()
    deadline = time.monotonic() + 5.0
    while len(collector.seen) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    dispatcher.stop()

    assert len(collector.seen) == 5
    assert dispatcher.pending == 0
    assert dispatcher.stats.handle == 5


def test_max_pending_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ReviewDispatcher(handler=lambda candidate: None, max_pending=0)
