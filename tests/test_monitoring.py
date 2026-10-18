"""Tests for the monitoring adapter (M5 slice 2, lld/06 §6).

The value here is the *inventory*: the monitors must come from configuration, and the
heartbeat keys must match what the tool will look for. Both are silent failures if wrong —
a config that omits a camera, or a key that disagrees with the config, leaves a monitor that
never alerts and a dashboard that looks calm.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import yaml

from cctv import db, health, monitoring, status

HEAD = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
defaults:
  detect:
    objects: [person]
  clip:
    pre_roll_s: 5
    post_roll_s: 10
cameras:
  front-door:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
  garage:
    rtsp:
      main: rtsp://192.168.1.51:554/stream1
"""


def make_config(tmp_path: Path):
    from cctv.config import load_cameras_config

    path = tmp_path / "cameras.yml"
    path.write_text(HEAD, encoding="utf-8")
    return load_cameras_config(path, environ={})


def inventory(tmp_path: Path) -> list[monitoring.Monitor]:
    return monitoring.monitors_for(
        make_config(tmp_path),
        frigate_url="http://frigate:5000",
        component_urls={
            "clip-worker": "http://clip-worker:8080",
            "uploader": "http://uploader:8082",
            "cleanup": "http://cleanup:8083",
        },
        mosquitto_host="mosquitto",
        mosquitto_port=1883,
        collect_interval_s=30.0,
    )


def names(monitors: list[monitoring.Monitor]) -> set[str]:
    return {monitor.name for monitor in monitors}


def test_the_inventory_is_derived_from_configuration(tmp_path: Path) -> None:
    monitors = inventory(tmp_path)
    probes = {m.name: m for m in monitors if isinstance(m, monitoring.Probe)}

    # clip-worker has no health endpoint by design, so it is not probed. Asking a URL that
    # does not exist is what made a healthy stack look UNKNOWN forever.
    assert set(probes) == {"frigate", "uploader", "cleanup", "mosquitto"}
    # Frigate answers /api/version with an empty body, so the probe must use /api/stats.
    assert probes["frigate"].target == "http://frigate:5000/api/stats"
    # DEGRADED should alert, so the service probes are strict.
    assert probes["uploader"].target.endswith("/health?strict=1")
    assert probes["mosquitto"].target == "tcp://mosquitto:1883"
    assert probes["mosquitto"].condition == "[CONNECTED] == true"


def test_each_camera_gets_its_own_monitor_without_editing_anything(
    tmp_path: Path,
) -> None:
    cameras = {m.name for m in inventory(tmp_path) if isinstance(m, monitoring.IngestMonitor)}

    assert {"camera-front-door", "camera-garage"} <= cameras
    assert {"storage", "disk", "backup-freshness", "retention"} <= cameras


def test_heartbeat_keys_match_what_the_tool_will_look_for() -> None:
    assert monitoring.heartbeat_key("camera-front-door") == "cctv_camera-front-door"
    # Gatus replaces spaces and separators in the key; one implementation produces both the
    # config and the push URL, so a mismatch cannot be introduced on one side only.
    assert monitoring.heartbeat_key("backup freshness") == "cctv_backup-freshness"


def test_the_rendered_config_is_deterministic(tmp_path: Path) -> None:
    adapter = monitoring.GatusAdapter(token="secret")

    first = adapter.render_config(inventory(tmp_path))
    second = adapter.render_config(inventory(tmp_path))

    assert first == second
    assert first.startswith("# GENERATED FILE — DO NOT EDIT.")


def test_external_endpoints_are_omitted_without_a_token(tmp_path: Path) -> None:
    """An invalid external-endpoint block would make Gatus reject the whole file, so it is
    omitted rather than written without the required token."""
    adapter = monitoring.GatusAdapter(token="")

    rendered = yaml.safe_load(adapter.render_config(inventory(tmp_path)))

    assert "external-endpoints" not in rendered
    assert len(rendered["endpoints"]) == 4


def test_external_endpoints_carry_the_token_and_a_heartbeat(tmp_path: Path) -> None:
    adapter = monitoring.GatusAdapter(token="secret")

    rendered = yaml.safe_load(adapter.render_config(inventory(tmp_path)))

    external = {entry["name"]: entry for entry in rendered["external-endpoints"]}
    assert external["camera-garage"]["token"] == "secret"
    assert external["camera-garage"]["group"] == "cctv"
    # Three collection intervals, floored at the tool's minimum of 10s.
    assert external["camera-garage"]["heartbeat"]["interval"] == "90s"


def test_the_alert_policy_is_recovery_aware_and_not_trigger_happy(
    tmp_path: Path,
) -> None:
    adapter = monitoring.GatusAdapter(token="secret", ntfy_url="http://ntfy:80")

    rendered = yaml.safe_load(adapter.render_config(inventory(tmp_path)))

    ntfy = rendered["alerting"]["ntfy"]
    assert ntfy["url"] == "http://ntfy:80"
    assert ntfy["topic"] == "cctv"
    assert ntfy["default-alert"]["failure-threshold"] == 3
    assert ntfy["default-alert"]["send-on-resolved"] is True


def test_the_config_file_is_owner_only(tmp_path: Path) -> None:
    import stat

    path = tmp_path / "gatus" / "config.yaml"
    monitoring.GatusAdapter(token="secret").write_config(path, inventory(tmp_path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_gatus_keeps_its_state_in_the_named_volume() -> None:
    """A host directory for Gatus's state is the ownership trap that crashed fresh installs."""
    adapter = monitoring.GatusAdapter(token="secret")
    rendered = yaml.safe_load(adapter.render_config([]))

    assert rendered["storage"]["path"] == "/gatus-data/gatus.db"


def test_compose_puts_the_generated_config_where_the_collector_can_write() -> None:
    """#3: a bind mount of a not-yet-existing directory is created root-owned, and the image
    runs as uid 10001 — which left Gatus in a crash loop on a fresh install."""
    path = Path(__file__).resolve().parents[1] / "compose.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = document["services"]

    config_path = services["status"]["environment"]["CCTV_STATUS_GATUS_CONFIG"]
    assert config_path == "/data/gatus/config.yaml"
    assert services["gatus"]["environment"]["GATUS_CONFIG_PATH"] == config_path
    assert any(entry.endswith(":/data:ro") for entry in services["gatus"]["volumes"])
    assert "gatus-data:/gatus-data" in services["gatus"]["volumes"]
    assert "gatus-data" in document["volumes"]
    # No service may bind-mount a subdirectory of config/ that it must create itself.
    assert not any("config/gatus" in str(entry) for entry in services["status"]["volumes"])


# ---------------------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------------------


def summary_with(**overrides) -> status.StatusSummary:
    backlog = overrides.pop(
        "backlog",
        db.QueueSummary(
            counts={},
            oldest_pending_age_s=None,
            uploaded_24h=0,
            last_successful_upload_at=None,
        ),
    )
    base: dict = dict(
        site="parents-home",
        generated_at=1_700_000_000.0,
        components=(status.ComponentStatus(name="cleanup", state=health.HEALTHY),),
        cameras=(
            status.CameraStatus(name="front-door", state=status.ONLINE),
            status.CameraStatus(name="garage", state=status.OFFLINE),
        ),
        disk=status.DiskFacts(
            free_bytes=400 * 1024**3,
            total_bytes=500 * 1024**3,
            clips_bytes=1024,
            media_bytes=0,
        ),
        projection=status.StorageProjection(None, None, "no history"),
        backlog=backlog,
        local_days=90,
        cloud_days=60,
        next_cleanup_at=None,
    )
    base.update(overrides)
    return status.StatusSummary(**base)


def test_heartbeats_are_per_camera_and_per_concern() -> None:
    beats = monitoring.heartbeats_for(summary_with())

    assert beats[monitoring.heartbeat_key("camera-front-door")] == (True, "ONLINE")
    assert beats[monitoring.heartbeat_key("camera-garage")] == (False, "OFFLINE")
    assert beats[monitoring.heartbeat_key("disk")][0] is True
    assert beats[monitoring.heartbeat_key("backup-freshness")][0] is True


def test_a_stalled_backup_fails_its_heartbeat() -> None:
    stalled = db.QueueSummary(
        counts={"PENDING": 2},
        oldest_pending_age_s=4000.0,
        uploaded_24h=0,
        last_successful_upload_at=1_700_000_000.0 - 4000.0,
    )

    beats = monitoring.heartbeats_for(summary_with(backlog=stalled))

    assert beats[monitoring.heartbeat_key("backup-freshness")][0] is False


# ---------------------------------------------------------------------------------------
# The push itself
# ---------------------------------------------------------------------------------------


class _Capture(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str | None]]] = []

    def do_POST(self) -> None:
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_a_heartbeat_is_a_post_with_a_bearer_token() -> None:
    _Capture.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        ok = monitoring.push_heartbeat(
            base_url=f"http://127.0.0.1:{port}",
            key="cctv_camera-front-door",
            token="secret",
            ok=False,
            detail="OFFLINE",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert ok is True
    path, authorization = _Capture.seen[-1]
    assert path.startswith("/api/v1/endpoints/cctv_camera-front-door/external?")
    assert "success=false" in path
    assert "error=OFFLINE" in path
    assert authorization == "Bearer secret"


def test_a_heartbeat_never_raises_when_the_tool_is_down() -> None:
    # Port 1 is closed; the collector must keep serving its own page regardless.
    assert (
        monitoring.push_heartbeat(
            base_url="http://127.0.0.1:1",
            key="cctv_disk",
            token="secret",
            ok=True,
        )
        is False
    )


def test_no_token_means_no_push_attempt() -> None:
    assert (
        monitoring.push_heartbeat(base_url="http://127.0.0.1:1", key="cctv_disk", token="", ok=True)
        is False
    )
