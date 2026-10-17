"""Tests for the state store. INV-1, INV-4, INV-7, INV-8 and the upload state machine."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from cctv import db
from cctv.db import (
    ClipRecord,
    DbError,
    IllegalTransitionError,
    MigrationChecksumError,
    SchemaTooNewError,
)

NOW = 1_700_000_000.0


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.open_db(tmp_path / "state.db")
    db.migrate(connection)
    db.ensure_camera(connection, name="front-door", display_name="Front Door", now=NOW)
    yield connection
    connection.close()


def make_clip(**overrides: object) -> ClipRecord:
    fields: dict[str, object] = {
        "review_id": "1718987129.308396-fqk5ka",
        "camera": "front-door",
        "severity": "alert",
        "start_time": NOW,
        "end_time": NOW + 30,
        "path": "front-door/2026/09/12/18-45-00.mp4",
    }
    fields.update(overrides)
    return ClipRecord(**fields)  # type: ignore[arg-type]


def claim(conn: sqlite3.Connection, *, now: float = NOW, lease: float = 60.0):
    return db.claim_next_upload(
        conn,
        owner="host:1",
        lease_seconds=lease,
        cloud_key_prefix="parents-home/clips/",
        now=now,
    )


# --------------------------------------------------------------------------------------
# Connection policy (contract §1.1)
# --------------------------------------------------------------------------------------


def test_open_db_applies_the_documented_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 is FULL. We are a low-rate workload where durability beats throughput.
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_readonly_connection_physically_cannot_write(tmp_path: Path) -> None:
    """The guarantee that status-collector never mutates what it observes."""
    path = tmp_path / "state.db"
    writable = db.open_db(path)
    db.migrate(writable)
    writable.close()

    readonly = db.open_db(path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute(
                "INSERT INTO system_events (at, severity, component, code) "
                "VALUES (1, 'info', 'test', 'X')"
            )
    finally:
        readonly.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    connection = db.open_db(tmp_path / "state.db")
    db.migrate(connection)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_clip(connection, make_clip(), now=NOW)
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# Migrations (LLD §4)
# --------------------------------------------------------------------------------------


def write_migration(directory: Path, version: int, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version:04d}_test.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def newest_schema_version() -> int:
    """The highest migration on disk.

    Derived rather than hardcoded so that adding a migration does not mean editing every
    test that happens to mention the schema version.
    """
    return max(int(path.stem.split("_", 1)[0]) for path in db.MIGRATIONS_DIR.glob("*.sql"))


def test_migrating_an_empty_database_reaches_the_newest_version(tmp_path: Path) -> None:
    connection = db.open_db(tmp_path / "state.db")
    try:
        assert db.schema_version(connection) == 0
        newest = newest_schema_version()
        assert db.migrate(connection) == newest
        assert db.schema_version(connection) == newest
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"clips", "cameras", "upload_attempts", "sweep_state"} <= tables
    finally:
        connection.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    connection = db.open_db(tmp_path / "state.db")
    try:
        db.migrate(connection)
        applied = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        version = db.schema_version(connection)

        db.migrate(connection)

        # Asserted against the first run's numbers rather than a literal, so a new migration
        # is a migration, not a test edit.
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == applied
        assert db.schema_version(connection) == version
        assert applied == len(list(db.MIGRATIONS_DIR.glob("*.sql")))
    finally:
        connection.close()


def test_integrity_check_passes_after_migration(conn: sqlite3.Connection) -> None:
    db.integrity_check(conn)


def test_a_newer_database_is_refused_not_downgraded(conn: sqlite3.Connection) -> None:
    """INV-8: ignoring columns a newer build wrote means losing upload progress."""
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (99, ?, 'x')",
        (NOW,),
    )
    with pytest.raises(SchemaTooNewError, match="Refusing to start"):
        db.migrate(conn)


def test_editing_an_applied_migration_is_refused(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, 1, "CREATE TABLE thing (id INTEGER PRIMARY KEY);")
    connection = db.open_db(tmp_path / "state.db")
    try:
        db.migrate(connection, migrations_dir=migrations)
        write_migration(migrations, 1, "CREATE TABLE thing (id INTEGER PRIMARY KEY, extra TEXT);")
        with pytest.raises(MigrationChecksumError, match="must never be edited"):
            db.migrate(connection, migrations_dir=migrations)
    finally:
        connection.close()


def test_a_failing_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    """Each migration is atomic, so a half-applied schema cannot exist."""
    migrations = tmp_path / "migrations"
    write_migration(
        migrations,
        1,
        "CREATE TABLE good (id INTEGER PRIMARY KEY);\nCREATE TABLE bad (this is not sql);\n",
    )
    connection = db.open_db(tmp_path / "state.db")
    try:
        with pytest.raises(DbError, match=re.escape("0001_test.sql failed")):
            db.migrate(connection, migrations_dir=migrations)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "good" not in tables
        assert db.schema_version(connection) == 0
    finally:
        connection.close()


def test_migration_failure_message_names_the_file(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, 1, "CREATE TABLE bad (this is not sql);")
    connection = db.open_db(tmp_path / "state.db")
    try:
        with pytest.raises(DbError) as excinfo:
            db.migrate(connection, migrations_dir=migrations)
        assert "0001_test.sql" in str(excinfo.value)
    finally:
        connection.close()


def test_a_pre_migration_backup_is_written(tmp_path: Path) -> None:
    """INV-8: a migration is not run without something to fall back to."""
    migrations = tmp_path / "migrations"
    write_migration(migrations, 1, "CREATE TABLE one (id INTEGER PRIMARY KEY);")
    write_migration(migrations, 2, "CREATE TABLE two (id INTEGER PRIMARY KEY);")
    connection = db.open_db(tmp_path / "state.db")
    try:
        db.migrate(connection, migrations_dir=migrations, backup_dir=tmp_path / "backups")
    finally:
        connection.close()
    assert (tmp_path / "backups" / "pre-0002.db").exists()


def test_comments_containing_semicolons_do_not_split_statements(tmp_path: Path) -> None:
    """The naive splitter would break here, which is why `executescript` is not used."""
    migrations = tmp_path / "migrations"
    write_migration(
        migrations,
        1,
        "-- drop everything; seriously\nCREATE TABLE real (id INTEGER PRIMARY KEY);\n",
    )
    connection = db.open_db(tmp_path / "state.db")
    try:
        db.migrate(connection, migrations_dir=migrations)
        assert (
            connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'real'").fetchone()[
                0
            ]
            == 1
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# Clips, and the idempotency that INV-7 rests on
# --------------------------------------------------------------------------------------


def test_insert_clip_returns_the_new_id(conn: sqlite3.Connection) -> None:
    clip_id, created = db.insert_clip(conn, make_clip(), now=NOW)
    assert created is True
    assert clip_id > 0


def test_delivering_the_same_review_five_times_creates_one_row(
    conn: sqlite3.Connection,
) -> None:
    """INV-7 as a schema property, not a promise."""
    results = [db.insert_clip(conn, make_clip(), now=NOW) for _ in range(5)]
    ids = {clip_id for clip_id, _ in results}
    assert len(ids) == 1
    assert [created for _, created in results] == [True, False, False, False, False]
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1


def test_derived_quality_columns_are_computed(conn: sqlite3.Connection) -> None:
    """INV-14 has to be queryable, or it is only an opinion."""
    clip_id, _ = db.insert_clip(
        conn,
        make_clip(duration_s=29.0, requested_start_ts=NOW - 5, requested_end_ts=NOW + 40),
        now=NOW,
    )
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["requested_s"] == pytest.approx(45.0)
    assert row["coverage_ratio"] == pytest.approx(29.0 / 45.0)
    assert row["review_id"] == "1718987129.308396-fqk5ka"


def test_a_truncated_clip_is_recorded_with_a_low_coverage_ratio(
    conn: sqlite3.Connection,
) -> None:
    clip_id, _ = db.insert_clip(
        conn,
        make_clip(duration_s=12.0, requested_start_ts=NOW - 5, requested_end_ts=NOW + 40),
        now=NOW,
    )
    row = conn.execute(
        "SELECT coverage_ratio, degraded FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    assert row["coverage_ratio"] < 0.95


def test_event_ids_and_zones_survive_as_json(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(
        conn,
        make_clip(event_ids=("a-1", "b-2"), zones=("porch",)),
        now=NOW,
    )
    row = conn.execute("SELECT event_ids, zones FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["event_ids"] == '["a-1", "b-2"]'
    assert row["zones"] == '["porch"]'


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"review_id": ""}, "dedup key"),
        ({"severity": "motion"}, "severity"),
        ({"path": ""}, "needs a path"),
        ({"path": "/abs/clip.mp4"}, "relative to DATA_DIR"),
        ({"upload_state": "DONE"}, "upload_state"),
        ({"end_time": NOW - 1}, "end_time is before"),
    ],
)
def test_invalid_clips_are_refused(conn: sqlite3.Connection, overrides: dict, match: str) -> None:
    with pytest.raises(DbError, match=match):
        db.insert_clip(conn, make_clip(**overrides), now=NOW)


def test_a_closed_state_set_rejects_a_typo(conn: sqlite3.Connection) -> None:
    """A typo must fail at write time, not create a row no query will ever find."""
    db.insert_clip(conn, make_clip(), now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE clips SET upload_state = 'UPLOADEED' WHERE id = 1")


def test_camera_first_seen_at_is_not_rewritten(conn: sqlite3.Connection) -> None:
    db.ensure_camera(conn, name="front-door", display_name="Renamed", now=NOW + 500)
    row = conn.execute("SELECT * FROM cameras WHERE name = 'front-door'").fetchone()
    assert row["first_seen_at"] == NOW
    assert row["display_name"] == "Renamed"


def test_re_enabling_a_camera_clears_retired_at(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE cameras SET retired_at = ? WHERE name = 'front-door'", (NOW,))
    db.ensure_camera(conn, name="front-door", enabled=True, now=NOW + 10)
    assert (
        conn.execute("SELECT retired_at FROM cameras WHERE name = 'front-door'").fetchone()[
            "retired_at"
        ]
        is None
    )


# --------------------------------------------------------------------------------------
# The claim query (contract §3.3)
# --------------------------------------------------------------------------------------


def test_claim_sets_state_lease_and_cloud_key(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(), now=NOW)
    row = claim(conn)
    assert row is not None
    assert row["attempts"] == 1

    stored = conn.execute("SELECT * FROM clips WHERE id = ?", (row["id"],)).fetchone()
    assert stored["upload_state"] == "UPLOADING"
    assert stored["lease_owner"] == "host:1"
    assert stored["lease_expires_at"] == NOW + 60
    assert stored["cloud_key"] == "parents-home/clips/front-door/2026/09/12/18-45-00.mp4"


def test_a_claim_inserts_an_upload_attempt(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(), now=NOW)
    row = claim(conn)
    attempt = conn.execute(
        "SELECT * FROM upload_attempts WHERE clip_id = ?", (row["id"],)
    ).fetchone()
    assert attempt["attempt_no"] == 1
    assert attempt["started_at"] == NOW


def test_claiming_is_exclusive(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(), now=NOW)
    assert claim(conn) is not None
    assert claim(conn) is None


def test_claim_takes_the_oldest_first(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(review_id="older", start_time=NOW - 100), now=NOW)
    db.insert_clip(conn, make_clip(review_id="newer", start_time=NOW), now=NOW)
    assert claim(conn)["review_id"] == "older"
    assert claim(conn)["review_id"] == "newer"


def test_an_expired_lease_is_reclaimed(conn: sqlite3.Connection) -> None:
    """A killed worker must not wedge a clip forever."""
    db.insert_clip(conn, make_clip(), now=NOW)
    assert claim(conn, lease=60.0) is not None
    assert claim(conn, now=NOW + 30) is None  # still held
    assert claim(conn, now=NOW + 61) is not None  # lease expired, reclaimed


def test_claim_returns_none_on_an_empty_queue(conn: sqlite3.Connection) -> None:
    assert claim(conn) is None


# --------------------------------------------------------------------------------------
# Transitions (contract §3.2)
# --------------------------------------------------------------------------------------


def test_mark_uploaded_requires_a_claim_first(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    with pytest.raises(IllegalTransitionError, match="PENDING -> UPLOADED"):
        db.mark_uploaded(conn, clip_id=clip_id, cloud_etag="e", cloud_size_bytes=1, now=NOW)


def test_mark_uploaded_records_the_provider_checksum(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_uploaded(conn, clip_id=clip_id, cloud_etag="abc123", cloud_size_bytes=4096, now=NOW + 5)

    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["upload_state"] == "UPLOADED"
    assert row["cloud_etag"] == "abc123"
    assert row["uploaded_at"] == NOW + 5
    assert row["lease_owner"] is None


def test_mark_uploaded_is_idempotent(conn: sqlite3.Connection) -> None:
    """A crash between the PUT and the record must be recoverable by redoing the upload."""
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_uploaded(conn, clip_id=clip_id, cloud_etag="abc", cloud_size_bytes=1, now=NOW)
    db.mark_uploaded(conn, clip_id=clip_id, cloud_etag="abc", cloud_size_bytes=1, now=NOW + 1)


def test_transient_failure_goes_to_retry_wait(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    entered = db.mark_transient_failure(
        conn,
        clip_id=clip_id,
        error_class="transient",
        message="503",
        next_attempt_at=NOW + 60,
        now=NOW,
    )
    assert entered == "RETRY_WAIT"
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["upload_state"] == "RETRY_WAIT"
    assert row["next_attempt_at"] == NOW + 60
    assert row["lease_owner"] is None


def test_a_retry_is_not_claimable_until_it_is_due(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_transient_failure(
        conn,
        clip_id=clip_id,
        error_class="transient",
        message="503",
        next_attempt_at=NOW + 60,
        now=NOW,
    )
    assert claim(conn, now=NOW + 30) is None
    reclaimed = claim(conn, now=NOW + 61)
    assert reclaimed is not None
    assert reclaimed["attempts"] == 2  # the retry counts as an attempt


def test_exhausted_attempts_become_a_permanent_failure(
    conn: sqlite3.Connection,
) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    conn.execute("UPDATE clips SET attempts = ? WHERE id = ?", (db.MAX_ATTEMPTS, clip_id))

    entered = db.mark_transient_failure(
        conn,
        clip_id=clip_id,
        error_class="transient",
        message="503",
        next_attempt_at=NOW + 60,
        now=NOW,
    )
    assert entered == "FAILED_PERMANENT"

    event = conn.execute(
        "SELECT * FROM system_events WHERE code = 'UPLOAD_ATTEMPTS_EXHAUSTED'"
    ).fetchone()
    assert event["severity"] == "critical"


def test_a_permanent_failure_keeps_the_local_file_and_alerts(
    conn: sqlite3.Connection,
) -> None:
    """INV-1: a failed upload holds storage rather than making room by destroying evidence."""
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_permanent_failure(
        conn, clip_id=clip_id, error_class="auth", message="invalid key", now=NOW
    )

    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["upload_state"] == "FAILED_PERMANENT"
    assert row["local_deleted_at"] is None
    assert db.counts_by_state(conn)["FAILED_PERMANENT"] == 1
    assert (
        conn.execute(
            "SELECT severity FROM system_events WHERE code = 'UPLOAD_PERMANENT_FAILURE'"
        ).fetchone()["severity"]
        == "critical"
    )


def test_error_messages_are_truncated_to_2000_characters(
    conn: sqlite3.Connection,
) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_permanent_failure(
        conn, clip_id=clip_id, error_class="unknown", message="x" * 5000, now=NOW
    )
    length = conn.execute(
        "SELECT length(last_error) FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()[0]
    assert length == 2000


def test_an_illegal_transition_is_rejected(conn: sqlite3.Connection) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    conn.execute("UPDATE clips SET upload_state = 'DELETED_LOCAL' WHERE id = ?", (clip_id,))
    with pytest.raises(IllegalTransitionError):
        db.mark_permanent_failure(conn, clip_id=clip_id, error_class="auth", message="x", now=NOW)


# --------------------------------------------------------------------------------------
# Sweep watermark (contract §2.5)
# --------------------------------------------------------------------------------------


def test_watermark_advances_and_is_monotonic(conn: sqlite3.Connection) -> None:
    assert db.set_watermark(conn, name="clips_sweep", watermark=100.0, now=NOW) == 100.0
    assert db.set_watermark(conn, name="clips_sweep", watermark=200.0, now=NOW + 1) == 200.0
    # Never backwards: redoing work is safe, skipping it is not.
    assert db.set_watermark(conn, name="clips_sweep", watermark=150.0, now=NOW + 2) == 200.0


def test_a_failed_sweep_leaves_the_watermark_where_it_was(
    conn: sqlite3.Connection,
) -> None:
    db.set_watermark(conn, name="clips_sweep", watermark=100.0, now=NOW)
    db.set_watermark(
        conn,
        name="clips_sweep",
        watermark=999.0,
        succeeded=False,
        note="frigate 502",
        now=NOW + 1,
    )

    row = db.get_watermark(conn, "clips_sweep")
    assert row["watermark"] == 100.0
    assert row["consecutive_failures"] == 1
    assert row["last_success_at"] == NOW
    assert row["notes"] == "frigate 502"


def test_a_successful_sweep_resets_the_failure_counter(
    conn: sqlite3.Connection,
) -> None:
    db.set_watermark(conn, name="s", watermark=1.0, succeeded=False, now=NOW)
    db.set_watermark(conn, name="s", watermark=1.0, succeeded=False, now=NOW)
    assert db.get_watermark(conn, "s")["consecutive_failures"] == 2
    db.set_watermark(conn, name="s", watermark=2.0, succeeded=True, now=NOW)
    assert db.get_watermark(conn, "s")["consecutive_failures"] == 0


def test_an_unknown_watermark_is_none_rather_than_an_error(
    conn: sqlite3.Connection,
) -> None:
    assert db.get_watermark(conn, "never_run") is None


# --------------------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------------------


def test_record_event_writes_a_row(conn: sqlite3.Connection) -> None:
    with db.write_txn(conn):
        db.record_event(
            conn,
            component="cleanup",
            code="RETENTION_RAN",
            severity="info",
            message="deleted 3",
            detail={"n": 3},
            now=NOW,
        )
    row = conn.execute("SELECT * FROM system_events").fetchone()
    assert row["code"] == "RETENTION_RAN"
    assert row["detail"] == '{"n": 3}'


def test_record_event_rejects_an_unknown_severity(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError), db.write_txn(conn):
        db.record_event(conn, component="x", code="Y", severity="chatty", now=NOW)


def test_write_txn_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), db.write_txn(conn):
        conn.execute(
            "INSERT INTO system_events (at, severity, component, code) "
            "VALUES (?, 'info', 'test', 'X')",
            (NOW,),
        )
        assert conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0] == 1
        raise Boom

    assert conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0] == 0


def test_accessors_cannot_be_wrapped_in_a_caller_transaction(
    conn: sqlite3.Connection,
) -> None:
    """Every accessor opens its own transaction, so callers must not open one for them.

    This is a real constraint on callers, not an accident: the accessors exist so that the
    claim/work/complete shape cannot be got wrong. Composing them means holding one
    transaction across several, which is exactly what the LLD forbids.
    """
    with (
        db.write_txn(conn),
        pytest.raises(sqlite3.OperationalError, match="within a transaction"),
    ):
        db.insert_clip(conn, make_clip(), now=NOW)


def test_counts_by_state_groups_correctly(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(review_id="a"), now=NOW)
    db.insert_clip(conn, make_clip(review_id="b"), now=NOW)
    claim(conn)
    assert db.counts_by_state(conn) == {"PENDING": 1, "UPLOADING": 1}


# --------------------------------------------------------------------------------------
# The uploader's accessors
# --------------------------------------------------------------------------------------


def test_max_attempts_matches_the_documented_ceiling() -> None:
    """lld/04 §5.1: about five hours of retrying. The schema and the LLD must agree."""
    assert db.MAX_ATTEMPTS == 12


def test_no_upload_has_ever_happened_is_distinguishable_from_a_long_time_ago(
    conn: sqlite3.Connection,
) -> None:
    """Health depends on this: a fresh install is not `STORAGE_STALE`."""
    assert db.last_successful_upload_at(conn) is None

    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_uploaded(conn, clip_id=clip_id, cloud_etag='"e"', cloud_size_bytes=10, now=NOW + 5)
    assert db.last_successful_upload_at(conn) == NOW + 5


def test_queue_summary_counts_the_backlog_by_age_of_materialisation(
    conn: sqlite3.Connection,
) -> None:
    db.insert_clip(conn, make_clip(review_id="old", materialised_at=NOW - 600), now=NOW)
    db.insert_clip(conn, make_clip(review_id="new", materialised_at=NOW - 10), now=NOW)

    summary = db.queue_summary(conn, now=NOW)

    assert summary.pending == 2
    # Age is measured from when the clip became uploadable, not from the event.
    assert summary.oldest_pending_age_s == pytest.approx(600.0)
    assert summary.uploaded_24h == 0
    assert summary.held == 2


def test_stored_clip_checks_covers_only_the_states_that_hold_a_file(
    conn: sqlite3.Connection,
) -> None:
    held, _ = db.insert_clip(conn, make_clip(review_id="held"), now=NOW)
    # Older, so the claim picks it up: only UPLOADING may become UPLOADED (contract §3.2).
    gone, _ = db.insert_clip(conn, make_clip(review_id="gone", start_time=NOW - 100), now=NOW)
    claim(conn)
    db.mark_uploaded(conn, clip_id=gone, cloud_etag=None, cloud_size_bytes=None, now=NOW)

    assert {row["id"] for row in db.stored_clip_checks(conn)} == {held}


def test_skip_by_policy_marks_only_pending_clips_for_those_cameras(
    conn: sqlite3.Connection,
) -> None:
    db.ensure_camera(conn, name="gate", display_name="Gate", now=NOW)
    db.insert_clip(conn, make_clip(review_id="front"), now=NOW)
    db.insert_clip(
        conn,
        make_clip(review_id="gate", camera="gate", path="gate/2026/09/12/18-45-00.mp4"),
        now=NOW,
    )

    assert db.skip_by_policy(conn, cameras=["gate"], now=NOW) == 1

    counts = db.counts_by_state(conn)
    assert counts == {"PENDING": 1, "SKIPPED_BY_POLICY": 1}


def test_skip_by_policy_is_a_no_op_with_no_cameras(conn: sqlite3.Connection) -> None:
    db.insert_clip(conn, make_clip(), now=NOW)
    assert db.skip_by_policy(conn, cameras=[], now=NOW) == 0
    assert db.counts_by_state(conn) == {"PENDING": 1}


def test_reset_failed_permanent_requires_a_reason_and_a_filter(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(DbError, match="reason is required"):
        db.reset_failed_permanent(conn, reason="", clip_id=1)
    with pytest.raises(DbError, match="Refusing to requeue every"):
        db.reset_failed_permanent(conn, reason="because", now=NOW)


def test_reset_failed_permanent_clears_the_attempt_history_and_audits_the_reason(
    conn: sqlite3.Connection,
) -> None:
    clip_id, _ = db.insert_clip(conn, make_clip(), now=NOW)
    claim(conn)
    db.mark_permanent_failure(conn, clip_id=clip_id, error_class="auth", message="403", now=NOW)

    requeued = db.reset_failed_permanent(
        conn, clip_id=clip_id, reason="credentials rotated", now=NOW
    )

    assert requeued == [clip_id]
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["upload_state"] == "PENDING"
    assert row["attempts"] == 0
    assert row["last_error"] is None and row["last_error_class"] is None

    audited = conn.execute(
        "SELECT * FROM system_events WHERE code = 'UPLOAD_RETRY_REQUESTED'"
    ).fetchall()
    assert len(audited) == 1
    assert "credentials rotated" in audited[0]["detail"]


def test_reset_failed_permanent_ignores_clips_that_are_not_permanently_failed(
    conn: sqlite3.Connection,
) -> None:
    db.insert_clip(conn, make_clip(review_id="fine"), now=NOW)
    assert db.reset_failed_permanent(conn, reason="nothing to do", camera="front-door") == []
    assert db.counts_by_state(conn) == {"PENDING": 1}


def test_two_services_starting_together_can_both_migrate(tmp_path: Path) -> None:
    """A fresh install starts the clip-worker and the uploader against one empty database.

    Both call `migrate`. Without the re-check inside the write transaction, both would read
    an empty `schema_migrations`, both would try to apply migration 0001, and one would fail
    at start-up with a primary-key conflict — on a first install, which is the worst possible
    moment to fail.
    """
    path = tmp_path / "state.db"
    connections = [db.open_db(path) for _ in range(4)]
    errors: list[BaseException] = []

    def run(conn: sqlite3.Connection) -> None:
        try:
            db.migrate(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(conn,)) for conn in connections]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert errors == []
        assert db.schema_version(connections[0]) == newest_schema_version()
        applied = connections[0].execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert applied == len(list(db.MIGRATIONS_DIR.glob("*.sql"))), (
            "every migration is recorded exactly once, however many services start together"
        )
    finally:
        for conn in connections:
            conn.close()
