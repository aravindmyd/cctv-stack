"""Tests for `cctv-doctor`.

Every probe is injected, so none of this needs a network, a Docker daemon or a camera. The
fixture builds a plausible installation — data directory, config, `.env`, migrated database
with one clip — and each test breaks exactly one thing, which is the only way to know that
the check that was supposed to notice it is the check that did.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cctv import db, doctor, host, timeutil
from cctv.doctor import FAIL, OK, SKIP, WARN, DoctorSettings, Finding, Probes, Report


def _detection(*, supported: bool = True, warnings: list[str] | None = None) -> host.Detection:
    facts = host.HostFacts(
        arch="x86_64",
        os_name="Linux",
        model="Test box",
        memory_bytes=8 * 1024**3,
        cpu_cores=4,
    )
    profile = host.HostProfile.named("intel-igpu") if supported else None
    return host.Detection(
        facts=facts,
        profile=profile,
        warnings=tuple(warnings or ()),
        unsupported_reason=None if supported else "no supported detector",
    )


class _Env:
    """A synthetic installation, plus probes that answer for it."""

    def __init__(self, root: Path, *, now: float) -> None:
        self.root = root
        self.now = now
        self.data_dir = root / "data"
        self.config_dir = root / "config"
        self.db_path = self.data_dir / "state.db"
        self.env_file = root / ".env"

        (self.data_dir / "clips").mkdir(parents=True)
        (self.config_dir / "frigate").mkdir(parents=True)
        (self.config_dir / "frigate" / "config.yml").write_text("mqtt: {}\n", encoding="utf-8")
        (self.config_dir / "cameras.yml").write_text(
            "site:\n"
            "  location: test-site\n"
            "  timezone: UTC\n"
            "min_headroom_days: 2\n"
            "frigate:\n"
            "  auth:\n"
            "    enabled: false\n"
            "defaults:\n"
            "  record:\n"
            "    mode: motion\n"
            "cameras:\n"
            "  front-door:\n"
            "    display_name: Front Door\n"
            "    enabled: true\n"
            "    rtsp:\n"
            "      main: rtsp://user:pass@10.0.0.5/stream\n"
            "      sub: rtsp://user:pass@10.0.0.5/sub\n",
            encoding="utf-8",
        )
        self.env_file.write_text("CCTV_SITE=test\n", encoding="utf-8")
        self.env_file.chmod(0o600)

        conn = db.open_db(self.db_path)
        db.migrate(conn)
        db.ensure_camera(conn, name="front-door")
        clip_id, _ = db.insert_clip(
            conn,
            db.ClipRecord(
                review_id="r-1",
                camera="front-door",
                severity="alert",
                start_time=now - 600,
                end_time=now - 590,
                path="clips/front-door/2026/10/20/10-00-00.mp4",
            ),
        )
        conn.close()
        self.clip_id = clip_id

        self.settings = DoctorSettings(
            data_dir=self.data_dir,
            config_dir=self.config_dir,
            db_path=self.db_path,
            env_file=self.env_file,
            frigate_url="http://frigate:5000",
            mosquitto_host="mosquitto",
        )
        self.http: dict[str, Any] = {
            "http://uploader:8082/health": {"state": "HEALTHY"},
            "http://cleanup:8083/health": {"state": "HEALTHY"},
            "http://status:8084/health": {"state": "HEALTHY"},
            "http://frigate:5000/api/stats": {"service": {"version": "0.18.0"}},
        }
        self.tcp_up = True

    def probes(self, **overrides: Any) -> Probes:
        def http_json(url: str) -> Any:
            if url not in self.http:
                raise OSError(f"no route to {url}")
            return self.http[url]

        base: dict[str, Any] = {
            "http_json": http_json,
            "tcp_open": lambda _h, _p: self.tcp_up,
            "detect_host": _detection,
            "disk_usage": lambda _p: type(
                "U", (), {"total": 500 * 1024**3, "free": 200 * 1024**3}
            )(),
        }
        base.update(overrides)
        return Probes(**base)

    def report(self, **overrides: Any) -> Report:
        return doctor.run(self.settings, probes=self.probes(**overrides))

    def states(self, section: str) -> dict[str, str]:
        report = self.report()
        return {f.name: f.state for f in report.findings if f.section == section}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    now = 1_790_000_000.0
    monkeypatch.setattr(timeutil, "now", lambda: now)
    return _Env(tmp_path, now=now)


# --- the happy path -------------------------------------------------------------------


def test_healthy_installation_exits_zero(env: _Env) -> None:
    report = env.report()
    failures = [f for f in report.findings if f.state == FAIL]
    assert failures == []
    assert report.exit_code == 0


def test_every_section_is_reported(env: _Env) -> None:
    report = env.report()
    assert list(report.sections()) == [
        "host",
        "storage",
        "config",
        "database",
        "connectivity",
        "pipeline",
        "versions",
    ]


# --- host -----------------------------------------------------------------------------


def test_unsupported_host_is_a_failure(env: _Env) -> None:
    report = env.report(detect_host=lambda: _detection(supported=False))
    states = {f.name: f.state for f in report.findings if f.section == "host"}
    assert states["profile"] == FAIL
    assert report.exit_code == 1


def test_host_warnings_are_surfaced(env: _Env) -> None:
    report = env.report(detect_host=lambda: _detection(warnings=["SD card storage"]))
    assert any(
        f.section == "host" and f.state == WARN and "SD card" in f.detail for f in report.findings
    )


def test_a_detection_crash_does_not_take_doctor_down(env: _Env) -> None:
    def boom() -> host.Detection:
        raise RuntimeError("no /proc here")

    report = env.report(detect_host=boom)
    assert any(f.section == "host" and f.state == FAIL for f in report.findings)


# --- storage --------------------------------------------------------------------------


def test_missing_data_dir_is_a_failure(tmp_path: Path) -> None:
    settings = DoctorSettings(
        data_dir=tmp_path / "nope",
        config_dir=tmp_path,
        db_path=tmp_path / "state.db",
        env_file=tmp_path / ".env",
    )
    report = doctor.run(settings, probes=Probes(detect_host=_detection))
    storage = [f for f in report.findings if f.section == "storage"]
    assert storage[0].name == "data-dir"
    assert storage[0].state == FAIL


def test_low_free_space_warns(env: _Env) -> None:
    report = env.report(
        disk_usage=lambda _p: type("U", (), {"total": 100 * 1024**3, "free": 1024**3})()
    )
    states = {f.name: f.state for f in report.findings if f.section == "storage"}
    assert states["free-space"] == WARN


def test_unwritable_data_dir_is_a_failure(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write_text = Path.write_text

    def refuse(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == ".doctor-write-test":
            raise PermissionError("read-only filesystem")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)
    report = env.report()
    states = {f.name: f.state for f in report.findings if f.section == "storage"}
    assert states["writable"] == FAIL


# --- config ---------------------------------------------------------------------------


def test_invalid_cameras_yml_is_a_failure(env: _Env) -> None:
    (env.config_dir / "cameras.yml").write_text("cameras: [not-a-mapping]\n", encoding="utf-8")
    states = env.states("config")
    assert states["cameras.yml"] == FAIL


def test_world_readable_env_warns(env: _Env) -> None:
    env.env_file.chmod(0o644)
    states = env.states("config")
    assert states[".env"] == WARN


def test_env_secrets_are_never_printed(env: _Env) -> None:
    env.env_file.write_text("CCTV_STORAGE_APPLICATION_KEY=topsecret\n", encoding="utf-8")
    rendered = env.report().render()
    assert "topsecret" not in rendered


def test_missing_generated_config_warns(env: _Env) -> None:
    (env.config_dir / "frigate" / "config.yml").unlink()
    states = env.states("config")
    assert states["generated-config"] == WARN


# --- database -------------------------------------------------------------------------


def test_corrupt_database_is_a_failure(env: _Env) -> None:
    env.db_path.write_bytes(b"not a database at all")
    report = env.report()
    states = {f.name: f.state for f in report.findings if f.section == "database"}
    assert states["state.db"] == FAIL


def test_schema_behind_is_a_failure(env: _Env) -> None:
    conn = sqlite3.connect(env.db_path)
    conn.execute(
        "DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)"
    )
    conn.commit()
    conn.close()
    report = env.report()
    states = {f.name: f.state for f in report.findings if f.section == "database"}
    assert states["schema"] == FAIL


def test_missing_database_is_a_failure(env: _Env) -> None:
    env.db_path.unlink()
    report = env.report()
    states = {f.name: f.state for f in report.findings if f.section == "database"}
    assert states["state.db"] == FAIL


# --- connectivity ---------------------------------------------------------------------


def test_unreachable_component_is_a_failure(env: _Env) -> None:
    del env.http["http://uploader:8082/health"]
    report = env.report()
    states = {f.name: f.state for f in report.findings if f.section == "connectivity"}
    assert states["uploader"] == FAIL
    assert report.exit_code == 1


def test_degraded_component_warns(env: _Env) -> None:
    env.http["http://cleanup:8083/health"] = {"state": "DEGRADED"}
    states = env.states("connectivity")
    assert states["cleanup"] == WARN


def test_closed_mqtt_port_is_a_failure(env: _Env) -> None:
    report = env.report(tcp_open=lambda _h, _p: False)
    states = {f.name: f.state for f in report.findings if f.section == "connectivity"}
    assert states["mqtt"] == FAIL


def test_frigate_version_is_read_from_stats(env: _Env) -> None:
    report = env.report()
    frigate = next(f for f in report.findings if f.name == "frigate")
    assert "0.18.0" in frigate.detail


# --- pipeline -------------------------------------------------------------------------


def test_recent_clip_is_a_pass(env: _Env) -> None:
    states = env.states("pipeline")
    assert states["camera front-door"] == OK


def test_quiet_camera_warns(env: _Env) -> None:
    old = env.now - 5 * 86400.0
    conn = db.open_db(env.db_path)
    conn.execute("UPDATE clips SET start_time = ?", (old,))
    conn.commit()
    conn.close()
    report = env.report()
    camera = next(f for f in report.findings if f.name == "camera front-door")
    assert camera.state == WARN


def test_failed_permanent_uploads_warn(env: _Env) -> None:
    conn = db.open_db(env.db_path)
    db.mark_permanent_failure(
        conn,
        clip_id=env.clip_id,
        error_class="auth",
        message="403 Forbidden",
    )
    conn.close()
    report = env.report()
    failed = next(f for f in report.findings if f.name == "failed-uploads")
    assert failed.state == WARN
    assert "recover-uploads" in failed.detail


# --- the report itself ----------------------------------------------------------------


def test_report_json_and_render_agree(env: _Env) -> None:
    report = env.report()
    payload = report.to_dict()
    assert payload["counts"][OK] == len([f for f in report.findings if f.state == OK])
    assert payload["verdict"] == "Everything doctor can check is fine."


def test_credentials_in_details_are_redacted() -> None:
    finding = Finding("connectivity", "camera", FAIL, "rtsp://user:p@ss@10.0.0.5/stream")
    assert "p@ss" not in finding.to_dict()["detail"]
    assert "10.0.0.5" in finding.to_dict()["detail"]


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        Finding("host", "x", "MAYBE")


def test_worst_of_a_section_wins(env: _Env) -> None:
    report = Report(
        findings=(
            Finding("a", "one", OK),
            Finding("a", "two", WARN),
            Finding("a", "three", SKIP),
        ),
        generated_at=env.now,
    )
    assert report.worst("a") == WARN
    assert report.worst("missing") == SKIP


def test_exit_code_is_one_only_on_failure() -> None:
    generated_at = 0.0
    assert Report(findings=(Finding("s", "n", WARN),), generated_at=generated_at).exit_code == 0
    assert Report(findings=(Finding("s", "n", FAIL),), generated_at=generated_at).exit_code == 1


def test_cli_writes_the_json_report(
    env: _Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "logs"
    code = doctor.main(["--save", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"]
    assert (out).is_dir()
    assert code in (0, 1)
