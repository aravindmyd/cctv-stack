"""Tests for the trigger. The restart experiment is why most of these exist."""

from __future__ import annotations

import json

import pytest

from cctv import reviews
from cctv.reviews import ReviewParseError, Verdict

START = 1_700_000_000.0
END = START + 30

CLOSED = {
    "id": "1718987129.308396-fqk5ka",
    "camera": "front-door",
    "severity": "alert",
    "start_time": START,
    "end_time": END,
    "thumb_path": "/media/frigate/clips/review/thumb.webp",
    "data": {
        "detections": ["1718987128.947436-g92ztx"],
        "objects": ["person"],
        "sub_labels": [],
        "zones": ["porch"],
        "audio": [],
    },
}

OPEN = {**CLOSED, "end_time": None}


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def test_parses_a_closed_review() -> None:
    candidate = reviews.parse_review(CLOSED)
    assert candidate.review_id == "1718987129.308396-fqk5ka"
    assert candidate.camera == "front-door"
    assert candidate.severity == "alert"
    assert candidate.start_time == START
    assert candidate.end_time == END
    assert candidate.event_ids == ("1718987128.947436-g92ztx",)
    assert candidate.objects == ("person",)
    assert candidate.zones == ("porch",)
    assert candidate.is_open is False
    assert candidate.duration_s == 30


def test_parses_an_open_review() -> None:
    """An open review is not an error — it is a visit in progress, which is not clipable."""
    candidate = reviews.parse_review(OPEN)
    assert candidate.is_open is True
    assert candidate.end_time is None
    assert candidate.duration_s is None


def test_data_may_arrive_as_a_json_string() -> None:
    """MQTT and the API send a dict; the same field read from Frigate's database is a
    string. Handling only one is a bug that appears in the sweep, hours later."""
    payload = {**CLOSED, "data": json.dumps(CLOSED["data"])}
    assert reviews.parse_review(payload).objects == ("person",)


def test_a_missing_data_block_is_tolerated() -> None:
    payload = {k: v for k, v in CLOSED.items() if k != "data"}
    candidate = reviews.parse_review(payload)
    assert candidate.objects == ()
    assert candidate.event_ids == ()


def test_a_single_label_may_be_a_bare_string() -> None:
    """Frigate serialises a one-element list as a string in places. Refusing it would
    drop a real review over a formatting detail."""
    payload = {**CLOSED, "data": {**CLOSED["data"], "objects": "person"}}
    assert reviews.parse_review(payload).objects == ("person",)


def test_unknown_fields_are_ignored() -> None:
    """Contract §3.4. A stricter parser would reject every message after a Frigate
    upgrade — remotely, with no way to patch it from here."""
    payload = {**CLOSED, "something_new": {"nested": [1, 2]}, "another": "field"}
    payload["data"] = {**CLOSED["data"], "future_key": "value"}
    assert reviews.parse_review(payload).objects == ("person",)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"id": ""}, "non-empty string `id`"),
        ({"id": None}, "non-empty string `id`"),
        ({"camera": ""}, "missing `camera`"),
        ({"camera": 7}, "missing `camera`"),
        ({"severity": "motion"}, "not one of"),
        ({"severity": None}, "not one of"),
        ({"start_time": "soon"}, "`start_time` must be a number"),
        ({"end_time": "soon"}, "`end_time` must be a number or null"),
        ({"end_time": START - 1}, "ends .* before it starts"),
    ],
)
def test_unusable_reviews_are_refused(mutation: dict, match: str) -> None:
    with pytest.raises(ReviewParseError, match=match):
        reviews.parse_review({**CLOSED, **mutation})


def test_a_non_mapping_review_is_refused() -> None:
    with pytest.raises(ReviewParseError, match="Expected a review object"):
        reviews.parse_review(["not", "a", "review"])  # type: ignore[arg-type]


def test_data_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ReviewParseError, match="must be an object"):
        reviews.parse_review({**CLOSED, "data": 42})


# --------------------------------------------------------------------------------------
# The MQTT message — where the restart finding lives
# --------------------------------------------------------------------------------------


def test_extracts_the_after_block() -> None:
    message = {"type": "end", "before": OPEN, "after": CLOSED}
    candidate = reviews.candidate_from_message(message)
    assert candidate is not None
    assert candidate.end_time == END


def test_the_message_type_is_not_consulted() -> None:
    """The whole point. A Frigate restart closes a review **without publishing `end`** —
    measured, with the subscriber on a broker that never restarted. A trigger keyed on
    `type == "end"` would lose that clip silently, so `after` is read on every message.
    """
    message = {"type": "update", "before": OPEN, "after": CLOSED}
    candidate = reviews.candidate_from_message(message)
    assert candidate is not None
    assert candidate.review_id == CLOSED["id"]


def test_an_update_still_carrying_an_open_review_is_not_a_candidate() -> None:
    """Reading every message must not mean acting on every message."""
    message = {"type": "update", "before": OPEN, "after": OPEN}
    candidate = reviews.candidate_from_message(message)
    assert candidate is not None  # it parsed…
    assert candidate.is_open is True  # …and `decide` will reject it


@pytest.mark.parametrize(
    "message",
    [
        {"type": "end"},  # no `after`
        {"type": "end", "after": None},
        {"type": "end", "after": "a string"},
        {"type": "end", "after": {"id": ""}},  # malformed enough to raise
        {"after": {"id": "x", "camera": "c", "severity": "alert", "start_time": "soon"}},
    ],
)
def test_an_unusable_message_returns_none_rather_than_raising(message: dict) -> None:
    """One bad message must not stop the worker. The sweep will find the review anyway."""
    assert reviews.candidate_from_message(message) is None


def test_an_empty_message_is_survivable() -> None:
    assert reviews.candidate_from_message({}) is None


# --------------------------------------------------------------------------------------
# The API list — what the sweep reads
# --------------------------------------------------------------------------------------


def test_parses_a_list_of_reviews_sorted_by_start() -> None:
    later = {**CLOSED, "id": "later", "start_time": START + 100, "end_time": START + 130}
    earlier = {**CLOSED, "id": "earlier"}
    candidates = reviews.candidates_from_reviews([later, earlier])
    assert [c.review_id for c in candidates] == ["earlier", "later"]


def test_a_bad_row_is_skipped_not_fatal() -> None:
    """The sweep is the only thing standing between a restart and a lost clip, so one
    malformed row must not abort it."""
    candidates = reviews.candidates_from_reviews(
        [CLOSED, {"id": ""}, "not a mapping", None, {**CLOSED, "id": "second"}]
    )
    assert [c.review_id for c in candidates] == [CLOSED["id"], "second"]


def test_an_empty_review_list_yields_nothing() -> None:
    assert reviews.candidates_from_reviews([]) == []


# --------------------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------------------


def test_an_alert_with_a_tracked_object_becomes_a_clip() -> None:
    verdict = reviews.decide(reviews.parse_review(CLOSED), severities={"alert"}, objects={"person"})
    assert verdict == Verdict(True, "ok")
    assert verdict.materialise is True


def test_an_open_review_is_never_materialised() -> None:
    """Clipping it would capture half a visit, and a clip cannot be extended afterwards."""
    verdict = reviews.decide(reviews.parse_review(OPEN), severities={"alert"}, objects={"person"})
    assert verdict.materialise is False
    assert verdict.reason == "review is still open"


def test_an_already_materialised_review_is_skipped() -> None:
    verdict = reviews.decide(
        reviews.parse_review(CLOSED),
        severities={"alert"},
        objects={"person"},
        known_review_ids={CLOSED["id"]},
    )
    assert verdict.reason == "already materialised"


def test_severity_filtering_only_keeps_alerts() -> None:
    detection = {**CLOSED, "severity": "detection"}
    verdict = reviews.decide(
        reviews.parse_review(detection), severities={"alert"}, objects={"person"}
    )
    assert verdict.materialise is False
    assert "not in clip.severities" in verdict.reason


def test_detections_are_kept_when_asked_for() -> None:
    detection = {**CLOSED, "severity": "detection"}
    assert reviews.decide(
        reviews.parse_review(detection), severities={"alert", "detection"}, objects={"person"}
    ).materialise


def test_a_review_without_a_tracked_label_is_skipped() -> None:
    payload = {**CLOSED, "data": {**CLOSED["data"], "objects": ["car", "dog"]}}
    verdict = reviews.decide(
        reviews.parse_review(payload), severities={"alert"}, objects={"person"}
    )
    assert verdict.materialise is False
    assert "no label in clip.objects" in verdict.reason


def test_an_audio_only_review_is_dropped() -> None:
    """It populates `data.audio` and leaves `data.objects` empty, and nothing can be
    pinned for it either — there is no event to retain."""
    payload = {
        **CLOSED,
        "data": {"detections": [], "objects": [], "audio": ["bark"], "zones": [], "sub_labels": []},
    }
    verdict = reviews.decide(
        reviews.parse_review(payload), severities={"alert"}, objects={"person"}
    )
    assert verdict.materialise is False
    assert "(none)" in verdict.reason  # the reason names the empty case explicitly


def test_an_empty_object_filter_is_refused_loudly() -> None:
    """ "Match nothing" would silently produce no clips at all, so it is reported."""
    verdict = reviews.decide(reviews.parse_review(CLOSED), severities={"alert"}, objects=set())
    assert verdict.materialise is False
    assert verdict.reason == "clip.objects is empty"


def test_any_one_matching_label_is_enough() -> None:
    payload = {**CLOSED, "data": {**CLOSED["data"], "objects": ["car", "person"]}}
    assert reviews.decide(
        reviews.parse_review(payload), severities={"alert"}, objects={"person"}
    ).materialise


def test_the_reason_order_is_cheapest_first() -> None:
    """An open review that is also the wrong severity reports "open", because that is the
    fact that changes first and the one an operator is looking for."""
    detection = {**OPEN, "severity": "detection"}
    verdict = reviews.decide(
        reviews.parse_review(detection), severities={"alert"}, objects={"person"}
    )
    assert verdict.reason == "review is still open"


def test_a_verdict_carries_a_reason_for_every_refusal() -> None:
    """A bare boolean would leave "why did this clip not appear" unanswerable from logs."""
    for verdict in (
        reviews.decide(reviews.parse_review(OPEN), severities={"alert"}, objects={"person"}),
        reviews.decide(reviews.parse_review(CLOSED), severities={"detection"}, objects={"person"}),
        reviews.decide(reviews.parse_review(CLOSED), severities={"alert"}, objects={"car"}),
    ):
        assert verdict.materialise is False
        assert verdict.reason and verdict.reason != "ok"
