"""`cctv-doctor` — the read-only answer to "why is my CCTV not working?".

The installer, the status page and the logs each answer part of the question. This is the one
command an operator runs when they do not yet know which part is wrong, so it checks *all* of
them and changes nothing: no writes to the database beyond a temp file in the data directory
that is removed immediately, no migrations, no container actions.

Two rules shape the implementation.

**A check that cannot run says so.** `SKIP` is a first-class verdict, not a silent pass. A
doctor that reported "all good" because it could not reach Docker would be worse than no
doctor at all — it would cost the operator the one thing they came for, which is knowing what
is actually true.

**Every finding is data.** Findings are collected into a `Report`, which renders to human text
and serialises to JSON from the same objects, so a bug report and the terminal cannot
disagree. Secrets are redacted on the way in (`logging.redact_url`) rather than on the way out,
because a diagnostics bundle posted to a forum is a real failure mode.

Exit codes: `0` when nothing failed (warnings included), `1` when any check failed. See
[`internal/architecture/lld/07-installer-and-host-profiles.md`] §10.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, db, host, timeutil
from .config import ConfigError, load_cameras_config
from .logging import redact_url
from .timeutil import Instant

__all__ = [
    "FAIL",
    "OK",
    "SKIP",
    "WARN",
    "DoctorSettings",
    "Finding",
    "Probes",
    "Report",
    "main",
    "run",
]

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

#: Ordered worst-last, so a section's verdict is the worst of its findings.
_SEVERITY = {SKIP: 0, OK: 1, WARN: 2, FAIL: 3}

#: Free space below this is a warning on any CCTV host: a full disk stops recording, and a
#: recording system that stops quietly is the failure this whole project exists to prevent.
MIN_FREE_GIB = 5.0

#: A clip queue older than this is behind, not merely busy.
BACKLOG_WARN_S = 3600.0

#: A camera that has produced nothing for this long is worth mentioning. Two days, not one:
#: a gate camera legitimately goes a day without a clip.
QUIET_CAMERA_WARN_S = 48 * 3600.0

#: WAL larger than this suggests a checkpoint has not happened, which usually means a writer
#: is stuck.
WAL_WARN_BYTES = 100 * 1024 * 1024


# ---------------------------------------------------------------------------------------
# Settings and probes
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DoctorSettings:
    """Where everything lives. Defaults match the on-disk layout contract."""

    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    db_path: Path = Path("/data/cctv.db")
    env_file: Path = Path("/site/.env")
    component_urls: Mapping[str, str] = field(
        default_factory=lambda: {
            "uploader": "http://uploader:8082",
            "cleanup": "http://cleanup:8083",
            "status": "http://status:8084",
        }
    )
    frigate_url: str = "http://frigate:5000"
    mosquitto_host: str = "mosquitto"
    mosquitto_port: int = 1883

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> DoctorSettings:
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))
        config_dir = Path(env.get("CCTV_CONFIG_DIR", "/config"))
        return cls(
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            env_file=Path(env.get("CCTV_ENV_FILE", "/site/.env")),
            frigate_url=env.get("CCTV_FRIGATE_URL", "http://frigate:5000"),
            mosquitto_host=env.get("CCTV_MOSQUITTO_HOST", "mosquitto"),
            mosquitto_port=int(env.get("CCTV_MOSQUITTO_PORT", "1883")),
        )


def _http_json(url: str, *, timeout: float = 5.0) -> Mapping[str, Any]:
    with urllib.request.urlopen(
        url, timeout=timeout
    ) as response:  # fixed scheme, internal URL
        return json.loads(response.read().decode("utf-8"))


def _tcp_open(hostname: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class Probes:
    """Every external fact doctor needs, injectable so the checks are testable."""

    http_json: Callable[[str], Mapping[str, Any]] = _http_json
    tcp_open: Callable[[str, int], bool] = _tcp_open
    detect_host: Callable[[], host.Detection] = host.detect
    disk_usage: Callable[[Path], Any] = shutil.disk_usage


# ---------------------------------------------------------------------------------------
# Findings and the report
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One checked fact, its verdict, and one sentence explaining it."""

    section: str
    name: str
    state: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in _SEVERITY:
            raise ValueError(f"unknown state {self.state!r}")

    def to_dict(self) -> dict[str, str]:
        return {
            "section": self.section,
            "name": self.name,
            "state": self.state,
            "detail": redact_url(self.detail),
        }


@dataclass(frozen=True, slots=True)
class Report:
    findings: tuple[Finding, ...]
    generated_at: Instant
    version: str = __version__

    @property
    def exit_code(self) -> int:
        return 0 if not any(f.state == FAIL for f in self.findings) else 1

    def counts(self) -> dict[str, int]:
        out = {state: 0 for state in (FAIL, WARN, OK, SKIP)}
        for finding in self.findings:
            out[finding.state] = out.get(finding.state, 0) + 1
        return out

    def sections(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for finding in self.findings:
            out.setdefault(finding.section, []).append(finding)
        return out

    def worst(self, section: str) -> str:
        states = [f.state for f in self.findings if f.section == section]
        return max(states, key=lambda s: _SEVERITY[s]) if states else SKIP

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "verdict": _verdict(counts),
            "counts": counts,
            "findings": [f.to_dict() for f in self.findings],
        }

    def render(self) -> str:
        counts = self.counts()
        lines = [
            "════════════════════════════════════════════════════════════════════",
            f" cctv-doctor {self.version}",
            "════════════════════════════════════════════════════════════════════",
        ]
        for section, findings in self.sections().items():
            lines.append("")
            lines.append(f"  {section.upper()}   {self.worst(section)}")
            for finding in findings:
                mark = {
                    OK: "ok  ",
                    WARN: "warn",
                    FAIL: "FAIL",
                    SKIP: "skip",
                }[finding.state]
                detail = redact_url(finding.detail)
                lines.append(f"    [{mark}] {finding.name}")
                if detail:
                    lines.append(f"           {detail}")
        lines.append("")
        lines.append(
            "────────────────────────────────────────────────────────────────────"
        )
        lines.append(
            f"  {counts[FAIL]} failed · {counts[WARN]} warnings · "
            f"{counts[OK]} passed · {counts[SKIP]} skipped"
        )
        lines.append(f"  {_verdict(counts)}")
        return "\n".join(lines) + "\n"


def _verdict(counts: Mapping[str, int]) -> str:
    if counts.get(FAIL):
        return "Something is broken. The FAIL lines above are where to start."
    if counts.get(WARN):
        return "Nothing is broken; the warnings are worth reading."
    return "Everything doctor can check is fine."


# ---------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------


def _check_host(settings: DoctorSettings, probes: Probes) -> list[Finding]:
    section = "host"
    try:
        detection = probes.detect_host()
    except Exception as exc:  # a check must never take doctor down
        return [
            Finding(
                section, "detection", FAIL, f"could not inspect this machine: {exc}"
            )
        ]

    findings = [
        Finding(
            section,
            "platform",
            OK,
            f"{detection.facts.arch} ({detection.facts.os_name})"
            + (
                f", {detection.facts.memory_bytes / 1024**3:.1f} GiB RAM"
                if detection.facts.memory_bytes
                else ""
            ),
        )
    ]
    if detection.profile is None:
        findings.append(
            Finding(
                section,
                "profile",
                FAIL,
                detection.unsupported_reason or "unsupported host",
            )
        )
    else:
        profile = detection.profile
        findings.append(
            Finding(
                section,
                "profile",
                OK,
                f"{profile.name}: {profile.detector}, up to {profile.camera_limit} cameras",
            )
        )
    for warning in detection.warnings:
        findings.append(Finding(section, "warning", WARN, warning))
    return findings


def _check_storage(settings: DoctorSettings, probes: Probes) -> list[Finding]:
    section = "storage"
    findings: list[Finding] = []

    if not settings.data_dir.is_dir():
        return [
            Finding(section, "data-dir", FAIL, f"{settings.data_dir} does not exist")
        ]

    probe_file = settings.data_dir / ".doctor-write-test"
    try:
        probe_file.write_text("doctor", encoding="utf-8")
        probe_file.unlink()
        findings.append(
            Finding(section, "writable", OK, f"{settings.data_dir} is writable")
        )
    except OSError as exc:
        findings.append(Finding(section, "writable", FAIL, f"cannot write: {exc}"))

    try:
        usage = probes.disk_usage(settings.data_dir)
        free_gib = usage.free / 1024**3
        state = OK if free_gib >= MIN_FREE_GIB else WARN
        findings.append(
            Finding(
                section,
                "free-space",
                state,
                f"{free_gib:.1f} GiB of {usage.total / 1024**3:.1f} GiB free",
            )
        )
    except OSError as exc:
        findings.append(Finding(section, "free-space", FAIL, f"cannot measure: {exc}"))

    clips = settings.data_dir / "clips"
    if clips.is_dir():
        findings.append(Finding(section, "clips", OK, f"{clips} present"))
    else:
        findings.append(
            Finding(
                section,
                "clips",
                WARN,
                f"{clips} is missing; nothing is being materialised",
            )
        )
    return findings


def _check_config(settings: DoctorSettings, probes: Probes) -> list[Finding]:
    section = "config"
    findings: list[Finding] = []
    cameras_file = settings.config_dir / "cameras.yml"

    if not cameras_file.exists():
        findings.append(
            Finding(section, "cameras.yml", FAIL, f"{cameras_file} is missing")
        )
    else:
        try:
            config = load_cameras_config(cameras_file)
        except (ConfigError, OSError) as exc:
            findings.append(Finding(section, "cameras.yml", FAIL, str(exc)))
        else:
            enabled = len(config.enabled_cameras())
            findings.append(
                Finding(
                    section, "cameras.yml", OK, f"valid, {enabled} enabled camera(s)"
                )
            )
            for warning in getattr(config, "warnings", ()):
                findings.append(Finding(section, "warning", WARN, warning))

    if settings.env_file.exists():
        mode = stat.S_IMODE(settings.env_file.stat().st_mode)
        if mode & 0o077:
            findings.append(
                Finding(
                    section,
                    ".env",
                    WARN,
                    f"mode {mode:04o} is readable beyond its owner; chmod 600 it",
                )
            )
        else:
            findings.append(Finding(section, ".env", OK, f"present, mode {mode:04o}"))
    else:
        findings.append(
            Finding(
                section, ".env", WARN, f"{settings.env_file} not found at this path"
            )
        )

    generated = settings.config_dir / "frigate" / "config.yml"
    if generated.exists():
        findings.append(Finding(section, "generated-config", OK, str(generated)))
    else:
        findings.append(
            Finding(
                section,
                "generated-config",
                WARN,
                "Frigate's config has not been generated; run cctv-config",
            )
        )
    return findings


def _expected_schema_version() -> int:
    versions = []
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdigit():
            versions.append(int(prefix))
    return max(versions, default=0)


def _check_database(
    settings: DoctorSettings, *, now: Instant
) -> tuple[list[Finding], db.QueueSummary | None]:
    section = "database"
    if not settings.db_path.exists():
        return [
            Finding(section, "state.db", FAIL, f"{settings.db_path} does not exist")
        ], None

    findings: list[Finding] = []
    summary: db.QueueSummary | None = None
    conn: sqlite3.Connection | None = None
    try:
        conn = db.open_db(settings.db_path, readonly=True)
        db.integrity_check(conn)
        findings.append(
            Finding(section, "integrity", OK, "PRAGMA integrity_check passed")
        )

        actual = db.schema_version(conn)
        expected = _expected_schema_version()
        if actual == expected:
            findings.append(Finding(section, "schema", OK, f"version {actual}"))
        elif actual < expected:
            findings.append(
                Finding(
                    section,
                    "schema",
                    FAIL,
                    f"version {actual}, expected {expected}; a service has not migrated yet",
                )
            )
        else:
            findings.append(
                Finding(
                    section,
                    "schema",
                    WARN,
                    f"version {actual} is newer than this build's {expected}",
                )
            )
        summary = db.queue_summary(conn, now=now)
        counts = ", ".join(
            f"{name}={n}" for name, n in sorted(summary.counts.items()) if n
        )
        findings.append(Finding(section, "clips", OK, counts or "no clips recorded"))
    except (sqlite3.Error, db.DbError, OSError) as exc:
        findings.append(Finding(section, "state.db", FAIL, str(exc)))
    finally:
        if conn is not None:
            conn.close()

    wal = settings.db_path.with_name(settings.db_path.name + "-wal")
    if wal.exists():
        size = wal.stat().st_size
        state = OK if size < WAL_WARN_BYTES else WARN
        findings.append(Finding(section, "wal", state, f"{size / 1024**2:.1f} MiB"))
    return findings, summary


def _check_connectivity(settings: DoctorSettings, probes: Probes) -> list[Finding]:
    section = "connectivity"
    findings: list[Finding] = []

    for name, url in settings.component_urls.items():
        try:
            payload = probes.http_json(url + "/health")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            findings.append(Finding(section, name, FAIL, f"{url}: {exc}"))
            continue
        state = str(payload.get("state", "UNKNOWN"))
        verdict = OK if state == "HEALTHY" else WARN
        findings.append(Finding(section, name, verdict, f"{url} reports {state}"))

    try:
        stats = probes.http_json(settings.frigate_url + "/api/stats")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        findings.append(
            Finding(section, "frigate", FAIL, f"{settings.frigate_url}: {exc}")
        )
    else:
        version = str(stats.get("service", {}).get("version", "unknown"))
        findings.append(
            Finding(section, "frigate", OK, f"{settings.frigate_url}, v{version}")
        )

    if probes.tcp_open(settings.mosquitto_host, settings.mosquitto_port):
        findings.append(
            Finding(
                section,
                "mqtt",
                OK,
                f"{settings.mosquitto_host}:{settings.mosquitto_port} accepts connections",
            )
        )
    else:
        findings.append(
            Finding(
                section,
                "mqtt",
                FAIL,
                f"cannot reach {settings.mosquitto_host}:{settings.mosquitto_port}",
            )
        )
    return findings


def _check_pipeline(
    settings: DoctorSettings,
    *,
    summary: db.QueueSummary | None,
    now: Instant,
) -> list[Finding]:
    section = "pipeline"
    findings: list[Finding] = []

    if summary is not None:
        age = summary.oldest_pending_age_s
        if age is None:
            findings.append(
                Finding(section, "backlog", OK, "nothing waiting to upload")
            )
        else:
            state = OK if age < BACKLOG_WARN_S else WARN
            findings.append(
                Finding(
                    section, "backlog", state, f"oldest pending is {age / 60:.0f} min"
                )
            )

        failed = summary.counts.get("FAILED_PERMANENT", 0)
        if failed:
            findings.append(
                Finding(
                    section,
                    "failed-uploads",
                    WARN,
                    f"{failed} clip(s) gave up; see docs/recover-uploads.md",
                )
            )
        else:
            findings.append(Finding(section, "failed-uploads", OK, "none"))

    if not settings.db_path.exists():
        return findings
    conn: sqlite3.Connection | None = None
    try:
        conn = db.open_db(settings.db_path, readonly=True)
        last = db.last_clip_per_camera(conn)
        if not last:
            findings.append(
                Finding(section, "camera-clips", WARN, "no camera has produced a clip")
            )
        for camera, started in sorted(last.items()):
            age_s = now - started
            state = OK if age_s < QUIET_CAMERA_WARN_S else WARN
            findings.append(
                Finding(
                    section,
                    f"camera {camera}",
                    state,
                    f"last clip {age_s / 3600:.1f} h ago",
                )
            )
    except (sqlite3.Error, db.DbError, OSError) as exc:
        findings.append(
            Finding(section, "camera-clips", SKIP, f"cannot read clip history: {exc}")
        )
    finally:
        if conn is not None:
            conn.close()
    return findings


def _check_versions() -> list[Finding]:
    section = "versions"
    return [
        Finding(section, "cctv", OK, __version__),
        Finding(section, "python", OK, f"{sys.version.split()[0]}"),
        Finding(
            section,
            "images",
            SKIP,
            "image digests live in the deployment, not in this container",
        ),
    ]


# ---------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------


def run(
    settings: DoctorSettings | None = None, *, probes: Probes | None = None
) -> Report:
    """Run every check and return the report. Never raises on a broken dependency."""
    settings = settings or DoctorSettings.from_environ()
    probes = probes or Probes()
    now = timeutil.now()

    findings: list[Finding] = []
    findings += _check_host(settings, probes)
    findings += _check_storage(settings, probes)
    findings += _check_config(settings, probes)
    database_findings, summary = _check_database(settings, now=now)
    findings += database_findings
    findings += _check_connectivity(settings, probes)
    findings += _check_pipeline(settings, summary=summary, now=now)
    findings += _check_versions()
    return Report(findings=tuple(findings), generated_at=now)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-doctor",
        description="Diagnose an installation. Read-only: nothing is changed.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--save",
        type=Path,
        nargs="?",
        const=Path("."),
        help="also write the JSON report here as doctor-<timestamp>.json",
    )
    args = parser.parse_args(argv)

    report = run()
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(report.generated_at))
        target = args.save / f"doctor-{stamp}.json"
        target.write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        if not args.json:
            print(f"report written to {target}", file=sys.stderr)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render(), end="")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
