"""The monitoring adapter: what we hand an external tool so it can alert (lld/06 §6).

We do not build alert delivery, and we do not build a dashboard. Gatus does both, and this
module is the only place that knows its name: it turns our configuration into the monitors a
tool should run, and it pushes the heartbeats that let the tool notice when *we* stop.

Three shapes of monitor, and the split is deliberate:

**HTTP probes** for the things that expose an endpoint. `?strict=1` is used so `DEGRADED`
alerts rather than passing as healthy — a backlog that never pages anyone is not monitoring.

**TCP probes** for the broker, because there is nothing HTTP to ask and a broker that is
listening is the question worth asking.

**Push heartbeats** for anything dynamic or not reachable: one per camera, plus disk, backup
freshness, retention and storage. A camera monitor cannot be a static check (there is no
per-camera URL), and regenerating the tool's config whenever a camera is added would mean
restarting it. A missed heartbeat is itself the alert, so the tool still notices when the
collector dies — the failure a polling-only design cannot see.

Gatus is the default because it is the only one of the two that is configured from a file we
generate. `MonitoringAdapter` exists so that stays a reversible choice.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import health
from .config import CamerasConfig
from .status import ONLINE, StatusSummary

__all__ = [
    "GATUS_GROUP",
    "GatusAdapter",
    "IngestMonitor",
    "Monitor",
    "Probe",
    "heartbeat_key",
    "heartbeats_for",
    "monitors_for",
    "push_heartbeat",
]

logger = logging.getLogger(__name__)

#: One group for everything, so the dashboard reads as one system rather than four services.
GATUS_GROUP = "cctv"

#: `failure-threshold` before an alert fires: one failed check is a blip (lld/06 §6.5).
DEFAULT_FAILURE_THRESHOLD = 3

#: Consecutive successes before an incident is marked resolved.
DEFAULT_SUCCESS_THRESHOLD = 2


@dataclass(frozen=True, slots=True)
class Probe:
    """A monitor the tool evaluates itself."""

    name: str
    kind: str  # "http" | "tcp"
    target: str
    condition: str
    interval_s: float = 30.0


@dataclass(frozen=True, slots=True)
class IngestMonitor:
    """A monitor that only exists because we push to it.

    `heartbeat_interval_s` is the tool-side deadline: no update within it and the tool raises
    the alert itself. That is what makes a dead collector visible.
    """

    name: str
    heartbeat_interval_s: float


#: The monitor inventory, per lld/06 §6.4.
Monitor = Probe | IngestMonitor


class MonitoringAdapter(Protocol):
    """Enough of an interface to keep the tool choice reversible."""

    def render_config(self, monitors: list[Monitor]) -> str: ...

    def write_config(self, path: Path, monitors: list[Monitor]) -> None: ...


def heartbeat_key(name: str) -> str:
    """Gatus's external-endpoint key: `<group>_<name>`, with unsafe characters replaced.

    Kept in one function because the key appears in a URL the collector builds and in a
    config the tool reads; two implementations would eventually disagree, and the symptom
    would be heartbeats that silently 404.
    """
    safe = "".join(
        "-" if character in " /_,.#+&" else character for character in name
    ).lower()
    return f"{GATUS_GROUP}_{safe}"


def _probe_target(url: str, suffix: str) -> str:
    return url.rstrip("/") + suffix


def monitors_for(
    config: CamerasConfig,
    *,
    frigate_url: str,
    component_urls: dict[str, str],
    mosquitto_host: str | None,
    mosquitto_port: int,
    collect_interval_s: float,
) -> list[Monitor]:
    """The full inventory, derived from configuration rather than typed by hand.

    Camera monitors are generated from `cameras.yml`, so adding a camera needs no edit here —
    which is the same reason the clip paths are derived (INV-3).
    """
    heartbeat = max(10.0, 3 * collect_interval_s)
    probes: list[Monitor] = [
        # `/api/stats` rather than `/api/version`: Frigate answers the latter with an empty
        # body, so a probe against it can never pass (frigate-integration §9).
        Probe(
            name="frigate",
            kind="http",
            target=_probe_target(frigate_url, "/api/stats"),
            condition="[STATUS] == 200",
            interval_s=collect_interval_s,
        )
    ]
    for name in ("uploader", "cleanup"):
        url = component_urls.get(name)
        if url:
            probes.append(
                Probe(
                    name=name,
                    kind="http",
                    target=_probe_target(url, "/health?strict=1"),
                    condition="[STATUS] == 200",
                    interval_s=collect_interval_s,
                )
            )
    if mosquitto_host:
        probes.append(
            Probe(
                name="mosquitto",
                kind="tcp",
                target=f"tcp://{mosquitto_host}:{mosquitto_port}",
                condition="[CONNECTED] == true",
                interval_s=collect_interval_s,
            )
        )

    ingest: list[Monitor] = [
        IngestMonitor(name=f"camera-{camera}", heartbeat_interval_s=heartbeat)
        for camera in sorted(config.enabled_cameras())
    ]
    ingest.extend(
        IngestMonitor(name=name, heartbeat_interval_s=heartbeat)
        for name in ("storage", "disk", "backup-freshness", "retention")
    )
    return probes + ingest


def _gatus_endpoint(probe: Probe) -> dict[str, Any]:
    return {
        "name": probe.name,
        "group": GATUS_GROUP,
        "url": probe.target,
        "interval": f"{probe.interval_s:g}s",
        "conditions": [probe.condition],
        "alerts": [{"type": "ntfy"}],
    }


@dataclass(frozen=True, slots=True)
class GatusAdapter:
    """Gatus's config file: probes as `endpoints`, heartbeats as `external-endpoints`.

    The ntfy provider is configured once, with a default alert, so every monitor inherits the
    same thresholds. Per-monitor alert tuning is deliberately not exposed yet: the documented
    policy is small (lld/06 §6.5) and a generated file that tries to express all of it would
    be harder to read than the policy itself.
    """

    ntfy_url: str = "http://ntfy:80"
    ntfy_topic: str = "cctv"
    ntfy_priority: int = 3
    token: str = ""
    status_page_url: str | None = None

    def render_config(self, monitors: list[Monitor]) -> str:
        probes = [monitor for monitor in monitors if isinstance(monitor, Probe)]
        ingests = [
            monitor for monitor in monitors if isinstance(monitor, IngestMonitor)
        ]

        config: dict[str, Any] = {
            "storage": {"type": "sqlite", "path": "/gatus-data/gatus.db"},
            "alerting": {
                "ntfy": {
                    "url": self.ntfy_url,
                    "topic": self.ntfy_topic,
                    "priority": self.ntfy_priority,
                    **(
                        {"click": self.status_page_url}
                        if self.status_page_url is not None
                        else {}
                    ),
                    "default-alert": {
                        "failure-threshold": DEFAULT_FAILURE_THRESHOLD,
                        "success-threshold": DEFAULT_SUCCESS_THRESHOLD,
                        "send-on-resolved": True,
                    },
                }
            },
            "endpoints": [_gatus_endpoint(probe) for probe in probes],
        }
        if ingests:
            # The token is required by Gatus for external endpoints. Without one the config
            # would be rejected as a whole, taking the probes with it, so the section is
            # omitted rather than written invalid.
            if not self.token:
                logger.warning(
                    "no monitoring token configured: push heartbeats are disabled",
                    extra={"count": len(ingests)},
                )
            else:
                config["external-endpoints"] = [
                    {
                        "name": monitor.name,
                        "group": GATUS_GROUP,
                        "token": self.token,
                        "heartbeat": {"interval": f"{monitor.heartbeat_interval_s:g}s"},
                        "alerts": [{"type": "ntfy"}],
                    }
                    for monitor in ingests
                ]
        header = (
            "# GENERATED FILE — DO NOT EDIT.\n"
            "# Written by cctv-status from cameras.yml and the environment. Camera monitors\n"
            "# are generated from configuration (INV-3); edits here are lost on the next\n"
            "# generation.\n"
        )
        return header + yaml.safe_dump(
            config, sort_keys=False, default_flow_style=False
        )

    def write_config(self, path: Path, monitors: list[Monitor]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_config(monitors), encoding="utf-8")
        # The file carries the heartbeat token, so it is readable by its owner only.
        path.chmod(0o600)


def heartbeats_for(summary: StatusSummary) -> dict[str, tuple[bool, str]]:
    """One heartbeat per dynamic monitor, derived from a single status summary.

    A heartbeat is binary: `success=false` is what raises the tool's alert. Each mapping below
    is therefore "is this *good enough that nobody needs to be woken?" rather than "is this
    perfect?" — a disk at 12 % free is a warning, not a page at 3 a.m.
    """
    out: dict[str, tuple[bool, str]] = {}

    for camera in summary.cameras:
        out[heartbeat_key(f"camera-{camera.name}")] = (
            camera.state == ONLINE,
            camera.state,
        )

    disk_ok = summary.disk is not None and summary.disk_state == health.HEALTHY
    out[heartbeat_key("disk")] = (disk_ok, summary.disk_state)

    # Backlog staleness is the honest "is backup happening?" signal: an empty queue on a
    # quiet site is not a failure, and `backlog_stalled` already encodes that.
    backup_ok = not summary.backlog_stalled
    out[heartbeat_key("backup-freshness")] = (
        backup_ok,
        "stalled" if not backup_ok else "fresh",
    )

    cleanup = summary.component("cleanup")
    retention_ok = cleanup is not None and cleanup.state in {
        health.HEALTHY,
        health.DISABLED,
    }
    out[heartbeat_key("retention")] = (
        retention_ok,
        "unobserved" if cleanup is None else cleanup.state,
    )

    storage_ok = summary.backlog.failed_permanent == 0
    out[heartbeat_key("storage")] = (
        storage_ok,
        "clips are failing permanently" if not storage_ok else "ok",
    )
    return out


def push_heartbeat(
    *,
    base_url: str,
    key: str,
    token: str,
    ok: bool,
    detail: str = "",
    timeout: float = 5.0,
) -> bool:
    """POST one heartbeat to the tool. Returns whether it was accepted.

    A failed push is logged and never raised: the status page is the product, and losing a
    heartbeat to a tool that is restarting must not also break the page. The next collection
    retries, and the tool's own grace window turns a sustained silence into an alert.
    """
    if not token:
        return False
    query = f"success={'true' if ok else 'false'}"
    if not ok and detail:
        from urllib.parse import quote

        query += f"&error={quote(detail[:500])}"
    url = f"{base_url.rstrip('/')}/api/v1/endpoints/{key}/external?{query}"
    request = urllib.request.Request(
        url, method="POST", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning(
            "heartbeat not accepted",
            extra={"endpoint": key, "reason": str(exc)},
        )
        return False
