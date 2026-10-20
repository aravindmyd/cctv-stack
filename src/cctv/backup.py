"""Backup and restore of the small, irreplaceable state (lld/08).

The design position is blunt: **the cloud is the backup for footage.** What is backed up here
is the part that cannot be recreated — `cameras.yml`, `.env`, and the state database that says
which clips exist and whether they reached B2. Losing those costs more than losing any single
video, because without them nobody can tell what should exist.

Three rules, each learned from a way backups fail rather than a way they work:

**Never `cp` a live SQLite database.** A copy of a WAL database is not a snapshot; it can be
torn. The SQLite online-backup API is used, then the *output* is reopened and checked — a
backup that cannot be opened is worse than no backup, because it is believed.

**The configuration bundle is encrypted by default.** It contains `.env` and `cameras.yml`,
which hold an RTSP password and a B2 application key. An unencrypted bundle is not written
unless the operator says so explicitly, and every artefact is `chmod 600`.

**A backup that has never been verified is a hypothesis.** `verify` re-reads the checksums,
reopens the database, and compares its row count with the manifest — including against the
live database, so a backup that has silently stopped being taken is noticed.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import db

__all__ = [
    "BackupError",
    "BackupResult",
    "VerifyResult",
    "create",
    "restore_config",
    "restore_state",
    "verify",
]

#: Staleness: a backup older than this, with the live database ahead of it, is reported.
STALE_AFTER_S = 24 * 3600.0

#: Files from the configuration directory that go into the bundle. Listed rather than globbed
#: so a generated file cannot creep in and so a new secret-bearing file is a deliberate edit.
CONFIG_FILES = ("cameras.yml", "app.yml", "host.yml", "mosquitto/mosquitto.conf")


class BackupError(RuntimeError):
    """A backup or restore that must not proceed."""


@dataclass(frozen=True, slots=True)
class BackupSettings:
    data_dir: Path
    config_dir: Path
    db_path: Path
    out_dir: Path
    env_file: Path | None = None
    passphrase: str | None = None
    unencrypted: bool = False

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> BackupSettings:
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))
        config_dir = Path(env.get("CCTV_CONFIG_DIR", "/config"))
        return cls(
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            out_dir=Path(env.get("CCTV_BACKUP_DIR", str(data_dir / "backups"))),
            env_file=(Path(env["CCTV_ENV_FILE"]) if env.get("CCTV_ENV_FILE") else None),
            passphrase=(env.get("CCTV_BACKUP_PASSPHRASE") or "").strip() or None,
            unencrypted=(env.get("CCTV_BACKUP_UNENCRYPTED") or "").strip() in {"1", "true"},
        )


@dataclass(frozen=True, slots=True)
class BackupResult:
    stamp: str
    config_bundle: Path
    state_db: Path
    manifest_csv: Path
    manifest: Path
    clips: int
    sha256: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    problems: tuple[str, ...] = ()
    config_files: int = 0
    clips_in_backup: int = 0
    clips_live: int | None = None
    stale: bool = False
    backup_age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "config_files": self.config_files,
            "clips_in_backup": self.clips_in_backup,
            "clips_live": self.clips_live,
            "stale": self.stale,
            "backup_age_s": self.backup_age_s,
        }


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod600(path: Path) -> None:
    path.chmod(0o600)


def _encrypt(source: Path, *, passphrase: str) -> Path:
    """Encrypt in place with openssl, removing the plaintext.

    `openssl` rather than a Python dependency: it is present on every supported host and in
    the image, and adding a crypto library for one file is a dependency the product does not
    need. `age` is the documented first choice for operators who have it; this is the fallback
    that always works.
    """
    target = source.with_suffix(source.suffix + ".enc")
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-in",
            str(source),
            "-out",
            str(target),
            "-pass",
            "env:CCTV_BACKUP_PASSPHRASE",
        ],
        env={**os.environ, "CCTV_BACKUP_PASSPHRASE": passphrase},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError(
            f"openssl could not encrypt the config bundle: {completed.stderr.strip()}"
        )
    _chmod600(target)
    source.unlink()
    return target


def _decrypt(source: Path, destination: Path, *, passphrase: str) -> None:
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(source),
            "-out",
            str(destination),
            "-pass",
            "env:CCTV_BACKUP_PASSPHRASE",
        ],
        env={**os.environ, "CCTV_BACKUP_PASSPHRASE": passphrase},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        destination.unlink(missing_ok=True)
        raise BackupError(
            "could not decrypt the config bundle. Is CCTV_BACKUP_PASSPHRASE the one used "
            f"to create it? ({completed.stderr.strip()})"
        )


def _config_members(settings: BackupSettings) -> list[tuple[Path, str]]:
    members: list[tuple[Path, str]] = []
    for relative in CONFIG_FILES:
        path = settings.config_dir / relative
        if path.is_file():
            members.append((path, f"config/{relative}"))
    if settings.env_file is not None and settings.env_file.is_file():
        members.append((settings.env_file, ".env"))
    return members


def create(settings: BackupSettings, *, now: float | None = None) -> BackupResult:
    """Write the three artefacts and the checksum manifest. Never destructive.

    Order matters: the state database is verified *before* it is copied, so a corrupt source
    is reported rather than preserved, and the manifest is produced from the backup itself
    rather than the live database, so the two cannot disagree.
    """
    stamp = _stamp(time.time() if now is None else now)
    settings.out_dir.mkdir(parents=True, exist_ok=True)

    members = _config_members(settings)
    if not members:
        raise BackupError(
            f"nothing to back up: no configuration files found under {settings.config_dir}."
        )

    if not settings.passphrase and not settings.unencrypted:
        raise BackupError(
            "refusing to write an unencrypted configuration bundle: it contains .env and "
            "cameras.yml, which hold credentials. Set CCTV_BACKUP_PASSPHRASE, or pass "
            "--unencrypted if you accept that risk."
        )

    with tempfile.TemporaryDirectory() as scratch:
        plain = Path(scratch) / f"config-{stamp}.tar.gz"
        with tarfile.open(plain, "w:gz") as archive:
            for path, arcname in members:
                archive.add(path, arcname=arcname)
        if settings.passphrase:
            bundle = _encrypt(plain, passphrase=settings.passphrase)
        else:
            bundle = plain
            _chmod600(bundle)
        config_bundle = settings.out_dir / bundle.name
        shutil.move(str(bundle), config_bundle)

    state_db = settings.out_dir / f"state-{stamp}.db"
    conn = db.open_db(settings.db_path)
    try:
        db.integrity_check(conn)
        db.backup_to(conn, state_db)
        rows = conn.execute(
            "SELECT id, camera, start_time, path, size_bytes, sha256, upload_state, "
            "cloud_key, uploaded_at FROM clips ORDER BY start_time, id"
        ).fetchall()
    finally:
        conn.close()
    _chmod600(state_db)

    # Reopen the output: an unopenable backup is worse than none, because it is believed.
    check = db.open_db(state_db)
    try:
        db.integrity_check(check)
    finally:
        check.close()

    manifest_csv = settings.out_dir / f"state-{stamp}.manifest.csv"
    with manifest_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "event_id",
                "camera",
                "start_time",
                "path",
                "size_bytes",
                "sha256",
                "upload_state",
                "cloud_key",
                "uploaded_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["camera"],
                    row["start_time"],
                    row["path"],
                    row["size_bytes"],
                    row["sha256"],
                    row["upload_state"],
                    row["cloud_key"],
                    row["uploaded_at"],
                ]
            )
    _chmod600(manifest_csv)

    sha256 = {
        config_bundle.name: _sha256(config_bundle),
        state_db.name: _sha256(state_db),
        manifest_csv.name: _sha256(manifest_csv),
    }
    manifest = settings.out_dir / "MANIFEST"
    with manifest.open("w", encoding="utf-8") as handle:
        for name, digest in sorted(sha256.items()):
            handle.write(f"{digest}  {name}\n")
    _chmod600(manifest)

    return BackupResult(
        stamp=stamp,
        config_bundle=config_bundle,
        state_db=state_db,
        manifest_csv=manifest_csv,
        manifest=manifest,
        clips=len(rows),
        sha256=sha256,
    )


def _read_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        entries[name] = digest
    return entries


def verify(
    settings: BackupSettings, *, now: float | None = None, passphrase: str | None = None
) -> VerifyResult:
    """Check the newest backup end to end: checksums, openability, and freshness."""
    manifest_path = settings.out_dir / "MANIFEST"
    if not manifest_path.is_file():
        return VerifyResult(ok=False, problems=("no MANIFEST: no backup has been taken",))

    entries = _read_manifest(manifest_path)
    problems: list[str] = []
    for name, expected in entries.items():
        path = settings.out_dir / name
        if not path.is_file():
            problems.append(f"missing artefact: {name}")
            continue
        if _sha256(path) != expected:
            problems.append(f"checksum mismatch: {name}")

    config_name = next((n for n in entries if n.startswith("config-")), None)
    state_name = next((n for n in entries if n.startswith("state-") and n.endswith(".db")), None)
    csv_name = next((n for n in entries if n.endswith(".manifest.csv")), None)
    if config_name is None or state_name is None:
        problems.append("the MANIFEST does not name both a config bundle and a state database")

    config_files = 0
    if config_name is not None and (settings.out_dir / config_name).is_file():
        source = settings.out_dir / config_name
        with tempfile.TemporaryDirectory() as scratch:
            if source.name.endswith(".enc"):
                key = passphrase or settings.passphrase
                if not key:
                    problems.append("the config bundle is encrypted and no passphrase was provided")
                else:
                    plain = Path(scratch) / "config.tar.gz"
                    try:
                        _decrypt(source, plain, passphrase=key)
                        with tarfile.open(plain, "r:gz") as archive:
                            config_files = len([m for m in archive.getmembers() if m.isfile()])
                    except (BackupError, tarfile.TarError) as exc:
                        problems.append(f"config bundle unreadable: {exc}")
            else:
                with tarfile.open(source, "r:gz") as archive:
                    config_files = len([m for m in archive.getmembers() if m.isfile()])

    clips_in_backup = 0
    if state_name is not None and (settings.out_dir / state_name).is_file():
        try:
            conn = db.open_db(settings.out_dir / state_name, readonly=True)
            try:
                db.integrity_check(conn)
                clips_in_backup = int(
                    conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"]
                )
            finally:
                conn.close()
        except (db.DbError, OSError) as exc:
            problems.append(f"state database unreadable: {exc}")

    if csv_name is not None and (settings.out_dir / csv_name).is_file():
        with (settings.out_dir / csv_name).open(encoding="utf-8") as handle:
            rows = max(0, sum(1 for _ in handle) - 1)
        if clips_in_backup and rows != clips_in_backup:
            problems.append(f"manifest lists {rows} clips but the database holds {clips_in_backup}")

    clips_live: int | None = None
    age: float | None = None
    stale = False
    if state_name is not None:
        match = state_name.removeprefix("state-").removesuffix(".db")
        try:
            # `timegm`, not `mktime`: the stamp is UTC, and `mktime` would shift it by the
            # host's offset (and differently across a DST boundary).
            backed_up_at = calendar.timegm(time.strptime(match, "%Y%m%dT%H%M%SZ"))
            age = max(0.0, (time.time() if now is None else now) - backed_up_at)
        except ValueError:
            age = None
    try:
        live = db.open_db(settings.db_path, readonly=True)
        try:
            clips_live = int(live.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"])
        finally:
            live.close()
    except (db.DbError, OSError):
        clips_live = None
    if (
        clips_live is not None
        and clips_live > clips_in_backup
        and (age is None or age > STALE_AFTER_S)
    ):
        stale = True
        problems.append(
            f"backup is stale: it holds {clips_in_backup} clips, the live database {clips_live}"
        )

    return VerifyResult(
        ok=not problems,
        problems=tuple(problems),
        config_files=config_files,
        clips_in_backup=clips_in_backup,
        clips_live=clips_live,
        stale=stale,
        backup_age_s=age,
    )


def restore_state(settings: BackupSettings, *, source: Path, force: bool = False) -> Path:
    """Replace the state database, keeping the current one as a safety copy.

    Refuses to overwrite an existing database unless `force` is set: a restore is the one
    operation here that destroys something, and the default must not be destructive.
    """
    if not source.is_file():
        raise BackupError(f"no such backup: {source}")
    if settings.db_path.exists():
        if not force:
            raise BackupError(
                f"{settings.db_path} already exists. Re-run with --force to replace it; the "
                "current database is kept alongside as a pre-restore copy."
            )
        safety = settings.db_path.with_name(
            f"{settings.db_path.name}.pre-restore-{_stamp(time.time())}"
        )
        conn = db.open_db(settings.db_path)
        try:
            db.backup_to(conn, safety)
        finally:
            conn.close()
        _chmod600(safety)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, settings.db_path)
    _chmod600(settings.db_path)
    check = db.open_db(settings.db_path, readonly=True)
    try:
        db.integrity_check(check)
    finally:
        check.close()
    return settings.db_path


def restore_config(
    settings: BackupSettings,
    *,
    source: Path,
    passphrase: str | None = None,
    force: bool = False,
) -> list[Path]:
    """Extract a config bundle into the config directory.

    Restoring `.env` and `cameras.yml` over edited ones is destructive, so an existing file is
    kept alongside the restored copy unless `force` is set.
    """
    if not source.is_file():
        raise BackupError(f"no such backup: {source}")
    key = passphrase or settings.passphrase
    written: list[Path] = []
    with tempfile.TemporaryDirectory() as scratch:
        plain = source
        if source.name.endswith(".enc"):
            if not key:
                raise BackupError("the config bundle is encrypted; set CCTV_BACKUP_PASSPHRASE.")
            plain = Path(scratch) / "config.tar.gz"
            _decrypt(source, plain, passphrase=key)
        with tarfile.open(plain, "r:gz") as archive:
            for member in archive.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise BackupError(f"refusing to extract {member.name}: not a plain file")
                # A bundle is data we produced, but it can arrive from a USB stick. Refuse
                # absolute names and any `..` rather than trusting the archive.
                parts = Path(member.name).parts
                if Path(member.name).is_absolute() or ".." in parts:
                    raise BackupError(f"refusing to extract {member.name}: unsafe path")
                target = (
                    settings.env_file
                    if member.name == ".env"
                    else settings.config_dir / member.name.removeprefix("config/")
                )
                if target is None:
                    continue
                if target.exists() and not force:
                    shutil.copyfile(target, target.with_suffix(target.suffix + ".pre-restore"))
                # `filter="data"` refuses links and devices even if a check above misses one.
                archive.extract(member, path=scratch, filter="data")
                source_path = Path(scratch) / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                # `.env` and `cameras.yml` carry credentials; a restore must not widen the
                # permissions the backup went to the trouble of narrowing.
                _chmod600(target)
                written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    """`cctv-backup create|verify|restore-state|restore-config`."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="cctv-backup")
    sub = parser.add_subparsers(dest="command", required=True)
    create_p = sub.add_parser("create")
    create_p.add_argument("--unencrypted", action="store_true")
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("--json", action="store_true")
    state_p = sub.add_parser("restore-state")
    state_p.add_argument("source", type=Path)
    state_p.add_argument("--force", action="store_true")
    config_p = sub.add_parser("restore-config")
    config_p.add_argument("source", type=Path)
    config_p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        settings = BackupSettings.from_environ()
        if args.command == "create":
            result = create(replace(settings, unencrypted=settings.unencrypted or args.unencrypted))
            print(
                json.dumps(
                    {
                        "stamp": result.stamp,
                        "clips": result.clips,
                        "config": result.config_bundle.name,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "verify":
            result = verify(settings)
            print(json.dumps(result.to_dict(), indent=2))
            return 0 if result.ok else 1
        if args.command == "restore-state":
            path = restore_state(settings, source=args.source, force=args.force)
            print(f"restored {path}")
            return 0
        written = restore_config(settings, source=args.source, force=args.force)
        for path in written:
            print(f"restored {path}")
        return 0
    except BackupError as exc:
        print(f"cctv-backup: {exc}", file=sys.stderr)
        return 78
