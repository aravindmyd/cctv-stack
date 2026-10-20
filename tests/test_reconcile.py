"""Tests for J3, reconciliation.

The job's value is that it tells the truth about drift and then *stops*. So these tests assert
both halves: each of the six findings is produced by the condition it names, and nothing is ever
deleted, re-uploaded or repaired — not even when the finding is a critical one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from cctv import config, db, paths, timeutil
from cctv.cleanup import CleanupHealth, CleanupService, CleanupSettings
from cctv.reconcile import CRITICAL_KINDS, Reconciler
from cctv.storage import DeleteResult, ObjectInfo, PutResult

NOW = 1_790_000_000.0
DAY = 86400.0
CAMERA = "front-door"
LOCATION = "parents-home"

CAMERAS = f"""\
site:
  location: {LOCATION}
  timezone: UTC
cameras:
  {CAMERA}:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
"""


class FakeBucket:
    """A listing, and a `HEAD` per object. Deliberately minimal."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.heads: list[str] = []
        self.deleted: list[str] = []
        self.uploaded: list[str] = []

    def add(self, key: str, *, body: bytes = b"object", sha256: str | None = None) -> None:
        self.objects[key] = {
            "size": len(body),
            "metadata": {"sha256": sha256 or hashlib.sha256(body).hexdigest()},
        }

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Any:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield ObjectInfo(key=key, size=int(self.objects[key]["size"]))

    def head_object(self, *, key: str) -> ObjectInfo | None:
        self.heads.append(key)
        entry = self.objects.get(key)
        if entry is None:
            return None
        return ObjectInfo(key=key, size=int(entry["size"]), metadata=dict(entry["metadata"]))

    # The rest of the protocol must never be called by reconciliation.
    def put_object(self, **kwargs: Any) -> PutResult:  # pragma: no cover - asserted below
        self.uploaded.append(str(kwargs.get("key")))
        raise AssertionError("reconciliation must never upload")

    def delete_object(self, *, key: str) -> DeleteResult:  # pragma: no cover - asserted below
        self.deleted.append(key)
        raise AssertionError("reconciliation must never delete")

    def presign_get(self, *, key: str, ttl_s: int) -> str:  # pragma: no cover - unused
        return key

    def healthcheck(self) -> Any:  # pragma: no cover - unused
        raise AssertionError("reconciliation must never probe health")


class Env:
    def __init__(self, root: Path, *, adapter: FakeBucket | None = None) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True)
        self.db_path = self.data_dir / "cctv.db"
        self.cameras_path = root / "cameras.yml"
        self.cameras_path.write_text(CAMERAS, encoding="utf-8")
        self.config = config.load_cameras_config(self.cameras_path, environ={})
        self.bucket = FakeBucket() if adapter is None else adapter
        self.now = NOW
        conn = db.open_db(self.db_path)
        db.migrate(conn)
        db.ensure_camera(conn, name=CAMERA)
        conn.close()

    def clock(self) -> float:
        return self.now

    def add_clip(
        self,
        *,
        review_id: str,
        state: str = "UPLOADED",
        cloud_key: str | None = "auto",
        body: bytes = b"a plausible clip",
        write_file: bool = True,
        sha256: str | None = None,
        size_bytes: int | None = None,
    ) -> tuple[int, str]:
        relpath = f"clips/{CAMERA}/2026/01/01/00-00-00-{review_id}.mp4"
        target = self.data_dir / relpath
        if write_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        key = paths.cloud_key_for_clip_path(LOCATION, relpath) if cloud_key == "auto" else cloud_key
        digest = sha256 if sha256 is not None else hashlib.sha256(body).hexdigest()
        conn = db.open_db(self.db_path)
        try:
            clip_id, _ = db.insert_clip(
                conn,
                db.ClipRecord(
                    review_id=review_id,
                    camera=CAMERA,
                    severity="alert",
                    start_time=NOW - 10 * DAY,
                    end_time=NOW - 10 * DAY + 20,
                    path=relpath,
                ),
            )
            conn.execute(
                "UPDATE clips SET upload_state = ?, cloud_key = ?, sha256 = ?, size_bytes = ?, "
                "uploaded_at = ? WHERE id = ?",
                (
                    state,
                    key,
                    digest,
                    len(body) if size_bytes is None else size_bytes,
                    NOW - 10 * DAY,
                    clip_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return clip_id, key

    def findings(self, kind: str | None = None) -> list[Any]:
        conn = db.open_db(self.db_path, readonly=True)
        try:
            if kind is None:
                return list(conn.execute("SELECT * FROM reconciliation_findings"))
            return list(
                conn.execute("SELECT * FROM reconciliation_findings WHERE kind = ?", (kind,))
            )
        finally:
            conn.close()

    def run(self, **kwargs: Any) -> Any:
        reconciler = Reconciler(
            data_dir=self.data_dir,
            location=LOCATION,
            adapter=self.bucket,
            logger=None,
            clock=self.clock,
            **kwargs,
        )
        conn = db.open_db(self.db_path)
        try:
            return reconciler.run_once(conn, self.config)
        finally:
            conn.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(timeutil, "now", lambda: NOW)
    return Env(tmp_path)


# --- agreement --------------------------------------------------------------------------


def test_a_matching_bucket_and_database_produce_nothing(env: Env) -> None:
    _, key = env.add_clip(review_id="a", body=b"hello")
    env.bucket.add(key, body=b"hello")

    run = env.run()

    assert run.total_findings == 0
    assert env.findings() == []
    assert run.rows_checked == 1
    assert run.objects_listed == 1


# --- the six findings -------------------------------------------------------------------


def test_a_row_with_no_object_is_dangling(env: Env) -> None:
    env.add_clip(review_id="b")

    run = env.run()

    assert run.findings == {"dangling_row": 1}
    assert run.critical_findings == 1
    assert env.findings("dangling_row")


def test_an_object_with_no_row_is_an_orphan(env: Env) -> None:
    env.bucket.add(f"{LOCATION}/clips/{CAMERA}/2026/01/01/orphan.mp4")

    run = env.run()

    assert run.findings == {"orphan_object": 1}
    assert run.critical_findings == 0


def test_a_different_checksum_is_reported(env: Env) -> None:
    _, key = env.add_clip(review_id="c", body=b"the real bytes")
    env.bucket.add(key, body=b"the real bytes", sha256="0" * 64)

    run = env.run()

    assert run.findings == {"checksum_mismatch": 1}
    detail = env.findings("checksum_mismatch")[0]["detail"]
    assert "sha256" in detail


def test_a_size_difference_is_a_mismatch_without_a_head(env: Env) -> None:
    _, key = env.add_clip(review_id="d", body=b"short")
    env.bucket.add(key, body=b"a much longer body than the row records")

    run = env.run()

    assert run.findings == {"checksum_mismatch": 1}
    assert env.bucket.heads == [], "a size difference is caught from the listing alone"


def test_a_key_that_does_not_match_its_path_is_reported(env: Env) -> None:
    _, key = env.add_clip(review_id="e", cloud_key=f"{LOCATION}/clips/wrong/place.mp4")
    env.bucket.add(key, body=b"a plausible clip")  # right bytes, wrong key

    run = env.run()

    assert run.findings == {"key_mismatch": 1}
    assert run.critical_findings == 1


def test_a_file_with_no_row_is_unexpected(env: Env) -> None:
    stray = env.data_dir / "clips" / CAMERA / "2026" / "01" / "01" / "stray.mp4"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"nobody's clip")

    run = env.run()

    assert run.findings == {"unexpected_local_file": 1}
    assert run.local_files_scanned == 1


def test_a_row_with_no_file_is_reported(env: Env) -> None:
    env.add_clip(review_id="f", write_file=False)

    run = env.run()

    assert run.findings["missing_local_file"] == 1


# --- what it must never do --------------------------------------------------------------


def test_it_never_deletes_or_uploads(env: Env) -> None:
    """INV-10: findings are not repairs. Every branch here would be a repair."""
    env.add_clip(review_id="g")  # dangling
    env.bucket.add(f"{LOCATION}/clips/{CAMERA}/2026/01/01/orphan.mp4")  # orphan
    stray = env.data_dir / "clips" / CAMERA / "2026" / "01" / "01" / "stray.mp4"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")

    env.run()

    assert env.bucket.deleted == []
    assert env.bucket.uploaded == []
    assert stray.exists()


def test_a_dry_run_records_nothing_but_counts(env: Env) -> None:
    env.add_clip(review_id="h")

    reconciler = Reconciler(
        data_dir=env.data_dir, location=LOCATION, adapter=env.bucket, clock=env.clock
    )
    conn = db.open_db(env.db_path)
    try:
        run = reconciler.run_once(conn, env.config, dry_run=True)
    finally:
        conn.close()

    assert run.findings == {"dangling_row": 1}
    assert env.findings() == []


# --- what is not drift -------------------------------------------------------------------


def test_an_expired_object_is_not_a_dangling_row(env: Env) -> None:
    """`EXPIRED_CLOUD` means the object is gone by policy; that is the desired state."""
    env.add_clip(review_id="i", state="EXPIRED_CLOUD")

    run = env.run()

    assert run.total_findings == 0


def test_a_purged_clip_is_ignored_even_with_a_key(env: Env) -> None:
    env.add_clip(review_id="j", state="PURGED")

    run = env.run()

    assert run.total_findings == 0


def test_a_locally_deleted_clip_still_expects_its_object(env: Env) -> None:
    env.add_clip(review_id="k", state="DELETED_LOCAL", write_file=False)
    conn = db.open_db(env.db_path)
    conn.execute("UPDATE clips SET local_deleted_at = ? WHERE review_id = 'k'", (NOW,))
    conn.commit()
    conn.close()

    run = env.run()

    assert run.findings == {"dangling_row": 1}
    assert env.findings("missing_local_file") == []


# --- repeat runs -------------------------------------------------------------------------


def test_the_same_drift_is_not_recorded_twice(env: Env) -> None:
    env.add_clip(review_id="l")

    first = env.run()
    second = env.run()

    assert first.findings == {"dangling_row": 1}
    assert second.findings == {}, "the finding is already open"
    assert len(env.findings("dangling_row")) == 1


def test_the_cap_and_truncation_are_reported(env: Env) -> None:
    for index in range(3):
        env.bucket.add(f"{LOCATION}/clips/{CAMERA}/2026/01/01/orphan-{index}.mp4")

    run = env.run(max_objects=1)

    assert run.truncated is True
    assert run.objects_listed == 1


# --- without a bucket --------------------------------------------------------------------


def test_a_local_only_site_still_reconciles_local_drift() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        reconciler = Reconciler(
            data_dir=env.data_dir, location=LOCATION, adapter=None, clock=env.clock
        )
        env.add_clip(review_id="m", write_file=False)
        conn = db.open_db(env.db_path)
        try:
            run = reconciler.run_once(conn, env.config)
        finally:
            conn.close()

    assert run.skipped_cloud is True
    assert run.findings == {"missing_local_file": 1}


# --- the service and its health ----------------------------------------------------------


def _settings(env: Env, **overrides: Any) -> CleanupSettings:
    base = CleanupSettings(
        data_dir=env.data_dir,
        cameras_path=env.cameras_path,
        db_path=env.db_path,
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
    from dataclasses import replace

    return replace(base, **overrides) if overrides else base


def _check(env: Env, settings: CleanupSettings) -> Any:
    reporter = CleanupHealth(settings=settings, config=env.config, clock=env.clock)
    return {check.name: check for check in reporter.report().checks}["reconciliation"]


def test_the_service_reconciles_on_the_first_cycle(env: Env) -> None:
    env.add_clip(review_id="n")
    service = CleanupService(
        settings=_settings(env), config=env.config, adapter=env.bucket, clock=env.clock
    )

    run = service.run_once()

    assert run.reconcile is not None
    assert run.reconcile.findings == {"dangling_row": 1}


def test_reconciliation_is_not_due_again_the_same_day(env: Env) -> None:
    env.add_clip(review_id="o")
    service = CleanupService(
        settings=_settings(env), config=env.config, adapter=env.bucket, clock=env.clock
    )
    service.run_once()

    second = service.run_once()

    assert second.reconcile is None, "the watermark says a pass already ran"


def test_it_becomes_due_after_the_interval(env: Env) -> None:
    service = CleanupService(
        settings=_settings(env, reconcile_interval_s=DAY),
        config=env.config,
        adapter=env.bucket,
        clock=env.clock,
    )
    service.run_once()
    env.now += DAY + 60

    assert service.run_once().reconcile is not None


def test_zero_disables_reconciliation(env: Env) -> None:
    service = CleanupService(
        settings=_settings(env, reconcile_interval_s=0),
        config=env.config,
        adapter=env.bucket,
        clock=env.clock,
    )

    assert service.run_once().reconcile is None


def test_health_reports_disabled_when_it_is_off(env: Env) -> None:
    check = _check(env, _settings(env, reconcile_interval_s=0))
    assert check.state == "DISABLED"


def test_health_is_unknown_before_the_first_pass(env: Env) -> None:
    check = _check(env, _settings(env))
    assert check.state == "UNKNOWN"
    assert check.code == "RECONCILE_STALE"


def test_health_goes_unhealthy_on_a_critical_finding(env: Env) -> None:
    env.add_clip(review_id="p")
    service = CleanupService(
        settings=_settings(env), config=env.config, adapter=env.bucket, clock=env.clock
    )
    service.run_once()

    check = _check(env, _settings(env))
    assert check.state == "UNHEALTHY"
    assert check.code == "RECONCILE_FINDINGS"


def test_health_goes_stale_when_the_pass_stops_running(env: Env) -> None:
    service = CleanupService(
        settings=_settings(env), config=env.config, adapter=env.bucket, clock=env.clock
    )
    service.run_once()
    env.now += 3 * DAY

    check = _check(env, _settings(env))
    assert check.state == "DEGRADED"
    assert check.code == "RECONCILE_STALE"


def test_the_critical_kinds_are_the_ones_that_mean_we_are_wrong() -> None:
    assert {"dangling_row", "checksum_mismatch", "key_mismatch"} == CRITICAL_KINDS
