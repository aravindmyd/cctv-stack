"""`cctv-migrate` — bring the schema up to date, once, from the command line.

Every service migrates on start-up, which is right for a service: it must never run against a
schema it does not understand. An **upgrade** needs the migration to happen at a different
moment — after the old workers have stopped and before the new containers start (lld/09 §4.2).
This entry point is that moment, and nothing else: no loop, no timers, no HTTP.

It is deliberately boring. `update.sh` runs it with the *new* image so the new code performs the
migration, then starts the stack; if it fails, the pre-upgrade backup is still on disk and
`update.sh --rollback` is the documented way back.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import db

__all__ = ["main"]

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-migrate",
        description="Apply pending database migrations and exit.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("CCTV_DB", "/data/cctv.db")),
        help="the state database (default: $CCTV_DB)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.db.exists():
        message = f"{args.db} does not exist; is this an installed site?"
        print(
            json.dumps({"ok": False, "error": message}) if args.json else f"error: {message}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    conn: sqlite3.Connection | None = None
    try:
        conn = db.open_db(args.db)
        before = db.schema_version(conn)
        after = db.migrate(conn)
        db.integrity_check(conn)
    except (sqlite3.Error, db.DbError) as exc:
        payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload) if args.json else f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps({"ok": True, "from": before, "to": after}))
    elif before == after:
        print(f"schema {after}: already current")
    else:
        print(f"schema {before} -> {after}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
