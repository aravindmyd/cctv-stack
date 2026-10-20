"""Local retention (cleanup J1) — INV-1, INV-13, and the M4 criteria J1 can satisfy.

The tests that matter most here are the negative ones. Deleting the right thing is easy;
*not* deleting the wrong thing is the whole point of [INV-1], so most of this file is about
clips that must survive: un-uploaded clips, unprovable backups, poisoned paths, and anything
that is not a clip at all.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctv import cleanup, db, health
from cctv.config import CamerasConfig, load_cameras_config

NOW = 1_700_000_000.0
SECONDS_PER_DAY = 86400.0
CAMERA = "front-door"


# ---------------------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self, value: float = NOW) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_config(tmp_path: Path, cameras: Mapping[str, Mapping[str, Any]]) -> CamerasConfig:
    """Write a minimal `cameras.yml` and load it through the real config model.

    Going through the loader rather than hand-building dataclasses means these tests break if
    the config contract changes, which is the point: retention is read from the operator's
    file, not from a test's idea of it.
    """
    lines = ["site:", "  location: test-site", "  timezone: UTC", "", "cameras:"]
    for name, body in cameras.items():
        lines.append(f"  {name}:")
        lines.append(f"    display_name: {body.get('display_name', name)}")
        lines.append("    rtsp:")
        lines.append(f"      main: rtsp://127.0.0.1:8554/{name}")
        retention = body.get("retention")
        if retention:
            lines.append("    retention:")
            for key, value in retention.items():
                lines.append(f"      {key}: {value}")
    path = tmp_path / "cameras.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_cameras_config(path)


def make_settings(tmp_path: Path, **overrides: Any) -> cleanup.CleanupSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path,
        "cameras_path": tmp_path / "cameras.yml",
        "db_path": tmp_path / "cctv.db",
        "run_interval_s": 3600.0,
        "batch_size": 200,
        "dry_run": False,
        "retire_grace_days": 0,
        "health_host": "127.0.0.1",
        "health_port": 0,
        "scheduler_tick_s": 0.01,
        "clips_overdue_warn": 1,
        "clips_overdue_critical": 10,
        "overdue_age_critical_days": 30,
        "disk_warn_pct": 15.0,
        "disk_critical_pct": 5.0,
        "log_level": None,
    }
    base.update(overrides)
    return cleanup.CleanupSettings(**base)


@dataclass
class Env:
    conn: sqlite3.Connection
    settings: cleanup.CleanupSettings
    retention: cleanup.LocalRetention
    config: CamerasConfig
    clock: Clock
    data_dir: Path

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"


def build_env(
    tmp_path: Path,
    cameras: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    logger: Any | None = None,
    **settings_overrides: Any,
) -> Env:
    cameras = cameras or {CAMERA: {}}
    settings = make_settings(tmp_path, **settings_overrides)
    config = make_config(tmp_path, cameras)
    # The clip-worker creates this on first materialisation; the health check treats its
    # absence as unhealthy, so a test environment without it is not representative.
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_db(settings.db_path)
    db.migrate(conn)
    for name in cameras:
        db.ensure_camera(conn, name=name, display_name=name, now=NOW)
    clock = Clock()
    retention = cleanup.LocalRetention(settings=settings, logger=logger, clock=clock)
    return Env(
        conn=conn,
        settings=settings,
        retention=retention,
        config=config,
        clock=clock,
        data_dir=tmp_path,
    )


def add_clip(
    env: Env,
    *,
    review_id: str,
    camera: str = CAMERA,
    days_old: float = 200.0,
    state: str = "PENDING",
    body: bytes = b"a plausible clip",
    sidecar: bool = True,
    relpath: str | None = None,
    cloud_key: str | None = None,
    uploaded_at: float | None = None,
    write_files: bool = True,
) -> tuple[int, Path, Path | None]:
    """Write a real file (and sidecar) and insert a real row.

    Test setup bypasses the uploader's state machine on purpose — the point is to arrange a
    row in a given state, not to re-test how it got there. `upload_state` is still validated
    by `insert_clip` against the contract's closed set. `write_files=False` records a row whose
    path is deliberately not created, which is how a poisoned or already-gone path is staged
    without the fixture itself writing outside the data directory.
    """
    start = NOW - days_old * SECONDS_PER_DAY
    relative = relpath or f"clips/{camera}/2026/09/12/18-45-00.mp4"
    target = env.data_dir / relative
    sidecar_path = None
    if write_files:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if sidecar:
            sidecar_path = relative + ".json"
            (env.data_dir / sidecar_path).write_text('{"label": "person"}', encoding="utf-8")
    elif sidecar:
        sidecar_path = relative + ".json"

    clip_id, _created = db.insert_clip(
        env.conn,
        db.ClipRecord(
            review_id=review_id,
            camera=camera,
            severity="alert",
            start_time=start,
            end_time=start + 30,
            path=relative,
            sidecar_path=sidecar_path,
            label="person",
            duration_s=30.0,
            size_bytes=len(body),
            materialised_at=start,
            upload_state="PENDING",
        ),
        now=NOW,
    )

    if state != "PENDING":
        # Raw SQL rather than the uploader's helpers: the state machine only reaches
        # UPLOADED through a claim and a lease, which is not what these tests are arranging.
        with db.write_txn(env.conn):
            env.conn.execute(
                "UPDATE clips SET upload_state = ?, cloud_key = ?, uploaded_at = ?, attempts = 1 "
                "WHERE id = ?",
                (
                    state,
                    cloud_key if cloud_key is not None else f"test-site/{relative}",
                    uploaded_at if uploaded_at is not None else NOW,
                    clip_id,
                ),
            )
    return clip_id, target, (env.data_dir / sidecar_path if sidecar_path else None)


def state_of(env: Env, clip_id: int) -> str:
    row = env.conn.execute("SELECT upload_state FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return str(row["upload_state"])


def row_of(env: Env, clip_id: int) -> sqlite3.Row:
    row = env.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row is not None
    return row


@pytest.fixture
def env(tmp_path: Path) -> Any:
    built = build_env(tmp_path)
    yield built
    built.conn.close()


# ---------------------------------------------------------------------------------------
# The happy path — an uploaded clip past its window goes, with its sidecar (M4.4)
# ---------------------------------------------------------------------------------------


def test_an_uploaded_clip_past_retention_is_deleted_with_its_sidecar(env: Env) -> None:
    clip_id, target, sidecar = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=100)

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 1
    assert not target.exists(), "the clip is gone"
    assert sidecar is not None and not sidecar.exists(), "and so is its sidecar"
    row = row_of(env, clip_id)
    assert row["upload_state"] == "DELETED_LOCAL"
    assert row["local_deleted_at"] is not None
    assert run.bytes_freed == len(b"a plausible clip")


def test_a_clip_inside_its_retention_window_is_kept(env: Env) -> None:
    clip_id, target, _ = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=10)

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 0
    assert target.exists()
    assert state_of(env, clip_id) == "UPLOADED"


def test_the_boundary_is_the_window_not_a_margin(env: Env) -> None:
    """A clip just inside `local_days` survives; just outside, it goes."""
    inside, inside_path, _ = add_clip(
        env,
        review_id="rev-in",
        state="UPLOADED",
        days_old=89.9,
        relpath="clips/f/d/inside.mp4",
    )
    outside, outside_path, _ = add_clip(
        env,
        review_id="rev-out",
        state="UPLOADED",
        days_old=90.1,
        relpath="clips/f/d/outside.mp4",
    )

    env.retention.run_once(env.conn, env.config)

    assert inside_path.exists() and state_of(env, inside) == "UPLOADED"
    assert not outside_path.exists() and state_of(env, outside) == "DELETED_LOCAL"


def test_skipped_by_policy_clips_expire_locally(env: Env) -> None:
    """`SKIPPED_BY_POLICY` means "deliberately never uploaded", so the file may go."""
    clip_id, target, _ = add_clip(env, review_id="rev-1", state="SKIPPED_BY_POLICY", days_old=100)

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 1
    assert not target.exists()
    assert state_of(env, clip_id) == "DELETED_LOCAL"


# ---------------------------------------------------------------------------------------
# INV-1 — the clips that must survive, whatever their age
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["PENDING", "UPLOADING", "RETRY_WAIT", "FAILED_PERMANENT"])
def test_a_clip_that_is_not_backed_up_is_never_deleted(env: Env, state: str) -> None:
    """[INV-1] The single most important assertion in this file.

    Every one of these states means "the cloud does not have this clip yet". Deleting any of
    them destroys the only copy — which is the failure INV-1 exists to make impossible.
    """
    clip_id, target, sidecar = add_clip(env, review_id="rev-1", state=state, days_old=3650)

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 0
    assert target.exists(), f"a {state} clip was deleted"
    assert sidecar is not None and sidecar.exists()
    assert state_of(env, clip_id) == state


def test_a_deletable_state_does_not_make_every_state_deletable(env: Env) -> None:
    """[M4.3] A state added later is safe by default.

    The query *enumerates* the states that permit deletion, so this is a structural property
    rather than a promise: adding a state to `CLIP_STATES` without adding it to
    `LOCAL_EXPIRY_STATES` cannot make it deletable.
    """
    # ADR-0023 added EXPIRED_CLOUD: the cloud copy is gone, so the file is the only one left.
    assert frozenset({"UPLOADED", "SKIPPED_BY_POLICY", "EXPIRED_CLOUD"}) == (db.LOCAL_EXPIRY_STATES)
    # The safety point is unchanged: a clip whose upload has not finished is never deletable.
    assert db.LOCAL_EXPIRY_STATES.isdisjoint(db.HELD_STATES)
    assert db.LOCAL_EXPIRY_STATES <= db.CLIP_STATES

    # A real state outside the permitted set is not returned by the query, even though it is
    # old enough and its row carries no local_deleted_at.
    clip_id, _target, _ = add_clip(env, review_id="rev-1", state="FORCE_DELETED", days_old=3650)
    batch = db.local_expiry_batch(env.conn, camera=CAMERA, cutoff=NOW, limit=10)
    assert [row["id"] for row in batch] == []
    assert "FORCE_DELETED" not in db.LOCAL_EXPIRY_STATES
    assert state_of(env, clip_id) == "FORCE_DELETED"


def test_an_uploaded_clip_without_a_cloud_key_is_refused(env: Env) -> None:
    """Guard 3: `UPLOADED` alone is not proof of a backup.

    A row marked uploaded with no recorded object is a bookkeeping bug, not a backup. The
    file stays, and the operator gets a line naming the reason.
    """
    clip_id, target, _ = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=200)
    with db.write_txn(env.conn):
        env.conn.execute("UPDATE clips SET cloud_key = NULL WHERE id = ?", (clip_id,))

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 0
    assert run.refused == 1
    assert target.exists()
    assert state_of(env, clip_id) == "UPLOADED"


def test_an_uploaded_clip_without_an_upload_timestamp_is_refused(env: Env) -> None:
    clip_id, target, _ = add_clip(
        env, review_id="rev-1", state="UPLOADED", days_old=200, uploaded_at=0.0
    )
    with db.write_txn(env.conn):
        env.conn.execute("UPDATE clips SET uploaded_at = NULL WHERE id = ?", (clip_id,))

    run = env.retention.run_once(env.conn, env.config)

    assert run.deleted == 0 and run.refused == 1
    assert target.exists()


def test_an_already_deleted_clip_is_not_counted_twice(env: Env) -> None:
    add_clip(env, review_id="rev-1", state="UPLOADED", days_old=100)
    first = env.retention.run_once(env.conn, env.config)
    second = env.retention.run_once(env.conn, env.config)

    assert first.deleted == 1
    assert second.deleted == 0, "a second pass finds nothing, because local_deleted_at is set"


# ---------------------------------------------------------------------------------------
# Guard 4 — a poisoned path deletes nothing (M4.9)
# ---------------------------------------------------------------------------------------


def test_a_path_that_escapes_the_clips_directory_raises(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()

    with pytest.raises(cleanup.CleanupPathError):
        cleanup.resolve_within_clips_dir(clips, "clips/../../etc/passwd")

    with pytest.raises(cleanup.CleanupPathError):
        cleanup.resolve_within_clips_dir(clips, "/etc/passwd")

    with pytest.raises(cleanup.CleanupPathError):
        cleanup.resolve_within_clips_dir(clips, "")


def test_a_poisoned_path_is_refused_and_nothing_else_is_affected(env: Env, tmp_path: Path) -> None:
    """A tampered row is a finding, not a crash: the run continues and deletes nothing.

    The file the poisoned path points at must still exist afterwards — the guard exists so
    that a corrupted database cannot turn cleanup into a weapon.
    """
    victim = tmp_path.parent / "victim.txt"
    victim.write_text("do not delete me", encoding="utf-8")

    poisoned, _target, _ = add_clip(
        env,
        review_id="rev-poison",
        state="UPLOADED",
        days_old=3650,
        relpath=f"clips/../../{victim.name}",
        write_files=False,
    )
    healthy, healthy_path, _ = add_clip(
        env,
        review_id="rev-healthy",
        state="UPLOADED",
        days_old=200,
        relpath="clips/front-door/2026/09/12/18-45-00.mp4",
    )

    run = env.retention.run_once(env.conn, env.config)

    assert victim.read_text(encoding="utf-8") == "do not delete me", (
        "cleanup reached outside clips/"
    )
    assert run.refused == 1
    assert state_of(env, poisoned) == "UPLOADED"
    assert run.deleted == 1, "the good clip was still handled"
    assert not healthy_path.exists()
    assert state_of(env, healthy) == "DELETED_LOCAL"


# ---------------------------------------------------------------------------------------
# Missing files and dry runs
# ---------------------------------------------------------------------------------------


def test_a_missing_file_is_recorded_and_marked_not_treated_as_an_error(
    env: Env,
) -> None:
    clip_id, target, sidecar = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=100)
    target.unlink()
    assert sidecar is not None
    sidecar.unlink()

    run = env.retention.run_once(env.conn, env.config)

    assert run.missing == 1
    assert run.deleted == 0
    assert state_of(env, clip_id) == "DELETED_LOCAL"
    finding = env.conn.execute(
        "SELECT kind, subject FROM reconciliation_findings ORDER BY id DESC"
    ).fetchone()
    assert finding["kind"] == "missing_local_file"
    assert finding["subject"] == "clips/front-door/2026/09/12/18-45-00.mp4"


def test_dry_run_deletes_nothing_and_logs_every_intention(env: Env, logs: Any) -> None:
    """[M4.12] The operator's way to see what retention *would* do before it does it."""
    env.settings = make_settings(env.data_dir, dry_run=True)
    env.retention = cleanup.LocalRetention(
        settings=env.settings, logger=logging.getLogger("cctv.cleanup"), clock=env.clock
    )

    clip_id, target, sidecar = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=100)

    run = env.retention.run_once(env.conn, env.config)

    assert run.dry_run is True
    assert run.would_delete == 1
    assert run.deleted == 0
    assert target.exists() and sidecar is not None and sidecar.exists()
    assert state_of(env, clip_id) == "UPLOADED", "not even the row is touched"
    assert any("CLEANUP_WOULD_DELETE" in message for message in logs.messages())


def test_a_dry_run_reports_every_expired_clip_across_pages(tmp_path: Path) -> None:
    """Regression: paging must advance even when a page changes nothing.

    Under `DRY_RUN` no row is updated, so the eligible set never shrinks. An implementation
    that paged by re-querying until the result was empty would loop forever here; one that
    stopped as soon as a page made no progress would silently report only the first page.
    Five clips with a batch size of two is therefore the smallest case that catches both.
    """
    env = build_env(tmp_path, {CAMERA: {}}, batch_size=2, dry_run=True)
    try:
        created = [
            add_clip(
                env,
                review_id=f"rev-{index}",
                state="UPLOADED",
                days_old=100 + index,
                relpath=f"clips/{CAMERA}/2026/09/12/clip-{index:02d}.mp4",
            )
            for index in range(5)
        ]

        run = env.retention.run_once(env.conn, env.config)

        assert run.would_delete == 5, "every clip must be reported, not just the first page"
        assert run.deleted == 0
        assert all(path.exists() for _clip_id, path, _side in created)
    finally:
        env.conn.close()


# ---------------------------------------------------------------------------------------
# Per-camera windows, retired cameras, and batching
# ---------------------------------------------------------------------------------------


def test_each_camera_uses_its_own_window(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        {
            "short": {"retention": {"local_days": 1}},
            "long": {"retention": {"local_days": 3650}},
        },
    )
    try:
        old_short, short_path, _ = add_clip(
            env,
            review_id="rev-short",
            camera="short",
            state="UPLOADED",
            days_old=30,
            relpath="clips/short/2026/09/12/a.mp4",
        )
        old_long, long_path, _ = add_clip(
            env,
            review_id="rev-long",
            camera="long",
            state="UPLOADED",
            days_old=30,
            relpath="clips/long/2026/09/12/b.mp4",
        )

        run = env.retention.run_once(env.conn, env.config)

        assert run.deleted == 1
        assert not short_path.exists() and state_of(env, old_short) == "DELETED_LOCAL"
        assert long_path.exists() and state_of(env, old_long) == "UPLOADED"
    finally:
        env.conn.close()


def test_a_retired_camera_still_expires_on_the_default_window(tmp_path: Path) -> None:
    """[INV-13] A removed camera keeps its rows — but its footage does not become immortal.

    Retiring the camera deletes nothing by itself; the clips expire on the default window
    like anyone else's. Skipping retired cameras would mean their files live forever.
    """
    env = build_env(tmp_path, {CAMERA: {}})
    try:
        fresh, fresh_path, _ = add_clip(
            env,
            review_id="rev-fresh",
            state="UPLOADED",
            days_old=1,
            relpath="clips/front-door/2026/09/12/fresh.mp4",
        )
        old, old_path, _ = add_clip(
            env,
            review_id="rev-old",
            state="UPLOADED",
            days_old=100,
            relpath="clips/front-door/2026/09/12/old.mp4",
        )
        # The camera is retired by removing it from config and stamping the row.
        env.config = make_config(tmp_path, {"other": {}})
        db.ensure_camera(env.conn, name=CAMERA, display_name="Front Door", enabled=False, now=NOW)
        with db.write_txn(env.conn):
            env.conn.execute("UPDATE cameras SET retired_at = ? WHERE name = ?", (NOW, CAMERA))

        run = env.retention.run_once(env.conn, env.config)

        assert run.deleted == 1
        assert fresh_path.exists() and state_of(env, fresh) == "UPLOADED"
        assert not old_path.exists() and state_of(env, old) == "DELETED_LOCAL"
        assert (
            env.conn.execute("SELECT COUNT(*) FROM cameras WHERE name = ?", (CAMERA,)).fetchone()[0]
            == 1
        ), "the row is retained, not deleted"
    finally:
        env.conn.close()


def test_retire_grace_days_extends_only_a_retired_cameras_window(
    tmp_path: Path,
) -> None:
    """The grace protects retired footage — and nothing else.

    A camera still in `cameras.yml` uses its **configured** window whatever its `retired_at`
    says. The grace exists for a camera the operator has removed from configuration, whose
    clips are therefore on a window nobody can see any more.
    """
    env = build_env(tmp_path, {CAMERA: {}, "gate": {}}, retire_grace_days=3650)
    try:
        retired_id, retired_path, _ = add_clip(
            env,
            review_id="rev-retired",
            camera=CAMERA,
            state="UPLOADED",
            days_old=200,
            relpath=f"clips/{CAMERA}/2026/09/12/retired.mp4",
        )
        active_id, active_path, _ = add_clip(
            env,
            review_id="rev-active",
            camera="gate",
            state="UPLOADED",
            days_old=200,
            relpath="clips/gate/2026/09/12/active.mp4",
        )
        # Leaving the configuration is what "retired" means (INV-13).
        env.config = make_config(tmp_path, {"gate": {}})
        db.ensure_camera(env.conn, name=CAMERA, display_name="Front Door", enabled=False, now=NOW)
        with db.write_txn(env.conn):
            env.conn.execute("UPDATE cameras SET retired_at = ? WHERE name = ?", (NOW, CAMERA))

        run = env.retention.run_once(env.conn, env.config)

        assert run.deleted == 1
        assert retired_path.exists(), "the retired camera's clip is inside the extended window"
        assert state_of(env, retired_id) == "UPLOADED"
        assert not active_path.exists(), "the grace must not protect a camera still in config"
        assert state_of(env, active_id) == "DELETED_LOCAL"
    finally:
        env.conn.close()


def test_more_clips_than_one_batch_are_all_expired(tmp_path: Path) -> None:
    """Batching must not become a ceiling: the page loop continues until nothing is left."""
    env = build_env(tmp_path, {CAMERA: {}}, batch_size=2)
    try:
        created = [
            add_clip(
                env,
                review_id=f"rev-{index}",
                state="UPLOADED",
                days_old=100 + index,
                relpath=f"clips/{CAMERA}/2026/09/12/clip-{index:02d}.mp4",
            )
            for index in range(5)
        ]

        run = env.retention.run_once(env.conn, env.config)

        assert run.deleted == 5
        assert run.considered == 5
        assert all(not path.exists() for _clip_id, path, _side in created)
    finally:
        env.conn.close()


def test_the_oldest_clip_is_expired_first(tmp_path: Path) -> None:
    env = build_env(tmp_path, {CAMERA: {}}, batch_size=1)
    try:
        oldest, oldest_path, _ = add_clip(
            env,
            review_id="rev-old",
            state="UPLOADED",
            days_old=300,
            relpath=f"clips/{CAMERA}/2026/09/12/old.mp4",
        )
        newest, newest_path, _ = add_clip(
            env,
            review_id="rev-new",
            state="UPLOADED",
            days_old=100,
            relpath=f"clips/{CAMERA}/2026/09/12/new.mp4",
        )

        first_batch = db.local_expiry_batch(env.conn, camera=CAMERA, cutoff=NOW, limit=1)
        assert [row["id"] for row in first_batch] == [oldest]

        env.retention.run_once(env.conn, env.config)
        assert not oldest_path.exists() and not newest_path.exists()
        assert state_of(env, oldest) == "DELETED_LOCAL" == state_of(env, newest)
    finally:
        env.conn.close()


# ---------------------------------------------------------------------------------------
# Nothing outside clips/ is touched (M4.10)
# ---------------------------------------------------------------------------------------


def test_frigates_footage_is_byte_identical_after_a_full_cycle(env: Env) -> None:
    """Retention owns `clips/` and nothing else. Frigate's recordings are its property."""
    media = env.data_dir / "media" / "recordings" / "2026-09-12" / "18" / CAMERA
    media.mkdir(parents=True)
    (media / "45.00.mp4").write_bytes(b"frigate segment bytes")
    frigate = env.data_dir / "frigate"
    frigate.mkdir()
    (frigate / "config.yml").write_text("cameras: {}\n", encoding="utf-8")

    add_clip(env, review_id="rev-1", state="UPLOADED", days_old=200)
    env.retention.run_once(env.conn, env.config)

    assert (media / "45.00.mp4").read_bytes() == b"frigate segment bytes"
    assert (frigate / "config.yml").read_text(encoding="utf-8") == "cameras: {}\n"


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------


def test_settings_defaults(tmp_path: Path) -> None:
    settings = cleanup.CleanupSettings.from_environ({})

    assert settings.data_dir == Path("/data")
    assert settings.db_path == Path("/data/cctv.db")
    assert settings.cameras_path == Path("/config/cameras.yml")
    assert settings.run_interval_s == cleanup.DEFAULT_RUN_INTERVAL_S
    assert settings.batch_size == cleanup.DEFAULT_BATCH_SIZE
    assert settings.dry_run is False
    assert settings.configuration_problem() is None


def test_settings_read_the_documented_variables() -> None:
    settings = cleanup.CleanupSettings.from_environ(
        {
            "CCTV_DATA_DIR": "/srv/cctv",
            "CCTV_DB": "/srv/cctv/state.db",
            "CCTV_CLEANUP_RUN_INTERVAL_S": "900",
            "CCTV_CLEANUP_BATCH_SIZE": "50",
            "DRY_RUN": "1",
            "CCTV_CLEANUP_RETIRE_GRACE_DAYS": "7",
        }
    )

    assert settings.data_dir == Path("/srv/cctv")
    assert settings.db_path == Path("/srv/cctv/state.db")
    assert settings.run_interval_s == 900.0
    assert settings.batch_size == 50
    assert settings.dry_run is True
    assert settings.retire_grace_days == 7
    assert settings.clips_dir == Path("/srv/cctv/clips")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"run_interval_s": 0.0}, "CCTV_CLEANUP_RUN_INTERVAL_S"),
        ({"batch_size": 0}, "CCTV_CLEANUP_BATCH_SIZE"),
        ({"retire_grace_days": -1}, "CCTV_CLEANUP_RETIRE_GRACE_DAYS"),
    ],
)
def test_a_bad_setting_names_itself(tmp_path: Path, overrides: Any, expected: str) -> None:
    problem = make_settings(tmp_path, **overrides).configuration_problem()
    assert problem is not None and expected in problem


def test_a_non_numeric_interval_is_a_config_error() -> None:
    from cctv.config import ConfigError

    with pytest.raises(ConfigError):
        cleanup.CleanupSettings.from_environ({"CCTV_CLEANUP_RUN_INTERVAL_S": "soon"})


# ---------------------------------------------------------------------------------------
# Guard 2 — the state machine still refuses an illegal deletion
# ---------------------------------------------------------------------------------------


def test_marking_a_pending_clip_deleted_is_an_illegal_transition(env: Env) -> None:
    """The row update goes through the same transition table as everything else.

    This is guard 2's mechanism: even if the page query were bypassed, the accessor refuses
    `PENDING -> DELETED_LOCAL`.
    """
    clip_id, _target, _ = add_clip(env, review_id="rev-1", state="PENDING", days_old=100)

    with pytest.raises(db.IllegalTransitionError), db.write_txn(env.conn):
        db.mark_local_deleted(env.conn, clip_id=clip_id, now=NOW)


# ---------------------------------------------------------------------------------------
# Migration 0002 — the finding kinds J1 needs
# ---------------------------------------------------------------------------------------


def test_every_documented_finding_kind_is_accepted_by_the_schema(env: Env) -> None:
    with db.write_txn(env.conn):
        for kind in sorted(db.FINDING_KINDS):
            db.record_finding(env.conn, kind=kind, subject="x", now=NOW)

    stored = {
        row["kind"]
        for row in env.conn.execute("SELECT kind FROM reconciliation_findings").fetchall()
    }
    assert stored == db.FINDING_KINDS, "the CHECK and the documented set must agree"


def test_an_undocumented_finding_kind_is_refused(env: Env) -> None:
    with pytest.raises(db.DbError), db.write_txn(env.conn):
        db.record_finding(env.conn, kind="something_invented", subject="x", now=NOW)


def test_migration_0002_preserves_existing_findings(tmp_path: Path) -> None:
    """[INV-8] A schema change must not destroy what it migrates.

    A table rebuild is the one migration shape that can lose data if it is written wrong, so
    this drives the real path: build a version-1 database, record a finding, then apply 0002
    and assert the finding is still there.
    """
    v1_only = tmp_path / "migrations_v1"
    v1_only.mkdir()
    shutil.copy(db.MIGRATIONS_DIR / "0001_init.sql", v1_only / "0001_init.sql")
    # Derived from disk rather than hard-coded: every migration added later would otherwise
    # turn this test red for a reason that has nothing to do with what it checks.
    newest = max(int(path.name.split("_", 1)[0]) for path in db.MIGRATIONS_DIR.glob("0*.sql"))

    conn = db.open_db(tmp_path / "state.db")
    try:
        assert db.migrate(conn, migrations_dir=v1_only) == 1
        with db.write_txn(conn):
            conn.execute(
                "INSERT INTO reconciliation_findings (run_at, kind, subject, detail) "
                "VALUES (?, 'orphan_object', 'some/key', NULL)",
                (NOW,),
            )

        assert db.migrate(conn, migrations_dir=db.MIGRATIONS_DIR) == newest

        row = conn.execute("SELECT kind, subject FROM reconciliation_findings").fetchone()
        assert row["kind"] == "orphan_object"
        assert row["subject"] == "some/key"
        assert db.schema_version(conn) == newest
        # The index the rebuild dropped must be back, or J3's finding lookup degrades to a scan.
        indexes = {
            item["name"]
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'reconciliation_findings'"
            ).fetchall()
        }
        assert "reconciliation_open" in indexes
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# Health — INV-6, and the honest half of J1's trade (lld/05 §7, §9)
# ---------------------------------------------------------------------------------------


def reporter_for(env: Env, **overrides: Any) -> cleanup.CleanupHealth:
    settings = make_settings(env.data_dir, **overrides) if overrides else env.settings
    return cleanup.CleanupHealth(
        settings=settings,
        config=env.config,
        started_at=env.clock(),
        clock=env.clock,
    )


def check_of(report: health.HealthReport, name: str) -> health.Check:
    return next(check for check in report.checks if check.name == name)


def service_for(env: Env, **overrides: Any) -> cleanup.CleanupService:
    settings = make_settings(env.data_dir, **overrides) if overrides else env.settings
    return cleanup.CleanupService(settings=settings, config=env.config, clock=env.clock)


def a_healthy_disk(monkeypatch: pytest.MonkeyPatch, *, free_pct: float = 50.0) -> None:
    """Pin free space, so a health assertion is about this code and not about the machine.

    Without this, `test_a_quiet_system_is_healthy` passes or fails depending on how full the
    developer's laptop is, which is the kind of test that gets deleted rather than fixed.
    """
    total = 1000.0
    free = total * free_pct / 100.0
    monkeypatch.setattr(
        cleanup.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=total, used=total - free, free=free),
    )


def test_a_quiet_system_is_healthy(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle is not unhealthy: retention with nothing to do is working correctly."""
    a_healthy_disk(monkeypatch)
    service_for(env).run_once()

    report = reporter_for(env).report()

    assert report.state == health.HEALTHY
    assert check_of(report, "clips_overdue_retention").state == health.HEALTHY
    assert check_of(report, "last_run").state == health.HEALTHY
    assert check_of(report, "state_db").state == health.HEALTHY
    assert check_of(report, "clips_dir").state == health.HEALTHY


def test_cloud_retention_without_a_bucket_is_disabled_not_healthy(env: Env) -> None:
    """No bucket is a supported choice, and `DISABLED` is neutral in `derive_state`.

    What must never happen is `HEALTHY`: a capability that cannot run is not a capability that
    passed.
    """
    check = check_of(reporter_for(env).report(), "cloud_retention")

    assert check.state == health.DISABLED
    assert check.code is None
    assert "no bucket" in (check.detail or "")


def test_a_service_that_has_never_run_is_not_healthy(env: Env) -> None:
    """[INV-6] An absence of information must never render as good news."""
    check = check_of(reporter_for(env).report(), "last_run")

    assert check.state == health.DEGRADED
    assert check.code == "CLEANUP_STALE"


def test_the_last_run_going_stale_degrades_then_fails(env: Env) -> None:
    service_for(env).run_once()
    reporter = reporter_for(env)
    assert check_of(reporter.report(), "last_run").state == health.HEALTHY

    env.clock.advance(3 * env.settings.run_interval_s)
    assert check_of(reporter.report(), "last_run").state == health.DEGRADED

    env.clock.advance(10 * env.settings.run_interval_s)
    assert check_of(reporter.report(), "last_run").state == health.UNHEALTHY


def test_one_held_clip_degrades_retention(env: Env) -> None:
    """A clip held past its window is the invariant costing disk, and is reported at once."""
    add_clip(env, review_id="rev-held", state="PENDING", days_old=100)
    # 100 days old on a 90-day window: ten days overdue, not a hundred.
    check = check_of(reporter_for(env).report(), "clips_overdue_retention")

    assert check.state == health.DEGRADED
    assert check.code == "CLIPS_OVERDUE_RETENTION"
    assert "10.0d" in (check.detail or "")


def test_more_held_clips_than_the_threshold_is_unhealthy(tmp_path: Path) -> None:
    env = build_env(tmp_path, {CAMERA: {}}, clips_overdue_critical=2)
    try:
        for index in range(3):
            add_clip(
                env,
                review_id=f"rev-{index}",
                state="FAILED_PERMANENT",
                days_old=100,
                relpath=f"clips/{CAMERA}/2026/09/12/held-{index}.mp4",
            )

        check = check_of(reporter_for(env).report(), "clips_overdue_retention")

        assert check.state == health.UNHEALTHY
        assert check.code == "CLIPS_OVERDUE_RETENTION"
    finally:
        env.conn.close()


def test_a_clip_held_far_past_its_deadline_is_unhealthy(env: Env) -> None:
    """One clip, but overdue by far more than the age limit: disk growth is unbounded."""
    add_clip(env, review_id="rev-ancient", state="PENDING", days_old=150)

    check = check_of(reporter_for(env).report(), "clips_overdue_retention")

    assert check.state == health.UNHEALTHY
    assert "60.0d" in (check.detail or "")


def test_a_recent_clip_is_not_counted_as_overdue(env: Env) -> None:
    add_clip(env, review_id="rev-fresh", state="PENDING", days_old=1)

    check = check_of(reporter_for(env).report(), "clips_overdue_retention")

    assert check.state == health.HEALTHY
    assert "0 clip(s) held" in (check.detail or "")


def test_an_absent_clips_directory_is_unhealthy(env: Env) -> None:
    shutil.rmtree(env.clips_dir, ignore_errors=True)

    check = check_of(reporter_for(env).report(), "clips_dir")

    assert check.state == health.UNHEALTHY
    assert check.code == "DISK_CRITICAL"


def test_an_unwritable_clips_directory_is_unhealthy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention that cannot delete is not working, however healthy the database is."""
    env.clips_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cleanup.os, "access", lambda *_args, **_kwargs: False)

    check = check_of(reporter_for(env).report(), "clips_dir")

    assert check.state == health.UNHEALTHY
    assert check.code == "DISK_CRITICAL"
    assert "not writable" in (check.detail or "")


@pytest.mark.parametrize(
    ("free_pct", "expected", "code"),
    [
        (50.0, health.HEALTHY, None),
        (10.0, health.DEGRADED, "DISK_PRESSURE"),
        (1.0, health.UNHEALTHY, "DISK_CRITICAL"),
    ],
)
def test_free_space_is_graded(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    free_pct: float,
    expected: str,
    code: str | None,
) -> None:
    a_healthy_disk(monkeypatch, free_pct=free_pct)

    check = check_of(reporter_for(env).report(), "disk_free")

    assert check.state == expected
    assert check.code == code


def test_an_unreadable_state_store_is_unhealthy(tmp_path: Path) -> None:
    env = build_env(tmp_path, {CAMERA: {}})
    try:
        env.settings = make_settings(tmp_path, db_path=tmp_path / "absent.db")
        check = check_of(reporter_for(env).report(), "state_db")

        assert check.state == health.UNHEALTHY
        assert check.code == "DB_UNAVAILABLE"
    finally:
        env.conn.close()


def test_every_code_the_report_uses_is_documented(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codes are a stable API, so this module may only emit ones from `health.CODES`.

    Exercises the unhappy branches rather than asserting the literals against themselves.
    """
    add_clip(env, review_id="rev-held", state="PENDING", days_old=200)
    env.settings = make_settings(env.data_dir, run_interval_s=1.0)
    env.clock.advance(100.0)
    monkeypatch.setattr(
        cleanup.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100.0, used=99.0, free=1.0),
    )

    report = reporter_for(env).report()
    codes = [check.code for check in report.checks if check.code is not None]

    assert codes, "the unhappy path should have produced at least one code"
    assert set(codes) <= health.CODES
    assert report.state == health.UNHEALTHY


def test_the_gauges_are_bounded(env: Env) -> None:
    """No clip ids or paths: a label per clip would grow the series count with the data."""
    env.clips_dir.mkdir(parents=True, exist_ok=True)
    add_clip(env, review_id="rev-held", state="PENDING", days_old=200)

    gauges = reporter_for(env).gauges()

    assert set(gauges) == {
        "cctv_clips_overdue_retention",
        "cctv_clips_overdue_bytes",
        "cctv_disk_free_pct",
    }
    assert gauges["cctv_clips_overdue_retention"] == 1
    assert isinstance(gauges["cctv_disk_free_pct"], float)


# ---------------------------------------------------------------------------------------
# The service loop
# ---------------------------------------------------------------------------------------


def test_a_pass_records_when_it_ran(env: Env) -> None:
    run = service_for(env).run_once()

    row = env.conn.execute(
        "SELECT * FROM sweep_state WHERE name = ?", (cleanup.LAST_RUN_NAME,)
    ).fetchone()
    assert row is not None
    assert float(row["last_success_at"]) == run.finished_at
    assert int(row["consecutive_failures"]) == 0


def test_a_pass_actually_expires_a_clip(env: Env) -> None:
    clip_id, target, _ = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=200)

    run = service_for(env).run_once()

    assert run.deleted == 1
    assert not target.exists()
    assert state_of(env, clip_id) == "DELETED_LOCAL"


def test_a_failing_pass_is_recorded_and_the_next_one_recovers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure must be visible as a *failed* run, not as a run that never happened."""
    real = cleanup.LocalRetention.run_once
    calls = {"n": 0}

    def flaky(self: Any, conn: Any, config: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated failure")
        return real(self, conn, config)

    # Patched on the class because the service builds its own runner from its settings —
    # which is the property that stops a dry-run setting being run by a live runner.
    monkeypatch.setattr(cleanup.LocalRetention, "run_once", flaky)
    service = service_for(env)

    with pytest.raises(RuntimeError):
        service.run_once()

    row = env.conn.execute(
        "SELECT * FROM sweep_state WHERE name = ?", (cleanup.LAST_RUN_NAME,)
    ).fetchone()
    assert row["last_success_at"] is None
    assert int(row["consecutive_failures"]) == 1
    assert check_of(reporter_for(env).report(), "last_run").state == health.DEGRADED

    service.run_once()

    row = env.conn.execute(
        "SELECT * FROM sweep_state WHERE name = ?", (cleanup.LAST_RUN_NAME,)
    ).fetchone()
    assert row["last_success_at"] is not None
    assert int(row["consecutive_failures"]) == 0


def test_the_service_runs_immediately_and_keeps_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first pass must not wait a whole interval, or an upgrade holds expired clips for an hour."""
    passes: list[int] = []
    real = cleanup.LocalRetention.run_once

    def counted(self: Any, conn: Any, config: Any) -> Any:
        passes.append(1)
        return real(self, conn, config)

    monkeypatch.setattr(cleanup.LocalRetention, "run_once", counted)
    service = service_for(env, run_interval_s=0.05, scheduler_tick_s=0.01)
    stop = threading.Event()
    thread = threading.Thread(target=service.run, args=(stop,), daemon=True)

    thread.start()
    deadline = time.monotonic() + 3.0
    while len(passes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=3.0)

    assert len(passes) >= 2, "the loop must keep passing on its cadence"
    assert not thread.is_alive(), "the loop must exit promptly on stop"


def test_a_dry_run_service_deletes_nothing(env: Env) -> None:
    clip_id, target, _ = add_clip(env, review_id="rev-1", state="UPLOADED", days_old=200)

    run = service_for(env, dry_run=True).run_once()

    assert run.dry_run is True
    assert run.would_delete == 1
    assert target.exists()
    assert state_of(env, clip_id) == "UPLOADED"


# ---------------------------------------------------------------------------------------
# Settings for the service
# ---------------------------------------------------------------------------------------


def test_the_service_settings_defaults() -> None:
    settings = cleanup.CleanupSettings.from_environ({})

    assert settings.health_port == cleanup.DEFAULT_HEALTH_PORT == 8083
    assert settings.health_host == "0.0.0.0"
    assert settings.clips_overdue_warn == 1
    assert settings.clips_overdue_critical == 10
    assert settings.disk_warn_pct == 15.0
    assert settings.disk_critical_pct == 5.0
    assert settings.configuration_problem() is None


def test_the_service_settings_read_their_variables() -> None:
    settings = cleanup.CleanupSettings.from_environ(
        {
            "CCTV_CLEANUP_HEALTH_PORT": "9099",
            "CCTV_HEALTH_HOST": "127.0.0.1",
            "CCTV_CLEANUP_OVERDUE_WARN": "2",
            "CCTV_CLEANUP_OVERDUE_CRITICAL": "50",
            "CCTV_HEALTH_DISK_WARN_PCT": "20",
            "CCTV_HEALTH_DISK_CRITICAL_PCT": "10",
        }
    )

    assert settings.health_port == 9099
    assert settings.health_host == "127.0.0.1"
    assert settings.clips_overdue_warn == 2
    assert settings.clips_overdue_critical == 50
    assert settings.disk_warn_pct == 20.0
    assert settings.disk_critical_pct == 10.0
    assert settings.configuration_problem() is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"health_port": -1}, "CCTV_CLEANUP_HEALTH_PORT"),
        (
            {"clips_overdue_warn": 10, "clips_overdue_critical": 5},
            "CCTV_CLEANUP_OVERDUE_CRITICAL",
        ),
        (
            {"disk_warn_pct": 5.0, "disk_critical_pct": 5.0},
            "CCTV_HEALTH_DISK_CRITICAL_PCT",
        ),
    ],
)
def test_a_bad_service_setting_names_itself(
    tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    problem = make_settings(tmp_path, **overrides).configuration_problem()

    assert problem is not None and expected in problem


# ---------------------------------------------------------------------------------------
# main — exit codes are the operator's first clue (service-contracts §7)
# ---------------------------------------------------------------------------------------


def test_main_refuses_a_missing_cameras_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CCTV_CAMERAS", str(tmp_path / "absent.yml"))
    monkeypatch.setenv("CCTV_DB", str(tmp_path / "cctv.db"))
    monkeypatch.setenv("CCTV_CLEANUP_HEALTH_PORT", "0")

    assert cleanup.main([]) == cleanup.EXIT_CONFIG


def test_main_refuses_an_unusable_state_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_config(tmp_path, {CAMERA: {}})
    monkeypatch.setenv("CCTV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CCTV_CAMERAS", str(tmp_path / "cameras.yml"))
    # A directory where a database file should be: opening it cannot succeed.
    (tmp_path / "not-a-db").mkdir()
    monkeypatch.setenv("CCTV_DB", str(tmp_path / "not-a-db"))
    monkeypatch.setenv("CCTV_CLEANUP_HEALTH_PORT", "0")

    assert cleanup.main([]) == cleanup.EXIT_UNAVAILABLE
