"""Getting review messages off the network thread and into a worker.

The MQTT callback must not do the work. Materialising a clip waits out the flush grace,
fetches, probes and writes — tens of seconds — and doing that on the network loop's thread
stops it servicing the socket, so messages are *missed*. That failure is invisible: the
broker does not know we were busy, the queue does not exist, and the only symptom is clips
that never appear.

So the callback parses, hands over, and returns. A bounded queue and a worker thread do the
rest.

## Dropping under load is correct, and blocking is not

MQTT delivery here is at-most-once (see below), so **the sweep is the guarantee and this is
the latency optimisation**. Given that, a full queue must drop and count rather than block:
blocking the callback costs messages *and* stalls every other topic, whereas dropping costs
only latency — the sweep will find the review on its next pass.

## The pre-filter is queue hygiene, not a second decision

Most traffic on `frigate/reviews` is `update` messages about reviews that are still **open**,
and an open review can never become a clip. Queueing them would let a chatty camera fill a
bounded queue and cause *closed* reviews to be dropped — losing exactly the messages that
mattered, to make room for ones that could not be acted on.

So open reviews are dropped at the door. This is not a duplicate of
[`reviews.decide`][cctv.reviews.decide] — it is the one condition that has to be applied
before the queue exists, and `decide` still applies it (and every other filter) in the
worker, because the sweep path has no door to filter at.

## Why not QoS 1

Subscribing at QoS 1 does not upgrade a publisher sending at QoS 0, and a broker restart
still loses in-flight messages. The completeness guarantee has to come from somewhere that
does not depend on delivery at all, which is the sweep. Claiming reliability here would
only move the lie somewhere harder to find.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .reviews import (
    ReviewCandidate,
    ReviewParseError,
    candidate_from_message,
    parse_review,
)

__all__ = [
    "DROPPED",
    "OPEN",
    "QUEUED",
    "UNPARSABLE",
    "DispatchStats",
    "ReviewDispatcher",
]

QUEUED = "queued"
DROPPED = "dropped"
UNPARSABLE = "unparsable"
OPEN = "open"

#: Deep enough to ride out a burst of closed reviews, shallow enough that a stall is
#: noticed as a count rather than as unbounded memory.
DEFAULT_MAX_PENDING = 1000


@dataclass(slots=True)
class DispatchStats:
    """Counters for the health check.

    `dropped` and `unparsable` existing as *numbers* is the point: a system that silently
    discards a tenth of its input and one that is simply idle look identical from the
    outside.
    """

    received: int = 0
    queued: int = 0
    handle: int = 0
    open_reviews: int = 0
    unparsable: int = 0
    dropped: int = 0
    handler_errors: int = 0
    last_error: str | None = None
    _reasons: dict[str, int] = field(default_factory=dict, repr=False)

    def note(self, reason: str) -> None:
        self._reasons[reason] = self._reasons.get(reason, 0) + 1

    @property
    def reasons(self) -> Mapping[str, int]:
        return dict(self._reasons)

    @property
    def lost(self) -> int:
        """Messages that will only be recovered by the sweep."""
        return self.dropped + self.unparsable


class ReviewDispatcher:
    """Bounded hand-off between the MQTT callback and the materialiser.

    `handler` receives a closed [`ReviewCandidate`][cctv.reviews.ReviewCandidate] and may
    take as long as it likes: it runs on the worker, never on the callback.
    """

    def __init__(
        self,
        *,
        handler: Callable[[ReviewCandidate], Any],
        max_pending: int = DEFAULT_MAX_PENDING,
        on_open: Callable[[ReviewCandidate], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1.")
        self.handler = handler
        self.max_pending = max_pending
        self.on_open = on_open
        self.stats = DispatchStats()

        self._queue: queue.Queue[ReviewCandidate] = queue.Queue(maxsize=max_pending)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -----------------------------------------------------------------------------------
    # Called from the network thread. Must not block, and must not raise.
    # -----------------------------------------------------------------------------------

    def dispatch(self, message: Mapping[str, Any]) -> str:
        """Parse and enqueue one `frigate/reviews` message.

        Returns what happened, so a caller can count it. **Never raises**: an exception
        escaping an MQTT callback is swallowed by the network loop, so a single bad message
        would otherwise stop every future message from being seen.
        """
        self.stats.received += 1

        candidate = self._parse(message)
        if candidate is None:
            # Counted, not just noted. `lost` is derived from this, and a discard that is
            # only in the reason map would make the health check report nothing discarded.
            self.stats.unparsable += 1
            return UNPARSABLE

        if candidate.is_open:
            # The majority of traffic, and none of it can ever be a clip. See the module
            # docstring: queueing it would evict the messages that matter.
            self.stats.open_reviews += 1
            self.stats.note("review still open")
            if self.on_open is not None:
                _swallow(self.on_open, candidate)
            return OPEN

        try:
            self._queue.put_nowait(candidate)
        except queue.Full:
            self.stats.dropped += 1
            self.stats.note("queue full")
            return DROPPED

        self.stats.queued += 1
        return QUEUED

    def _parse(self, message: Mapping[str, Any]) -> ReviewCandidate | None:
        """Parse without trusting the shape.

        `after` is read on **every** message type. The restart experiment showed Frigate
        closes a review without publishing `end` at all, so a trigger keyed on the message
        type would lose that clip silently.
        """
        if not isinstance(message, Mapping):
            self.stats.note("message is not an object")
            return None
        try:
            if "after" in message:
                return candidate_from_message(message)
            # Accept a bare review object too: the sweep reads that shape from the API, and
            # a test or a replay tool will hand it over in the same form.
            return parse_review(message)
        except ReviewParseError as exc:
            self.stats.note(str(exc)[:120])
            return None

    # -----------------------------------------------------------------------------------
    # The worker
    # -----------------------------------------------------------------------------------

    def work_once(self, *, timeout: float | None = None) -> ReviewCandidate | None:
        """Take one candidate and handle it. Returns what was handled, if anything.

        Synchronous so tests need neither a broker nor a thread.
        """
        try:
            candidate = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

        try:
            self.handler(candidate)
        except Exception as exc:
            # A handler failure is counted, not fatal. The review is still in Frigate, so
            # the sweep will offer it again — which is the whole reason this can be
            # forgiving.
            self.stats.handler_errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self.stats.note("handler error")
        else:
            self.stats.handle += 1
        finally:
            self._queue.task_done()

        return candidate

    def drain(self, *, limit: int | None = None) -> int:
        """Handle everything currently queued. Returns how many were handled."""
        handled = 0
        while limit is None or handled < limit:
            if self.work_once(timeout=0) is None:
                break
            handled += 1
        return handled

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # -----------------------------------------------------------------------------------
    # Thread lifecycle
    # -----------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Dispatcher already started.")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="review-dispatcher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.work_once(timeout=0.5)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> ReviewDispatcher:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _swallow(callback: Callable[[ReviewCandidate], Any], candidate: ReviewCandidate) -> None:
    """Run an optional observer, ignoring whatever it does.

    Used for the `on_open` hook, which exists so a caller can log or count open reviews
    without that being able to break the callback.
    """
    with contextlib.suppress(Exception):
        callback(candidate)
