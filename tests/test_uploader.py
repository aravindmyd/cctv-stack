"""Tests for the uploader service.

The behaviours pinned here are the ones whose absence would only be discovered by
discovering missing footage: the claim is atomic and oldest-first, a crash between the PUT
and the state update converges to one object, a failure is classified into the *right* retry
decision, a file is never touched, and an unverified backup is reported as unverified.

The storage adapter is a fake throughout. That is deliberate: `boto3` is not installed in
this environment, and the interesting logic is not the SDK — it is what we do with what the
SDK tells us.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctv import db, health, storage, uploader

NOW = 1_700_000_000.0
LOCATION = "parents-home"
CAMERA = "front-door"
REL = f"clips/{CAMERA}/2026/09/12/18-45-00.mp4"


# ---------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self, value: float = NOW) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeAdapter:
    """An S3-shaped object store with no provider and no network."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        verified_by: str = "sha256",
        health: storage.AdapterHealth | None = None,
    ) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.puts: list[str] = []
        self.metadata: dict[str, dict[str, str]] = {}
        self.error = error
        self.verified_by = verified_by
        self._health = health or storage.AdapterHealth(True, True, True, "ok", latency_ms=1)
        self._lock = threading.Lock()

    def put_object(
        self, *, key: str, path: Path, metadata: dict[str, str], checksum_sha256: str
    ) -> storage.PutResult:
        if self.error is not None:
            raise self.error
        body = Path(path).read_bytes()
        with self._lock:
            if key in self.objects:
                if self.objects[key]["sha256"] == checksum_sha256:
                    return storage.PutResult(
                        key=key,
                        size=len(body),
                        verified_by="sha256",
                        already_present=True,
                    )
                raise storage.ObjectConflictError(f"{key} already exists with other content")
            self.objects[key] = {"sha256": checksum_sha256, "body": body}
            self.puts.append(key)
            self.metadata[key] = dict(metadata)
        return storage.PutResult(
            key=key, size=len(body), verified_by=self.verified_by, etag='"etag"'
        )

    def head_object(self, *, key: str) -> storage.ObjectInfo | None:
        stored = self.objects.get(key)
        if stored is None:
            return None
        return storage.ObjectInfo(
            key=key, size=len(stored["body"]), metadata={"sha256": stored["sha256"]}
        )

    def delete_object(self, *, key: str) -> storage.DeleteResult:
        self.objects.pop(key, None)
        return storage.DeleteResult(key=key, deleted=True)

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Any:
        del max_keys
        return iter(
            storage.ObjectInfo(key=key, size=len(value["body"]))
            for key, value in self.objects.items()
            if key.startswith(prefix)
        )

    def presign_get(self, *, key: str, ttl_s: int) -> str:
        del ttl_s
        return f"https://example.invalid/{key}"

    def healthcheck(self) -> storage.AdapterHealth:
        return self._health


def make_settings(**overrides: Any) -> uploader.UploadSettings:
    base: dict[str, Any] = {
        "cameras_path": Path("/config/cameras.yml"),
        "data_dir": Path("/data"),
        "db_path": Path("/data/cctv.db"),
        "bucket": "cctv-parents-home",
        "endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "region": "us-west-004",
        "key_id": "key-id",
        "application_key": "app-key",
        "health_host": "127.0.0.1",
        "health_port": 0,
        "internal_token": None,
        "dry_run": False,
        "scheduler_tick_s": 1.0,
        "lease_seconds": 900.0,
        "max_attempts": db.MAX_ATTEMPTS,
        "backoff_base_s": 15.0,
        "backoff_max_s": 3600.0,
        "quota_backoff_max_s": 21600.0,
        "upload_concurrency": 1,
        "multipart_threshold_bytes": storage.DEFAULT_MULTIPART_THRESHOLD_BYTES,
        "stale_upload_warn_s": 1800.0,
        "backlog_warn_s": 1800.0,
        "backlog_critical_s": 21600.0,
        "log_level": None,
    }
    base.update(overrides)
    return uploader.UploadSettings(**base)


@pytest.fixture
def env(tmp_path: Path) -> Any:
    clock = Clock()
    settings = make_settings(data_dir=tmp_path, db_path=tmp_path / "cctv.db")
    conn = db.open_db(settings.db_path)
    db.migrate(conn)
    db.ensure_camera(conn, name=CAMERA, display_name="Front Door", now=NOW)
    adapter = FakeAdapter()
    instance = uploader.Uploader(
        data_dir=tmp_path,
        location=LOCATION,
        adapter=adapter,
        settings=settings,
        clock=clock,
        rng=random.Random(1234),
    )
    yield SimpleNamespace(
        conn=conn,
        settings=settings,
        adapter=adapter,
        uploader=instance,
        clock=clock,
        data_dir=tmp_path,
    )
    conn.close()


def add_clip(
    env: Any,
    *,
    review_id: str = "rev-1",
    camera: str = CAMERA,
    start: float = NOW,
    body: bytes = b"a plausible clip",
    relpath: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    materialised_at: float | None = NOW,
    severity: str = "alert",
    label: str | None = "person",
    zones: tuple[str, ...] = ("driveway",),
    event_ids: tuple[str, ...] = ("evt-1",),
) -> tuple[int, Path]:
    relative = relpath or f"clips/{camera}/2026/09/12/18-45-00.mp4"
    target = env.data_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest() if sha256 is None else sha256
    clip_id, _ = db.insert_clip(
        env.conn,
        db.ClipRecord(
            review_id=review_id,
            camera=camera,
            severity=severity,
            start_time=start,
            end_time=start + 30,
            path=relative,
            event_ids=event_ids,
            zones=zones,
            label=label,
            duration_s=30.0,
            sha256=digest,
            size_bytes=len(body) if size_bytes is None else size_bytes,
            materialised_at=materialised_at,
        ),
        now=NOW,
    )
    return clip_id, target


def claim(env: Any) -> uploader.UploadOutcome | None:
    return env.uploader.upload_one(env.conn, owner="test:1")


def state_of(env: Any, clip_id: int) -> str:
    row = env.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return str(row["upload_state"])


def row_of(env: Any, clip_id: int) -> sqlite3.Row:
    return env.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()


def attempts(env: Any, clip_id: int) -> list[sqlite3.Row]:
    return env.conn.execute(
        "SELECT * FROM upload_attempts WHERE clip_id = ? ORDER BY attempt_no",
        (clip_id,),
    ).fetchall()


def events(env: Any, code: str) -> list[sqlite3.Row]:
    return env.conn.execute("SELECT * FROM system_events WHERE code = ?", (code,)).fetchall()


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------


def test_settings_defaults() -> None:
    settings = uploader.UploadSettings.from_environ({})
    assert settings.bucket is None
    assert settings.storage_configured is False
    assert settings.health_port == uploader.DEFAULT_HEALTH_PORT
    assert settings.upload_concurrency == uploader.DEFAULT_UPLOAD_CONCURRENCY
    assert settings.endpoint_url == "https://s3.us-west-004.backblazeb2.com"


def test_settings_accept_the_b2_variable_names_the_prd_uses() -> None:
    settings = uploader.UploadSettings.from_environ(
        {
            "B2_BUCKET": "cctv-parents-home",
            "B2_KEY_ID": "id",
            "B2_APPLICATION_KEY": "secret",
            "B2_REGION": "eu-central-003",
        }
    )
    assert settings.bucket == "cctv-parents-home"
    assert settings.storage_configured is True
    assert settings.endpoint_url == "https://s3.eu-central-003.backblazeb2.com"


def test_an_unconfigured_bucket_is_not_a_problem() -> None:
    """A local-only install is a supported choice, not a misconfiguration."""
    assert uploader.UploadSettings.from_environ({}).configuration_problem() is None


def test_a_half_configured_bucket_is_refused_with_the_missing_name() -> None:
    settings = make_settings(application_key=None)
    problem = settings.configuration_problem()
    assert problem is not None
    assert "CCTV_STORAGE_APPLICATION_KEY" in problem


def test_a_complete_configuration_has_no_problem() -> None:
    assert make_settings().configuration_problem() is None


def test_zero_concurrency_is_refused() -> None:
    assert make_settings(upload_concurrency=0).configuration_problem() is not None


# ---------------------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    rng = random.Random(7)
    first = uploader.backoff_delay(1, base_s=15.0, max_s=3600.0, rng=rng)
    assert 12.0 <= first <= 18.0
    assert 24.0 <= uploader.backoff_delay(2, base_s=15.0, max_s=3600.0, rng=rng) <= 36.0
    assert uploader.backoff_delay(40, base_s=15.0, max_s=3600.0, rng=rng) <= 4320.0


def test_backoff_never_exceeds_the_ceiling_on_average() -> None:
    rng = random.Random(11)
    for _ in range(200):
        assert uploader.backoff_delay(30, base_s=15.0, max_s=3600.0, rng=rng) <= 3600 * 1.2


def test_backoff_requires_a_one_based_attempt() -> None:
    with pytest.raises(ValueError, match="1-based"):
        uploader.backoff_delay(0, base_s=15.0, max_s=3600.0)


def test_backoff_is_jittered_across_calls() -> None:
    """Without jitter, 200 affected clips retry in lock-step against a struggling endpoint."""
    rng = random.Random(3)
    values = {uploader.backoff_delay(5, base_s=15.0, max_s=3600.0, rng=rng) for _ in range(50)}
    assert len(values) > 40


# ---------------------------------------------------------------------------------------
# Key and metadata construction
# ---------------------------------------------------------------------------------------


def test_a_good_key_passes_through() -> None:
    key = "parents-home/clips/front-door/2026/09/12/18-45-00.mp4"
    assert uploader.validate_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/leading/slash",
        "double//slash",
        "parent/../escape",
        "back\\slash",
        "control\x01char",
        "x" * 1025,
    ],
)
def test_bad_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError):
        uploader.validate_key(key)


def test_object_metadata_carries_the_documented_keys(env: Any) -> None:
    clip_id, _ = add_clip(env)
    row = row_of(env, clip_id)
    metadata = uploader.object_metadata(row, location=LOCATION)

    assert metadata["camera"] == CAMERA
    assert metadata["location"] == LOCATION
    assert metadata["review-id"] == "rev-1"
    assert metadata["event-id"] == "evt-1"
    assert metadata["label"] == "person"
    assert metadata["zones"] == "driveway"
    assert metadata["start-time"].startswith("1700000000")
    assert metadata["cctv-version"]


def test_object_metadata_omits_absent_optionals(env: Any) -> None:
    clip_id, _ = add_clip(env, label=None, zones=(), event_ids=())
    metadata = uploader.object_metadata(row_of(env, clip_id), location=LOCATION)
    assert "label" not in metadata
    assert "zones" not in metadata
    # With no events, the review id is the only identifier there is.
    assert metadata["event-id"] == "rev-1"


# ---------------------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------------------


def test_nothing_pending_means_no_work(env: Any) -> None:
    assert claim(env) is None


def test_a_clip_is_uploaded_once_and_recorded(env: Any) -> None:
    clip_id, target = add_clip(env)
    before = target.read_bytes()

    outcome = claim(env)

    assert outcome is not None and outcome.ok
    assert outcome.status == uploader.UPLOADED
    assert outcome.verified_by == "sha256"
    assert outcome.bytes_sent == len(before)
    assert env.adapter.puts == [f"{LOCATION}/{REL}"]

    row = row_of(env, clip_id)
    assert row["upload_state"] == "UPLOADED"
    assert row["uploaded_at"] == NOW
    assert row["cloud_etag"] is not None
    assert row["cloud_size_bytes"] == len(before)
    assert row["lease_owner"] is None

    recorded = attempts(env, clip_id)
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == "success"
    assert recorded[0]["bytes_sent"] == len(before)

    # The uploader has no code path that writes to the clip (lld/04 §1).
    assert target.read_bytes() == before


def test_the_object_key_is_the_location_plus_the_local_path(env: Any) -> None:
    """[INV-5] key equality makes restore a string operation, not a mapping table."""
    clip_id, _ = add_clip(env)
    claim(env)
    key = env.adapter.puts[0]
    assert key == f"{LOCATION}/{row_of(env, clip_id)['path']}"


def test_object_keys_are_confined_to_the_clip_namespace(env: Any) -> None:
    """Frigate's own recordings can never reach the bucket ([ADR-0022]).

    Two days of continuous footage are kept locally. They are not rows in `clips`, and the
    uploader only ever uploads a row's `path` — every one of which sits under `clips/`.
    There is no code path that enumerates `data/media/`, so continuous footage cannot be
    uploaded, listed, or billed.
    """
    add_clip(env)
    claim(env)
    assert REL.startswith("clips/")
    assert env.adapter.puts[0].startswith(f"{LOCATION}/clips/")


def test_metadata_reaches_the_adapter(env: Any) -> None:
    add_clip(env)
    claim(env)
    metadata = env.adapter.metadata[f"{LOCATION}/{REL}"]
    assert metadata["camera"] == CAMERA
    assert metadata["location"] == LOCATION


def test_the_claim_increments_attempts_and_opens_an_attempt_row(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.TransientStorageError("down")
    claim(env)
    assert row_of(env, clip_id)["attempts"] == 1
    assert len(attempts(env, clip_id)) == 1


def test_a_crash_between_the_put_and_the_record_converges_to_one_object(
    env: Any,
) -> None:
    """[INV-7], the scenario the deterministic key exists for."""
    clip_id, _ = add_clip(env)
    assert claim(env) is not None
    assert env.adapter.puts == [f"{LOCATION}/{REL}"]

    # Emulate the crash: the object is in the bucket, the record never happened.
    with db.write_txn(env.conn):
        env.conn.execute(
            "UPDATE clips SET upload_state = 'PENDING', uploaded_at = NULL, "
            "cloud_etag = NULL WHERE id = ?",
            (clip_id,),
        )

    second = claim(env)

    assert second is not None and second.ok
    assert second.skipped is True, "the second run must not send the bytes again"
    assert env.adapter.puts == [f"{LOCATION}/{REL}"], "exactly one object"
    assert state_of(env, clip_id) == "UPLOADED"


def test_an_unverifiable_upload_is_recorded_as_such(env: Any) -> None:
    env.adapter.verified_by = "none"
    add_clip(env)
    outcome = claim(env)
    assert outcome is not None and outcome.ok
    assert outcome.verified_by == "none"


# ---------------------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------------------


def test_a_transient_failure_schedules_a_retry(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.TransientStorageError("503 from the provider")

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.RETRY
    row = row_of(env, clip_id)
    assert row["upload_state"] == "RETRY_WAIT"
    assert row["last_error_class"] == "transient"
    assert row["next_attempt_at"] > NOW
    assert row["lease_owner"] is None
    assert row["uploaded_at"] is None
    assert attempts(env, clip_id)[0]["outcome"] == "transient_error"


def test_attempts_exhausted_becomes_permanent_with_a_critical_event(env: Any) -> None:
    env.settings = make_settings(
        data_dir=env.data_dir, db_path=env.settings.db_path, max_attempts=1
    )
    env.uploader = uploader.Uploader(
        data_dir=env.data_dir,
        location=LOCATION,
        adapter=env.adapter,
        settings=env.settings,
        clock=env.clock,
    )
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.TransientStorageError("still down")

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.PERMANENT
    assert state_of(env, clip_id) == "FAILED_PERMANENT"
    assert events(env, "UPLOAD_ATTEMPTS_EXHAUSTED")
    assert env.adapter.objects == {}, "nothing was uploaded, and nothing was deleted"


def test_a_rejected_credential_stops_immediately_without_retrying(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.AuthStorageError("403 AccessDenied")

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.PERMANENT
    row = row_of(env, clip_id)
    assert row["upload_state"] == "FAILED_PERMANENT"
    assert row["last_error_class"] == "auth"
    assert row["next_attempt_at"] is None, "a 403 must not be scheduled for another try"
    assert attempts(env, clip_id)[0]["outcome"] == "permanent_error"


def test_a_checksum_mismatch_is_permanent_and_keeps_the_local_file(env: Any) -> None:
    clip_id, target = add_clip(env)
    env.adapter.error = storage.ChecksumMismatchError("provider stored different bytes")

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.PERMANENT
    assert row_of(env, clip_id)["last_error_class"] == "checksum"
    assert attempts(env, clip_id)[0]["outcome"] == "checksum_mismatch"
    assert target.exists(), "[INV-1]: the local file is preserved"


def test_a_missing_local_file_is_a_critical_consistency_finding(env: Any) -> None:
    clip_id, target = add_clip(env)
    target.unlink()

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.PERMANENT
    assert state_of(env, clip_id) == "FAILED_PERMANENT"
    audit = events(env, "UPLOAD_LOCAL_FILE_MISSING")
    assert len(audit) == 1
    assert audit[0]["severity"] == "critical"
    assert not target.exists(), "nothing was (re)created to paper over the inconsistency"


def test_an_unexpected_error_is_retried_conservatively(env: Any) -> None:
    """Asymmetric costs mean an unrecognised failure is retried, not abandoned."""
    clip_id, _ = add_clip(env)
    env.adapter.error = RuntimeError("a bug we have not seen before")

    outcome = claim(env)

    assert outcome is not None and outcome.status == uploader.RETRY
    assert state_of(env, clip_id) == "RETRY_WAIT"


# ---------------------------------------------------------------------------------------
# Ordering, digests and policy
# ---------------------------------------------------------------------------------------


def test_the_oldest_clip_is_claimed_first(env: Any) -> None:
    """[M3.7] bounded backlog age, which ordering by insertion order would not give."""
    newer, _ = add_clip(
        env, review_id="newer", start=NOW + 3600, relpath="clips/front-door/newer.mp4"
    )
    older, _ = add_clip(env, review_id="older", start=NOW, relpath="clips/front-door/older.mp4")
    # Insertion order is deliberately the reverse of event order.
    assert claim(env) is not None
    assert state_of(env, older) == "UPLOADED"
    assert state_of(env, newer) == "PENDING"


def test_a_clip_with_no_usable_digest_is_rehashed_and_recorded(env: Any) -> None:
    clip_id, target = add_clip(env, sha256="")
    claim(env)
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert row_of(env, clip_id)["sha256"] == expected
    assert env.adapter.objects[f"{LOCATION}/{REL}"]["sha256"] == expected


def test_a_size_mismatch_triggers_a_rehash(env: Any) -> None:
    clip_id, target = add_clip(env, size_bytes=1)
    claim(env)
    assert row_of(env, clip_id)["size_bytes"] == target.stat().st_size


def test_a_camera_with_upload_disabled_is_skipped_not_failed(env: Any) -> None:
    clip_id, _ = add_clip(env)
    changed = db.skip_by_policy(env.conn, cameras=[CAMERA], now=NOW)

    assert changed == 1
    assert state_of(env, clip_id) == "SKIPPED_BY_POLICY"
    assert claim(env) is None, "a skipped clip must never be claimed"
    assert env.adapter.puts == []
    assert events(env, "UPLOAD_SKIPPED_BY_POLICY")


def test_a_lease_can_be_released_for_an_immediate_retry(env: Any) -> None:
    clip_id, _ = add_clip(env)
    db.claim_next_upload(
        env.conn,
        owner="test:1",
        lease_seconds=900.0,
        cloud_key_prefix=f"{LOCATION}/",
        now=NOW,
    )
    assert state_of(env, clip_id) == "UPLOADING"

    assert db.release_lease(env.conn, clip_id=clip_id, now=NOW) is True
    assert state_of(env, clip_id) == "PENDING"
    assert row_of(env, clip_id)["lease_owner"] is None

    # Releasing a clip that is not mid-upload is a no-op, not a state change.
    assert db.release_lease(env.conn, clip_id=clip_id, now=NOW) is False


# ---------------------------------------------------------------------------------------
# Concurrency — the claim is the only coordination
# ---------------------------------------------------------------------------------------


def test_many_workers_upload_every_clip_exactly_once(env: Any) -> None:
    """[M3.12] the atomic claim, not coordination, is what makes this safe."""
    total = 40
    for index in range(total):
        add_clip(
            env,
            review_id=f"rev-{index}",
            start=NOW + index,
            relpath=f"clips/{CAMERA}/2026/09/12/clip-{index:02d}.mp4",
        )

    stop = threading.Event()
    finished: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        conn = db.open_db(env.settings.db_path)
        try:
            while not stop.is_set():
                outcome = env.uploader.upload_one(conn, owner=f"worker:{index}")
                if outcome is None:
                    with lock:
                        finished.append(index)
                    return
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    stop.set()

    assert len(set(env.adapter.puts)) == total, "every clip uploaded"
    assert sorted(env.adapter.puts) == sorted(set(env.adapter.puts)), "nothing uploaded twice"
    assert len(env.adapter.objects) == total
    uploaded = env.conn.execute(
        "SELECT COUNT(*) AS n FROM clips WHERE upload_state = 'UPLOADED'"
    ).fetchone()["n"]
    assert uploaded == total


# ---------------------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------------------


def reporter(env: Any, *, adapter: Any | None = None, settings: Any | None = None) -> Any:
    return uploader.UploaderHealth(
        db_path=(settings or env.settings).db_path,
        data_dir=env.data_dir,
        adapter=env.adapter if adapter is None else adapter,
        settings=settings or env.settings,
        uploader=env.uploader,
        started_at=NOW,
        clock=env.clock,
    )


def test_health_is_disabled_when_no_bucket_is_configured(env: Any) -> None:
    settings = make_settings(data_dir=env.data_dir, db_path=env.settings.db_path, bucket=None)
    report = reporter(env, adapter=None, settings=settings).report()
    assert report.state == health.DISABLED
    assert all(check.state == health.DISABLED for check in report.checks)
    assert "bucket" in report.summary


def test_health_is_healthy_with_an_empty_queue(env: Any) -> None:
    """Idle is not unhealthy: a component with nothing to do is working correctly."""
    report = reporter(env).report()
    assert report.state == health.HEALTHY
    assert "0 pending" in report.summary


def test_a_healthy_upload_leaves_health_healthy(env: Any) -> None:
    add_clip(env)
    claim(env)
    report = reporter(env).report()
    assert report.state == health.HEALTHY
    names = {check.name for check in report.checks}
    assert {
        "state_db",
        "storage_reachable",
        "storage_credentials",
        "local_files_readable",
        "upload_staleness",
        "backlog_age",
        "failed_permanent",
    } <= names


def test_an_ageing_backlog_degrades_health(env: Any) -> None:
    add_clip(env, materialised_at=NOW - 3600)
    report = reporter(env).report()
    assert report.state == health.DEGRADED
    backlog = next(check for check in report.checks if check.name == "backlog_age")
    assert backlog.code == "BACKLOG_AGING"


def test_a_stale_backlog_is_unhealthy(env: Any) -> None:
    add_clip(env, materialised_at=NOW - 7 * 3600)
    assert reporter(env).report().state == health.UNHEALTHY


def test_a_permanently_failed_clip_is_unhealthy(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.AuthStorageError("403")
    claim(env)
    assert state_of(env, clip_id) == "FAILED_PERMANENT"

    report = reporter(env).report()
    assert report.state == health.UNHEALTHY
    assert any(check.code == "UPLOAD_FAILED_PERMANENT" for check in report.checks)


def test_a_held_clip_with_no_file_is_unhealthy(env: Any) -> None:
    _, target = add_clip(env)
    target.unlink()
    report = reporter(env).report()
    assert report.state == health.UNHEALTHY
    assert any(check.code == "UPLOAD_LOCAL_FILE_MISSING" for check in report.checks)


def test_an_unverified_upload_degrades_health(env: Any) -> None:
    env.adapter.verified_by = "none"
    add_clip(env)
    claim(env)
    report = reporter(env).report()
    assert report.state == health.DEGRADED
    assert any(check.code == "UPLOAD_UNVERIFIED" for check in report.checks)


def test_a_size_verified_multipart_upload_is_healthy_not_degraded(env: Any) -> None:
    """#2: multipart uploads are verified by size plus our own sha256, by design.

    `cloud-object-layout` §4.2 documents `verified_by="size"` as the expected outcome above
    the multipart threshold. Degrading on it made every healthy B2 site permanently DEGRADED
    and, through `?strict=1`, permanently 503.
    """
    env.adapter.verified_by = "size"
    add_clip(env)
    claim(env)

    report = reporter(env).report()
    verification = next(c for c in report.checks if c.name == "upload_verification")

    assert report.state == health.HEALTHY
    assert verification.state == health.HEALTHY
    assert "size" in (verification.detail or "")


def test_bad_credentials_are_named_in_health(env: Any) -> None:
    env.adapter._health = storage.AdapterHealth(
        reachable=True,
        credentials_ok=False,
        bucket_present=True,
        detail="403 AccessDenied",
        error=storage.AuthStorageError("403 AccessDenied"),
    )
    report = reporter(env).report()
    assert report.state == health.UNHEALTHY
    assert any(check.code == "STORAGE_AUTH_FAILED" for check in report.checks)


def test_health_reports_unavailable_rather_than_raising(env: Any) -> None:
    settings = make_settings(data_dir=env.data_dir, db_path=env.data_dir / "does-not-exist.db")
    report = reporter(env, settings=settings).report()
    assert report.state == health.UNHEALTHY
    assert any(check.code == "DB_UNAVAILABLE" for check in report.checks)


def test_gauges_expose_the_backlog(env: Any) -> None:
    add_clip(env, materialised_at=NOW - 60)
    gauges = reporter(env).gauges()
    assert gauges["cctv_clips_pending"] == 1
    assert gauges["cctv_oldest_pending_age_seconds"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------------------


def test_queue_reports_the_backlog_and_the_failures(env: Any) -> None:
    add_clip(env, review_id="pending")
    failing, _ = add_clip(
        env,
        review_id="failing",
        start=NOW - 100,
        relpath="clips/front-door/failing.mp4",
    )
    env.adapter.error = storage.AuthStorageError("403")
    claim(env)
    assert state_of(env, failing) == "FAILED_PERMANENT"

    payload = uploader.admin_routes(settings=env.settings, clock=env.clock).queue()

    assert payload["counts"]["PENDING"] == 1
    assert payload["counts"]["FAILED_PERMANENT"] == 1
    assert payload["oldest_pending"][0]["path"] == REL
    assert payload["failed"][0]["last_error_class"] == "auth"


def test_retry_requires_a_reason(env: Any) -> None:
    routes = uploader.admin_routes(settings=env.settings, clock=env.clock)
    with pytest.raises(ValueError, match="reason"):
        routes.retry({"camera": CAMERA})


def test_retry_refuses_an_unfiltered_request(env: Any) -> None:
    routes = uploader.admin_routes(settings=env.settings, clock=env.clock)
    with pytest.raises(ValueError, match="Refusing to requeue every"):
        routes.retry({"reason": "because"})


def test_retry_requeues_a_failed_clip(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.AuthStorageError("403")
    claim(env)
    assert state_of(env, clip_id) == "FAILED_PERMANENT"

    routes = uploader.admin_routes(settings=env.settings, clock=env.clock)
    result = routes.retry({"clip_id": clip_id, "reason": "credentials rotated"})

    assert result["requeued"] == 1
    row = row_of(env, clip_id)
    assert row["upload_state"] == "PENDING"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert events(env, "UPLOAD_RETRY_REQUESTED")[0]["detail"] is not None


def test_a_retried_clip_can_be_uploaded_again(env: Any) -> None:
    clip_id, _ = add_clip(env)
    env.adapter.error = storage.AuthStorageError("403")
    claim(env)
    env.adapter.error = None

    routes = uploader.admin_routes(settings=env.settings, clock=env.clock)
    routes.retry({"clip_id": clip_id, "reason": "credentials rotated"})

    outcome = claim(env)
    assert outcome is not None and outcome.ok
    assert state_of(env, clip_id) == "UPLOADED"


# ---------------------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------------------


def test_policy_pass_skips_only_the_disabled_cameras(env: Any) -> None:
    clip_id, _ = add_clip(env)
    config = SimpleNamespace(
        cameras={CAMERA: SimpleNamespace(retention=SimpleNamespace(upload=False))}
    )
    service = uploader.UploadService(
        settings=env.settings,
        config=config,
        uploader=env.uploader,
        health_reporter=reporter(env),
    )
    assert service.policy_pass() == 1
    assert state_of(env, clip_id) == "SKIPPED_BY_POLICY"


def test_policy_pass_does_nothing_when_every_camera_uploads(env: Any) -> None:
    add_clip(env)
    config = SimpleNamespace(
        cameras={CAMERA: SimpleNamespace(retention=SimpleNamespace(upload=True))}
    )
    service = uploader.UploadService(
        settings=env.settings,
        config=config,
        uploader=env.uploader,
        health_reporter=reporter(env),
    )
    assert service.policy_pass() == 0
    assert (
        env.conn.execute("SELECT COUNT(*) AS n FROM clips WHERE upload_state='PENDING'").fetchone()[
            "n"
        ]
        == 1
    )


def test_the_service_drains_the_queue_and_stops(env: Any) -> None:
    for index in range(5):
        add_clip(
            env,
            review_id=f"r{index}",
            start=NOW + index,
            relpath=f"clips/{CAMERA}/2026/09/12/svc-{index}.mp4",
        )
    config = SimpleNamespace(
        cameras={CAMERA: SimpleNamespace(retention=SimpleNamespace(upload=True))}
    )
    service = uploader.UploadService(
        settings=env.settings,
        config=config,
        uploader=env.uploader,
        health_reporter=reporter(env),
    )

    stop = threading.Event()
    thread = threading.Thread(target=service.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        pending = env.conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE upload_state = 'PENDING'"
        ).fetchone()["n"]
        if pending == 0:
            break
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=10)

    assert (
        env.conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE upload_state = 'UPLOADED'"
        ).fetchone()["n"]
        == 5
    )
    assert len(env.adapter.objects) == 5


# ---------------------------------------------------------------------------------------
# Deployment wiring — the read-only mount is the enforcement, so it is asserted
# ---------------------------------------------------------------------------------------


def compose_service(name: str) -> dict[str, Any]:
    import yaml

    path = Path(__file__).resolve().parents[1] / "compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"][name]


def test_the_uploader_mounts_clips_read_only() -> None:
    """[M3.11] The uploader must not be able to delete a clip, even by accident.

    The mount flag is the enforcement and the rule in `lld/04` is the intent; a guarantee
    that lives only in a comment is one an edit can remove silently.
    """
    volumes = [str(entry) for entry in compose_service("uploader")["volumes"]]
    clips = [entry for entry in volumes if "/clips" in entry and "clips:" in entry]
    assert clips, "the uploader must mount the clips directory"
    assert all(entry.endswith(":ro") for entry in clips), clips


def test_the_uploader_reads_the_shared_database() -> None:
    """One database, named explicitly, so a changed default cannot split the system."""
    environment = compose_service("uploader")["environment"]
    worker_environment = compose_service("clip-worker")["environment"]
    assert environment["CCTV_DB"] == worker_environment["CCTV_DB"] == "/data/cctv.db"


def test_the_uploader_health_port_is_bound_to_loopback() -> None:
    ports = [str(entry) for entry in compose_service("uploader")["ports"]]
    assert ports, "the operator needs a health endpoint"
    assert all(entry.startswith("127.0.0.1:") for entry in ports), ports


def test_the_uploader_is_not_restarted_forever_on_a_configuration_error() -> None:
    """A bad `.env` must end as a visibly exited container, not a restart storm."""
    assert compose_service("uploader")["restart"] == "on-failure:5"
