"""Tests for the worker loop — the ordering rules, which are the part that matters."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from cctv import db, materialise
from cctv.ffprobe import ClipFacts, ProbeError
from cctv.frigate import ClipUnavailable, FrigateUnavailable, NoRecordingsYet
from cctv.materialise import CREATED, DEFERRED, FAILED, SKIPPED, ClipMaterialiser
from cctv.reviews import parse_review
from cctv.timeutil import resolve_zone

NOW = 1_700_000_000.0
END = NOW + 30
#: A 30 s review with the 5 s / 10 s defaults, so `requested_s` is 45.
REQUESTED_S = 45.0


def facts(
    duration_s: float | None, *, audio: str | None = None, gap: float | None = None
) -> ClipFacts:
    return ClipFacts(
        duration_s=duration_s,
        video_codec="h264",
        audio_codec=audio,
        has_audio=audio is not None,
        format_name="mov,mp4,m4a",
        packet_count=10,
        max_packet_gap_s=gap,
        keyframe_interval_s=0.04,
    )


class FakeSource:
    """Records every call, in order, so the pin/download/unpin sequence is assertable."""

    def __init__(
        self,
        *,
        bodies: list[bytes] | None = None,
        download_errors: list[Exception | None] | None = None,
        pin_error: Exception | None = None,
    ) -> None:
        self.actions: list[tuple] = []
        self.bodies = list(bodies or [])
        self.download_errors = list(download_errors or [])
        self.pin_error = pin_error
        self.downloads: list[tuple[float, float]] = []

    def retain(self, event_id: str) -> None:
        self.actions.append(("retain", event_id))
        if self.pin_error is not None:
            raise self.pin_error

    def unretain(self, event_id: str) -> None:
        self.actions.append(("unretain", event_id))

    def download(self, camera: str, start: float, end: float, sink, *, timeout_s: float) -> int:
        self.actions.append(("download", camera, round(start, 3)))
        self.downloads.append((start, end))
        if self.download_errors:
            error = self.download_errors.pop(0)
            if error is not None:
                raise error
        body = self.bodies.pop(0) if self.bodies else b"BODY"
        sink.write(body)
        return len(body)


class FakeProber:
    def __init__(self, results: list[ClipFacts | Exception]) -> None:
        self.results = list(results)
        self.paths: list[Path] = []

    def __call__(self, path: Path) -> ClipFacts:
        self.paths.append(path)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


REVIEW = {
    "id": "1718987129.308396-fqk5ka",
    "camera": "front-door",
    "severity": "alert",
    "start_time": NOW,
    "end_time": END,
    "data": {"detections": ["ev-1"], "objects": ["person"], "zones": ["porch"]},
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_db(tmp_path / "state.db")
    db.migrate(connection)
    db.ensure_camera(connection, name="front-door", now=NOW)
    yield connection
    connection.close()


def build(
    conn: sqlite3.Connection,
    tmp_path: Path,
    source: FakeSource,
    prober: FakeProber,
    **kwargs,
) -> tuple[ClipMaterialiser, list[float]]:
    slept: list[float] = []
    materialiser = ClipMaterialiser(
        conn=conn,
        source=source,
        data_dir=tmp_path,
        location="parents-home",
        zone=resolve_zone("Asia/Kolkata"),
        prober=prober,
        # A clock past the flush grace, so happy-path tests never wait. The wait itself is
        # asserted separately.
        clock=lambda: NOW + 10_000,
        sleep=slept.append,
        backoff_s=(0.0,),
        **kwargs,
    )
    return materialiser, slept


#: `NOW` is 1700000000 = 2023-11-14T22:13:20Z = 2023-11-15 03:43:20 IST, which is the
#: timezone the materialiser is built with. Deriving this by hand is easy to get wrong,
#: so it is asserted explicitly in `test_creates_the_file_the_row_and_the_sidecar`.
CANONICAL = "clips/front-door/2023/11/15/03-43-20.mp4"


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_creates_the_file_the_row_and_the_sidecar(conn, tmp_path) -> None:
    source = FakeSource(bodies=[b"CLIPBYTES"])
    materialiser, _ = build(conn, tmp_path, source, FakeProber([facts(44.0)]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == CREATED
    assert outcome.materialised is True
    assert outcome.path == CANONICAL
    assert outcome.degraded is False

    assert (tmp_path / CANONICAL).read_bytes() == b"CLIPBYTES"
    assert json.loads((tmp_path / (CANONICAL + ".json")).read_text())["review_id"] == REVIEW["id"]

    row = conn.execute("SELECT * FROM clips").fetchone()
    assert row["review_id"] == REVIEW["id"]
    assert row["path"] == CANONICAL
    assert row["size_bytes"] == len(b"CLIPBYTES")
    assert row["coverage_ratio"] == pytest.approx(44.0 / REQUESTED_S)
    assert row["end_time"] == END
    assert row["event_ids"] == '["ev-1"]'
    assert row["zones"] == '["porch"]'

    # `cloud_key` stays NULL until the uploader claims the clip — the contract sets it at
    # claim time, and pre-empting that would make the claim's COALESCE pointless. The
    # sidecar still records it, because the key is derivable and useful to a human.
    assert row["cloud_key"] is None
    assert row["upload_state"] == "PENDING"
    assert json.loads((tmp_path / (CANONICAL + ".json")).read_text())["cloud_key"] == (
        "parents-home/" + CANONICAL
    )


def test_the_row_is_written_only_after_the_file_exists(conn, tmp_path) -> None:
    """The database is the record of what is on disk. A row before the rename would let
    every consumer believe in a file that a crash prevented."""
    seen: list[bool] = []

    class RecordingProber:
        def __call__(self, path: Path) -> ClipFacts:
            seen.append((tmp_path / CANONICAL).exists())
            return facts(44.0)

    materialiser, _ = build(conn, tmp_path, FakeSource(), RecordingProber())
    materialiser.materialise(parse_review(REVIEW))

    # Probed on a scratch path, so the canonical one did not exist yet.
    assert seen == [False]
    assert (tmp_path / CANONICAL).exists()


def test_no_scratch_files_survive_a_success(conn, tmp_path) -> None:
    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(44.0)]))
    materialiser.materialise(parse_review(REVIEW))
    leftovers = [p.name for p in (tmp_path / "clips").rglob("*") if ".tmp-" in p.name]
    assert leftovers == []


# --------------------------------------------------------------------------------------
# INV-14 — a short clip is kept, and the best attempt wins
# --------------------------------------------------------------------------------------


def test_a_short_clip_is_stored_rather_than_discarded(conn, tmp_path) -> None:
    """Retrying improves the outcome; discarding cannot. So the floor is "keep it".

    Three short results, because that is what exhausting the attempts looks like: two
    retries and then the floor.
    """
    materialiser, _ = build(
        conn, tmp_path, FakeSource(), FakeProber([facts(18.0), facts(18.0), facts(18.0)])
    )

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == CREATED
    assert outcome.degraded is True
    assert outcome.coverage_ratio == pytest.approx(0.4)
    assert outcome.fetch_attempts == 3
    assert (tmp_path / CANONICAL).exists()
    assert "short clip" in outcome.reason


def test_the_best_attempt_is_promoted_not_the_last(conn, tmp_path) -> None:
    """Three attempts, the middle one best. Writing retries straight onto the canonical
    path would have kept the worst — the exact opposite of retry-then-keep-best."""
    source = FakeSource(bodies=[b"ATTEMPT-1", b"ATTEMPT-2", b"ATTEMPT-3"])
    prober = FakeProber([facts(27.0), facts(31.5), facts(22.5)])  # 0.60, 0.70, 0.50
    materialiser, _ = build(conn, tmp_path, source, prober)

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.fetch_attempts == 3
    assert outcome.coverage_ratio == pytest.approx(0.70)
    assert (tmp_path / CANONICAL).read_bytes() == b"ATTEMPT-2"


def test_an_acceptable_first_attempt_stops_the_retries(conn, tmp_path) -> None:
    source = FakeSource()
    prober = FakeProber([facts(45.0)])
    materialiser, _ = build(conn, tmp_path, source, prober)

    materialiser.materialise(parse_review(REVIEW))

    assert len(source.downloads) == 1


def test_erring_outward_is_not_retried(conn, tmp_path) -> None:
    """`-c copy` overshoots by design, so a ratio above 1.0 is a success."""
    source = FakeSource()
    materialiser, _ = build(conn, tmp_path, source, FakeProber([facts(52.0)]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.degraded is False
    assert len(source.downloads) == 1


# --------------------------------------------------------------------------------------
# INV-9 — pin before download, unpin after
# --------------------------------------------------------------------------------------


def test_pins_before_downloading_and_unpins_afterwards(conn, tmp_path) -> None:
    source = FakeSource()
    materialiser, _ = build(conn, tmp_path, source, FakeProber([facts(44.0)]))

    materialiser.materialise(parse_review(REVIEW))

    kinds = [action[0] for action in source.actions]
    assert kinds == ["retain", "download", "unretain"]
    assert source.actions[0] == ("retain", "ev-1")


def test_the_pin_is_released_even_when_the_fetch_fails(conn, tmp_path) -> None:
    """A stuck pin wastes Frigate's disk, but leaking one on every failure would fill it."""
    source = FakeSource(download_errors=[ClipUnavailable("gone")])
    materialiser, _ = build(conn, tmp_path, source, FakeProber([]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == FAILED
    assert ("unretain", "ev-1") in source.actions


def test_a_failed_pin_refuses_to_download(conn, tmp_path) -> None:
    """Fatal for that review: downloading without a pin risks Frigate expiring the
    footage underneath us, and a missing clip beats a late one only if deliberate."""
    source = FakeSource(pin_error=FrigateUnavailable("frigate is down"))
    materialiser, _ = build(conn, tmp_path, source, FakeProber([]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == FAILED
    assert "could not pin" in outcome.reason
    assert source.downloads == []
    assert not (tmp_path / CANONICAL).exists()


def test_a_review_with_no_events_is_still_fetched(conn, tmp_path) -> None:
    """Nothing to pin is not a failure — the clip is still worth having."""
    source = FakeSource()
    materialiser, _ = build(conn, tmp_path, source, FakeProber([facts(44.0)]))
    payload = {**REVIEW, "data": {**REVIEW["data"], "detections": []}}

    outcome = materialiser.materialise(parse_review(payload))

    assert outcome.status == CREATED
    assert source.actions == [("download", "front-door", pytest.approx(NOW - 5, abs=0.01))]


# --------------------------------------------------------------------------------------
# The flush grace — the measured race
# --------------------------------------------------------------------------------------


def test_it_waits_for_the_flush_grace_before_fetching(conn, tmp_path) -> None:
    """At 5 s after a review ended, 5 of 6 fetches raced the segment flush and returned a
    short clip. The wait is the fix, so it is asserted rather than assumed."""
    slept: list[float] = []
    materialiser = ClipMaterialiser(
        conn=conn,
        source=FakeSource(),
        data_dir=tmp_path,
        location="parents-home",
        zone=resolve_zone("Asia/Kolkata"),
        prober=FakeProber([facts(44.0)]),
        clock=lambda: NOW,  # the review has just ended; nothing is flushed yet
        sleep=slept.append,
        backoff_s=(0.0,),
    )

    materialiser.materialise(parse_review(REVIEW))

    # requested_end is END + post_roll (10), plus the 15 s grace.
    assert slept[0] == pytest.approx(END + 10 + materialise.DEFAULT_SEGMENT_FLUSH_GRACE_S - NOW)


def test_no_wait_when_the_grace_has_already_elapsed(conn, tmp_path) -> None:
    source = FakeSource()
    materialiser, slept = build(conn, tmp_path, source, FakeProber([facts(44.0)]))
    materialiser.materialise(parse_review(REVIEW))
    assert slept == []


# --------------------------------------------------------------------------------------
# Failure classification — the difference between "retry" and "lost"
# --------------------------------------------------------------------------------------


def test_no_recordings_defers_rather_than_failing(conn, tmp_path) -> None:
    """The flush race is retryable. Calling it permanent would discard a clip that was
    seconds away from being retrievable."""
    source = FakeSource(download_errors=[NoRecordingsYet("not flushed")] * 4)
    materialiser, _ = build(conn, tmp_path, source, FakeProber([]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == DEFERRED
    assert len(source.downloads) == 3  # max_attempts
    assert not (tmp_path / CANONICAL).exists()
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0


def test_a_transient_error_then_success_stores_the_clip(conn, tmp_path) -> None:
    source = FakeSource(download_errors=[NoRecordingsYet("not yet")], bodies=[b"SECOND"])
    materialiser, _ = build(conn, tmp_path, source, FakeProber([facts(44.0)]))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == CREATED
    assert outcome.fetch_attempts == 1
    assert (tmp_path / CANONICAL).read_bytes() == b"SECOND"


def test_a_probe_failure_is_retried(conn, tmp_path) -> None:
    source = FakeSource(bodies=[b"BAD", b"GOOD"])
    prober = FakeProber([ProbeError("not a video"), facts(44.0)])
    materialiser, _ = build(conn, tmp_path, source, prober)

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == CREATED
    assert (tmp_path / CANONICAL).read_bytes() == b"GOOD"


def test_an_unmeasurable_clip_is_kept_and_flagged(conn, tmp_path) -> None:
    """`None` duration is not a perfect clip, and it is not grounds for throwing one away."""
    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(None)] * 3))

    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.status == CREATED
    assert outcome.degraded is True
    assert conn.execute("SELECT coverage_ratio FROM clips").fetchone()[0] is None


# --------------------------------------------------------------------------------------
# Skip and collision
# --------------------------------------------------------------------------------------


def test_an_open_review_is_skipped_without_touching_anything(conn, tmp_path) -> None:
    source = FakeSource()
    materialiser, slept = build(conn, tmp_path, source, FakeProber([]))

    outcome = materialiser.materialise(parse_review({**REVIEW, "end_time": None}))

    assert outcome.status == SKIPPED
    assert outcome.reason == "review is still open"
    assert source.actions == []
    assert slept == []


def test_a_second_clip_in_the_same_second_gets_the_collision_path(conn, tmp_path) -> None:
    """Two reviews can start in the same second, and a DST fallback makes it certain."""
    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(44.0), facts(44.0)]))
    materialiser.materialise(parse_review(REVIEW))

    other = {**REVIEW, "id": "1718987129.308396-zz99zz"}
    outcome = materialiser.materialise(parse_review(other))

    assert outcome.status == CREATED
    assert outcome.path == "clips/front-door/2023/11/15/03-43-20-zz99zz.mp4"
    assert (tmp_path / CANONICAL).exists()  # the first clip is untouched


def test_an_orphan_file_forces_the_collision_path(conn, tmp_path) -> None:
    """A file with no row is an orphan from a crash. Overwriting it would destroy a clip
    to make room, which is the one thing the design never does."""
    orphan = tmp_path / CANONICAL
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"ORPHAN")

    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(44.0)]))
    outcome = materialiser.materialise(parse_review(REVIEW))

    assert outcome.path != CANONICAL
    assert orphan.read_bytes() == b"ORPHAN"


# --------------------------------------------------------------------------------------
# Recording honesty
# --------------------------------------------------------------------------------------


def test_has_gaps_is_never_set_from_the_packet_gap(conn, tmp_path) -> None:
    """A healthy 30 s clip measured a 3.55 s packet gap against a 2.55 s keyframe interval.
    Setting `has_gaps` from that would mark nearly every clip as damaged, making the flag
    meaningless. The gap is recorded; the flag is not derived from it."""
    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(44.0, gap=3.55)]))

    materialiser.materialise(parse_review(REVIEW))

    row = conn.execute("SELECT has_gaps, max_gap_s FROM clips").fetchone()
    assert row["has_gaps"] == 0
    assert row["max_gap_s"] == pytest.approx(3.55)


def test_a_long_clip_is_flagged_and_stored_in_full(conn, tmp_path) -> None:
    materialiser, _ = build(
        conn, tmp_path, FakeSource(), FakeProber([facts(7200.0)]), long_clip_warn_s=600
    )

    outcome = materialiser.materialise(parse_review({**REVIEW, "end_time": NOW + 7200}))

    assert outcome.long_clip is True
    assert (tmp_path / outcome.path).exists()
    assert conn.execute("SELECT long_clip, duration_s FROM clips").fetchone()["long_clip"] == 1


def test_the_sidecar_records_what_the_row_records(conn, tmp_path) -> None:
    materialiser, _ = build(conn, tmp_path, FakeSource(), FakeProber([facts(44.0, audio="aac")]))
    outcome = materialiser.materialise(parse_review(REVIEW))

    sidecar = json.loads((tmp_path / (outcome.path + ".json")).read_text())
    assert sidecar["review_id"] == REVIEW["id"]
    assert sidecar["has_audio"] is True
    assert sidecar["event_ids"] == ["ev-1"]
    assert sidecar["requested_s"] == pytest.approx(REQUESTED_S)
    assert sidecar["sha256"] == conn.execute("SELECT sha256 FROM clips").fetchone()[0]
