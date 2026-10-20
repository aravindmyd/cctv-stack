"""Tests for `cctv-migrate`.

The command exists so an upgrade can migrate *between* stopping the old workers and starting the
new containers, which means its contract is small on purpose: apply what is pending, prove the
result is sound, and say what happened.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cctv import db, migrate


def test_fresh_database_is_migrated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cctv.db"
    sqlite3.connect(target).close()

    assert migrate.main(["--db", str(target)]) == 0
    assert "0 ->" in capsys.readouterr().out

    conn = db.open_db(target, readonly=True)
    try:
        assert db.schema_version(conn) == migrate_expected_version()
    finally:
        conn.close()


def migrate_expected_version() -> int:
    versions = [
        int(p.name.split("_", 1)[0])
        for p in db.MIGRATIONS_DIR.glob("*.sql")
        if p.name.split("_", 1)[0].isdigit()
    ]
    return max(versions, default=0)


def test_current_database_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cctv.db"
    conn = db.open_db(target)
    db.migrate(conn)
    conn.close()

    assert migrate.main(["--db", str(target)]) == 0
    assert "already current" in capsys.readouterr().out


def test_json_output_reports_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cctv.db"
    sqlite3.connect(target).close()

    assert migrate.main(["--db", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "from": 0, "to": migrate_expected_version()}


def test_missing_database_fails_with_a_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = migrate.main(["--db", str(tmp_path / "absent.db")])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_corrupt_database_fails_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cctv.db"
    target.write_bytes(b"this is not a database")

    code = migrate.main(["--db", str(target)])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_json_failure_is_still_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cctv.db"
    target.write_bytes(b"this is not a database")

    assert migrate.main(["--db", str(target), "--json"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    """A database from a future release must not be "migrated" downwards."""
    target = tmp_path / "cctv.db"
    conn = db.open_db(target)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
        (migrate_expected_version() + 1, "future", 0.0),
    )
    conn.commit()
    conn.close()

    assert migrate.main(["--db", str(target)]) == 1
