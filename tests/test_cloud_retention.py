"""Tests for J2, cloud retention.

What is asserted here is what stops an object being deleted for the wrong reason or kept for
one: the cutoff is `uploaded_at` (never `start_time`), a clip that never uploaded is never
considered, and a `DELETE` the provider accepts is not a deletion until a read-back says the
object is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cctv import config, db, health, timeutil
from cctv.cleanup import (
    CleanupHealth,
    CleanupService,
    CleanupSettings,
    CloudRetention,
)
from cctv.storage import (
    AdapterHealth,
    DeleteResult,
    ObjectInfo,
    PutResult,
    TransientStorageError,
)

NOW = 1_790_000_000.0
DAY = 86400.0
CAMERA = "front-door"
LOCATION = "parents-home"

CAMERAS = f"""\
site:
  location: {LOCATION}
  timezone: UTC
defaults:
  retention:
    local_days: 90
    cloud_days: 60
cameras:
  {CAMERA}:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
"""


class FakeAdapter:
    """Records deletes and decides how the provider answers."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.leave_object = False
        self.fail_with: Exception | None = None

    def delete_object(self, *, key: str) -> DeleteResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append(key)
        if self.leave_object:
            return DeleteResult(key=key, deleted=False, detail="hidden, still billed")
        return DeleteResult(key=key, deleted=True)

    # The rest of the protocol is unused by retention.
    def put_object(self, **kwargs: Any) -> PutResult:  # pragma: no cover - unused
        raise AssertionError("retention must never upload")

    def head_object(self, *, key: str) -> ObjectInfo | None:  # pragma: no cover - unused
        return None

    def list_objects(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        return iter(())

    def presign_get(self, *, key: str, ttl_s: int) -> str:  # pragma: no cover - unused
        return key

    def healthcheck(self) -> AdapterHealth:  # pragma: no cover - unused
        return AdapterHealth(True, True, True, "ok")


class Env:
    def __init__(self, root: Path, *, adapter: FakeAdapter | None = None) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True)
        self.db_path = self.data_dir / "cctv.db"
        self.cameras_path = root / "cameras.yml"
        self.cameras_path.write_text(CAMERAS, encoding="utf-8")
        self.config = config.load_cameras_config(self.cameras_path, environ={})
        self.adapter = FakeAdapter() if adapter is None else adapter
        self.clock_value = NOW
        self.settings = CleanupSettings(
            data_dir=self.data_dir,
            cameras_path=self.cameras_path,
            db_path=self.db_path,
            run_interval_s=3600.0,
            batch_size=50,
            dry_run=False,
            retire_grace_days=0,
            health_host="127.0.0.1",
            health_port=0,
            scheduler_tick_s=1.0,
            clips_overdue_warn=1,
            clips_overdue_critical=10,
            overdue_age_critical_days=30,
            disk_warn_pct=15.0,
            disk_critical_pct=5.0,
            log_level=None,
        )
        conn = db.open_db(self.db_path)
        db.migrate(conn)
        db.ensure_camera(conn, name=CAMERA)
        conn.close()

    def clock(self) -> float:
        return self.clock_value

    # -- clips -------------------------------------------------------------------------

    def add_clip(
        self,
        *,
        review_id: str,
        uploaded_days_ago: float | None = 90.0,
        state: str = "UPLOADED",
        cloud_key: str | None = "auto",
        start_days_ago: float | None = None,
    ) -> int:
        conn = db.open_db(self.db_path)
        try:
            clip_id, _ = db.insert_clip(
                conn,
                db.ClipRecord(
                    review_id=review_id,
                    camera=CAMERA,
                    severity="alert",
                    start_time=NOW - (start_days_ago if start_days_ago is not None else 90) * DAY,
                    end_time=NOW
                    - (start_days_ago if start_days_ago is not None else 90) * DAY
                    + 20,
                    path=f"clips/{CAMERA}/2026/01/01/00-00-00-{review_id}.mp4",
                ),
            )
            key = (
                f"{LOCATION}/clips/{CAMERA}/2026/01/01/00-00-00-{review_id}.mp4"
                if cloud_key == "auto"
                else cloud_key
            )
            uploaded_at = None if uploaded_days_ago is None else NOW - uploaded_days_ago * DAY
            conn.execute(
                "UPDATE clips SET upload_state = ?, cloud_key = ?, uploaded_at = ? WHERE id = ?",
                (state, key, uploaded_at, clip_id),
            )
            conn.commit()
        finally:
            conn.close()
        return clip_id

    def row(self, clip_id: int) -> Any:
        conn = db.open_db(self.db_path, readonly=True)
        try:
            return conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        finally:
            conn.close()

    def findings(self) -> list[Any]:
        conn = db.open_db(self.db_path, readonly=True)
        try:
            return list(conn.execute("SELECT * FROM reconciliation_findings"))
        finally:
            conn.close()

    def retention(self, **overrides: Any) -> CloudRetention:
        settings = self.settings
        if overrides:
            from dataclasses import replace

            settings = replace(settings, **overrides)
        return CloudRetention(
            settings=settings,
            adapter=self.adapter,
            location=self.config.site.location,
            clock=self.clock,
        )

    def run(self, **overrides: Any) -> Any:
        conn = db.open_db(self.db_path)
        try:
            return self.retention(**overrides).run_once(conn, self.config)
        finally:
            conn.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(timeutil, "now", lambda: NOW)
    return Env(tmp_path)


# --- the happy path --------------------------------------------------------------------


def test_an_uploaded_clip_past_cloud_days_is_expired(env: Env) -> None:
    clip_id = env.add_clip(review_id="a", uploaded_days_ago=90)

    run = env.run()

    assert run.deleted == 1
    assert env.adapter.deleted and env.adapter.deleted[0].startswith(f"{LOCATION}/clips/")
    row = env.row(clip_id)
    assert row["upload_state"] == "EXPIRED_CLOUD"
    assert row["cloud_deleted_at"] == NOW


def test_a_recent_upload_is_left_alone(env: Env) -> None:
    clip_id = env.add_clip(review_id="b", uploaded_days_ago=10)

    run = env.run()

    assert run.deleted == 0
    assert env.adapter.deleted == []
    assert env.row(clip_id)["upload_state"] == "UPLOADED"


def test_nothing_is_considered_twice(env: Env) -> None:
    env.add_clip(review_id="c", uploaded_days_ago=90)

    assert env.run().deleted == 1
    second = env.run()

    assert second.considered == 0
    assert second.deleted == 0


# --- the cutoff is uploaded_at ----------------------------------------------------------


def test_an_old_recording_uploaded_recently_keeps_its_full_life(env: Env) -> None:
    """`lld/05` §4.3: a worker that was down for months then caught up must not lose it all."""
    clip_id = env.add_clip(review_id="d", uploaded_days_ago=1, start_days_ago=200)

    run = env.run()

    assert run.deleted == 0
    assert env.row(clip_id)["upload_state"] == "UPLOADED"


def test_a_clip_that_never_uploaded_is_never_considered(env: Env) -> None:
    pending = env.add_clip(review_id="e", state="PENDING", uploaded_days_ago=None)
    failed = env.add_clip(review_id="f", state="FAILED_PERMANENT", uploaded_days_ago=None)

    run = env.run()

    assert run.considered == 0
    assert env.adapter.deleted == []
    assert env.row(pending)["upload_state"] == "PENDING"
    assert env.row(failed)["upload_state"] == "FAILED_PERMANENT"


# --- honesty about the provider ---------------------------------------------------------


def test_a_delete_that_only_hides_is_a_failure_not_a_deletion(env: Env) -> None:
    """The bytes are still billed, so the clip stays UPLOADED and the finding says so."""
    env.adapter.leave_object = True
    clip_id = env.add_clip(review_id="g", uploaded_days_ago=90)

    run = env.run()

    assert run.deleted == 0
    assert run.failed == 1
    assert env.row(clip_id)["upload_state"] == "UPLOADED"
    findings = env.findings()
    assert [f["kind"] for f in findings] == ["cloud_delete_failed"]
    assert "hidden" in findings[0]["detail"]


def test_a_storage_error_keeps_the_clip_and_records_a_finding(env: Env) -> None:
    env.adapter.fail_with = TransientStorageError("connection reset")
    clip_id = env.add_clip(review_id="h", uploaded_days_ago=90)

    run = env.run()

    assert run.failed == 1
    assert env.row(clip_id)["upload_state"] == "UPLOADED"
    assert "connection reset" in env.findings()[0]["detail"]


def test_consecutive_failures_count_up_and_reset_on_success(env: Env) -> None:
    retention = env.retention()
    conn = db.open_db(env.db_path)
    try:
        env.adapter.fail_with = TransientStorageError("nope")
        env.add_clip(review_id="i", uploaded_days_ago=90)
        retention.run_once(conn, env.config)
        assert retention.consecutive_failures == 1

        env.adapter.fail_with = None
        retention.run_once(conn, env.config)
        assert retention.consecutive_failures == 0
    finally:
        conn.close()


# --- dry run ----------------------------------------------------------------------------


def test_a_dry_run_deletes_nothing_and_counts_what_it_would(env: Env) -> None:
    clip_id = env.add_clip(review_id="j", uploaded_days_ago=90)

    run = env.run(dry_run=True)

    assert run.would_delete == 1
    assert env.adapter.deleted == []
    assert env.row(clip_id)["upload_state"] == "UPLOADED"


# --- config -----------------------------------------------------------------------------


def test_a_retired_camera_falls_back_to_the_default_window(env: Env) -> None:
    """Footage does not become immortal because its camera left `cameras.yml` (INV-13)."""
    conn = db.open_db(env.db_path)
    db.ensure_camera(conn, name="old-camera")
    conn.execute("UPDATE cameras SET retired_at = ? WHERE name = 'old-camera'", (NOW,))
    conn.commit()
    conn.close()

    conn = db.open_db(env.db_path)
    try:
        clip_id, _ = db.insert_clip(
            conn,
            db.ClipRecord(
                review_id="k",
                camera="old-camera",
                severity="alert",
                start_time=NOW - 200 * DAY,
                end_time=NOW - 200 * DAY + 20,
                path="clips/old-camera/2025/01/01/00-00-00.mp4",
            ),
        )
        conn.execute(
            "UPDATE clips SET upload_state = 'UPLOADED', cloud_key = ?, uploaded_at = ? "
            "WHERE id = ?",
            (f"{LOCATION}/clips/old-camera/x.mp4", NOW - 61 * DAY, clip_id),
        )
        conn.commit()
        run = env.retention().run_once(conn, env.config)
    finally:
        conn.close()

    assert run.deleted == 1


def test_a_clip_without_a_recorded_key_derives_one(env: Env) -> None:
    """[INV-5] makes the key a function of the path, so a missing one is survivable."""
    clip_id = env.add_clip(review_id="l", uploaded_days_ago=90, cloud_key=None)

    env.run()

    assert env.row(clip_id)["upload_state"] == "EXPIRED_CLOUD"
    assert env.adapter.deleted[0].startswith(f"{LOCATION}/clips/")


# --- health -----------------------------------------------------------------------------


def _health(env: Env, retention: CloudRetention) -> Any:
    reporter = CleanupHealth(
        settings=env.settings, config=env.config, cloud=retention, clock=env.clock
    )
    return {check.name: check for check in reporter.report().checks}


def test_health_is_healthy_while_deletions_work(env: Env) -> None:
    retention = env.retention()
    env.add_clip(review_id="m", uploaded_days_ago=90)
    conn = db.open_db(env.db_path)
    retention.run_once(conn, env.config)
    conn.close()

    assert _health(env, retention)["cloud_retention"].state == health.HEALTHY


def test_health_degrades_then_fails_on_failed_deletions(env: Env) -> None:
    retention = env.retention()
    retention.consecutive_failures = 1
    check = _health(env, retention)["cloud_retention"]
    assert check.state == health.DEGRADED
    assert check.code == "CLOUD_DELETE_FAILING"

    retention.consecutive_failures = env.settings.cloud_delete_failure_critical
    assert _health(env, retention)["cloud_retention"].state == health.UNHEALTHY


# --- the service ------------------------------------------------------------------------


def test_one_cycle_finishes_both_copies(env: Env) -> None:
    """ADR-0023: J1 takes the file, J2 takes the object, and the row ends `PURGED`.

    This is the case the old state machine could not express — one copy gone by the time the
    other pass runs.
    """
    clip_id = env.add_clip(review_id="n", uploaded_days_ago=90)

    run = CleanupService(settings=env.settings, config=env.config, adapter=env.adapter).run_once()

    assert run.cloud is not None
    assert run.cloud.deleted == 1
    assert env.row(clip_id)["upload_state"] == "PURGED"


def test_the_service_without_a_bucket_reports_cloud_disabled(env: Env) -> None:
    service = CleanupService(settings=env.settings, config=env.config, adapter=None)
    run = service.run_once()
    assert run.cloud is not None and run.cloud.disabled is True
