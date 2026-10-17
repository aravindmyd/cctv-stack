"""Tests for the reconciliation sweep.

The two rules worth testing are both about the watermark: it must not advance past a
review we failed to fetch, and it must advance past one we decided to ignore.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from cctv import config as config_module
from cctv import db, sweep
from cctv.ffprobe import ClipFacts
from cctv.frigate import FrigateUnavailable, NoRecordingsYet
from cctv.materialise import ClipMaterialiser
from cctv.sweep import WATERMARK_EPSILON, WATERMARK_NAME, ReviewSweep

NOW = 1_700_000_000.0
END = NOW + 30

#: Just after the reviews, and close enough that the default lookback window still
#: contains them. A clock far in the future would put the reviews outside the window and
#: make the watermark assertions test nothing.
CLOCK = NOW + 200.0

BASE_CONFIG = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
defaults:
  clip:
    pre_roll_s: 5
    post_roll_s: 10
cameras:
  front-door:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
"""


def make_config(tmp_path: Path, text: str = BASE_CONFIG):
    path = tmp_path / "cameras.yml"
    path.write_text(text, encoding="utf-8")
    return config_module.load_cameras_config(path, environ={})


def review(
    review_id: str,
    *,
    start: float = NOW,
    end: float | None = END,
    camera: str = "front-door",
    severity: str = "alert",
    objects: tuple[str, ...] = ("person",),
) -> dict:
    return {
        "id": review_id,
        "camera": camera,
        "severity": severity,
        "start_time": start,
        "end_time": end,
        "data": {"detections": ["ev-" + review_id], "objects": list(objects)},
    }


class FakeLister:
    """Returns canned reviews and records what window it was asked for."""

    def __init__(self, reviews: list[dict] | None = None, error: Exception | None = None):
        self.reviews = list(reviews or [])
        self.error = error
        self.calls: list[dict] = []

    def list_reviews(self, *, after=None, before=None, limit=100) -> list[dict]:
        self.calls.append({"after": after, "before": before, "limit": limit})
        if self.error is not None:
            raise self.error
        return list(self.reviews)


class FakeSource:
    def __init__(self, *, download_errors: list[Exception | None] | None = None) -> None:
        self.download_errors = list(download_errors or [])
        self.downloads: list[tuple[float, float]] = []

    def retain(self, event_id: str) -> None:
        pass

    def unretain(self, event_id: str) -> None:
        pass

    def download(self, camera, start, end, sink, *, timeout_s) -> int:
        self.downloads.append((start, end))
        if self.download_errors:
            error = self.download_errors.pop(0)
            if error is not None:
                raise error
        sink.write(b"CLIP")
        return 4


class FakeProber:
    def __init__(self, duration_s: float = 44.0) -> None:
        self.duration_s = duration_s

    def __call__(self, path: Path) -> ClipFacts:
        return ClipFacts(
            duration_s=self.duration_s,
            video_codec="h264",
            audio_codec=None,
            has_audio=False,
            format_name="mov,mp4",
            packet_count=5,
            max_packet_gap_s=None,
            keyframe_interval_s=0.04,
        )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_db(tmp_path / "state.db")
    db.migrate(connection)
    db.ensure_camera(connection, name="front-door", now=NOW)
    yield connection
    connection.close()


def build_sweep(
    conn: sqlite3.Connection,
    tmp_path: Path,
    lister: FakeLister,
    *,
    source: FakeSource | None = None,
    **kwargs,
) -> ReviewSweep:
    materialiser = ClipMaterialiser(
        conn=conn,
        source=source or FakeSource(),
        data_dir=tmp_path,
        location="parents-home",
        zone=config_module.resolve_zone("Asia/Kolkata"),
        prober=FakeProber(),
        clock=lambda: CLOCK,
        sleep=lambda _: None,
        backoff_s=(0.0,),
    )
    return ReviewSweep(
        conn=conn,
        client=lister,
        materialiser=materialiser,
        clock=lambda: CLOCK,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_materialises_a_review_we_do_not_have(conn, tmp_path) -> None:
    lister = FakeLister([review("r-1")])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.ok
    assert result.considered == 1
    assert result.materialised == 1
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1


def test_skips_a_review_already_materialised(conn, tmp_path) -> None:
    """The whole point of `UNIQUE(review_id)`: a review seen twice produces one clip."""
    lister = FakeLister([review("r-1")])
    sweep_instance = build_sweep(conn, tmp_path, lister)
    config = make_config(tmp_path)

    sweep_instance.run(config=config)
    second = sweep_instance.run(config=config)

    assert second.materialised == 0
    assert second.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1


def test_an_open_review_is_skipped_and_comes_back(conn, tmp_path) -> None:
    lister = FakeLister([review("r-1", end=None)])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.materialised == 0
    assert result.skipped == 1
    # No end time, so nothing to advance to: the same review is offered again.
    assert result.watermark == CLOCK - sweep.DEFAULT_LOOKBACK_S


def test_a_review_for_an_unknown_camera_is_counted_not_failed(conn, tmp_path) -> None:
    """Frigate may hold history for a camera we no longer manage. That is not an error."""
    lister = FakeLister([review("r-1", camera="retired-camera")])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.ok
    assert result.unknown_camera == 1
    assert result.materialised == 0
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0


def test_the_cameras_severity_filter_is_applied(conn, tmp_path) -> None:
    text = BASE_CONFIG.replace(
        "    post_roll_s: 10", "    post_roll_s: 10\n    severities: [alert]"
    )
    lister = FakeLister([review("r-1", severity="detection")])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path, text))

    assert result.materialised == 0
    assert result.skipped == 1


def test_the_cameras_object_filter_is_applied(conn, tmp_path) -> None:
    lister = FakeLister([review("r-1", objects=("car",))])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.materialised == 0
    assert result.skipped == 1


def test_the_cameras_roll_settings_reach_the_materialiser(conn, tmp_path) -> None:
    text = BASE_CONFIG.replace("    post_roll_s: 10", "    post_roll_s: 30")
    lister = FakeLister([review("r-1")])
    source = FakeSource()
    build_sweep(conn, tmp_path, lister, source=source).run(config=make_config(tmp_path, text))

    _start, end = source.downloads[0]
    assert end == pytest.approx(END + 30)  # the camera's post_roll, not the default


# --------------------------------------------------------------------------------------
# The watermark
# --------------------------------------------------------------------------------------


def test_advances_to_the_highest_end_time_seen(conn, tmp_path) -> None:
    lister = FakeLister(
        [review("r-1", start=NOW, end=NOW + 10), review("r-2", start=NOW + 100, end=NOW + 130)]
    )
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.watermark == pytest.approx(NOW + 130)
    assert db.get_watermark(conn, WATERMARK_NAME)["watermark"] == pytest.approx(NOW + 130)


def test_the_next_run_resumes_from_the_watermark(conn, tmp_path) -> None:
    lister = FakeLister([review("r-1")])
    sweep_instance = build_sweep(conn, tmp_path, lister)
    sweep_instance.run(config=make_config(tmp_path))
    sweep_instance.run(config=make_config(tmp_path))

    assert lister.calls[0]["after"] == pytest.approx(CLOCK - sweep.DEFAULT_LOOKBACK_S)
    assert lister.calls[1]["after"] == pytest.approx(END)


def test_does_not_advance_past_a_deferred_review(conn, tmp_path) -> None:
    """The rule that stops the sweep from losing a clip *by retrying it*.

    A deferred review must be offered again next run. If the watermark moved past its
    `end_time`, `end_time > after` would exclude it and it would never come back.
    """
    source = FakeSource(download_errors=[NoRecordingsYet("not flushed")] * 4)
    lister = FakeLister(
        [review("r-1", start=NOW, end=NOW + 10), review("r-2", start=NOW + 50, end=NOW + 20)]
    )
    result = build_sweep(conn, tmp_path, lister, source=source).run(config=make_config(tmp_path))

    assert result.deferred >= 1
    assert result.watermark <= NOW + 10 - WATERMARK_EPSILON + 1e-9
    assert result.watermark < NOW + 10


def test_a_permanently_failed_review_does_not_freeze_the_sweep(conn, tmp_path) -> None:
    """A failure is alerted on; it must not hold the watermark back forever, or one
    un-fetchable review stops every later clip from ever being made."""
    from cctv.frigate import ClipUnavailable

    source = FakeSource(download_errors=[ClipUnavailable("gone")])
    lister = FakeLister([review("r-1", start=NOW, end=NOW + 10)])
    result = build_sweep(conn, tmp_path, lister, source=source).run(config=make_config(tmp_path))

    assert result.failed == 1
    assert result.watermark == pytest.approx(NOW + 10)


def test_the_watermark_never_moves_backwards(conn, tmp_path) -> None:
    db.set_watermark(conn, name=WATERMARK_NAME, watermark=NOW + 5_000, now=NOW)
    lister = FakeLister([review("r-1", start=NOW, end=NOW + 10)])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.watermark >= NOW + 5_000


def test_it_never_looks_further_back_than_the_lookback_on_a_first_run(conn, tmp_path) -> None:
    """A fresh install must not try to materialise months of expired history."""
    lister = FakeLister([])
    build_sweep(conn, tmp_path, lister, lookback_s=600).run(config=make_config(tmp_path))
    assert lister.calls[0]["after"] == pytest.approx(CLOCK - 600)


# --------------------------------------------------------------------------------------
# Failure of the sweep itself
# --------------------------------------------------------------------------------------


def test_frigate_being_unreachable_does_not_move_the_watermark(conn, tmp_path) -> None:
    db.set_watermark(conn, name=WATERMARK_NAME, watermark=NOW, now=NOW)
    lister = FakeLister(error=FrigateUnavailable("connection refused"))
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.ok is False
    assert "could not list reviews" in (result.failure or "")
    state = db.get_watermark(conn, WATERMARK_NAME)
    assert state["watermark"] == pytest.approx(NOW)
    assert state["consecutive_failures"] == 1
    assert "connection refused" in state["notes"]


def test_a_healthy_run_resets_the_failure_counter(conn, tmp_path) -> None:
    db.set_watermark(conn, name=WATERMARK_NAME, watermark=NOW, succeeded=False, now=NOW)
    lister = FakeLister([review("r-1")])
    result = build_sweep(conn, tmp_path, lister).run(config=make_config(tmp_path))

    assert result.ok
    assert db.get_watermark(conn, WATERMARK_NAME)["consecutive_failures"] == 0


def test_an_empty_window_is_a_success_not_a_failure(conn, tmp_path) -> None:
    result = build_sweep(conn, tmp_path, FakeLister([])).run(config=make_config(tmp_path))

    assert result.ok
    assert result.considered == 0
    assert result.needs_another_run is False


def test_deferrals_signal_that_another_run_is_needed(conn, tmp_path) -> None:
    source = FakeSource(download_errors=[NoRecordingsYet("not flushed")] * 4)
    lister = FakeLister([review("r-1")])
    result = build_sweep(conn, tmp_path, lister, source=source).run(config=make_config(tmp_path))

    assert result.deferred == 1
    assert result.needs_another_run is True


def test_the_sweep_and_the_trigger_share_one_decision_function(conn, tmp_path) -> None:
    """Both paths call `reviews.decide`.

    Two implementations would let the trigger decline a review that the sweep then
    materialises, or accept one the sweep keeps re-offering — a divergence that shows up as
    duplicate work or missing clips rather than as a failure. The sharing is the design, so
    it is asserted rather than assumed.
    """
    from cctv import reviews as reviews_module

    assert sweep.decide is reviews_module.decide
    assert sweep.candidates_from_reviews is reviews_module.candidates_from_reviews
