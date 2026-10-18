"""The state store: one SQLite database, and the only coordination between services.

Services never call each other. They coordinate exclusively through these tables, which
is what makes any one of them being down a survivable condition rather than a cascading
failure. See [`internal/architecture/lld/03-state-store.md`] and the schema contract.

Three rules from that LLD are enforced here rather than left to discipline:

1. **No network I/O inside a transaction.** A slow Frigate response or a 30-second B2
   upload holding the write lock stalls every other writer. The accessors below are
   deliberately shaped to make the safe pattern the easy one: `claim_next_upload` commits
   before it returns, and `mark_uploaded` opens its own short transaction afterwards.
2. **`BEGIN IMMEDIATE`, never deferred.** A deferred transaction that reads and then
   writes can fail on lock upgrade and cannot be safely retried mid-transaction.
3. **Every state transition is validated.** The contract lists the legal edges; every
   other one raises. An illegal transition means a logic bug or database tampering, and
   silently accepting it would corrupt the record that INV-1 depends on.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import timeutil
from .timeutil import Instant

__all__ = [
    "CLIP_STATES",
    "FINDING_KINDS",
    "LOCAL_EXPIRY_STATES",
    "MAX_ATTEMPTS",
    "STATE_ORDER",
    "ClipRecord",
    "DbError",
    "IllegalTransitionError",
    "MigrationChecksumError",
    "OverdueSummary",
    "QueueSummary",
    "SchemaTooNewError",
    "claim_next_upload",
    "counts_by_state",
    "ensure_camera",
    "finish_upload_attempt",
    "get_watermark",
    "insert_clip",
    "integrity_check",
    "known_cameras",
    "known_review_ids",
    "last_successful_upload_at",
    "local_expiry_batch",
    "mark_local_deleted",
    "migrate",
    "open_db",
    "overdue_local",
    "queue_summary",
    "record_event",
    "record_finding",
    "release_lease",
    "reset_failed_permanent",
    "schema_version",
    "set_clip_checksum",
    "set_watermark",
    "skip_by_policy",
    "stored_clip_checks",
    "write_txn",
]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: Every legal `upload_state`. The CHECK in migration 0001 carries the same set; this is
#: the copy the accessors validate against so a bad name fails before SQLite sees it.
CLIP_STATES = frozenset(
    {
        "PENDING",
        "UPLOADING",
        "RETRY_WAIT",
        "UPLOADED",
        "FAILED_PERMANENT",
        "SKIPPED_BY_POLICY",
        "DELETED_LOCAL",
        "EXPIRED_CLOUD",
        "FORCE_DELETED",
    }
)

#: The transition table from the contract §3.2, verbatim. Any edge not listed raises.
VALID_TRANSITIONS = frozenset(
    {
        ("PENDING", "UPLOADING"),
        ("UPLOADING", "UPLOADED"),
        ("UPLOADING", "PENDING"),  # lease expired
        ("UPLOADING", "RETRY_WAIT"),
        ("UPLOADING", "FAILED_PERMANENT"),
        ("RETRY_WAIT", "PENDING"),
        ("RETRY_WAIT", "FAILED_PERMANENT"),
        ("PENDING", "FAILED_PERMANENT"),
        ("PENDING", "SKIPPED_BY_POLICY"),
        ("UPLOADED", "DELETED_LOCAL"),
        ("UPLOADED", "EXPIRED_CLOUD"),
        ("FAILED_PERMANENT", "FORCE_DELETED"),
        ("SKIPPED_BY_POLICY", "DELETED_LOCAL"),
    }
)

#: States a clip can never leave. Re-marking one is a no-op rather than an error, because
#: a crash between an upload and its record must be recoverable by simply redoing it.
TERMINAL_STATES = frozenset({"DELETED_LOCAL", "EXPIRED_CLOUD", "FORCE_DELETED"})

#: Retries before a transient failure becomes permanent. See lld/04 §5.1.
#:
#: Twelve attempts with the documented backoff is roughly five hours of retrying: long
#: enough to ride out a provider incident or an evening's internet outage, short enough that
#: a genuinely broken configuration surfaces the next morning rather than the next week.
MAX_ATTEMPTS = 12


class DbError(RuntimeError):
    """Anything wrong with the database itself: corruption, a bad migration, misuse."""


class SchemaTooNewError(DbError):
    """The database was written by a newer build. Downgrades are refused, not attempted.

    Refusing is the point ([INV-8]): a newer schema may hold columns and rows this build
    would silently ignore, and "ignore" here means losing upload state.
    """


class MigrationChecksumError(DbError):
    """A migration file changed after it was applied.

    That means someone edited history, and the database no longer describes how it was
    built. Guessing which version is right is worse than refusing.
    """


class IllegalTransitionError(DbError):
    """A state change the contract does not allow."""


# ---------------------------------------------------------------------------------------
# Connection and transaction policy — contract §1.1, LLD §2 and §3
# ---------------------------------------------------------------------------------------

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",  # readers never block the writer
    "PRAGMA synchronous = FULL",  # durability over throughput; we are low-rate
    "PRAGMA foreign_keys = ON",  # off by default in SQLite, which surprises people
    "PRAGMA busy_timeout = 5000",  # wait rather than fail on a brief lock
    "PRAGMA wal_autocheckpoint = 1000",
)


def open_db(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with the project's connection policy applied.

    `readonly=True` uses SQLite's `mode=ro`, so an observer physically cannot write. That
    matters for `status-collector`: it is a structural guarantee that monitoring never
    mutates the thing it monitors, rather than a promise not to.

    Note that `journal_mode = WAL` is a no-op for `:memory:`. Tests that care about WAL
    use a temporary file, because otherwise they would be asserting nothing.
    """
    path = Path(path)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        f"file:{path}?mode={'ro' if readonly else 'rwc'}",
        uri=True,
        isolation_level=None,  # explicit transaction control; see write_txn
        timeout=5.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    for pragma in _PRAGMAS:
        if readonly and pragma.startswith("PRAGMA journal_mode"):
            continue  # cannot set journal mode on a read-only connection
        conn.execute(pragma)

    return conn


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that takes the lock immediately.

    **Nothing slow may happen inside this block.** No HTTP, no upload, no `ffprobe`.
    Claim, commit, work, complete.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---------------------------------------------------------------------------------------
# Migrations — LLD §4
# ---------------------------------------------------------------------------------------


def _split_statements(sql: str) -> list[str]:
    """Split a migration into statements without `executescript`.

    `sqlite3.Connection.executescript` issues an implicit COMMIT before it runs, which
    would silently break the per-migration atomicity the LLD requires. So statements are
    executed one at a time inside our own transaction instead.

    Comment lines are dropped before accumulating, which is also what makes a `;` inside a
    comment harmless.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise DbError(
            "A migration ends with an incomplete statement. That usually means a missing "
            "semicolon, and it is better to fail here than to half-apply a schema change."
        )
    return statements


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _discover(migrations_dir: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        prefix = path.stem.split("_", 1)[0]
        if not prefix.isdigit():
            raise DbError(f"{path.name}: migration files must be named NNNN_description.sql")
        version = int(prefix)
        if version in found:
            raise DbError(f"Two migrations claim version {version}: {path.name} and more.")
        found[version] = path
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration, or 0 for an empty database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"])


def migrate(
    conn: sqlite3.Connection,
    *,
    migrations_dir: Path | None = None,
    backup_dir: Path | None = None,
) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    Runs before any service serves traffic. Each migration is checksummed, optionally
    backed up, and applied inside its own transaction, so a partial migration cannot
    exist.

    Raises:
        SchemaTooNewError: the database is from a newer build.
        MigrationChecksumError: an applied migration file has changed.
        DbError: a statement failed, or the files are malformed.
    """
    migrations_dir = MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    available = _discover(migrations_dir)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL,
            checksum   TEXT NOT NULL
        )
        """)

    applied = {
        int(row["version"]): row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    newest_available = max(available, default=0)
    newest_applied = max(applied, default=0)

    if newest_applied > newest_available:
        raise SchemaTooNewError(
            f"The database is at schema version {newest_applied} but this build only knows "
            f"up to {newest_available}. Refusing to start: a newer schema may contain state "
            f"this build would silently ignore, and that state is upload progress (INV-8). "
            f"Upgrade the software, or restore a backup taken before the newer version ran."
        )

    for version in sorted(available):
        if version in applied:
            if applied[version] != _checksum(available[version].read_text(encoding="utf-8")):
                raise MigrationChecksumError(
                    f"{available[version].name} has changed since it was applied. An applied "
                    f"migration is history and must never be edited — add a new one instead."
                )
            continue

        sql = available[version].read_text(encoding="utf-8")

        if backup_dir is not None:
            backup_to(conn, backup_dir / f"pre-{version:04d}.db")

        try:
            with write_txn(conn):
                # Re-check *inside* the write transaction. Two services start together on a
                # fresh install (the clip-worker and the uploader both open this database),
                # and the pre-loop read above is outside any transaction — so without this,
                # both would try to insert the same `schema_migrations` row and one would
                # fail at startup. `BEGIN IMMEDIATE` serialises them, which makes this read
                # authoritative.
                peer = conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if peer is not None:
                    if peer["checksum"] != _checksum(sql):
                        raise MigrationChecksumError(
                            f"{available[version].name} has changed since it was applied."
                        )
                    continue

                for statement in _split_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, checksum) "
                    "VALUES (?, ?, ?)",
                    (version, timeutil.now(), _checksum(sql)),
                )
        except sqlite3.Error as exc:
            raise DbError(f"Migration {available[version].name} failed: {exc}") from exc

    return newest_available


def backup_to(conn: sqlite3.Connection, destination: str | Path) -> Path:
    """Online backup. Safe while other connections are reading and writing."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def integrity_check(conn: sqlite3.Connection) -> None:
    """Verify structure and referential integrity. Called at startup (LLD R5)."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise DbError(f"PRAGMA integrity_check failed: {row[0] if row else 'no result'}")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        detail = ", ".join(f"{v[0]}→{v[2]}" for v in violations[:5])
        raise DbError(
            f"PRAGMA foreign_key_check found {len(violations)} violation(s): {detail}. "
            f"A dangling reference means a row was written while foreign keys were off."
        )


# ---------------------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClipRecord:
    """A clip about to be recorded. Everything the `clips` row needs.

    The quality fields (`requested_*`, `coverage_ratio`, `max_gap_s`, `fetch_attempts`)
    exist so that INV-14 is auditable. A truncated clip and a perfect clip are both valid
    MP4s that play and upload fine; without these columns, "are the clips right?" is an
    opinion rather than a query.
    """

    review_id: str
    camera: str
    severity: str
    start_time: Instant
    end_time: Instant
    path: str
    event_ids: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    label: str | None = None
    duration_s: float | None = None
    requested_start_ts: float | None = None
    requested_end_ts: float | None = None
    pre_roll_s: float | None = None
    post_roll_s: float | None = None
    max_gap_s: float | None = None
    has_gaps: bool = False
    has_audio: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    fetch_attempts: int = 0
    degraded: bool = False
    long_clip: bool = False
    size_bytes: int | None = None
    sha256: str | None = None
    sidecar_path: str | None = None
    materialised_at: Instant | None = None
    upload_state: str = "PENDING"

    @property
    def requested_s(self) -> float | None:
        if self.requested_start_ts is None or self.requested_end_ts is None:
            return None
        return self.requested_end_ts - self.requested_start_ts

    @property
    def coverage_ratio(self) -> float | None:
        requested = self.requested_s
        if requested is None or requested <= 0 or self.duration_s is None:
            return None
        return self.duration_s / requested


def _validate_clip(clip: ClipRecord) -> None:
    if not clip.review_id:
        raise DbError("A clip needs a review_id: it is the dedup key (ADR-0016).")
    if clip.severity not in {"alert", "detection"}:
        raise DbError(f"severity must be 'alert' or 'detection', got {clip.severity!r}.")
    if not clip.path:
        raise DbError("A clip needs a path.")
    if clip.path.startswith("/"):
        raise DbError(
            f"path must be relative to DATA_DIR, got {clip.path!r}. An absolute path breaks "
            f"restore to a different mount point."
        )
    if clip.upload_state not in CLIP_STATES:
        raise DbError(f"Unknown upload_state {clip.upload_state!r}.")
    if clip.end_time < clip.start_time:
        raise DbError("end_time is before start_time.")


def insert_clip(
    conn: sqlite3.Connection, clip: ClipRecord, *, now: Instant | None = None
) -> tuple[int, bool]:
    """Record a clip. Returns `(clip_id, created)`.

    **Idempotent by construction**, which is [INV-7] and the reason the schema has
    `UNIQUE(review_id)`. Delivering the same review five times produces one row, and
    `created=False` on the repeat deliveries — so the caller does not have to check first,
    and there is no window between a check and an insert.
    """
    _validate_clip(clip)
    now = timeutil.now() if now is None else now

    with write_txn(conn):
        row = conn.execute(
            """
            INSERT INTO clips (
                review_id, event_ids, severity, camera, label, zones,
                start_time, end_time, duration_s,
                requested_start_ts, requested_end_ts, requested_s, coverage_ratio,
                pre_roll_s, post_roll_s, max_gap_s,
                has_gaps, has_audio, video_codec, audio_codec, fetch_attempts,
                degraded, long_clip,
                path, sidecar_path, size_bytes, sha256,
                upload_state, attempts, materialised_at, created_at, updated_at
            ) VALUES (
                :review_id, :event_ids, :severity, :camera, :label, :zones,
                :start_time, :end_time, :duration_s,
                :requested_start_ts, :requested_end_ts, :requested_s, :coverage_ratio,
                :pre_roll_s, :post_roll_s, :max_gap_s,
                :has_gaps, :has_audio, :video_codec, :audio_codec, :fetch_attempts,
                :degraded, :long_clip,
                :path, :sidecar_path, :size_bytes, :sha256,
                :upload_state, 0, :materialised_at, :created_at, :updated_at
            )
            ON CONFLICT (review_id) DO NOTHING
            RETURNING id
            """,
            {
                "review_id": clip.review_id,
                "event_ids": json.dumps(list(clip.event_ids)),
                "severity": clip.severity,
                "camera": clip.camera,
                "label": clip.label,
                "zones": json.dumps(list(clip.zones)),
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "duration_s": clip.duration_s,
                "requested_start_ts": clip.requested_start_ts,
                "requested_end_ts": clip.requested_end_ts,
                "requested_s": clip.requested_s,
                "coverage_ratio": clip.coverage_ratio,
                "pre_roll_s": clip.pre_roll_s,
                "post_roll_s": clip.post_roll_s,
                "max_gap_s": clip.max_gap_s,
                "has_gaps": int(clip.has_gaps),
                "has_audio": int(clip.has_audio),
                "video_codec": clip.video_codec,
                "audio_codec": clip.audio_codec,
                "fetch_attempts": clip.fetch_attempts,
                "degraded": int(clip.degraded),
                "long_clip": int(clip.long_clip),
                "path": clip.path,
                "sidecar_path": clip.sidecar_path,
                "size_bytes": clip.size_bytes,
                "sha256": clip.sha256,
                "upload_state": clip.upload_state,
                "materialised_at": clip.materialised_at,
                "created_at": now,
                "updated_at": now,
            },
        ).fetchone()

        if row is not None:
            return int(row["id"]), True

        existing = conn.execute(
            "SELECT id FROM clips WHERE review_id = ?", (clip.review_id,)
        ).fetchone()
        # Not reachable in practice — ON CONFLICT DO NOTHING with no other constraint can
        # only return no row when review_id collided — but raising beats returning None.
        if existing is None:
            raise DbError(
                f"Insert of review {clip.review_id!r} affected no row and no existing row "
                f"was found. The database is in an unexpected state."
            )
        return int(existing["id"]), False


def ensure_camera(
    conn: sqlite3.Connection,
    *,
    name: str,
    display_name: str | None = None,
    enabled: bool = True,
    config_hash: str | None = None,
    now: Instant | None = None,
) -> None:
    """Register or update a camera.

    `first_seen_at` is never rewritten, and re-enabling clears `retired_at`. A camera that
    disappears from config is retired rather than deleted ([INV-13]) so its clips keep
    resolving.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        conn.execute(
            """
            INSERT INTO cameras (
                name, display_name, enabled, first_seen_at, config_hash, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                display_name = excluded.display_name,
                enabled      = excluded.enabled,
                retired_at   = CASE WHEN excluded.enabled = 1 THEN NULL ELSE cameras.retired_at END,
                config_hash  = excluded.config_hash,
                updated_at   = excluded.updated_at
            """,
            (name, display_name or name, int(enabled), now, config_hash, now),
        )


# ---------------------------------------------------------------------------------------
# Upload state machine — contract §3.2
# ---------------------------------------------------------------------------------------


def _state_of(conn: sqlite3.Connection, clip_id: int) -> str:
    row = conn.execute("SELECT upload_state FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if row is None:
        raise DbError(f"No clip with id {clip_id}.")
    return str(row["upload_state"])


def _require_transition(current: str, target: str, clip_id: int) -> None:
    if current == target and target in TERMINAL_STATES | {"UPLOADED"}:
        return  # idempotent re-marking; a crash must be recoverable by redoing the work
    if (current, target) not in VALID_TRANSITIONS:
        raise IllegalTransitionError(
            f"Clip {clip_id}: {current} -> {target} is not a legal transition "
            f"(contract §3.2). An illegal transition means a logic bug or tampering, and "
            f"accepting it would corrupt the state that INV-1 depends on."
        )


def claim_next_upload(
    conn: sqlite3.Connection,
    *,
    owner: str,
    lease_seconds: float,
    cloud_key_prefix: str,
    now: Instant | None = None,
) -> sqlite3.Row | None:
    """Claim the oldest eligible clip, or return `None`.

    Returns **after committing**. The caller then does the network work with no transaction
    held, and calls `mark_uploaded` or `mark_transient_failure` to open a new one. This is
    the single most important shape in the module.

    In one transaction it also promotes due retries and reclaims expired leases, so a
    previous worker being killed cannot wedge a clip.

    `attempts` is incremented here, not on failure, so a clip that crashes the worker
    repeatedly cannot be retried forever.
    """
    now = timeutil.now() if now is None else now
    lease_until = now + lease_seconds

    with write_txn(conn):
        conn.execute(
            """
            UPDATE clips
               SET upload_state = 'PENDING', last_error_class = NULL, updated_at = ?
             WHERE upload_state = 'RETRY_WAIT'
               AND next_attempt_at IS NOT NULL
               AND next_attempt_at <= ?
            """,
            (now, now),
        )
        conn.execute(
            """
            UPDATE clips
               SET upload_state = 'PENDING', lease_owner = NULL, lease_expires_at = NULL,
                   updated_at = ?
             WHERE upload_state = 'UPLOADING'
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at <= ?
            """,
            (now, now),
        )

        row = conn.execute(
            """
            UPDATE clips
               SET upload_state    = 'UPLOADING',
                   lease_owner     = ?,
                   lease_expires_at = ?,
                   attempts        = attempts + 1,
                   cloud_key       = COALESCE(cloud_key, ? || path),
                   updated_at      = ?
             WHERE id = (
                   SELECT id
                     FROM clips
                    WHERE upload_state = 'PENDING'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    -- Oldest first, by *event* time (contract §3.3). Ordering by id would
                    -- be FIFO by materialisation and would let a clip from a camera that
                    -- had one long outage age indefinitely behind newer ones.
                    ORDER BY start_time ASC, id ASC
                    LIMIT 1
             )
            RETURNING id, review_id, camera, severity, label, zones, event_ids,
                      path, sha256, size_bytes, cloud_key, attempts,
                      start_time, end_time, duration_s
            """,
            (owner, lease_until, cloud_key_prefix, now, now),
        ).fetchone()

        if row is None:
            return None

        conn.execute(
            """
            INSERT INTO upload_attempts (clip_id, attempt_no, started_at)
            VALUES (?, ?, ?)
            """,
            (row["id"], row["attempts"], now),
        )
        return row


def mark_uploaded(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    cloud_etag: str | None,
    cloud_size_bytes: int | None,
    now: Instant | None = None,
) -> None:
    """UPLOADING → UPLOADED, after the provider checksum has been verified."""
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        _require_transition(_state_of(conn, clip_id), "UPLOADED", clip_id)
        conn.execute(
            """
            UPDATE clips
               SET upload_state = 'UPLOADED', cloud_etag = ?, cloud_size_bytes = ?,
                   uploaded_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                   last_error_class = NULL, last_error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (cloud_etag, cloud_size_bytes, now, now, clip_id),
        )


def mark_transient_failure(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    error_class: str,
    message: str,
    next_attempt_at: Instant,
    max_attempts: int = MAX_ATTEMPTS,
    now: Instant | None = None,
) -> str:
    """UPLOADING → RETRY_WAIT, or → FAILED_PERMANENT once attempts are exhausted.

    Returns the state actually entered, so the caller can alert on the difference rather
    than inferring it.

    `max_attempts` is a parameter rather than the module constant alone so the value is
    configurable per deployment without a code change, while the default stays the one the
    LLD documents.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        current = _state_of(conn, clip_id)
        _require_transition(current, "RETRY_WAIT", clip_id)

        attempts = int(
            conn.execute("SELECT attempts FROM clips WHERE id = ?", (clip_id,)).fetchone()[
                "attempts"
            ]
        )
        target = "FAILED_PERMANENT" if attempts >= max_attempts else "RETRY_WAIT"

        conn.execute(
            """
            UPDATE clips
               SET upload_state = ?, last_error_class = ?, last_error = ?,
                   next_attempt_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (target, error_class, message[:2000], next_attempt_at, now, clip_id),
        )

        if target == "FAILED_PERMANENT":
            record_event(
                conn,
                component="uploader",
                code="UPLOAD_ATTEMPTS_EXHAUSTED",
                severity="critical",
                message=f"Clip {clip_id} failed {attempts} upload attempts.",
                detail={"error_class": error_class},
                now=now,
            )
        return target


def mark_permanent_failure(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    error_class: str,
    message: str,
    now: Instant | None = None,
) -> None:
    """UPLOADING → FAILED_PERMANENT, for an error that retrying cannot fix.

    The local file stays. That is [INV-1]: a clip is never deleted before its upload
    succeeded, so a permanent failure holds storage and raises an alert rather than making
    room by destroying evidence.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        _require_transition(_state_of(conn, clip_id), "FAILED_PERMANENT", clip_id)
        conn.execute(
            """
            UPDATE clips
               SET upload_state = 'FAILED_PERMANENT', last_error_class = ?, last_error = ?,
                   lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (error_class, message[:2000], now, clip_id),
        )
        record_event(
            conn,
            component="uploader",
            code="UPLOAD_PERMANENT_FAILURE",
            severity="critical",
            message=f"Clip {clip_id} cannot be uploaded: {message[:200]}",
            detail={"error_class": error_class},
            now=now,
        )


# ---------------------------------------------------------------------------------------
# Sweep watermarks and the audit log
# ---------------------------------------------------------------------------------------


def get_watermark(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sweep_state WHERE name = ?", (name,)).fetchone()


def set_watermark(
    conn: sqlite3.Connection,
    *,
    name: str,
    watermark: Instant,
    succeeded: bool = True,
    note: str | None = None,
    now: Instant | None = None,
) -> float:
    """Advance a sweep watermark, or record a failure. Returns the stored watermark.

    **Monotonic.** The watermark can never move backwards, because that would mean
    re-notifying work already durably recorded — harmless — versus the failure mode it
    prevents, which is skipping work by advancing past something that failed.

    On failure the watermark does not move at all and `consecutive_failures` increments.
    The next run redoes the batch. Redoing is safe; skipping is not.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        conn.execute(
            """
            INSERT INTO sweep_state (name, watermark, last_run_at, last_success_at,
                                     consecutive_failures, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                watermark = CASE
                    WHEN ? = 1 THEN MAX(COALESCE(sweep_state.watermark, excluded.watermark),
                                        excluded.watermark)
                    ELSE sweep_state.watermark
                END,
                last_run_at = excluded.last_run_at,
                last_success_at = CASE WHEN ? = 1 THEN excluded.last_success_at
                                       ELSE sweep_state.last_success_at END,
                consecutive_failures = CASE WHEN ? = 1 THEN 0
                                            ELSE sweep_state.consecutive_failures + 1 END,
                notes = COALESCE(excluded.notes, sweep_state.notes)
            """,
            (
                name,
                watermark if succeeded else None,
                now,
                now if succeeded else None,
                0 if succeeded else 1,
                note,
                1 if succeeded else 0,
                1 if succeeded else 0,
                1 if succeeded else 0,
            ),
        )
        row = conn.execute("SELECT watermark FROM sweep_state WHERE name = ?", (name,)).fetchone()
        return float(row["watermark"]) if row and row["watermark"] is not None else 0.0


def record_event(
    conn: sqlite3.Connection,
    *,
    component: str,
    code: str,
    severity: str,
    message: str | None = None,
    detail: object | None = None,
    dedupe_key: str | None = None,
    now: Instant | None = None,
) -> None:
    """Append to the audit log. Doubles as the alert-deduplication store.

    `code` is a stable machine API — alerting rules reference it — so changing one is a
    breaking change.

    Assumes a transaction is already open, since every caller is mid-transition and
    nesting transactions is not possible. That is deliberate: an audit row and the state
    change it describes must commit together or not at all.
    """
    conn.execute(
        """
        INSERT INTO system_events (at, severity, component, code, message, detail, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timeutil.now() if now is None else now,
            severity,
            component,
            code,
            message,
            None if detail is None else json.dumps(detail),
            dedupe_key,
        ),
    )


def known_review_ids(conn: sqlite3.Connection, review_ids: Collection[str]) -> set[str]:
    """Which of these reviews already have a row.

    One indexed query rather than one per candidate: `UNIQUE(review_id)` is on the table
    precisely so this is a lookup and not a scan, and the sweep asks this question for
    every review it considers.
    """
    ids = list(review_ids)
    if not ids:
        return set()
    # SQLite's default limit on bound parameters is 999; a sweep window is far smaller, but
    # chunking here means a large `limit` cannot fail in a way that looks like a bug in the
    # sweep rather than in the caller's configuration.
    found: set[str] = set()
    for start in range(0, len(ids), 900):
        chunk = ids[start : start + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT review_id FROM clips WHERE review_id IN ({placeholders})", chunk
        )
        found.update(str(row["review_id"]) for row in rows)
    return found


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Clips per upload state. The dashboard's headline query, and the invariant check."""
    return {
        str(row["upload_state"]): int(row["n"])
        for row in conn.execute(
            "SELECT upload_state, COUNT(*) AS n FROM clips "
            "GROUP BY upload_state ORDER BY upload_state"
        )
    }


#: Kept for callers that need the documented state list in a stable order.
STATE_ORDER: Sequence[str] = tuple(sorted(CLIP_STATES))

#: The states in which the local file **must** exist. INV-1 expressed as a query: these are
#: the clips retention may not touch, and therefore the ones the uploader must still be able
#: to read. A missing file in one of these states is a real inconsistency, not a cache miss.
HELD_STATES: tuple[str, ...] = (
    "PENDING",
    "UPLOADING",
    "RETRY_WAIT",
    "FAILED_PERMANENT",
)


@dataclass(frozen=True, slots=True)
class QueueSummary:
    """The upload backlog, as the health checks and the status page need it.

    `oldest_pending_age_s` is the single most useful number the uploader produces
    (lld/04 §6). A *running* uploader with a six-hour-old backlog is not working, and a
    *stopped* uploader with an empty queue is a monitoring problem rather than a data
    problem. Process liveness cannot tell those apart; this number can.
    """

    counts: Mapping[str, int]
    oldest_pending_age_s: float | None
    uploaded_24h: int
    last_successful_upload_at: Instant | None

    def _n(self, state: str) -> int:
        return self.counts.get(state, 0)

    @property
    def pending(self) -> int:
        return self._n("PENDING")

    @property
    def uploading(self) -> int:
        return self._n("UPLOADING")

    @property
    def retry_wait(self) -> int:
        return self._n("RETRY_WAIT")

    @property
    def failed_permanent(self) -> int:
        return self._n("FAILED_PERMANENT")

    @property
    def skipped_by_policy(self) -> int:
        return self._n("SKIPPED_BY_POLICY")

    @property
    def uploaded(self) -> int:
        return self._n("UPLOADED")

    @property
    def held(self) -> int:
        """Clips still holding local storage because their upload has not succeeded."""
        return sum(self.counts.get(state, 0) for state in HELD_STATES)

    @property
    def stale_upload_s(self) -> float | None:
        """Seconds since the last successful upload, or `None` if there has never been one."""
        if self.last_successful_upload_at is None:
            return None
        return max(0.0, timeutil.now() - self.last_successful_upload_at)


def queue_summary(
    conn: sqlite3.Connection, *, now: Instant | None = None, window_s: float = 86400.0
) -> QueueSummary:
    """One snapshot of the backlog. Read-only: no transaction, no writes.

    Backlog age is measured from when the clip became *uploadable* rather than from the
    event's own start time — a clip materialised an hour after its event is not an hour
    behind on upload, and conflating the two would make the metric unactionable.
    """
    now = timeutil.now() if now is None else now

    oldest_row = conn.execute(
        "SELECT MIN(COALESCE(materialised_at, created_at)) AS oldest FROM clips "
        "WHERE upload_state = 'PENDING'"
    ).fetchone()
    oldest = None if oldest_row is None else oldest_row["oldest"]

    return QueueSummary(
        counts=counts_by_state(conn),
        oldest_pending_age_s=None if oldest is None else max(0.0, now - float(oldest)),
        uploaded_24h=int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM clips WHERE uploaded_at IS NOT NULL AND uploaded_at > ?",
                (now - window_s,),
            ).fetchone()["n"]
        ),
        last_successful_upload_at=last_successful_upload_at(conn),
    )


def set_clip_checksum(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    sha256: str,
    size_bytes: int,
    now: Instant | None = None,
) -> None:
    """Backfill a clip's digest and size.

    The clip-worker computes both at materialisation, so this is a repair path: an older row
    written before the column existed, or one whose file was re-hashed because the size on
    disk no longer matched what was recorded. The digest is what INV-7's idempotence check
    compares against the provider's, so a clip without one cannot be verified at all.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        conn.execute(
            "UPDATE clips SET sha256 = ?, size_bytes = ?, updated_at = ? WHERE id = ?",
            (sha256, size_bytes, now, clip_id),
        )


def finish_upload_attempt(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    attempt_no: int,
    outcome: str,
    http_status: int | None = None,
    bytes_sent: int | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    now: Instant | None = None,
) -> None:
    """Close the `upload_attempts` row that `claim_next_upload` opened.

    Append-only history is what turns "why is this clip stuck?" into a query rather than a
    guess. A failed update here is never allowed to fail the upload: the attempt row is
    diagnostics, and losing diagnostics must not lose an upload.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        conn.execute(
            """
            UPDATE upload_attempts
               SET finished_at = ?, outcome = ?, http_status = ?, bytes_sent = ?,
                   error_class = ?, error_message = ?, duration_ms = ?
             WHERE clip_id = ? AND attempt_no = ? AND finished_at IS NULL
            """,
            (
                now,
                outcome,
                http_status,
                bytes_sent,
                error_class,
                None if error_message is None else error_message[:2000],
                duration_ms,
                clip_id,
                attempt_no,
            ),
        )


def last_successful_upload_at(conn: sqlite3.Connection) -> Instant | None:
    """When an upload last succeeded, or `None` if one never has.

    `None` and "a long time ago" must stay distinguishable: on a fresh install the correct
    answer is "no upload has happened yet, and that is expected", not `STORAGE_STALE`.
    """
    row = conn.execute("SELECT MAX(uploaded_at) AS at FROM clips").fetchone()
    return None if row is None or row["at"] is None else float(row["at"])


def stored_clip_checks(conn: sqlite3.Connection, *, limit: int = 1000) -> list[sqlite3.Row]:
    """Rows whose local file must exist, for the `local_files_readable` health check.

    Bounded on purpose: the check exists to catch a mount that went away or a volume that
    was restored from the wrong snapshot, and the first N rows catch both. Scanning every
    held clip on every health request would turn a 2 ms check into a table scan.
    """
    placeholders = ",".join("?" * len(HELD_STATES))
    return conn.execute(
        f"SELECT id, path, camera, upload_state FROM clips "
        f"WHERE upload_state IN ({placeholders}) ORDER BY id LIMIT ?",
        (*HELD_STATES, limit),
    ).fetchall()


def skip_by_policy(
    conn: sqlite3.Connection, *, cameras: Collection[str], now: Instant | None = None
) -> int:
    """PENDING → SKIPPED_BY_POLICY for cameras with `retention.upload: false`.

    Done in bulk *before* claiming rather than per-clip: the state machine's only legal edge
    into `SKIPPED_BY_POLICY` is from `PENDING` (contract §3.2), so a clip that has already
    been claimed cannot be skipped — it can only be uploaded or failed.

    This never alerts. Not backing up a camera is a decision the operator made, and a
    notification about a deliberate choice is noise that trains people to ignore alerts.
    """
    names = list(cameras)
    if not names:
        return 0
    now = timeutil.now() if now is None else now
    placeholders = ",".join("?" * len(names))

    with write_txn(conn):
        changed = conn.execute(
            f"""
            UPDATE clips
               SET upload_state = 'SKIPPED_BY_POLICY', lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ?
             WHERE upload_state = 'PENDING'
               AND camera IN ({placeholders})
            """,
            (now, *names),
        ).rowcount
        if changed:
            record_event(
                conn,
                component="uploader",
                code="UPLOAD_SKIPPED_BY_POLICY",
                severity="info",
                message=(
                    f"{changed} clip(s) will not be backed up: their camera sets "
                    f"retention.upload: false."
                ),
                detail={"cameras": names, "count": changed},
                dedupe_key=f"skip_by_policy:{','.join(sorted(names))}",
                now=now,
            )
        return int(changed)


def release_lease(conn: sqlite3.Connection, *, clip_id: int, now: Instant | None = None) -> bool:
    """UPLOADING → PENDING on a clean shutdown, so the retry is immediate.

    Leases expire on their own, so this is an optimisation rather than a correctness
    requirement — but the difference for the operator is a clip that resumes in five
    seconds versus one that resumes in fifteen minutes.

    Returns whether anything was released. A clip that finished while the signal was being
    handled is left alone: undoing a verified upload would be far worse than a slow retry.
    """
    now = timeutil.now() if now is None else now
    with write_txn(conn):
        if _state_of(conn, clip_id) != "UPLOADING":
            return False
        _require_transition("UPLOADING", "PENDING", clip_id)
        conn.execute(
            "UPDATE clips SET upload_state = 'PENDING', lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (now, clip_id),
        )
        return True


def reset_failed_permanent(
    conn: sqlite3.Connection,
    *,
    reason: str,
    clip_id: int | None = None,
    camera: str | None = None,
    since: Instant | None = None,
    now: Instant | None = None,
) -> list[int]:
    """FAILED_PERMANENT → PENDING, for the documented recovery path.

    Returns the clip ids requeued. Two rules are enforced here rather than documented and
    hoped for:

    * **A reason is mandatory.** "Why did someone retry 400 uploads, and was that before or
      after the credentials were rotated?" is a question worth being able to answer.
    * **A filter is mandatory.** Retrying *every* permanently-failed clip with one call is
      how a mistyped command becomes a thundering herd against an endpoint that is still
      down. The operator names a clip, a camera, or a starting point.
    """
    if not reason or not reason.strip():
        raise DbError(
            "A reason is required to requeue failed uploads; it is written to the audit log."
        )
    if clip_id is None and camera is None and since is None:
        raise DbError(
            "Refusing to requeue every FAILED_PERMANENT clip at once. Give a clip_id, a "
            "camera, or a since — or fix the cause and let the next attempt happen by "
            "itself. See docs/recover-uploads.md."
        )

    now = timeutil.now() if now is None else now
    clauses = ["upload_state = 'FAILED_PERMANENT'"]
    params: list[object] = []
    if clip_id is not None:
        clauses.append("id = ?")
        params.append(clip_id)
    if camera is not None:
        clauses.append("camera = ?")
        params.append(camera)
    if since is not None:
        clauses.append("start_time >= ?")
        params.append(since)

    with write_txn(conn):
        ids = [
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM clips WHERE {' AND '.join(clauses)} ORDER BY id",
                params,
            )
        ]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""
                UPDATE clips
                   SET upload_state = 'PENDING', attempts = 0, next_attempt_at = NULL,
                       lease_owner = NULL, lease_expires_at = NULL,
                       last_error = NULL, last_error_class = NULL, updated_at = ?
                 WHERE id IN ({placeholders})
                """,
                (now, *ids),
            )
            record_event(
                conn,
                component="uploader",
                code="UPLOAD_RETRY_REQUESTED",
                severity="info",
                message=f"Operator requeued {len(ids)} permanently failed clip(s).",
                detail={
                    "reason": reason.strip(),
                    "count": len(ids),
                    "clip_ids": ids[:50],
                },
                now=now,
            )
        return ids


# ---------------------------------------------------------------------------------------
# Local retention — cleanup J1 (LLD 05 §2, §3). The only queries that authorise a deletion.
# ---------------------------------------------------------------------------------------

#: The upload states whose clips **may** be deleted locally, enumerated rather than negated.
#:
#: This is the primary guard behind [INV-1]. Written as `upload_state NOT IN ('PENDING', …)`
#: every state added later would be deletable by default; naming the permitted states means a
#: new one is safe until somebody deliberately adds it here and considers the consequences.
LOCAL_EXPIRY_STATES: frozenset[str] = frozenset({"UPLOADED", "SKIPPED_BY_POLICY"})

#: The finding kinds the schema accepts (migration 0002, `lld/05` §5.1). Kept here so a typo
#: fails with a message naming the accepted values rather than as a bare CHECK violation.
FINDING_KINDS: frozenset[str] = frozenset(
    {
        "orphan_object",
        "dangling_row",
        "checksum_mismatch",
        "unexpected_local_file",
        "missing_local_file",
        "key_mismatch",
    }
)


def known_cameras(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every camera ever seen, retired ones included ([INV-13]).

    Cleanup iterates this rather than the config: a camera removed from `cameras.yml` still
    has clips on disk, and they still expire. Skipping retired cameras would mean their clips
    live forever, which is the opposite of what INV-13 intends — the invariant preserves the
    *rows*, not the files.
    """
    return list(conn.execute("SELECT name, retired_at FROM cameras ORDER BY name"))


def local_expiry_batch(
    conn: sqlite3.Connection,
    *,
    camera: str,
    cutoff: Instant,
    limit: int,
    states: Collection[str] = LOCAL_EXPIRY_STATES,
    after: tuple[float, int] | None = None,
) -> list[sqlite3.Row]:
    """One page of clips eligible for local deletion for one camera, oldest first.

    Read-only, deliberately: the caller opens the write transaction only when it is about to
    act, so a page query never holds the write lock (contract §1.1).

    `local_deleted_at IS NULL` keeps a re-run idempotent, and ordering by `start_time` makes
    the oldest clip the first to go — the operator's expectation, and what leaves a coherent
    tree behind after a partial run.

    `after` is a keyset cursor: the `(start_time, id)` of the last row the caller handled.
    Paging **must** be keyset rather than "re-query and the set shrinks", because the set does
    not always shrink — a refused clip keeps its `local_deleted_at`, and under `DRY_RUN`
    nothing changes at all. A shrink-based loop would return the same page forever, which is
    an infinite loop rather than a slow run.
    """
    if not states:
        return []
    placeholders = ",".join("?" * len(states))
    params: list[object] = [camera, *sorted(states), cutoff]
    cursor = ""
    if after is not None:
        # Row-value comparison written longhand: `(a, b) > (?, ?)` needs SQLite 3.15+, and
        # this form is portable and unambiguous. `<=` ordering cannot drift because the
        # ORDER BY is on the same two columns.
        cursor = "AND (start_time > ? OR (start_time = ? AND id > ?))"
        params.extend([after[0], after[0], after[1]])
    params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT id, camera, path, sidecar_path, size_bytes, sha256,
                   upload_state, start_time, cloud_key, uploaded_at
              FROM clips
             WHERE camera = ?
               AND upload_state IN ({placeholders})
               AND start_time < ?
               AND local_deleted_at IS NULL
               {cursor}
             ORDER BY start_time ASC, id ASC
             LIMIT ?
            """,
            params,
        )
    )


def mark_local_deleted(
    conn: sqlite3.Connection, *, clip_id: int, now: Instant | None = None
) -> bool:
    """`UPLOADED`/`SKIPPED_BY_POLICY` → `DELETED_LOCAL`, and stamp `local_deleted_at`.

    **Assumes a transaction is already open**, like `record_event`: the row update and the
    audit record describing it must commit together, and the caller opens one transaction per
    batch (`lld/05` §3.3).

    Returns whether anything changed. A row that already carries `local_deleted_at` is left
    alone — a crash between the unlink and the commit simply means the work is redone, and
    redoing it must not be an error.
    """
    now = timeutil.now() if now is None else now
    row = conn.execute(
        "SELECT upload_state, local_deleted_at FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    if row is None:
        raise DbError(f"No clip with id {clip_id}.")
    if row["local_deleted_at"] is not None:
        return False
    _require_transition(str(row["upload_state"]), "DELETED_LOCAL", clip_id)
    conn.execute(
        "UPDATE clips SET upload_state = 'DELETED_LOCAL', local_deleted_at = ?, updated_at = ? "
        "WHERE id = ?",
        (now, now, clip_id),
    )
    return True


def record_finding(
    conn: sqlite3.Connection,
    *,
    kind: str,
    subject: str,
    detail: object | None = None,
    now: Instant | None = None,
) -> None:
    """Append a reconciliation finding. Assumes a transaction is already open.

    A finding is a **report**, never a repair ([INV-10]): resolving one takes an explicit,
    audited operator action. J1 records exactly one kind — `missing_local_file`, when a row
    points at footage that is already gone — because that state is not a guess, it is a fact
    observed while trying to act.
    """
    if kind not in FINDING_KINDS:
        raise DbError(
            f"{kind!r} is not a finding kind. The documented kinds are "
            f"{', '.join(sorted(FINDING_KINDS))} (lld/05 §5.1)."
        )
    conn.execute(
        "INSERT INTO reconciliation_findings (run_at, kind, subject, detail) VALUES (?, ?, ?, ?)",
        (
            timeutil.now() if now is None else now,
            kind,
            subject,
            None if detail is None else json.dumps(detail),
        ),
    )


@dataclass(frozen=True, slots=True)
class OverdueSummary:
    """Clips held past local retention because their upload is not terminal-success.

    The honest side of [INV-1]: a clip is held rather than deleted, and this is the count and
    the cost. See `lld/05` §7.
    """

    count: int = 0
    bytes_held: int = 0
    oldest_start_time: Instant | None = None
    #: How long past its retention deadline the oldest held clip is. This — not the clip's
    #: age — is what `lld/05` §9's "older than 30 days" threshold is about: a 100-day-old clip
    #: on a 90-day window has been held for 10 days, not 100.
    oldest_overdue_s: float | None = None


def overdue_local(conn: sqlite3.Connection, *, camera: str, cutoff: Instant) -> OverdueSummary:
    """Count the clips one camera is holding past its retention window.

    This filter **is** a negation, unlike the deletion query, and that is safe for the same
    reason it is unsafe there: nothing is deleted from this result. The question is "what is
    not finished?", and a state added later should count as unfinished until someone decides
    otherwise.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(size_bytes), 0) AS bytes,
               MIN(start_time) AS oldest
          FROM clips
         WHERE camera = ?
           AND upload_state NOT IN ('UPLOADED', 'SKIPPED_BY_POLICY')
           AND start_time < ?
           AND local_deleted_at IS NULL
        """,
        (camera, cutoff),
    ).fetchone()
    return OverdueSummary(
        count=int(row["n"]),
        bytes_held=int(row["bytes"]),
        oldest_start_time=None if row["oldest"] is None else float(row["oldest"]),
        oldest_overdue_s=(
            None if row["oldest"] is None else max(0.0, cutoff - float(row["oldest"]))
        ),
    )
