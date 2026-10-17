"""Turning a review item into a candidate clip, and deciding whether to make one.

This is the trigger. It exists as pure functions because the restart experiment made its
shape non-negotiable: **a Frigate restart closes a review without publishing `end` at
all** — measured, with the subscriber connected to a broker that never restarted. A
trigger that waits for `type: end` loses that clip silently. So the trigger evaluates on
*every* message, and the only condition that matters is whether `end_time` is set.

## Two shapes, one parser

The same review arrives in two forms and both must parse:

```jsonc
// frigate/reviews on MQTT
{"type": "end", "before": {…}, "after": {"id": …, "end_time": …}}

// GET /api/review
[{"id": …, "end_time": …}]
```

Writing two parsers would mean two places to keep in step, and the sweep — which reads the
API — would drift from the trigger, which reads MQTT. That drift would show up as clipped
reviews the sweep keeps re-offering forever.

## Tolerance is a requirement, not politeness

Contract §3.4: "Unknown fields and unknown cameras must not break parsing." Frigate adds
fields between releases, and a stricter parser would start rejecting every message after an
upgrade — turning a routine update into an outage, remotely, with no way to patch it from
here. Unknown keys are therefore ignored, and only the fields we actually need are read.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .timeutil import Instant

__all__ = [
    "ReviewCandidate",
    "Verdict",
    "candidate_from_message",
    "candidates_from_reviews",
    "decide",
    "parse_review",
]

SEVERITIES = frozenset({"alert", "detection"})


class ReviewParseError(ValueError):
    """A review payload missing something we cannot proceed without."""


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """A review item, reduced to the fields we act on.

    `event_ids` come from `data.detections` and are preserved rather than discarded, so a
    per-visit clip stays searchable by event — the sidecar and the `clips.event_ids` column
    both carry them.
    """

    review_id: str
    camera: str
    severity: str
    start_time: Instant
    #: `None` while the review is still open. Not a missing value — an open visit, which
    #: must not be clipped, because clipping it would capture half a visit with no way to
    #: extend it afterwards.
    end_time: Instant | None
    event_ids: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    sub_labels: tuple[str, ...] = ()
    audio: tuple[str, ...] = ()
    thumb_path: str | None = None

    @property
    def is_open(self) -> bool:
        return self.end_time is None

    @property
    def duration_s(self) -> float | None:
        return None if self.end_time is None else self.end_time - self.start_time


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether to materialise, and why not.

    A reason rather than a bare boolean, because "why did this clip not appear" is the
    question an operator actually asks, and answering it from a log line beats
    reconstructing it from config.
    """

    materialise: bool
    reason: str

    @classmethod
    def yes(cls) -> Verdict:
        return cls(True, "ok")

    @classmethod
    def no(cls, reason: str) -> Verdict:
        return cls(False, reason)


def _as_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # A single label serialised as a bare string rather than a one-element list.
        # Frigate does this in places, and refusing it would drop a real review.
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    raise ReviewParseError(f"{where} must be a list or a string, got {type(value).__name__}.")


def _data_of(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract the `data` block, which arrives as a dict or as a JSON string.

    Both are real: MQTT and the HTTP API deliver a dict, while the same field read back
    out of Frigate's own database is a JSON string. Handling one and not the other is the
    kind of bug that only appears in the sweep, hours later.
    """
    raw = payload.get("data")
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewParseError(f"`data` is a string but not JSON: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise ReviewParseError("`data` decoded to something other than an object.")
        return decoded
    raise ReviewParseError(f"`data` must be an object or a JSON string, got {type(raw).__name__}.")


def parse_review(payload: Mapping[str, Any]) -> ReviewCandidate:
    """Parse one review object, in either shape, tolerantly.

    Raises:
        ReviewParseError: an id, camera, severity or start time is missing, or a field we
            read has the wrong type. Unknown *keys* are ignored by design.
    """
    if not isinstance(payload, Mapping):
        raise ReviewParseError(f"Expected a review object, got {type(payload).__name__}.")

    review_id = payload.get("id")
    if not isinstance(review_id, str) or not review_id:
        raise ReviewParseError("A review must have a non-empty string `id`.")

    camera = payload.get("camera")
    if not isinstance(camera, str) or not camera:
        raise ReviewParseError(f"Review {review_id}: missing `camera`.")

    severity = payload.get("severity")
    if severity not in SEVERITIES:
        raise ReviewParseError(
            f"Review {review_id}: severity {severity!r} is not one of {sorted(SEVERITIES)}."
        )

    start_time = payload.get("start_time")
    if not isinstance(start_time, (int, float)) or isinstance(start_time, bool):
        raise ReviewParseError(f"Review {review_id}: `start_time` must be a number.")

    end_time = payload.get("end_time")
    if end_time is not None and (
        not isinstance(end_time, (int, float)) or isinstance(end_time, bool)
    ):
        raise ReviewParseError(f"Review {review_id}: `end_time` must be a number or null.")
    if end_time is not None and end_time < start_time:
        raise ReviewParseError(
            f"Review {review_id}: ends ({end_time}) before it starts ({start_time})."
        )

    data = _data_of(payload)
    return ReviewCandidate(
        review_id=review_id,
        camera=camera,
        severity=str(severity),
        start_time=float(start_time),
        end_time=None if end_time is None else float(end_time),
        event_ids=_as_tuple(data.get("detections"), where=f"Review {review_id}: data.detections"),
        objects=_as_tuple(data.get("objects"), where=f"Review {review_id}: data.objects"),
        zones=_as_tuple(data.get("zones"), where=f"Review {review_id}: data.zones"),
        sub_labels=_as_tuple(data.get("sub_labels"), where=f"Review {review_id}: data.sub_labels"),
        audio=_as_tuple(data.get("audio"), where=f"Review {review_id}: data.audio"),
        thumb_path=(
            payload.get("thumb_path") if isinstance(payload.get("thumb_path"), str) else None
        ),
    )


def candidate_from_message(message: Mapping[str, Any]) -> ReviewCandidate | None:
    """Parse an MQTT `frigate/reviews` message.

    **The message `type` is deliberately not consulted.** `new`, `update` and `end` all
    carry the current state of the review in `after`, and the restart experiment showed
    that `end` cannot be trusted to arrive. Reading `after` on every message is what makes
    the trigger work regardless.

    Returns `None` when a message is structurally unusable — a payload with no `after`, or
    one that fails to parse. A malformed message must never stop the worker: it is one
    message, and the sweep will find the review anyway.
    """
    after = message.get("after")
    if not isinstance(after, Mapping):
        return None
    try:
        return parse_review(after)
    except ReviewParseError:
        return None


def candidates_from_reviews(reviews: Sequence[Any]) -> list[ReviewCandidate]:
    """Parse `GET /api/review`, skipping anything unusable.

    Skips rather than raises, for the same reason as above: one bad row must not stop a
    sweep that is the only thing standing between a restart and a lost clip.
    """
    candidates: list[ReviewCandidate] = []
    for entry in reviews:
        if not isinstance(entry, Mapping):
            continue
        try:
            candidates.append(parse_review(entry))
        except ReviewParseError:
            continue
    return sorted(candidates, key=lambda c: c.start_time)


def decide(
    candidate: ReviewCandidate,
    *,
    severities: Collection[str],
    objects: Collection[str],
    known_review_ids: Collection[str] = (),
) -> Verdict:
    """Whether this review should become a clip, and if not, why.

    The order of the checks is the order of the log lines an operator will read: an open
    review is the most common reason by a wide margin and the cheapest to rule out, and
    "already materialised" comes next because it is what a sweep sees for every review it
    has already handled.

    `known_review_ids` arrives **already resolved** rather than being queried here, so this
    function stays pure and a caller can batch the lookup for a whole page of reviews. That
    also makes the membership test a set lookup rather than a database round trip.
    """
    if candidate.is_open:
        return Verdict.no("review is still open")

    if candidate.review_id in known_review_ids:
        return Verdict.no("already materialised")

    if candidate.severity not in severities:
        return Verdict.no(f"severity {candidate.severity} is not in clip.severities")

    if not objects:
        # An empty filter is a configuration error caught earlier, but treating it as
        # "match nothing" here would silently produce no clips at all.
        return Verdict.no("clip.objects is empty")

    if not any(label in objects for label in candidate.objects):
        # This is also where an audio-only review lands: it populates `data.audio` and
        # leaves `data.objects` empty, so with the default `[person]` filter it is dropped.
        # Nothing can be pinned for it either, since there is no event to retain.
        return Verdict.no(f"no label in clip.objects among {list(candidate.objects) or ['(none)']}")

    return Verdict.yes()
