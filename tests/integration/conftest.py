"""Fixtures for the live integration suite — see `README.md` beside this file, and
[`lld/00` §5](../../internal/architecture/lld/00-overview.md).

Everything that needs a provider is **skipped** unless an endpoint is configured, because
the default `uv run pytest` has to stay green with no containers and no network. The runner
`run.sh` starts RustFS and sets these; pointing the same variables at a real Backblaze B2
bucket also works, and is the only way to settle backlog §1.9 for real.

    CCTV_TEST_STORAGE_ENDPOINT_URL    e.g. http://127.0.0.1:19000
    CCTV_TEST_STORAGE_KEY_ID
    CCTV_TEST_STORAGE_APPLICATION_KEY
    CCTV_TEST_STORAGE_REGION          optional, default ``us-east-1``

The unit suite drives an in-process ``FakeAdapter``, which is the right tool for proving what
we *do* with what a provider says. It cannot prove that a real provider says it, that
concurrent claims survive real HTTP, or that a container cannot write to a read-only mount.
That is what lives here.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cctv import db, storage, uploader

ENDPOINT_VAR = "CCTV_TEST_STORAGE_ENDPOINT_URL"
KEY_VAR = "CCTV_TEST_STORAGE_KEY_ID"
SECRET_VAR = "CCTV_TEST_STORAGE_APPLICATION_KEY"
REGION_VAR = "CCTV_TEST_STORAGE_REGION"

NOW = 1_700_000_000.0
CAMERA = "front-door"
LOCATION = "integration"
CLIP_REL = f"clips/{CAMERA}/2026/09/12/18-45-00.mp4"


# ---------------------------------------------------------------------------------------
# Provider fixtures — absent unless an endpoint was configured
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def endpoint() -> str:
    url = (os.environ.get(ENDPOINT_VAR) or "").strip()
    if not url:
        pytest.skip(
            f"{ENDPOINT_VAR} is not set. Run tests/integration/run.sh, or point the "
            f"CCTV_TEST_STORAGE_* variables at any S3-compatible endpoint."
        )
    return url


@pytest.fixture(scope="session")
def region() -> str:
    return (os.environ.get(REGION_VAR) or "").strip() or "us-east-1"


@pytest.fixture(scope="session")
def credentials() -> tuple[str, str]:
    key = (os.environ.get(KEY_VAR) or "").strip()
    secret = (os.environ.get(SECRET_VAR) or "").strip()
    if not key or not secret:
        pytest.skip(f"{KEY_VAR} and {SECRET_VAR} must be set alongside {ENDPOINT_VAR}.")
    return key, secret


@pytest.fixture(scope="session")
def admin(endpoint: str, region: str, credentials: tuple[str, str]) -> Iterator[Any]:
    """A raw ``boto3`` client: creates buckets and counts objects independently of the code
    under test. A check that reuses the implementation it is checking can agree with a bug.
    """
    try:
        import boto3
    except ModuleNotFoundError:  # pragma: no cover - depends on how pytest was invoked
        pytest.skip("integration tests need boto3: `uv run --extra storage pytest ...`")
    key, secret = credentials
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
    )
    yield client
    client.close()


@pytest.fixture
def bucket(admin: Any) -> Iterator[str]:
    """A fresh bucket per test.

    Per test rather than per session: an object left behind by an earlier test could make a
    later one pass for the wrong reason, and "exactly once" is precisely the claim under test.
    """
    name = f"cctv-test-{uuid.uuid4().hex[:12]}"
    admin.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        _purge_bucket(admin, name)


def _purge_bucket(admin: Any, name: str) -> None:
    """Best-effort teardown, including object versions.

    A versioned test leaves delete markers and non-current versions behind, and a bucket
    cannot be removed while either exists. Teardown failures must not mask the test result,
    hence the suppression — the bucket is uniquely named and the server is ephemeral.
    """
    with suppress(Exception):
        paginator = admin.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            entries = list(page.get("Versions") or []) + list(page.get("DeleteMarkers") or [])
            for entry in entries:
                admin.delete_object(Bucket=name, Key=entry["Key"], VersionId=entry["VersionId"])
        for key in object_keys(admin, name):
            admin.delete_object(Bucket=name, Key=key)
        admin.delete_bucket(Bucket=name)


@pytest.fixture
def adapter(
    bucket: str, endpoint: str, region: str, credentials: tuple[str, str]
) -> storage.S3StorageAdapter:
    key, secret = credentials
    return storage.S3StorageAdapter(
        bucket=bucket,
        access_key_id=key,
        secret_access_key=secret,
        endpoint_url=endpoint,
        region=region,
    )


def object_keys(admin: Any, bucket: str) -> list[str]:
    """Every current object key, via ``list_objects_v2`` with pagination."""
    keys: list[str] = []
    token: str | None = None
    while True:
        parameters: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            parameters["ContinuationToken"] = token
        response = admin.list_objects_v2(**parameters)
        keys.extend(str(item["Key"]) for item in response.get("Contents") or [])
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not token:
            return keys


# ---------------------------------------------------------------------------------------
# The world a test drives: a real database, a real adapter, and a clock we can move
# ---------------------------------------------------------------------------------------


class Clock:
    """A mutable clock, so an hour of simulated outage costs no wall time."""

    def __init__(self, value: float = NOW) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_settings(data_dir: Path, **overrides: Any) -> uploader.UploadSettings:
    base: dict[str, Any] = {
        "cameras_path": data_dir / "cameras.yml",
        "data_dir": data_dir,
        "db_path": data_dir / "cctv.db",
        # The adapter is injected directly, so the bucket name here is never used to reach
        # a provider; it exists only to satisfy the model.
        "bucket": "injected-adapter",
        "endpoint_url": None,
        "region": "us-east-1",
        "key_id": "key",
        "application_key": "secret",
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


def build_uploader(
    *,
    data_dir: Path,
    adapter: Any,
    settings: uploader.UploadSettings,
    clock: Clock,
) -> uploader.Uploader:
    return uploader.Uploader(
        data_dir=data_dir,
        location=LOCATION,
        adapter=adapter,
        settings=settings,
        clock=clock,
    )


@dataclass
class LiveEnv:
    """One test's world. Tests may call `rebuild` to swap in a fault-injecting adapter."""

    conn: sqlite3.Connection
    settings: uploader.UploadSettings
    clock: Clock
    data_dir: Path
    admin: Any
    bucket: str
    adapter: Any
    uploader: uploader.Uploader = field(init=False)
    created: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.uploader = build_uploader(
            data_dir=self.data_dir,
            adapter=self.adapter,
            settings=self.settings,
            clock=self.clock,
        )

    # -- driving the uploader -----------------------------------------------------------

    def rebuild(self, adapter: Any) -> uploader.Uploader:
        """Replace the adapter and rebuild the uploader around it (used for fault injection)."""
        self.adapter = adapter
        self.uploader = build_uploader(
            data_dir=self.data_dir,
            adapter=adapter,
            settings=self.settings,
            clock=self.clock,
        )
        return self.uploader

    def reopen(self) -> None:
        """Close and reopen the connection, standing in for a process restart."""
        self.conn.close()
        self.conn = db.open_db(self.settings.db_path)

    def claim_all(self) -> list[uploader.UploadOutcome]:
        """Claim and process every clip that is eligible *right now*, and no more."""
        outcomes: list[uploader.UploadOutcome] = []
        while True:
            outcome = self.uploader.upload_one(self.conn, owner="integration:1")
            if outcome is None:
                return outcomes
            outcomes.append(outcome)

    def pending(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE upload_state IN ('PENDING', 'RETRY_WAIT')"
        ).fetchone()
        return int(row["n"])

    def drain(self, *, max_advances: int = 64) -> list[uploader.UploadOutcome]:
        """Upload everything, advancing the clock past each backoff instead of sleeping."""
        outcomes: list[uploader.UploadOutcome] = []
        for _ in range(max_advances):
            outcomes.extend(self.claim_all())
            if self.pending() == 0:
                return outcomes
            self.clock.advance(3600)
        raise AssertionError("drain did not converge: a clip is stuck in a retry loop")

    # -- reading the world --------------------------------------------------------------

    def add_clip(
        self,
        *,
        review_id: str,
        relpath: str = CLIP_REL,
        body: bytes = b"pretend mp4 bytes",
    ) -> tuple[int, Path]:
        """Write a real file and insert a real row, exactly as the clip-worker would."""
        target = self.data_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        clip_id, _created = db.insert_clip(
            self.conn,
            db.ClipRecord(
                review_id=review_id,
                camera=CAMERA,
                severity="alert",
                start_time=NOW,
                end_time=NOW + 30,
                path=relpath,
                event_ids=("evt-1",),
                zones=("driveway",),
                label="person",
                duration_s=30.0,
                sha256=digest,
                size_bytes=len(body),
                materialised_at=NOW,
            ),
            now=NOW,
        )
        self.created.append(clip_id)
        return clip_id, target

    def object_keys(self) -> list[str]:
        return object_keys(self.admin, self.bucket)

    def row(self, clip_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        assert row is not None, f"no clip {clip_id}"
        return row

    def states(self) -> list[str]:
        rows = self.conn.execute("SELECT upload_state FROM clips ORDER BY id").fetchall()
        return [str(row["upload_state"]) for row in rows]


@pytest.fixture
def live(tmp_path: Path, adapter: Any, admin: Any, bucket: str) -> Iterator[LiveEnv]:
    settings = make_settings(tmp_path)
    conn = db.open_db(settings.db_path)
    db.migrate(conn)
    db.ensure_camera(conn, name=CAMERA, display_name="Front Door", now=NOW)
    env = LiveEnv(
        conn=conn,
        settings=settings,
        clock=Clock(),
        data_dir=tmp_path,
        admin=admin,
        bucket=bucket,
        adapter=adapter,
    )
    try:
        yield env
    finally:
        conn.close()
