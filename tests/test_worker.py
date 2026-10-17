"""Tests for the clip-worker service.

The worker is where the camera config, the trigger verdict and the materialiser meet, and
each claim below fails in a way that looks like something else: a review for an unknown
camera that raises would take down the sweep with it, a sweep that raises would take down
the worker, and a signal that is not handled leaves a half-written clip and a `.tmp` file
behind.

It is exercised through `handle`, `tick` and `run` rather than through `main`, because
`main` is process plumbing: it opens a real database, builds a real HTTP client and blocks
on a real broker. The parts of `main` that *can* be tested without any of that are tested
at the end, and they are the parts that decide the process's exit code.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cctv import db
from cctv import worker as worker_module
from cctv.config import CamerasConfig, load_cameras_config
from cctv.db import ClipRecord
from cctv.materialise import CREATED, FAILED, ClipMaterialiser, MaterialiseOutcome
from cctv.reviews import ReviewCandidate
from cctv.sweep import ReviewSweep, SweepResult
from cctv.worker import (
    DEFAULT_SWEEP_INTERVAL_S,
    EXIT_CONFIG,
    EXIT_UNAVAILABLE,
    ClipWorker,
    Settings,
    build_worker,
    main,
)

NOW = 1_700_000_000.0
END = NOW + 30

#: One enabled camera. `garage` is added by the tests that need a second one, because the
#: `with_camera` helper below appends to whichever camera is last.
HEAD = """\
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
"""

WITH_DISABLED = (
    HEAD
    + """\
  garage:
    enabled: false
    rtsp:
      main: rtsp://192.168.1.51:554/stream1
"""
)

#: Every camera disabled. Used to reach `main`'s "nothing can ever be recorded" exit, and it
#: has to be *every* camera: `main` blocks in `worker.run` once it finds one it can record,
#: which in a test is an endless loop rather than a slow one.
ALL_DISABLED = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
cameras:
  front-door:
    enabled: false
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
"""


def with_camera(extra: str) -> str:
    """Append camera-level keys to `front-door`, with indentation handled in one place.

    Splicing YAML by hand is how a test passes for the wrong reason — see the same helper
    in `test_config.py`, where a first draft put `record:` at the wrong depth and YAML read
    it as a camera named `record`.
    """
    return HEAD + textwrap.indent(textwrap.dedent(extra).strip("\n") + "\n", "    ")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `Settings` field comes from the environment, so a stray `CCTV_*` variable in
    the developer's shell would silently become part of the test."""
    for name in [n for n in os.environ if n.startswith("CCTV_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def handler_cleanup() -> Iterator[None]:
    """`_install_signal_handlers` is process-wide and `main` calls it; put it back, or a
    later test's SIGINT would set an event on a dead worker instead of interrupting."""
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    yield

    for signum, handler in previous.items():
        signal.signal(signum, handler)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_db(tmp_path / "state.db")
    db.migrate(connection)
    # `clips.camera` is a foreign key, so a clip cannot be recorded for a camera the state
    # store has never been told about — the same reason `main` calls this for every enabled
    # camera before the worker starts.
    db.ensure_camera(connection, name="front-door", display_name="Front Door", now=NOW)
    yield connection
    connection.close()


def make_config(tmp_path: Path, text: str = HEAD) -> CamerasConfig:
    path = tmp_path / "cameras.yml"
    path.write_text(text, encoding="utf-8")
    return load_cameras_config(path, environ={})


def make_candidate(
    *,
    review_id: str = "1718987129.308396-fqk5ka",
    camera: str = "front-door",
    severity: str = "alert",
    end: float | None = END,
    objects: tuple[str, ...] = ("person",),
) -> ReviewCandidate:
    return ReviewCandidate(
        review_id=review_id,
        camera=camera,
        severity=severity,
        start_time=NOW,
        end_time=end,
        event_ids=("ev-1",),
        objects=objects,
    )


def message(**overrides: Any) -> dict[str, Any]:
    review: dict[str, Any] = {
        "id": "1718987129.308396-fqk5ka",
        "camera": "front-door",
        "severity": "alert",
        "start_time": NOW,
        "end_time": END,
        "data": {"detections": ["ev-1"], "objects": ["person"]},
    }
    review.update(overrides)
    return {"type": "end", "after": review}


class Clock:
    """A clock the test moves. The alternative is a test that waits 60 seconds."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeMaterialiser:
    """Records what it was asked to do, and returns a canned outcome."""

    def __init__(self, outcome: MaterialiseOutcome | None = None) -> None:
        self.calls: list[tuple[ReviewCandidate, float, float]] = []
        self.outcome = outcome

    def materialise(
        self, candidate: ReviewCandidate, *, pre_roll_s: float, post_roll_s: float
    ) -> MaterialiseOutcome:
        self.calls.append((candidate, pre_roll_s, post_roll_s))
        if self.outcome is not None:
            return self.outcome
        return MaterialiseOutcome(
            review_id=candidate.review_id,
            status=CREATED,
            reason="ok",
            clip_id=1,
            path=f"{candidate.camera}/2026/09/13/18-53-50.mp4",
            coverage_ratio=0.99,
            fetch_attempts=1,
        )


class FakeSweep:
    """A sweep that can fail, or take measurable time."""

    def __init__(
        self,
        *,
        result: SweepResult | None = None,
        error: Exception | None = None,
        clock: Clock | None = None,
        duration_s: float = 0.0,
    ) -> None:
        self.result = result
        self.error = error
        self.clock = clock
        self.duration_s = duration_s
        self.configs: list[CamerasConfig] = []
        self.calls = 0

    def run(self, *, config: CamerasConfig) -> SweepResult:
        self.calls += 1
        self.configs.append(config)
        if self.clock is not None:
            self.clock.advance(self.duration_s)
        if self.error is not None:
            raise self.error
        return SweepResult() if self.result is None else self.result


def make_worker(
    config: CamerasConfig,
    conn: sqlite3.Connection,
    *,
    clock: Clock | None = None,
    sweep: FakeSweep | None = None,
    materialiser: FakeMaterialiser | None = None,
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
) -> tuple[ClipWorker, FakeSweep, FakeMaterialiser]:
    clock = clock if clock is not None else Clock()
    sweep = sweep if sweep is not None else FakeSweep()
    materialiser = materialiser if materialiser is not None else FakeMaterialiser()
    worker = ClipWorker(
        config=config,
        conn=conn,
        sweep=sweep,  # type: ignore[arg-type]
        materialiser=materialiser,  # type: ignore[arg-type]
        sweep_interval_s=sweep_interval_s,
        clock=clock,
    )
    return worker, sweep, materialiser


# --------------------------------------------------------------------------------------
# Settings — every input is an environment variable
# --------------------------------------------------------------------------------------


def test_an_unset_mqtt_host_means_sweep_only() -> None:
    settings = Settings.from_environ({})

    assert settings.mqtt_host is None


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_a_blank_mqtt_host_collapses_to_sweep_only(value: str) -> None:
    """The shape `CCTV_MQTT_HOST=` takes in a `.env` file when someone comments the line out
    but leaves the value behind. Treating it as a host named "" would be a connect attempt
    against a name that cannot resolve."""
    settings = Settings.from_environ({"CCTV_MQTT_HOST": value})

    assert settings.mqtt_host is None


def test_a_configured_mqtt_host_is_kept_verbatim() -> None:
    settings = Settings.from_environ({"CCTV_MQTT_HOST": " mosquitto "})

    assert settings.mqtt_host == "mosquitto"


def test_the_paths_derive_from_the_two_root_directories() -> None:
    settings = Settings.from_environ(
        {"CCTV_CONFIG_DIR": "/srv/cctv/config", "CCTV_DATA_DIR": "/srv/data"}
    )

    assert settings.cameras_path == Path("/srv/cctv/config/cameras.yml")
    assert settings.db_path == Path("/srv/data/cctv.db")
    assert settings.data_dir == Path("/srv/data")


def test_an_explicit_path_beats_the_derived_one() -> None:
    settings = Settings.from_environ(
        {
            "CCTV_CONFIG_DIR": "/config",
            "CCTV_DATA_DIR": "/data",
            "CCTV_CAMERAS": "/etc/cameras.yml",
        }
    )

    assert settings.cameras_path == Path("/etc/cameras.yml")


def test_the_defaults_match_the_container_layout() -> None:
    """These defaults are what `compose.yml` relies on when it sets nothing but the volumes.
    A change here is a change to the deployment boundary."""
    settings = Settings.from_environ({})

    assert settings.cameras_path == Path("/config/cameras.yml")
    assert settings.data_dir == Path("/data")
    assert settings.db_path == Path("/data/cctv.db")
    assert settings.frigate_url == "http://frigate:5000"
    assert settings.mqtt_port == 1883
    assert settings.mqtt_topic_prefix == "frigate"
    assert settings.sweep_interval_s == DEFAULT_SWEEP_INTERVAL_S


def test_numbers_and_credentials_are_read_from_the_environment() -> None:
    settings = Settings.from_environ(
        {
            "CCTV_MQTT_HOST": "broker",
            "CCTV_MQTT_PORT": "8883",
            "CCTV_MQTT_TOPIC_PREFIX": "home",
            "CCTV_MQTT_USERNAME": "cctv",
            "CCTV_MQTT_PASSWORD": "hunter2",
            "CCTV_FRIGATE_URL": "http://192.168.1.9:5000",
            "CCTV_SWEEP_INTERVAL_S": "5.5",
            "CCTV_LOG_LEVEL": "debug",
        }
    )

    assert settings.mqtt_port == 8883
    assert settings.mqtt_topic_prefix == "home"
    assert settings.mqtt_username == "cctv"
    assert settings.mqtt_password == "hunter2"
    assert settings.frigate_url == "http://192.168.1.9:5000"
    assert settings.sweep_interval_s == 5.5
    assert settings.log_level == "debug"


def test_a_blank_username_collapses_to_none_but_a_password_is_kept_verbatim() -> None:
    """The asymmetry is deliberate. A blank username means "no authentication", but a
    password's whitespace is part of the password, and stripping it would produce a
    credential that is silently wrong."""
    settings = Settings.from_environ({"CCTV_MQTT_USERNAME": " ", "CCTV_MQTT_PASSWORD": " spaced "})

    assert settings.mqtt_username is None
    assert settings.mqtt_password == " spaced "


def test_an_empty_password_collapses_to_none() -> None:
    settings = Settings.from_environ({"CCTV_MQTT_PASSWORD": ""})

    assert settings.mqtt_password is None


def test_a_missing_log_level_means_the_logging_default() -> None:
    assert Settings.from_environ({}).log_level is None
    assert Settings.from_environ({"CCTV_LOG_LEVEL": ""}).log_level is None


def test_the_environment_is_read_when_no_mapping_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container path: `main` calls `from_environ()` with no argument."""
    monkeypatch.setenv("CCTV_FRIGATE_URL", "http://elsewhere:5000")

    assert Settings.from_environ().frigate_url == "http://elsewhere:5000"


def test_a_mistyped_port_fails_loudly() -> None:
    """Silently falling back to 1883 would connect to the wrong port and look like a broker
    problem, which is a much more expensive thing to debug than a refused start-up."""
    with pytest.raises(ValueError):
        Settings.from_environ({"CCTV_MQTT_PORT": "eighteen-eighty-three"})


# --------------------------------------------------------------------------------------
# handle — decide, then materialise
# --------------------------------------------------------------------------------------


def test_a_review_for_an_unknown_camera_is_dropped(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """Not an error: Frigate still holds history for a camera we no longer manage. Dropped
    without a retry loop, because it will not be in `cameras.yml` on the next attempt
    either — and a retry loop here is one that runs forever."""
    worker, _, materialiser = make_worker(make_config(tmp_path), conn)

    worker.handle(make_candidate(camera="back-garden"))  # must not raise

    assert materialiser.calls == []
    assert logs.last()["msg"] == "review for a camera that is not enabled"
    assert logs.last()["camera"] == "back-garden"


def test_a_review_for_a_disabled_camera_is_dropped(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """Retiring a camera has to actually stop it recording, or the operator's only lever is
    to delete the entry and lose its history."""
    worker, _, materialiser = make_worker(make_config(tmp_path, WITH_DISABLED), conn)

    worker.handle(make_candidate(camera="garage"))

    assert materialiser.calls == []
    assert logs.last()["camera"] == "garage"


def test_a_review_is_materialised_with_that_cameras_own_padding(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """The per-camera value, not the default: a camera pointed at a door needs a different
    pre-roll from one watching a car park."""
    config = make_config(tmp_path, with_camera("clip:\n  pre_roll_s: 7\n  post_roll_s: 3"))
    worker, _, materialiser = make_worker(config, conn)
    candidate = make_candidate()

    worker.handle(candidate)

    assert materialiser.calls == [(candidate, 7.0, 3.0)]
    assert logs.last()["msg"] == "review handled"


def test_the_outcome_is_logged_with_what_an_operator_would_ask_for(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """INV-14 in practice: which file, how well it covers the window, and how many attempts
    it took. Without these the only question worth asking has no answer."""
    worker, _, _ = make_worker(make_config(tmp_path), conn)

    worker.handle(make_candidate())

    record = logs.last()
    assert record["path"] == "front-door/2026/09/13/18-53-50.mp4"
    assert record["coverage"] == 0.99
    assert record["attempts"] == 1
    assert record["status"] == CREATED
    assert record["review_id"] == "1718987129.308396-fqk5ka"


def test_a_failed_materialisation_is_logged_and_not_raised(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """The worker does not retry: the sweep will offer the review again, which is the whole
    reason a failure here can be forgiving. Raising would kill the tick instead."""
    outcome = MaterialiseOutcome(
        review_id="1718987129.308396-fqk5ka",
        status=FAILED,
        reason="no recordings yet",
        fetch_attempts=3,
    )
    worker, _, _ = make_worker(make_config(tmp_path), conn, materialiser=FakeMaterialiser(outcome))

    worker.handle(make_candidate())  # must not raise

    assert logs.last()["status"] == FAILED
    assert logs.last()["reason"] == "no recordings yet"


def test_a_review_whose_severity_is_not_configured_is_skipped(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    config = make_config(tmp_path, with_camera("clip:\n  severities: [alert]"))
    worker, _, materialiser = make_worker(config, conn)

    worker.handle(make_candidate(severity="detection"))

    assert materialiser.calls == []
    assert logs.last()["msg"] == "review skipped"
    assert "severity" in logs.last()["reason"]


def test_a_review_with_no_matching_label_is_skipped(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """This is also where an audio-only review lands: it carries no objects, so the default
    `[person]` filter drops it, and there is no event to pin either."""
    worker, _, materialiser = make_worker(make_config(tmp_path), conn)

    worker.handle(make_candidate(objects=("dog",)))

    assert materialiser.calls == []
    assert "clip.objects" in logs.last()["reason"]


def test_an_open_review_never_reaches_the_materialiser(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """The dispatcher drops these before they reach the queue, so this is the second line of
    defence — and the one the *sweep* relies on, which has no queue to filter at."""
    worker, _, materialiser = make_worker(make_config(tmp_path), conn)

    worker.handle(make_candidate(end=None))

    assert materialiser.calls == []
    assert logs.last()["reason"] == "review is still open"


def test_a_review_that_already_has_a_clip_is_skipped(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """INV-7 on the second delivery: the sweep re-offers what MQTT just materialised, and
    the state store is what makes that a no-op rather than a duplicate file."""
    config = make_config(tmp_path)
    worker, _, materialiser = make_worker(config, conn)
    db.insert_clip(
        conn,
        ClipRecord(
            review_id="1718987129.308396-fqk5ka",
            camera="front-door",
            severity="alert",
            start_time=NOW,
            end_time=END,
            path="front-door/2026/09/13/18-53-50.mp4",
        ),
        now=NOW,
    )

    worker.handle(make_candidate())

    assert materialiser.calls == []
    assert logs.last()["reason"] == "already materialised"


# --------------------------------------------------------------------------------------
# sweep_once and tick — the reconciliation guarantee
# --------------------------------------------------------------------------------------


def test_sweep_once_passes_our_config_and_returns_the_result(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    config = make_config(tmp_path)
    result = SweepResult(considered=1, materialised=1, watermark=NOW)
    worker, sweep, _ = make_worker(config, conn, sweep=FakeSweep(result=result))

    assert worker.sweep_once() is result
    assert sweep.configs == [config]


def test_sweep_once_logs_the_counts(tmp_path: Path, conn: sqlite3.Connection, logs) -> None:
    """ "How far behind am I" is the question this line exists to answer, and a placeholder
    mismatch here would be swallowed by the logging machinery and print a warning instead of
    the sentence."""
    result = SweepResult(considered=22, materialised=21, skipped=1, deferred=0, failed=0)
    worker, _, _ = make_worker(make_config(tmp_path), conn, sweep=FakeSweep(result=result))

    worker.sweep_once()

    assert (
        logs.last()["msg"]
        == "sweep: 22 considered, 21 materialised, 1 skipped, 0 deferred, 0 failed"
    )


def test_a_sweep_that_raises_does_not_stop_the_worker(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """The sweep *is* the guarantee, and a worker that exits because the guarantee failed
    guarantees nothing — it would also take the MQTT path down with it."""
    worker, _, _ = make_worker(
        make_config(tmp_path), conn, sweep=FakeSweep(error=RuntimeError("boom"))
    )

    assert worker.sweep_once() is None

    assert logs.last()["msg"] == "sweep raised"
    assert "boom" in logs.last()["exception"]


def test_a_sweep_that_could_not_complete_is_not_treated_as_a_crash(
    tmp_path: Path, conn: sqlite3.Connection, logs
) -> None:
    """Frigate being unreachable is a count, not an exception: the watermark does not move
    and the failure is reported, but the worker keeps running and keeps retrying."""
    result = SweepResult(watermark=NOW, failure="could not list reviews: connection refused")
    worker, _, _ = make_worker(make_config(tmp_path), conn, sweep=FakeSweep(result=result))

    assert worker.sweep_once() is result

    assert logs.last()["msg"] == "sweep could not complete"
    assert "connection refused" in logs.last()["reason"]


def test_the_first_tick_sweeps(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A restart has a hole in it until the first sweep closes it, so the first tick must
    not wait for an interval."""
    clock = Clock()
    worker, sweep, _ = make_worker(make_config(tmp_path), conn, clock=clock)

    worker.tick(timeout=0)

    assert sweep.calls == 1


def test_tick_does_not_sweep_again_before_the_interval(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    clock = Clock()
    worker, sweep, _ = make_worker(make_config(tmp_path), conn, clock=clock, sweep_interval_s=60.0)

    for _ in range(50):
        worker.tick(timeout=0)

    assert sweep.calls == 1


def test_tick_sweeps_again_once_the_interval_has_elapsed(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    clock = Clock()
    worker, sweep, _ = make_worker(make_config(tmp_path), conn, clock=clock, sweep_interval_s=60.0)
    worker.tick(timeout=0)

    clock.advance(60.0)
    worker.tick(timeout=0)

    assert sweep.calls == 2


def test_a_sweep_slower_than_the_interval_runs_back_to_back(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """The interval is measured from when the sweep *started*, not when it finished.

    So a sweep that takes longer than the interval is immediately due again, rather than
    running once every two intervals — which is the behaviour a timer that restarts after
    completion would give, and it would silently halve the reconciliation rate exactly when
    the system is behind.
    """
    clock = Clock()
    sweep = FakeSweep(clock=clock, duration_s=100.0)
    worker, _, _ = make_worker(
        make_config(tmp_path), conn, clock=clock, sweep=sweep, sweep_interval_s=60.0
    )

    worker.tick(timeout=0)
    worker.tick(timeout=0)

    assert sweep.calls == 2


def test_tick_drains_one_queued_candidate(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """The MQTT path end to end, minus the network: the callback queues, the worker thread
    handles. Nothing else may touch the database, which is why the hand-off is a queue.
    """
    worker, _, materialiser = make_worker(make_config(tmp_path), conn)
    assert worker.dispatcher.dispatch(message()) == "queued"

    worker.tick(timeout=0)

    assert [c.review_id for c, _, _ in materialiser.calls] == ["1718987129.308396-fqk5ka"]
    assert worker.dispatcher.pending == 0


# --------------------------------------------------------------------------------------
# run — the loop
# --------------------------------------------------------------------------------------


def test_run_sweeps_before_the_loop_starts(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """Anything that happened while the process was down is already late, and MQTT takes a
    moment to establish. Waiting a full interval first would mean every restart has a hole
    in it."""
    worker, sweep, _ = make_worker(make_config(tmp_path), conn)
    stop = threading.Event()
    stop.set()

    worker.run(stop)

    assert sweep.calls == 1


def test_run_survives_a_tick_that_raises(
    tmp_path: Path, conn: sqlite3.Connection, logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberately broad and deliberately not fatal. This is the outermost loop of a service
    expected to run for years; the alternative is that one unhandled edge case in one review
    takes down every camera until someone notices."""
    worker, _, _ = make_worker(make_config(tmp_path), conn)
    stop = threading.Event()
    calls = 0

    def exploding_tick(*, timeout: float = 1.0) -> None:
        nonlocal calls
        calls += 1
        stop.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "tick", exploding_tick)

    worker.run(stop)  # must not raise

    assert calls == 1
    assert logs.last()["msg"] == "worker tick failed; continuing"
    assert "boom" in logs.last()["exception"]


def test_a_non_positive_sweep_interval_is_refused(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """Zero would sweep continuously against Frigate's API; negative would never sweep at
    all, silently removing the guarantee."""
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="positive"):
        make_worker(config, conn, sweep_interval_s=0)


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


def test_build_worker_wires_one_connection_and_one_materialiser(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """The sweep and the MQTT path have to share the materialiser and the connection,
    because the worker owns every database write. Two materialisers would mean two things
    deciding independently whether a clip exists (INV-7)."""
    config = make_config(tmp_path)
    settings = Settings.from_environ(
        {
            "CCTV_CAMERAS": str(tmp_path / "cameras.yml"),
            "CCTV_DATA_DIR": str(tmp_path / "data"),
            "CCTV_FRIGATE_URL": "http://frigate:5000",
            "CCTV_SWEEP_INTERVAL_S": "15",
        }
    )

    worker = build_worker(settings, config, conn)

    assert isinstance(worker.sweep, ReviewSweep)
    assert isinstance(worker.materialiser, ClipMaterialiser)
    assert worker.sweep.materialiser is worker.materialiser
    assert worker.sweep.conn is conn
    assert worker.materialiser.data_dir == settings.data_dir
    assert worker.materialiser.location == config.site.location
    assert worker.materialiser.zone == config.site.timezone
    assert worker.sweep_interval_s == 15.0


def test_the_worker_owns_the_dispatcher_it_hands_to_mqtt(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """The handler and the queue must not be wired to different objects: the MQTT subscriber
    is given the worker's dispatcher, so the only path from the network thread to the
    database runs through `handle`."""
    worker, _, _ = make_worker(make_config(tmp_path), conn)

    assert worker.dispatcher.handler == worker.handle


# --------------------------------------------------------------------------------------
# Signals — the cooperative shutdown
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_sets_the_stop_event_rather_than_raising(
    signum: signal.Signals, handler_cleanup: None, logs
) -> None:
    """Cooperative because the alternative is a `KeyboardInterrupt` raised *inside* a fetch,
    which leaves a `.tmp` file and a half-written clip behind."""
    stop = threading.Event()
    worker_module._install_signal_handlers(stop)

    signal.raise_signal(signum)

    assert stop.is_set()
    assert logs.last()["reason"] == signum.name


def test_installing_the_handlers_covers_both_signals(handler_cleanup: None) -> None:
    """`docker stop` sends SIGTERM and Ctrl-C sends SIGINT. A handler for only one of them
    leaves the other killing the process mid-write."""
    stop = threading.Event()
    worker_module._install_signal_handlers(stop)

    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_DFL


# --------------------------------------------------------------------------------------
# main — the exits a supervisor depends on
# --------------------------------------------------------------------------------------


def run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int, list[dict[str, Any]]]:
    """Run `main` and return its exit code plus what it logged.

    `main` calls `configure_logging` itself, so the only way to read what it logged is to
    own stdout *before* it does.
    """
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    code = main(list(argv))

    records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    return code, records


def messages(records: list[dict[str, Any]]) -> list[str]:
    return [record["msg"] for record in records]


def test_a_bad_log_level_exits_as_a_config_error(
    handler_cleanup: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """EX_CONFIG rather than EX_TEMPFAIL, because a bad level never becomes good: a
    supervisor that restarts on 75 would restart forever."""
    monkeypatch.setenv("CCTV_LOG_LEVEL", "shout")

    code, records = run_main(monkeypatch)

    assert code == EXIT_CONFIG
    assert records == []
    assert "shout" in capsys.readouterr().err


def test_a_missing_config_file_exits_as_a_config_error(
    handler_cleanup: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCTV_CAMERAS", str(tmp_path / "not-here.yml"))

    code, records = run_main(monkeypatch)

    assert code == EXIT_CONFIG
    assert "configuration is unusable" in messages(records)
    assert str(tmp_path / "not-here.yml") in records[-1]["reason"]


def test_a_config_with_no_enabled_cameras_exits_before_touching_state(
    handler_cleanup: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run with every camera disabled and nothing can ever be recorded, so refusing to start
    is the only honest outcome. The state store is asserted *absent* because the check has to
    happen before anything is written — a rejected config must not leave a database behind.
    """
    config = tmp_path / "cameras.yml"
    config.write_text(ALL_DISABLED, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CCTV_CAMERAS", str(config))
    monkeypatch.setenv("CCTV_DATA_DIR", str(data_dir))

    code, records = run_main(monkeypatch)

    assert code == EXIT_CONFIG
    assert "no cameras are enabled, so nothing can ever be recorded" in messages(records)
    assert not data_dir.exists()


def test_an_unreachable_state_store_exits_as_temporarily_unavailable(
    handler_cleanup: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the distinction LLD 00 §3 draws: a database that is briefly locked
    might work on the next attempt, so this is the code a supervisor should retry.

    A *directory* where the file belongs is the smallest way to make `open_db` fail rather
    than succeed and leave `main` blocking on a broker that is not there.
    """
    config = tmp_path / "cameras.yml"
    config.write_text(HEAD, encoding="utf-8")
    blocker = tmp_path / "not-a-file"
    blocker.mkdir()
    monkeypatch.setenv("CCTV_CAMERAS", str(config))
    monkeypatch.setenv("CCTV_DB", str(blocker))

    code, records = run_main(monkeypatch)

    assert code == EXIT_UNAVAILABLE
    assert "state store is unusable" in messages(records)
