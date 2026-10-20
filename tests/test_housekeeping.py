"""Tests for J4, housekeeping (`lld/05` §6).

Housekeeping is the one pass in cleanup that deletes things nobody asked it to delete, so the
tests are written as a list of promises rather than a list of behaviours:

* a clip and its sidecar are never touched, whatever their age;
* `media/` and `frigate/` are never touched;
* nothing is deleted on a young file, because a `.tmp` may belong to a fetch in progress;
* rotation is bounded, never emptied;
* an upload attempt belonging to a clip that is still in flight is kept forever.

The one thing J4 may do that surprises: it removes directories. So there is a test that it does
not remove `clips/` itself, which is a volume mount point.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cctv import cleanup, config, db, timeutil
from cctv.cleanup import Housekeeper, HousekeepRun
from cctv.reconcile import ReconcileRun

NOW = 1_790_000_000.0
DAY = 86400.0
CAMERA = "front-door"

CAMERAS = f"""\
site:
  location: parents-home
  timezone: UTC
cameras:
  {CAMERA}:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
"""

#: A predictable sub-second-free timestamp, so `utime` round-trips on every filesystem.
BASE = 1_789_000_000.0


def _aged(path: Path, *, age_s: float, now: float = NOW) -> Path:
    """Backdate a file, so the age gates can be exercised without waiting a week."""
    when = now - age_s
    os.utime(path, (when, when))
    return path


@dataclass
class Env:
    root: Path
    data_dir: Path
    db_path: Path
    settings: cleanup.CleanupSettings

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    def write(self, relpath: str, *, body: bytes = b"x" * 10, age_s: float = 0.0) -> Path:
        path = self.clips_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        if age_s:
            _aged(path, age_s=age_s)
        return path

    def day_dir(self, day: str = "01") -> Path:
        return self.clips_dir / CAMERA / "2026" / "01" / day

    def housekeep(self, **kwargs: Any) -> HousekeepRun:
        settings = kwargs.pop("settings", None) or self.settings
        keeper = Housekeeper(settings=settings, logger=None, clock=lambda: NOW)
        conn = db.open_db(self.db_path)
        try:
            return keeper.run_once(conn)
        finally:
            conn.close()

    # -- fixtures for the database side --------------------------------------------------

    def add_clip(self, *, review_id: str, state: str = "UPLOADED") -> int:
        conn = db.open_db(self.db_path)
        try:
            db.ensure_camera(conn, name=CAMERA)
            clip_id, _ = db.insert_clip(
                conn,
                db.ClipRecord(
                    review_id=review_id,
                    camera=CAMERA,
                    severity="alert",
                    start_time=NOW - 10 * DAY,
                    end_time=NOW - 10 * DAY + 20,
                    path=f"clips/{CAMERA}/2026/01/01/00-00-00-{review_id}.mp4",
                ),
            )
            conn.execute("UPDATE clips SET upload_state = ? WHERE id = ?", (state, clip_id))
            conn.commit()
        finally:
            conn.close()
        return clip_id

    def add_attempt(self, *, clip_id: int, started_at: float) -> None:
        conn = db.open_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO upload_attempts (clip_id, attempt_no, started_at) VALUES (?, ?, ?)",
                (clip_id, 1, started_at),
            )
            conn.commit()
        finally:
            conn.close()

    def add_event(self, *, at: float) -> None:
        conn = db.open_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO system_events (at, severity, component, code) "
                "VALUES (?, 'info', 'cleanup', 'TEST')",
                (at,),
            )
            conn.commit()
        finally:
            conn.close()

    def add_health(self, *, observed_at: float) -> None:
        conn = db.open_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO health_history (subject, state, observed_at) VALUES (?, ?, ?)",
                ("uploader", "HEALTHY", observed_at),
            )
            conn.commit()
        finally:
            conn.close()

    def add_pin(self, *, event_id: str, state: str, settled_at: float) -> None:
        conn = db.open_db(self.db_path)
        try:
            conn.execute(
                "INSERT INTO pinned_events (event_id, camera, pinned_at, unpinned_at, reason, "
                "state) VALUES (?, ?, ?, ?, 'materialisation', ?)",
                (event_id, CAMERA, settled_at - DAY, settled_at, state),
            )
            conn.commit()
        finally:
            conn.close()

    # -- assertions ----------------------------------------------------------------------

    def count(self, table: str) -> int:
        conn = db.open_db(self.db_path, readonly=True)
        try:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        finally:
            conn.close()

    def dirs(self) -> list[str]:
        if not self.clips_dir.is_dir():
            return []
        return sorted(
            path.relative_to(self.clips_dir).as_posix()
            for path in self.clips_dir.rglob("*")
            if path.is_dir()
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(timeutil, "now", lambda: NOW)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "cctv.db"
    conn = db.open_db(db_path)
    db.migrate(conn)
    conn.close()
    cameras_path = tmp_path / "cameras.yml"
    cameras_path.write_text(CAMERAS, encoding="utf-8")
    settings = cleanup.CleanupSettings(
        data_dir=data_dir,
        cameras_path=cameras_path,
        db_path=db_path,
        run_interval_s=3600.0,
        batch_size=200,
        dry_run=False,
        retire_grace_days=0,
        health_host="127.0.0.1",
        health_port=0,
        scheduler_tick_s=0.01,
        clips_overdue_warn=1,
        clips_overdue_critical=10,
        overdue_age_critical_days=30,
        disk_warn_pct=15.0,
        disk_critical_pct=5.0,
        log_level=None,
    )
    return Env(root=tmp_path, data_dir=data_dir, db_path=db_path, settings=settings)


# ---------------------------------------------------------------------------------------
# The promises
# ---------------------------------------------------------------------------------------


def test_a_clip_and_its_sidecar_are_never_touched(env: Env) -> None:
    clip = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4", age_s=400 * DAY)
    sidecar = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.json", age_s=400 * DAY)

    env.housekeep()

    assert clip.exists()
    assert sidecar.exists()


def test_media_and_frigate_are_never_touched(env: Env) -> None:
    stale = env.data_dir / "media" / "recordings" / "2026-01" / "00.mp4"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"continuous footage")
    _aged(stale, age_s=400 * DAY)
    temp = env.data_dir / "frigate" / "cache.tmp-99"
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"frigate scratch")
    _aged(temp, age_s=400 * DAY)

    env.housekeep()

    assert stale.exists()
    assert temp.exists()


def test_a_fresh_tmp_is_kept_because_a_fetch_may_be_writing_it(env: Env) -> None:
    live = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.tmp-4242", age_s=60)

    run = env.housekeep()

    assert live.exists()
    assert run.tmp_files_removed == 0


def test_a_stale_tmp_is_reaped(env: Env) -> None:
    stale = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.tmp-4242", age_s=2 * DAY)
    env.write(f"{CAMERA}/2026/01/01/00-05-00.mp4", age_s=2 * DAY)

    run = env.housekeep()

    assert not stale.exists()
    assert run.tmp_files_removed == 1
    assert (env.clips_dir / f"{CAMERA}/2026/01/01/00-05-00.mp4").exists()


def test_a_bad_file_keeps_its_evidence_for_a_week(env: Env) -> None:
    fresh = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.bad", age_s=2 * DAY)
    old = env.write(f"{CAMERA}/2026/01/01/00-05-00.mp4.bad", age_s=8 * DAY)

    run = env.housekeep()

    assert fresh.exists()
    assert not old.exists()
    assert run.bad_files_removed == 1


def test_an_emptied_day_takes_its_month_and_year_with_it(env: Env) -> None:
    env.day_dir("01").mkdir(parents=True)
    env.day_dir("02").mkdir(parents=True)
    env.write(f"{CAMERA}/2026/01/02/00-00-00.mp4")

    run = env.housekeep()

    assert run.empty_dirs_removed == 1
    assert env.dirs() == [CAMERA, f"{CAMERA}/2026", f"{CAMERA}/2026/01", f"{CAMERA}/2026/01/02"]


def test_removing_a_day_also_removes_a_now_empty_camera(env: Env) -> None:
    env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4")
    (env.clips_dir / f"{CAMERA}/2026/01/01/00-00-00.mp4").unlink()

    run = env.housekeep()

    # The day, its month, its year and the camera itself: four levels, deepest first, so a
    # single pass is enough.
    assert run.empty_dirs_removed == 4
    assert env.dirs() == []


def test_clips_itself_survives(env: Env) -> None:
    env.clips_dir.mkdir(parents=True, exist_ok=True)

    env.housekeep()

    # It is a volume mount point. Removing it would turn a tidy-up into an outage.
    assert env.clips_dir.is_dir()


def test_database_backups_are_rotated_to_the_newest_n(env: Env) -> None:
    backups = env.data_dir / "db-backups"
    backups.mkdir(parents=True)
    for index in range(14):
        path = backups / f"pre-upgrade-{index:02d}.db"
        path.write_bytes(b"db")
        _aged(path, age_s=(14 - index) * DAY)

    run = env.housekeep()

    remaining = sorted(path.name for path in backups.iterdir())
    assert run.db_backups_removed == 4
    assert len(remaining) == 10
    assert remaining[0] == "pre-upgrade-04.db"
    assert remaining[-1] == "pre-upgrade-13.db"


def test_rotation_never_deletes_anything_but_database_files(env: Env) -> None:
    backups = env.data_dir / "db-backups"
    backups.mkdir(parents=True)
    for index in range(12):
        path = backups / f"pre-upgrade-{index:02d}.db"
        path.write_bytes(b"db")
        _aged(path, age_s=(12 - index) * DAY)
    notes = backups / "README.txt"
    notes.write_text("do not delete me", encoding="utf-8")
    _aged(notes, age_s=99 * DAY)

    env.housekeep()

    assert notes.exists()


def test_rotation_has_a_floor_so_it_cannot_empty_the_directory(env: Env) -> None:
    backups = env.data_dir / "db-backups"
    backups.mkdir(parents=True)
    for index in range(3):
        (backups / f"pre-upgrade-{index:02d}.db").write_bytes(b"db")

    run = env.housekeep(settings=_with(env.settings, keep_db_backups=1))

    assert run.db_backups_removed == 2
    assert len(list(backups.iterdir())) == 1


# ---------------------------------------------------------------------------------------
# The database side
# ---------------------------------------------------------------------------------------


def test_health_history_is_a_ring_buffer_of_a_week(env: Env) -> None:
    env.add_health(observed_at=NOW - 8 * DAY)
    env.add_health(observed_at=NOW - 2 * DAY)

    run = env.housekeep()

    assert run.rows_pruned == {"health_history": 1}
    assert env.count("health_history") == 1


def test_audit_events_are_kept_for_ninety_days(env: Env) -> None:
    env.add_event(at=NOW - 91 * DAY)
    env.add_event(at=NOW - 89 * DAY)

    run = env.housekeep()

    assert run.rows_pruned == {"system_events": 1}
    assert env.count("system_events") == 1


def test_an_old_attempt_for_a_settled_clip_is_forgotten(env: Env) -> None:
    clip_id = env.add_clip(review_id="r-done", state="UPLOADED")
    env.add_attempt(clip_id=clip_id, started_at=NOW - 40 * DAY)

    run = env.housekeep()

    assert run.rows_pruned == {"upload_attempts": 1}
    assert env.count("upload_attempts") == 0


def test_an_old_attempt_for_a_clip_still_in_flight_is_kept(env: Env) -> None:
    clip_id = env.add_clip(review_id="r-stuck", state="RETRY_WAIT")
    env.add_attempt(clip_id=clip_id, started_at=NOW - 40 * DAY)

    run = env.housekeep()

    # The history of *why* an upload keeps failing is the only evidence an operator has.
    assert run.rows_pruned == {}
    assert env.count("upload_attempts") == 1


def test_a_permanently_failed_clip_keeps_its_attempts(env: Env) -> None:
    clip_id = env.add_clip(review_id="r-bad", state="FAILED_PERMANENT")
    env.add_attempt(clip_id=clip_id, started_at=NOW - 400 * DAY)

    env.housekeep()

    # FAILED_PERMANENT holds its file until a human decides, so its evidence is held too.
    assert env.count("upload_attempts") == 1


def test_a_held_pin_is_never_pruned(env: Env) -> None:
    env.add_pin(event_id="e-held", state="held", settled_at=NOW - 400 * DAY)
    env.add_pin(event_id="e-released", state="released", settled_at=NOW - 40 * DAY)
    env.add_pin(event_id="e-recent", state="released", settled_at=NOW - 2 * DAY)

    run = env.housekeep()

    assert run.rows_pruned == {"pinned_events": 1}
    assert env.count("pinned_events") == 2


def test_a_dry_run_deletes_nothing_and_reports_the_same_shape(env: Env) -> None:
    stale = env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.tmp-1", age_s=2 * DAY)
    env.write(f"{CAMERA}/2026/01/01/00-05-00.mp4.bad", age_s=30 * DAY)
    env.add_event(at=NOW - 400 * DAY)

    run = env.housekeep(settings=_with(env.settings, dry_run=True))

    assert run.dry_run
    assert stale.exists()
    assert env.count("system_events") == 1
    assert "stale tmp" in run.summary()


def test_the_summary_names_what_it_did(env: Env) -> None:
    env.write(f"{CAMERA}/2026/01/01/00-00-00.mp4.tmp-1", age_s=2 * DAY)
    env.add_event(at=NOW - 400 * DAY)

    run = env.housekeep()

    assert "1 stale tmp" in run.summary()
    assert "system_events=1" in run.summary()


# ---------------------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------------------


def test_housekeeping_runs_when_it_has_never_run(env: Env) -> None:
    service = cleanup.CleanupService(settings=env.settings, config=_config(env), adapter=None)

    run = service.run_once()

    assert isinstance(run.housekeep, HousekeepRun)


def test_housekeeping_does_not_run_again_inside_its_interval(env: Env) -> None:
    service = cleanup.CleanupService(settings=env.settings, config=_config(env), adapter=None)
    service.run_once()

    second = service.run_once()

    assert second.housekeep is None


def test_an_interval_of_zero_disables_housekeeping(env: Env) -> None:
    settings = _with(env.settings, housekeep_interval_s=0.0)
    service = cleanup.CleanupService(settings=settings, config=_config(env), adapter=None)

    run = service.run_once()

    assert run.housekeep is None


def test_a_zero_rotation_floor_is_a_configuration_error() -> None:
    settings = _with(cleanup.CleanupSettings.from_environ({}), keep_db_backups=0, health_port=0)

    assert settings.configuration_problem() is not None


def test_the_backup_directory_defaults_to_the_conventional_one(env: Env) -> None:
    assert env.settings.db_backups_dir == env.data_dir / "db-backups"
    assert _with(env.settings, db_backup_dir=Path("/elsewhere")).db_backups_dir == Path(
        "/elsewhere"
    )


def test_reconcile_and_housekeeping_are_independent_passes(env: Env) -> None:
    settings = _with(env.settings, reconcile_interval_s=0.0)
    service = cleanup.CleanupService(settings=settings, config=_config(env), adapter=None)

    run = service.run_once()

    # Disabling one daily pass must not disable the other: they answer different questions.
    assert run.reconcile is None
    assert isinstance(run.housekeep, HousekeepRun)


def test_a_single_cycle_can_carry_all_three_daily_passes(env: Env) -> None:
    service = cleanup.CleanupService(settings=env.settings, config=_config(env), adapter=None)

    run = service.run_once()

    # A fresh installation has no watermarks, so every pass is due at once. The hourly passes
    # (J1, J2) are always present; the two daily ones are `None` on a later cycle.
    assert isinstance(run.cloud, cleanup.CloudRun)
    assert isinstance(run.reconcile, ReconcileRun)
    assert isinstance(run.housekeep, HousekeepRun)


def _with(settings: cleanup.CleanupSettings, **changes: Any) -> cleanup.CleanupSettings:
    import dataclasses

    return dataclasses.replace(settings, **changes)


def _config(env: Env) -> config.CamerasConfig:
    return config.load_cameras_config(env.root / "cameras.yml", environ={})
