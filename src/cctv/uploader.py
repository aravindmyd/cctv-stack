"""The uploader service — a canonical clip becomes a durable cloud object, exactly once.

```text
claim (lease) ──→ pre-flight ──→ PUT ──→ verify ──→ record
   │                │             │        │          │
   └── commit ──────┘             └── no transaction is ever held across the network ──┘
```

The service is a small pool of worker loops, each owning its own SQLite connection, each
independently running the five steps above. There is no scheduler thread and no queue in
memory: **the database is the queue**, and the atomic claim (contract §3.3) is the only
coordination. That is what makes any number of workers — in this process or another —
correct rather than merely concurrent.

Four rules from the design carry real weight here:

* **Nothing slow happens inside a transaction.** Claim, commit, upload, record. A PUT
  holding the write lock would stall the clip-worker's writes for the duration of a 50 MB
  upload.
* **Never claim a verification that was not performed.** `verified_by="none"` is reported as
  `DEGRADED` (`UPLOAD_UNVERIFIED`) rather than being rounded up to success, because
  `UPLOADED` is what tells cleanup a local file may be deleted.
* **Never delete, move or modify a file under `clips/`.** The compose mount makes that
  structural ([INV-1](../../../internal/architecture/invariants.md)); this module does not even
  have a code path that could.
* **An unconfigured deployment is `DISABLED`, not broken.** With no bucket set the service
  starts, reports `DISABLED`, and does no work, so a local-only install is a supported
  choice rather than a restart loop ([INV-6](../../../internal/architecture/invariants.md)).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, db, health, storage, timeutil
from .config import CamerasConfig, ConfigError, load_cameras_config
from .logging import configure_logging
from .storage import StorageAdapter, StorageError

__all__ = [
    "COMPONENT",
    "EXIT_CONFIG",
    "EXIT_UNAVAILABLE",
    "PERMANENT",
    "RETRY",
    "UPLOADED",
    "UploadOutcome",
    "UploadService",
    "UploadSettings",
    "Uploader",
    "UploaderHealth",
    "backoff_delay",
    "build_adapter",
    "main",
    "object_metadata",
    "validate_key",
]

logger = logging.getLogger(__name__)

COMPONENT = "uploader"

#: `EX_CONFIG` / `EX_TEMPFAIL` from `sysexits.h`, per LLD 00 §3. A bad config never becomes
#: good, so restarting cannot help; a briefly unavailable dependency might.
EXIT_CONFIG = 78
EXIT_UNAVAILABLE = 75
EXIT_OK = 0

DEFAULT_HEALTH_PORT = 8082
DEFAULT_SCHEDULER_TICK_S = 5.0
DEFAULT_LEASE_SECONDS = 900.0
DEFAULT_BACKOFF_BASE_S = 15.0
DEFAULT_BACKOFF_MAX_S = 3600.0
DEFAULT_QUOTA_BACKOFF_MAX_S = 21600.0
DEFAULT_UPLOAD_CONCURRENCY = 4
DEFAULT_STALE_UPLOAD_WARN_S = 1800.0
DEFAULT_BACKLOG_WARN_S = 1800.0
DEFAULT_BACKLOG_CRITICAL_S = 21600.0

#: How often the bulk policy pass runs, and how often the backlog is logged.
POLICY_INTERVAL_S = 60.0
SUMMARY_INTERVAL_S = 60.0

#: Seconds a storage `healthcheck()` result is reused. A `HEAD` per scrape would turn a
#: 2-second monitoring interval into a stream of requests against a provider that bills for
#: nothing but will rate-limit us.
DEFAULT_HEALTHCHECK_CACHE_S = 30.0

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: Key rules from cloud-object-layout §7. A leading slash, a doubled slash, a backslash, a
#: parent reference or a control character are all refused rather than normalised: a key we
#: silently rewrote is a key that no longer matches the local path, and key equality is the
#: property restore and round-trip verification depend on.
_BAD_KEY_RE = re.compile(r"^/|//|\\|\.\.|[\x00-\x1f]")
MAX_KEY_BYTES = 1024

UPLOADED = "uploaded"
RETRY = "retry"
PERMANENT = "permanent"


def validate_key(key: str) -> str:
    """Validate an object key, or raise. Returns the key unchanged when it is usable."""
    if not key:
        raise ValueError("A cloud key is required.")
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ValueError(f"Cloud key exceeds {MAX_KEY_BYTES} bytes.")
    if _BAD_KEY_RE.search(key):
        raise ValueError(
            f"Cloud key {key!r} contains a leading or doubled slash, a backslash, a parent "
            f"reference, or a control character (cloud-object-layout §7)."
        )
    return key


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadSettings:
    """Every input is an environment variable, so there are no CLI arguments to get wrong."""

    cameras_path: Path
    data_dir: Path
    db_path: Path
    bucket: str | None
    endpoint_url: str | None
    region: str
    key_id: str | None
    application_key: str | None
    health_host: str
    health_port: int
    internal_token: str | None
    dry_run: bool
    scheduler_tick_s: float
    lease_seconds: float
    max_attempts: int
    backoff_base_s: float
    backoff_max_s: float
    quota_backoff_max_s: float
    upload_concurrency: int
    multipart_threshold_bytes: int
    stale_upload_warn_s: float
    backlog_warn_s: float
    backlog_critical_s: float
    log_level: str | None

    @property
    def storage_configured(self) -> bool:
        return bool(self.bucket and self.key_id and self.application_key)

    def configuration_problem(self) -> str | None:
        """A single sentence naming what is wrong, or `None`.

        A half-configured bucket is refused rather than guessed at: defaulting the key id or
        the secret would produce an `UNHEALTHY` service whose error message says "denied"
        when the real problem is a `.env` that was never finished.
        """
        if not self.bucket:
            return None  # deliberately unconfigured; the service reports DISABLED
        missing = [
            name
            for name, value in (
                ("CCTV_STORAGE_KEY_ID", self.key_id),
                ("CCTV_STORAGE_APPLICATION_KEY", self.application_key),
            )
            if not value
        ]
        if missing:
            return (
                f"CCTV_STORAGE_BUCKET is set but {', '.join(missing)} is not. Set the "
                f"credentials, or unset CCTV_STORAGE_BUCKET to run without cloud backup."
            )
        if self.upload_concurrency < 1:
            return "CCTV_UPLOAD_CONCURRENCY must be at least 1."
        if self.scheduler_tick_s <= 0 or self.lease_seconds <= 0:
            return "CCTV_UPLOAD_TICK_S and CCTV_UPLOAD_LEASE_S must be positive."
        return None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> UploadSettings:
        env = os.environ if environ is None else environ

        def pick(*names: str) -> str | None:
            for name in names:
                value = (env.get(name) or "").strip()
                if value:
                    return value
            return None

        def number(name: str, default: float) -> float:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}={raw!r} is not a number.") from exc

        config_dir = Path(env.get("CCTV_CONFIG_DIR", "/config"))
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))
        region = pick("CCTV_STORAGE_REGION", "B2_REGION") or "us-west-004"

        return cls(
            cameras_path=Path(env.get("CCTV_CAMERAS", str(config_dir / "cameras.yml"))),
            data_dir=data_dir,
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            bucket=pick("CCTV_STORAGE_BUCKET", "B2_BUCKET"),
            endpoint_url=pick("CCTV_STORAGE_ENDPOINT_URL", "B2_ENDPOINT_URL")
            or f"https://s3.{region}.backblazeb2.com",
            region=region,
            key_id=pick("CCTV_STORAGE_KEY_ID", "B2_KEY_ID", "B2_APPLICATION_KEY_ID"),
            application_key=pick("CCTV_STORAGE_APPLICATION_KEY", "B2_APPLICATION_KEY"),
            health_host=(env.get("CCTV_HEALTH_HOST") or "0.0.0.0").strip(),
            health_port=int(number("CCTV_UPLOADER_HEALTH_PORT", DEFAULT_HEALTH_PORT)),
            internal_token=(env.get("INTERNAL_API_TOKEN") or "").strip() or None,
            dry_run=(env.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes"},
            scheduler_tick_s=number("CCTV_UPLOAD_TICK_S", DEFAULT_SCHEDULER_TICK_S),
            lease_seconds=number("CCTV_UPLOAD_LEASE_S", DEFAULT_LEASE_SECONDS),
            max_attempts=int(number("CCTV_UPLOAD_MAX_ATTEMPTS", db.MAX_ATTEMPTS)),
            backoff_base_s=number("CCTV_UPLOAD_BACKOFF_BASE_S", DEFAULT_BACKOFF_BASE_S),
            backoff_max_s=number("CCTV_UPLOAD_BACKOFF_MAX_S", DEFAULT_BACKOFF_MAX_S),
            quota_backoff_max_s=number(
                "CCTV_UPLOAD_QUOTA_BACKOFF_MAX_S", DEFAULT_QUOTA_BACKOFF_MAX_S
            ),
            upload_concurrency=int(
                number("CCTV_UPLOAD_CONCURRENCY", DEFAULT_UPLOAD_CONCURRENCY)
            ),
            multipart_threshold_bytes=int(
                number(
                    "CCTV_UPLOAD_MULTIPART_THRESHOLD_BYTES",
                    storage.DEFAULT_MULTIPART_THRESHOLD_BYTES,
                )
            ),
            stale_upload_warn_s=number(
                "CCTV_UPLOAD_STALE_WARN_S", DEFAULT_STALE_UPLOAD_WARN_S
            ),
            backlog_warn_s=number("CCTV_UPLOAD_BACKLOG_WARN_S", DEFAULT_BACKLOG_WARN_S),
            backlog_critical_s=number(
                "CCTV_UPLOAD_BACKLOG_CRITICAL_S", DEFAULT_BACKLOG_CRITICAL_S
            ),
            log_level=env.get("CCTV_LOG_LEVEL") or None,
        )


# ---------------------------------------------------------------------------------------
# Backoff (lld/04 §5)
# ---------------------------------------------------------------------------------------


def backoff_delay(
    attempt: int,
    *,
    base_s: float,
    max_s: float,
    rng: random.Random | None = None,
) -> float:
    """`min(base * 2^(attempt-1), max)` with ±20 % jitter.

    `attempt` is 1-based and is the value the claim already incremented, so the first failure
    waits about `base_s`.

    **The jitter is not decorative.** Without it, a provider outage that affects 200 clips
    produces 200 identically-scheduled retries — a self-inflicted thundering herd against an
    endpoint that has just told us it is unwell.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based; it is the value the claim incremented.")
    generator = rng if rng is not None else random
    ceiling = max(0.0, max_s)
    raw = min(base_s * math.pow(2, attempt - 1), ceiling)
    return raw * generator.uniform(0.8, 1.2)


# ---------------------------------------------------------------------------------------
# Object metadata (cloud-object-layout §3.2)
# ---------------------------------------------------------------------------------------


def object_metadata(
    row: sqlite3.Row, *, location: str, version: str = __version__
) -> dict[str, str]:
    """The self-describing metadata attached to every object.

    This is the recovery path of last resort: with a bucket listing and these keys alone,
    most of `clips` can be reconstructed after a total loss of `state.db`. It deliberately
    carries no credential and nothing derived from one ([INV-2]).

    Values are strings and ASCII-only, because S3 user metadata is ASCII and a provider that
    accepts more is not one we should depend on.
    """
    metadata: dict[str, str] = {
        "camera": str(row["camera"]),
        "location": location,
        "start-time": f"{float(row['start_time']):.6f}",
        "cctv-version": version,
        "review-id": str(row["review_id"]),
    }

    event_ids = _json_list(row["event_ids"])
    metadata["event-id"] = event_ids[0] if event_ids else str(row["review_id"])

    if row["end_time"] is not None:
        metadata["end-time"] = f"{float(row['end_time']):.6f}"
    if row["duration_s"] is not None:
        metadata["duration-s"] = f"{float(row['duration_s']):.3f}"
    if row["label"]:
        metadata["label"] = str(row["label"])

    zones = _json_list(row["zones"])
    if zones:
        metadata["zones"] = ",".join(zones)

    return metadata


def _json_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


# ---------------------------------------------------------------------------------------
# One clip
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """What happened to one clip. Returned rather than inferred from the database."""

    clip_id: int
    status: str
    camera: str
    path: str
    attempts: int = 0
    cloud_key: str | None = None
    bytes_sent: int = 0
    duration_ms: int = 0
    verified_by: str | None = None
    error_class: str | None = None
    reason: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.status == UPLOADED


class Uploader:
    """Claims one clip and does the work. Stateless apart from counters.

    Instantiated once and called from several worker threads; every method takes the
    connection it should use, which is what keeps the concurrency story trivial.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        location: str,
        adapter: StorageAdapter | None,
        settings: UploadSettings,
        clock: Callable[[], float] = timeutil.now,
        rng: random.Random | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._location = location
        self._adapter = adapter
        self._settings = settings
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self._unverified = 0
        self._size_verified = 0
        self._uploaded = 0
        self._failed_permanent = 0

    # -- counters (read by the health checks) ------------------------------------------

    @property
    def unverified_uploads(self) -> int:
        """Uploads with no provider confirmation at all — the only case that degrades."""
        with self._lock:
            return self._unverified

    @property
    def size_verified_uploads(self) -> int:
        with self._lock:
            return self._size_verified

    @property
    def uploaded_uploads(self) -> int:
        with self._lock:
            return self._uploaded

    # -- the loop ----------------------------------------------------------------------

    def upload_one(
        self, conn: sqlite3.Connection, *, owner: str
    ) -> UploadOutcome | None:
        """Claim and process the oldest eligible clip, or return `None` when idle."""
        if self._adapter is None:
            return None

        row = db.claim_next_upload(
            conn,
            owner=owner,
            lease_seconds=self._settings.lease_seconds,
            cloud_key_prefix=f"{self._location}/",
            now=self._clock(),
        )
        if row is None:
            return None
        return self._perform(conn, row)

    def _perform(self, conn: sqlite3.Connection, row: sqlite3.Row) -> UploadOutcome:
        """Do the work, and turn *any* failure into a recorded state transition.

        The broad `except` is deliberate and is the documented behaviour for an
        unrecognised error (lld/04 §4): treat it as transient and retry, because the costs
        are asymmetric — a bounded number of retries against a lost clip. The attempt
        counter bounds it, so a genuine bug reaches `FAILED_PERMANENT` with an alert rather
        than looping forever.
        """
        started = time.perf_counter()
        try:
            return self._upload(conn, row, started=started)
        except StorageError as exc:
            return self._record_failure(conn, row, exc, started=started)
        except Exception as exc:
            logger.exception(
                "unexpected upload failure",
                extra={"clip_id": int(row["id"]), "camera": row["camera"]},
            )
            classified = storage.TransientStorageError(
                f"Unexpected {type(exc).__name__} during upload: {exc}"
            )
            return self._record_failure(conn, row, classified, started=started)

    def _upload(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, started: float
    ) -> UploadOutcome:
        clip_id = int(row["id"])
        attempt_no = int(row["attempts"])
        target = self._data_dir / str(row["path"])

        size_on_disk = _size_or_none(target)
        if size_on_disk is None:
            # The database says a clip exists and it does not. That is a real inconsistency,
            # and it is loud on purpose — but the clip is not deleted and not retried.
            with db.write_txn(conn):
                db.record_event(
                    conn,
                    component=COMPONENT,
                    code="UPLOAD_LOCAL_FILE_MISSING",
                    severity="critical",
                    message=f"Clip {clip_id} has no local file at {row['path']}.",
                    detail={"clip_id": clip_id, "path": str(row["path"])},
                    now=self._clock(),
                )
            raise storage.InvalidRequestError(
                f"The local file for clip {clip_id} is missing ({target}). Nothing was "
                f"uploaded and nothing was deleted; the database and the disk disagree."
            )

        digest = str(row["sha256"] or "")
        if not SHA256_HEX_RE.match(digest) or row["size_bytes"] != size_on_disk:
            # Recompute rather than trust a digest we cannot use, and record it so the next
            # attempt does not have to. This is the only place the uploader writes anything
            # about a clip other than its upload state.
            digest = _sha256(target)
            db.set_clip_checksum(
                conn,
                clip_id=clip_id,
                sha256=digest,
                size_bytes=size_on_disk,
                now=self._clock(),
            )

        key = validate_key(str(row["cloud_key"] or ""))
        metadata = object_metadata(row, location=self._location)

        assert self._adapter is not None  # guarded by upload_one
        result = self._adapter.put_object(
            key=key, path=target, metadata=metadata, checksum_sha256=digest
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        db.mark_uploaded(
            conn,
            clip_id=clip_id,
            cloud_etag=result.etag,
            cloud_size_bytes=result.size,
            now=self._clock(),
        )
        db.finish_upload_attempt(
            conn,
            clip_id=clip_id,
            attempt_no=attempt_no,
            outcome="success",
            bytes_sent=result.size,
            duration_ms=duration_ms,
            now=self._clock(),
        )

        with self._lock:
            self._uploaded += 1
            # `size` is not a failure. Multipart uploads return a composite digest, so the
            # adapter compares size plus our own sha256 metadata instead — documented in
            # `cloud-object-layout` §4.2 and expected above 100 MB. Only `none`, where no
            # confirmation exists at all, is the condition worth degrading over.
            if result.verified_by == "none":
                self._unverified += 1
            elif result.verified_by == "size":
                self._size_verified += 1

        logger.info(
            "uploaded clip",
            extra={
                "clip_id": clip_id,
                "camera": row["camera"],
                "path": key,
                "attempts": attempt_no,
                "duration_s": duration_ms / 1000.0,
                "state": f"verified:{result.verified_by}",
                "count": result.size,
            },
        )

        return UploadOutcome(
            clip_id=clip_id,
            status=UPLOADED,
            camera=str(row["camera"]),
            path=str(row["path"]),
            attempts=attempt_no,
            cloud_key=key,
            bytes_sent=result.size,
            duration_ms=duration_ms,
            verified_by=result.verified_by,
            skipped=result.already_present,
        )

    def _record_failure(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        exc: StorageError,
        *,
        started: float,
    ) -> UploadOutcome:
        clip_id = int(row["id"])
        attempt_no = int(row["attempts"])
        duration_ms = int((time.perf_counter() - started) * 1000)
        message = str(exc)

        if exc.retryable:
            ceiling = (
                self._settings.quota_backoff_max_s
                if exc.error_class == "quota"
                else self._settings.backoff_max_s
            )
            delay = backoff_delay(
                attempt_no,
                base_s=self._settings.backoff_base_s,
                max_s=ceiling,
                rng=self._rng,
            )
            state = db.mark_transient_failure(
                conn,
                clip_id=clip_id,
                error_class=exc.error_class,
                message=message,
                next_attempt_at=self._clock() + delay,
                max_attempts=self._settings.max_attempts,
                now=self._clock(),
            )
            final = state == "FAILED_PERMANENT"
            outcome = "transient_error"
            status = PERMANENT if final else RETRY
            log = logger.error if final else logger.warning
            log(
                "upload failed; %s",
                "attempts exhausted" if final else f"retrying in {delay:.0f}s",
                extra={
                    "clip_id": clip_id,
                    "camera": row["camera"],
                    "attempts": attempt_no,
                    "reason": exc.error_class,
                    "state": state,
                },
            )
            if final:
                with self._lock:
                    self._failed_permanent += 1
        else:
            db.mark_permanent_failure(
                conn,
                clip_id=clip_id,
                error_class=exc.error_class,
                message=message,
                now=self._clock(),
            )
            outcome = (
                "checksum_mismatch"
                if exc.error_class == "checksum"
                else "permanent_error"
            )
            status = PERMANENT
            with self._lock:
                self._failed_permanent += 1
            logger.error(
                "upload failed permanently",
                extra={
                    "clip_id": clip_id,
                    "camera": row["camera"],
                    "attempts": attempt_no,
                    "reason": exc.error_class,
                    "state": "FAILED_PERMANENT",
                },
            )

        db.finish_upload_attempt(
            conn,
            clip_id=clip_id,
            attempt_no=attempt_no,
            outcome=outcome,
            http_status=exc.http_status,
            error_class=exc.error_class,
            error_message=message,
            duration_ms=duration_ms,
            now=self._clock(),
        )

        return UploadOutcome(
            clip_id=clip_id,
            status=status,
            camera=str(row["camera"]),
            path=str(row["path"]),
            attempts=attempt_no,
            cloud_key=str(row["cloud_key"] or ""),
            duration_ms=duration_ms,
            error_class=exc.error_class,
            reason=message,
        )


# ---------------------------------------------------------------------------------------
# Health (service-contracts §1)
# ---------------------------------------------------------------------------------------


class UploaderHealth:
    """Builds the uploader's health report. Read-only by construction."""

    def __init__(
        self,
        *,
        db_path: Path,
        data_dir: Path,
        adapter: StorageAdapter | None,
        settings: UploadSettings,
        uploader: Uploader,
        started_at: float,
        clock: Callable[[], float] = timeutil.now,
        cache_s: float = DEFAULT_HEALTHCHECK_CACHE_S,
    ) -> None:
        self._db_path = Path(db_path)
        self._data_dir = Path(data_dir)
        self._adapter = adapter
        self._settings = settings
        self._uploader = uploader
        self._started_at = started_at
        self._clock = clock
        self._cache_s = cache_s
        self._cached: storage.AdapterHealth | None = None
        self._cached_at: float = 0.0
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------------------

    @contextmanager
    def _readonly(self) -> Iterator[sqlite3.Connection]:
        """A short-lived read-only connection.

        Read-only so that monitoring physically cannot mutate what it monitors, and
        short-lived so that the HTTP thread and the main loop never share a cursor. Opening
        a connection is microseconds; reasoning about a shared one is not.
        """
        conn = db.open_db(self._db_path, readonly=True)
        try:
            yield conn
        finally:
            conn.close()

    def _storage_health(self) -> storage.AdapterHealth | None:
        if self._adapter is None:
            return None
        with self._lock:
            now = self._clock()
            if self._cached is not None and (now - self._cached_at) < self._cache_s:
                return self._cached
            self._cached = self._adapter.healthcheck()
            self._cached_at = now
            return self._cached

    def summary(self) -> db.QueueSummary | None:
        try:
            with self._readonly() as conn:
                return db.queue_summary(conn, now=self._clock())
        except sqlite3.Error:
            return None

    # -- the report --------------------------------------------------------------------

    def report(self) -> health.HealthReport:
        now = self._clock()
        if not self._settings.storage_configured:
            return self._disabled_report(now)

        try:
            with self._readonly() as conn:
                queue: db.QueueSummary | None = db.queue_summary(conn, now=now)
                checks: list[health.Check] = [
                    self._state_db_check(conn),
                    self._local_files_check(conn),
                ]
        except sqlite3.Error as exc:
            queue = None
            checks = [
                health.Check(
                    name="state_db",
                    state=health.UNHEALTHY,
                    code="DB_UNAVAILABLE",
                    detail=f"cannot open {self._db_path}: {exc}",
                ),
                health.Check(
                    name="local_files_readable",
                    state=health.UNKNOWN,
                    code="DB_UNAVAILABLE",
                    detail="the state database could not be read",
                ),
            ]

        checks.extend(self._storage_checks())
        if queue is None:
            checks.append(
                health.Check(
                    name="backlog_age",
                    state=health.UNKNOWN,
                    code="DB_UNAVAILABLE",
                    detail="the state database could not be read",
                )
            )
        else:
            checks.extend(self._backlog_checks(queue))

        return health.HealthReport(
            component=COMPONENT,
            checks=tuple(checks),
            summary=self._summary_text(queue),
            since=self._started_at,
            observed_at=now,
            uptime_s=max(0.0, now - self._started_at),
        )

    def _disabled_report(self, now: float) -> health.HealthReport:
        """All checks `DISABLED`, so the overall state is `DISABLED` — not healthy, not broken.

        The distinction is the point: the operator chose not to configure cloud backup, and
        a status page that reports `HEALTHY` for a component doing nothing would be a lie
        ([INV-6](../../../internal/architecture/invariants.md)).
        """
        detail = "no storage bucket is configured, so nothing is backed up"
        names = (
            "state_db",
            "storage_reachable",
            "storage_credentials",
            "local_files_readable",
            "upload_staleness",
            "backlog_age",
            "failed_permanent",
        )
        return health.HealthReport(
            component=COMPONENT,
            checks=tuple(
                health.Check(name=name, state=health.DISABLED, detail=detail)
                for name in names
            ),
            summary=detail,
            since=self._started_at,
            observed_at=now,
            uptime_s=max(0.0, now - self._started_at),
        )

    def _state_db_check(self, conn: sqlite3.Connection) -> health.Check:
        started = time.perf_counter()
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"]
        except sqlite3.Error as exc:
            return health.Check(
                name="state_db",
                state=health.UNHEALTHY,
                code="DB_UNAVAILABLE",
                detail=f"cannot query {self._db_path}: {exc}",
            )
        return health.Check(
            name="state_db",
            state=health.HEALTHY,
            detail=f"read/write ok, {int(count)} clips",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _local_files_check(self, conn: sqlite3.Connection) -> health.Check:
        try:
            rows = db.stored_clip_checks(conn)
        except sqlite3.Error as exc:
            return health.Check(
                name="local_files_readable",
                state=health.UNKNOWN,
                code="DB_UNAVAILABLE",
                detail=f"could not list stored clips: {exc}",
            )
        missing = [
            str(row["path"])
            for row in rows
            if not (self._data_dir / row["path"]).exists()
        ]
        if missing:
            return health.Check(
                name="local_files_readable",
                state=health.UNHEALTHY,
                code="UPLOAD_LOCAL_FILE_MISSING",
                detail=(
                    f"{len(missing)} clip(s) in a held state have no file, e.g. {missing[0]}. "
                    f"The database and the disk disagree."
                ),
            )
        return health.Check(
            name="local_files_readable",
            state=health.HEALTHY,
            detail=f"{len(rows)} held clip(s) readable",
        )

    def _storage_checks(self) -> list[health.Check]:
        info = self._storage_health()
        assert info is not None  # guarded by the caller

        if not info.reachable:
            reachable = health.Check(
                name="storage_reachable",
                state=health.UNHEALTHY,
                code="STORAGE_UNREACHABLE",
                detail=info.detail,
                latency_ms=info.latency_ms,
            )
        elif not info.bucket_present:
            reachable = health.Check(
                name="storage_reachable",
                state=health.UNHEALTHY,
                code="STORAGE_UNREACHABLE",
                detail=f"{info.detail} (expected bucket {self._settings.bucket!r})",
                latency_ms=info.latency_ms,
            )
        else:
            reachable = health.Check(
                name="storage_reachable",
                state=health.HEALTHY,
                detail=info.detail,
                latency_ms=info.latency_ms,
            )

        credentials = health.Check(
            name="storage_credentials",
            state=health.HEALTHY if info.credentials_ok else health.UNHEALTHY,
            code=None if info.credentials_ok else "STORAGE_AUTH_FAILED",
            detail=(
                "accepted"
                if info.credentials_ok
                else f"{info.detail} Rotate them: docs/rotate-credentials.md."
            ),
        )
        return [reachable, credentials]

    def _backlog_checks(self, queue: db.QueueSummary) -> list[health.Check]:
        checks = [self._backlog_age_check(queue), self._staleness_check(queue)]

        if queue.failed_permanent:
            checks.append(
                health.Check(
                    name="failed_permanent",
                    state=health.UNHEALTHY,
                    code="UPLOAD_FAILED_PERMANENT",
                    detail=(
                        f"{queue.failed_permanent} clip(s) will not retry without "
                        f"intervention. See docs/recover-uploads.md."
                    ),
                )
            )
        else:
            checks.append(
                health.Check(
                    name="failed_permanent",
                    state=health.HEALTHY,
                    detail="none",
                )
            )

        if self._uploader.unverified_uploads:
            checks.append(
                health.Check(
                    name="upload_verification",
                    state=health.DEGRADED,
                    code="UPLOAD_UNVERIFIED",
                    detail=(
                        f"{self._uploader.unverified_uploads} upload(s) since start could not "
                        f"be verified at all. An unverified backup is not a backup"
                    ),
                )
            )
        else:
            # Reported rather than hidden: size verification is the documented outcome for
            # multipart uploads, but the operator should still be able to see it happened.
            size_verified = self._uploader.size_verified_uploads
            detail = (
                "all verified by provider checksum"
                if size_verified == 0
                else f"{size_verified} multipart upload(s) verified by size and local sha256"
            )
            checks.append(
                health.Check(
                    name="upload_verification", state=health.HEALTHY, detail=detail
                )
            )
        return checks

    def _backlog_age_check(self, queue: db.QueueSummary) -> health.Check:
        age = queue.oldest_pending_age_s
        if age is None:
            return health.Check(
                name="backlog_age",
                state=health.HEALTHY,
                detail=f"nothing pending; {queue.uploaded_24h} uploaded in the last 24h",
            )
        if age >= self._settings.backlog_critical_s:
            state, code = health.UNHEALTHY, "BACKLOG_AGING"
        elif age >= self._settings.backlog_warn_s:
            state, code = health.DEGRADED, "BACKLOG_AGING"
        else:
            return health.Check(
                name="backlog_age",
                state=health.HEALTHY,
                detail=f"{queue.pending} pending, oldest {_human(age)}",
            )
        return health.Check(
            name="backlog_age",
            state=state,
            code=code,
            detail=f"{queue.pending} pending, oldest {_human(age)}",
        )

    def _staleness_check(self, queue: db.QueueSummary) -> health.Check:
        """No successful upload recently — but only meaningful when there is work to do.

        An uploader with an empty queue and no recent upload is *idle*, which is healthy. This
        is the distinction that stops a quiet site from alarming every night.
        """
        age = queue.stale_upload_s
        if queue.pending == 0 and queue.uploading == 0 and queue.retry_wait == 0:
            return health.Check(
                name="upload_staleness",
                state=health.HEALTHY,
                detail="nothing to upload",
            )
        if age is None:
            # A backlog with no upload ever recorded: judge it by the backlog itself.
            age = queue.oldest_pending_age_s or 0.0
        if age >= self._settings.backlog_critical_s:
            state, code = health.UNHEALTHY, "STORAGE_STALE"
        elif age >= self._settings.stale_upload_warn_s:
            state, code = health.DEGRADED, "STORAGE_STALE"
        else:
            return health.Check(
                name="upload_staleness",
                state=health.HEALTHY,
                detail=f"last success {_human(age)} ago",
            )
        return health.Check(
            name="upload_staleness",
            state=state,
            code=code,
            detail=f"last successful upload {_human(age)} ago",
        )

    def _summary_text(self, queue: db.QueueSummary | None) -> str:
        if queue is None:
            return "state database unreadable"
        parts = [f"{queue.pending} pending"]
        if queue.oldest_pending_age_s is not None:
            parts.append(f"oldest {_human(queue.oldest_pending_age_s)}")
        parts.append(f"{queue.uploaded_24h} uploaded in 24h")
        if queue.failed_permanent:
            parts.append(f"{queue.failed_permanent} failed permanently")
        return "; ".join(parts)

    def gauges(self) -> dict[str, float | int]:
        queue = self.summary()
        if queue is None:
            return {}
        return {
            "cctv_clips_pending": queue.pending,
            "cctv_clips_uploading": queue.uploading,
            "cctv_clips_retry_wait": queue.retry_wait,
            "cctv_clips_failed_permanent": queue.failed_permanent,
            "cctv_clips_uploaded_24h": queue.uploaded_24h,
            "cctv_oldest_pending_age_seconds": round(
                queue.oldest_pending_age_s or 0.0, 3
            ),
        }


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------------------


def admin_routes(
    *, settings: UploadSettings, clock: Callable[[], float] = timeutil.now
) -> health.AdminRoutes:
    """The two admin operations the runbook actually uses.

    A fresh connection per request: these are human-driven, so the cost is irrelevant and a
    connection shared with the workers would be one more thing to reason about.
    """

    def _connect() -> sqlite3.Connection:
        return db.open_db(settings.db_path)

    def queue() -> Mapping[str, Any]:
        conn = _connect()
        try:
            summary = db.queue_summary(conn, now=clock())
            oldest = conn.execute("""
                SELECT id, camera, path, start_time, attempts, last_error_class
                  FROM clips
                 WHERE upload_state = 'PENDING'
                 ORDER BY start_time ASC, id ASC
                 LIMIT 20
                """).fetchall()
            failed = conn.execute("""
                SELECT id, camera, path, attempts, last_error_class, last_error
                  FROM clips
                 WHERE upload_state = 'FAILED_PERMANENT'
                 ORDER BY id
                """).fetchall()
            return {
                "counts": dict(summary.counts),
                "pending": summary.pending,
                "uploading": summary.uploading,
                "retry_wait": summary.retry_wait,
                "failed_permanent": summary.failed_permanent,
                "uploaded_24h": summary.uploaded_24h,
                "oldest_pending_age_s": summary.oldest_pending_age_s,
                "oldest_pending": [dict(row) for row in oldest],
                "failed": [dict(row) for row in failed],
            }
        finally:
            conn.close()

    def retry(body: Mapping[str, Any]) -> Mapping[str, Any]:
        reason = str(body.get("reason") or "")
        clip_id = body.get("clip_id")
        camera = body.get("camera")
        since = body.get("since")
        conn = _connect()
        try:
            # `reset_failed_permanent` refuses an unfiltered or unexplained request, which is
            # an operator mistake rather than a database fault. Translating it to a ValueError
            # is what lets the HTTP layer answer `400` instead of `500`.
            try:
                ids = db.reset_failed_permanent(
                    conn,
                    reason=reason,
                    clip_id=int(clip_id) if clip_id is not None else None,
                    camera=str(camera) if camera is not None else None,
                    since=float(since) if since is not None else None,
                    now=clock(),
                )
            except db.DbError as exc:
                raise ValueError(str(exc)) from exc
            return {"requeued": len(ids), "clip_ids": ids}
        finally:
            conn.close()

    return health.AdminRoutes(queue=queue, retry=retry)


# ---------------------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------------------


def build_adapter(settings: UploadSettings) -> StorageAdapter | None:
    """Construct the adapter, or `None` when cloud backup is deliberately unconfigured."""
    if not settings.storage_configured:
        return None
    assert settings.bucket and settings.key_id and settings.application_key
    return storage.S3StorageAdapter(
        bucket=settings.bucket,
        access_key_id=settings.key_id,
        secret_access_key=settings.application_key,
        endpoint_url=settings.endpoint_url,
        region=settings.region,
        multipart_threshold_bytes=settings.multipart_threshold_bytes,
    )


class UploadService:
    """Worker threads, the periodic policy pass, and the health surface."""

    def __init__(
        self,
        *,
        settings: UploadSettings,
        config: CamerasConfig,
        uploader: Uploader,
        health_reporter: UploaderHealth,
        clock: Callable[[], float] = timeutil.now,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._uploader = uploader
        self._health = health_reporter
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_outcome: UploadOutcome | None = None

    # -- policy ------------------------------------------------------------------------

    def policy_pass(self) -> int:
        """Mark clips from `retention.upload: false` cameras as `SKIPPED_BY_POLICY`."""
        skipped_cameras = [
            name
            for name, camera in self._config.cameras.items()
            if not camera.retention.upload
        ]
        if not skipped_cameras:
            return 0
        conn = db.open_db(self._settings.db_path)
        try:
            changed = db.skip_by_policy(
                conn, cameras=skipped_cameras, now=self._clock()
            )
            if changed:
                logger.info(
                    "clips skipped by policy",
                    extra={
                        "count": changed,
                        "reason": ",".join(sorted(skipped_cameras)),
                    },
                )
            return changed
        finally:
            conn.close()

    # -- workers -----------------------------------------------------------------------

    def _worker_loop(self, index: int, stop: threading.Event) -> None:
        """One claim→upload→record loop, owning its own connection.

        Its own connection rather than a shared one: SQLite connections are not safe for
        concurrent use, and "each thread owns its connection" is a rule that needs no
        discipline to keep.
        """
        conn = db.open_db(self._settings.db_path)
        owner = f"{socket.gethostname()}:{os.getpid()}:{index}"
        try:
            while not stop.is_set():
                outcome: UploadOutcome | None = None
                try:
                    outcome = self._uploader.upload_one(conn, owner=owner)
                except Exception:
                    # The outermost loop of a process meant to run for years: log and keep
                    # going. The lease on any claimed clip expires, so nothing is wedged.
                    logger.exception("upload worker failed; continuing")
                if outcome is not None:
                    self._last_outcome = outcome
                else:
                    self._idle(stop)
        finally:
            conn.close()

    def _idle(self, stop: threading.Event) -> None:
        """Sleep with jitter, so N idle workers do not wake in lock-step."""
        stop.wait(self._settings.scheduler_tick_s * self._rng.uniform(0.8, 1.2))

    # -- the run loop ------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        self.policy_pass()

        threads = [
            threading.Thread(
                target=self._worker_loop,
                args=(index, stop),
                name=f"upload-{index}",
                daemon=True,
            )
            for index in range(max(1, self._settings.upload_concurrency))
        ]
        for thread in threads:
            thread.start()
        logger.info(
            "uploading",
            extra={"count": len(threads), "host": self._settings.bucket},
        )

        last_policy = self._clock()
        last_summary = self._clock()
        while not stop.wait(1.0):
            now = self._clock()
            if now - last_policy >= POLICY_INTERVAL_S:
                self.policy_pass()
                last_policy = now
            if now - last_summary >= SUMMARY_INTERVAL_S:
                self._log_summary()
                last_summary = now

        for thread in threads:
            thread.join(timeout=self._settings.scheduler_tick_s + 1.0)

    def _log_summary(self) -> None:
        queue = self._health.summary()
        if queue is None:
            return
        logger.info(
            "upload backlog",
            extra={
                "count": queue.pending,
                "state": (
                    f"pending={queue.pending} retry={queue.retry_wait} "
                    f"failed={queue.failed_permanent} uploaded24h={queue.uploaded_24h}"
                ),
                "attempts": int(queue.oldest_pending_age_s or 0),
            },
        )


def _install_signal_handlers(stop: threading.Event) -> None:
    """Turn SIGTERM/SIGINT into a cooperative shutdown.

    Cooperative so that a `SIGTERM` does not interrupt a PUT half-way: the worker finishes the
    clip it is uploading, then exits. A killed upload leaves no lease held in practice — the
    lease simply expires — but finishing means the next retry is immediate rather than
    fifteen minutes away.
    """

    def request_stop(signum: int, _frame: object) -> None:
        # Logged once. `timeout`, Docker and a shell can each deliver the signal to both the
        # process and its group, and two "shutdown requested" lines for one shutdown reads
        # like two shutdowns.
        if not stop.is_set():
            logger.info(
                "shutdown requested", extra={"reason": signal.Signals(signum).name}
            )
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)


def build_health_reporter(
    *, settings: UploadSettings, uploader: Uploader, adapter: StorageAdapter | None
) -> UploaderHealth:
    return UploaderHealth(
        db_path=settings.db_path,
        data_dir=settings.data_dir,
        adapter=adapter,
        settings=settings,
        uploader=uploader,
        started_at=timeutil.now(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """LLD 00 §3's service skeleton: configure, validate, serve health, then work."""
    del argv  # every input is an environment variable; see UploadSettings

    try:
        settings = UploadSettings.from_environ()
    except ConfigError as exc:
        print(f"cctv-uploader: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        configure_logging(settings.log_level)
    except ValueError as exc:
        print(f"cctv-uploader: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    problem = settings.configuration_problem()
    if problem is not None:
        logger.error("configuration is unusable", extra={"reason": problem})
        return EXIT_CONFIG

    logger.info(
        "starting",
        extra={
            "host": socket.gethostname(),
            "state": "disabled" if not settings.storage_configured else "configured",
        },
    )

    try:
        config = load_cameras_config(settings.cameras_path)
    except (ConfigError, OSError) as exc:
        logger.error(
            "configuration is unusable",
            extra={"path": str(settings.cameras_path), "reason": str(exc)},
        )
        return EXIT_CONFIG

    for warning in config.warnings:
        logger.warning(warning)

    try:
        conn = db.open_db(settings.db_path)
        version = db.migrate(conn)
        db.integrity_check(conn)
    except (db.DbError, sqlite3.Error) as exc:
        logger.error(
            "state store is unusable",
            extra={"path": str(settings.db_path), "reason": str(exc)},
        )
        return EXIT_UNAVAILABLE

    logger.info("state store ready", extra={"state": f"schema-{version}"})

    adapter = build_adapter(settings)
    if adapter is None:
        logger.warning(
            "CCTV_STORAGE_BUCKET is unset: cloud backup is disabled and clips stay local. "
            "This is a supported choice, and the health endpoint reports DISABLED."
        )
    else:
        # A missing SDK is a configuration error, not a runtime one: every clip would fail
        # the same way, and the message is about an install, not about the network.
        probe = adapter.healthcheck()
        if isinstance(probe.error, storage.StorageBackendMissingError):
            logger.error("storage backend is unusable", extra={"reason": probe.detail})
            return EXIT_CONFIG
        try:
            adapter.ensure_bucket()
        except StorageError as exc:
            # Not fatal: the health check reports it, and a bucket can appear later.
            logger.warning("could not confirm the bucket", extra={"reason": str(exc)})

    uploader = Uploader(
        data_dir=settings.data_dir,
        location=config.site.location,
        adapter=adapter,
        settings=settings,
    )
    reporter = build_health_reporter(
        settings=settings, uploader=uploader, adapter=adapter
    )

    stop = threading.Event()
    _install_signal_handlers(stop)

    server = None
    if settings.health_port > 0:
        server, _thread = health.start_health_server(
            port=settings.health_port,
            host=settings.health_host,
            snapshot=reporter.report,
            admin=admin_routes(settings=settings),
            token=settings.internal_token,
            gauges=reporter.gauges,
        )
        logger.info(
            "health endpoint listening",
            extra={"host": settings.health_host, "count": settings.health_port},
        )
        if not settings.internal_token:
            logger.warning(
                "INTERNAL_API_TOKEN is unset, so the admin endpoints are not mounted"
            )
    else:
        logger.warning("no health endpoint: CCTV_UPLOADER_HEALTH_PORT is 0")

    service = UploadService(
        settings=settings, config=config, uploader=uploader, health_reporter=reporter
    )

    try:
        if settings.dry_run:
            logger.warning("DRY_RUN is set: nothing will be uploaded")
            while not stop.wait(1.0):
                pass
        elif adapter is None:
            # Nothing to do, but the container stays up and reports DISABLED, so a
            # local-only install is a stable state rather than a crash loop.
            while not stop.wait(1.0):
                pass
        else:
            service.run(stop)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        conn.close()
        logger.info("stopped")

    return EXIT_OK


def _size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
