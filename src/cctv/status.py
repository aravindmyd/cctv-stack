"""Aggregate status: the one place that answers "is my CCTV actually working?".

[INV-6](../../../internal/architecture/invariants.md) requires every component to be honest
about itself. This module is the other half: taking four honest per-service reports, Frigate's
own view of the cameras, the database and the filesystem, and deriving a single answer from
them. It is specified by
[`lld/06`](../../../internal/architecture/lld/06-health-and-monitoring.md) and
[`service-contracts.md`](../../../internal/architecture/contracts/service-contracts.md) §2.

Three rules shape the code, and each exists because the obvious alternative lies:

**Partial observation is reported, never blanked.** If Frigate is unreachable, the page still
says what it knows about the uploader and the disk, and names what it could not observe. A page
that renders blank, or green, when a third of its sources are missing is worse than no page.

**A stale reading is `UNKNOWN`, never `HEALTHY`.** A component that reported health ten minutes
ago and has since died must not look fine. This is the most common real-world monitoring
failure and it is silent by construction.

**The headline is generated from the same state the details show.** "Recording is working"
requires every camera online and Frigate healthy; it is not a template that can drift from the
numbers underneath it.

Nothing here mutates: the collector reads, derives, and renders. It never writes a clip, a
state or a config. That is what lets `/status` be published on the LAN without becoming an
attack surface.
"""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import socket
import sqlite3
import statistics
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import __version__, db, health, timeutil
from .config import CamerasConfig
from .timeutil import Instant

__all__ = [
    "COMPONENT_NAMES",
    "CameraStatus",
    "ComponentStatus",
    "DiskFacts",
    "StatusSettings",
    "StatusSummary",
    "answer",
    "overall_state",
    "render_html",
    "storage_projection",
]

logger = logging.getLogger(__name__)

#: The collector's own component name, in logs, health and `system_events`.
COMPONENT = "status-collector"

#: Everything the collector expects to hear from, in the order the page lists them. Frigate and
#: storage are not here: neither speaks our `/health` contract, and both are observed
#: differently.
COMPONENT_NAMES: tuple[str, ...] = ("clip-worker", "uploader", "cleanup")

#: Camera states. Separate from the component states on purpose: a camera is `ONLINE`,
#: `OFFLINE` or `QUIET`, and calling it `HEALTHY` would invite somebody to alert on the wrong
#: vocabulary.
ONLINE = "ONLINE"
OFFLINE = "OFFLINE"
QUIET = "QUIET"
UNKNOWN_CAMERA = "UNKNOWN"

#: Frigate's live UI and the monitoring tool are links on the page, not checks.
DEFAULT_FRIGATE_UI_URL = "https://127.0.0.1:8971"

#: A component that misses this many consecutive polls becomes `UNKNOWN` (lld/06 §3.1).
MISSES_BEFORE_UNKNOWN = 2


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusSettings:
    """Everything the collector's behaviour depends on, parsed once at start-up.

    Defaults are the documented ones (lld/06 §9). An invalid value is a start-up failure
    rather than a silent coercion: a status page that quietly used the wrong threshold would
    be worse than one that refused to start.
    """

    data_dir: Path
    db_path: Path
    collect_interval_s: float = 30.0
    disk_interval_s: float = 60.0
    health_port: int = 8084
    health_host: str = "0.0.0.0"
    frigate_url: str = "http://frigate:5000"
    mosquitto_host: str | None = "mosquitto"
    mosquitto_port: int = 1883
    component_urls: Mapping[str, str] = field(
        default_factory=lambda: {
            "clip-worker": "http://clip-worker:8080",
            "uploader": "http://uploader:8082",
            "cleanup": "http://cleanup:8083",
        }
    )
    camera_offline_grace_s: float = 300.0
    camera_quiet_multiplier: float = 3.0
    camera_quiet_min_gap_s: float = 7200.0
    camera_quiet_min_events_7d: int = 20
    disk_warn_pct: float = 15.0
    disk_critical_pct: float = 5.0
    disk_project_warn_days: float = 60.0
    disk_project_critical_days: float = 30.0
    disk_projection_min_days_history: float = 3.0
    backup_stale_warn_s: float = 1800.0
    backup_stale_critical_s: float = 21600.0
    cleanup_interval_s: float = 3600.0
    frigate_ui_url: str = DEFAULT_FRIGATE_UI_URL
    monitoring_url: str | None = None
    #: Where the generated monitoring config is written, and where heartbeats go. Both are
    #: optional: a local-only deployment with no monitoring tool must still work.
    gatus_config_path: Path | None = None
    gatus_url: str | None = None
    monitoring_token: str = ""
    ntfy_url: str = "http://ntfy:80"
    ntfy_topic: str = "cctv"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> StatusSettings:
        env = os.environ if environ is None else environ

        def number(name: str, default: float) -> float:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number, got {raw!r}.") from exc

        def integer(name: str, default: int) -> int:
            value = number(name, float(default))
            if value != int(value):
                raise ValueError(f"{name} must be a whole number, got {value!r}.")
            return int(value)

        components = {
            "clip-worker": env.get("CCTV_STATUS_CLIP_WORKER_URL", "http://clip-worker:8080"),
            "uploader": env.get("CCTV_STATUS_UPLOADER_URL", "http://uploader:8082"),
            "cleanup": env.get("CCTV_STATUS_CLEANUP_URL", "http://cleanup:8083"),
        }

        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))

        settings = cls(
            data_dir=data_dir,
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            collect_interval_s=number("CCTV_STATUS_COLLECT_INTERVAL_S", 30.0),
            disk_interval_s=number("CCTV_STATUS_DISK_INTERVAL_S", 60.0),
            health_port=integer("CCTV_STATUS_HEALTH_PORT", 8084),
            health_host=env.get("CCTV_HEALTH_HOST", "0.0.0.0"),
            frigate_url=env.get("CCTV_FRIGATE_URL", "http://frigate:5000"),
            mosquitto_host=(env.get("CCTV_MQTT_HOST") or "").strip() or None,
            mosquitto_port=integer("CCTV_MQTT_PORT", 1883),
            component_urls=components,
            camera_offline_grace_s=number("CCTV_STATUS_CAMERA_OFFLINE_GRACE_S", 300.0),
            camera_quiet_multiplier=number("CCTV_STATUS_CAMERA_QUIET_MULTIPLIER", 3.0),
            camera_quiet_min_gap_s=number("CCTV_STATUS_CAMERA_QUIET_MIN_GAP_S", 7200.0),
            camera_quiet_min_events_7d=integer("CCTV_STATUS_CAMERA_QUIET_MIN_EVENTS_7D", 20),
            disk_warn_pct=number("CCTV_STATUS_DISK_WARN_PCT", 15.0),
            disk_critical_pct=number("CCTV_STATUS_DISK_CRITICAL_PCT", 5.0),
            disk_project_warn_days=number("CCTV_STATUS_DISK_PROJECT_WARN_DAYS", 60.0),
            disk_project_critical_days=number("CCTV_STATUS_DISK_PROJECT_CRITICAL_DAYS", 30.0),
            disk_projection_min_days_history=number(
                "CCTV_STATUS_DISK_PROJECTION_MIN_DAYS_HISTORY", 3.0
            ),
            backup_stale_warn_s=number("CCTV_STATUS_BACKUP_STALE_WARN_S", 1800.0),
            backup_stale_critical_s=number("CCTV_STATUS_BACKUP_STALE_CRITICAL_S", 21600.0),
            cleanup_interval_s=number("CCTV_CLEANUP_RUN_INTERVAL_S", 3600.0),
            frigate_ui_url=env.get("CCTV_STATUS_FRIGATE_UI_URL", DEFAULT_FRIGATE_UI_URL),
            monitoring_url=(env.get("CCTV_STATUS_MONITORING_URL") or "").strip() or None,
            gatus_config_path=(
                Path(value)
                if (value := (env.get("CCTV_STATUS_GATUS_CONFIG") or "").strip())
                else None
            ),
            gatus_url=(env.get("CCTV_STATUS_GATUS_URL") or "").strip() or None,
            monitoring_token=(env.get("CCTV_MONITORING_TOKEN") or "").strip(),
            ntfy_url=env.get("CCTV_STATUS_NTFY_URL", "http://ntfy:80"),
            ntfy_topic=env.get("CCTV_STATUS_NTFY_TOPIC", "cctv"),
        )
        if settings.collect_interval_s <= 0:
            raise ValueError("CCTV_STATUS_COLLECT_INTERVAL_S must be positive.")
        if settings.health_port < 0:
            raise ValueError("CCTV_STATUS_HEALTH_PORT cannot be negative.")
        if not 0 <= settings.disk_critical_pct <= settings.disk_warn_pct <= 100:
            raise ValueError(
                "disk thresholds must satisfy 0 <= CCTV_STATUS_DISK_CRITICAL_PCT "
                "<= CCTV_STATUS_DISK_WARN_PCT <= 100."
            )
        if settings.camera_quiet_min_events_7d < 0:
            raise ValueError("CCTV_STATUS_CAMERA_QUIET_MIN_EVENTS_7D cannot be negative.")
        return settings


# ---------------------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiskFacts:
    """What the filesystem says, measured rather than assumed."""

    free_bytes: int
    total_bytes: int
    clips_bytes: int
    media_bytes: int

    @property
    def free_pct(self) -> float:
        return 100.0 if self.total_bytes <= 0 else 100.0 * self.free_bytes / self.total_bytes


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """One service's contribution to the page."""

    name: str
    state: str
    summary: str = ""
    version: str | None = None
    code: str | None = None
    observed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "summary": self.summary,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class CameraStatus:
    name: str
    state: str
    last_clip_at: Instant | None = None
    detection_fps: float | None = None
    offline_since: Instant | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "last_clip_at": self.last_clip_at,
            "detection_fps": self.detection_fps,
            "offline_since": self.offline_since,
        }


@dataclass(frozen=True, slots=True)
class StorageProjection:
    """Days until full, and whether there is enough history to say."""

    days_to_full: float | None
    growth_bytes_per_day: float | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StatusSummary:
    """The `/status` payload. One object, so the JSON and the HTML cannot disagree."""

    site: str
    generated_at: Instant
    components: tuple[ComponentStatus, ...]
    cameras: tuple[CameraStatus, ...]
    disk: DiskFacts | None
    projection: StorageProjection
    backlog: db.QueueSummary
    local_days: int
    cloud_days: int
    next_cleanup_at: Instant | None
    data_dir: str = ""
    disk_warn_pct: float = 15.0
    disk_critical_pct: float = 5.0
    project_warn_days: float = 60.0
    project_critical_days: float = 30.0
    backup_warn_s: float = 1800.0
    backup_critical_s: float = 21600.0
    unobserved: tuple[str, ...] = ()

    # -- derived ------------------------------------------------------------------------

    @property
    def cameras_offline(self) -> tuple[CameraStatus, ...]:
        return tuple(c for c in self.cameras if c.state == OFFLINE)

    @property
    def cameras_quiet(self) -> tuple[CameraStatus, ...]:
        return tuple(c for c in self.cameras if c.state == QUIET)

    def component(self, name: str) -> ComponentStatus | None:
        return next((c for c in self.components if c.name == name), None)

    @property
    def disk_state(self) -> str:
        if self.disk is None:
            return health.UNKNOWN
        free_pct = self.disk.free_pct
        days = self.projection.days_to_full
        if free_pct < self.disk_critical_pct:
            return health.UNHEALTHY
        if days is not None and days < self.project_critical_days:
            return health.UNHEALTHY
        if free_pct < self.disk_warn_pct or (days is not None and days < self.project_warn_days):
            return health.DEGRADED
        return health.HEALTHY

    @property
    def overall(self) -> str:
        return overall_state(self)

    @property
    def answer(self) -> str:
        return answer(self)

    @property
    def storage_stale_s(self) -> float | None:
        """Seconds since the last successful upload, if one has ever happened."""
        last = self.backlog.last_successful_upload_at
        return None if last is None else max(0.0, self.generated_at - last)

    @property
    def backlog_stalled(self) -> bool:
        """A non-empty backlog with no successful upload in the warning window.

        Deliberately includes the "never uploaded" case only when there is something waiting:
        a fresh install with an empty queue is expected, and alarming on it would teach the
        operator to ignore the page in the first hour they own it.
        """
        if self.backlog.held == 0:
            return False
        stale = self.storage_stale_s
        return stale is None or stale > self.backup_warn_s

    def to_dict(self) -> dict[str, Any]:
        disk = None
        if self.disk is not None:
            disk = {
                "data_dir": self.data_dir,
                "total_bytes": self.disk.total_bytes,
                "free_bytes": self.disk.free_bytes,
                "clips_bytes": self.disk.clips_bytes,
                "projected_days_to_full": self.projection.days_to_full,
                "growth_bytes_per_day": self.projection.growth_bytes_per_day,
            }
        return {
            "site": self.site,
            "version": __version__,
            "generated_at": self.generated_at,
            "overall": self.overall,
            "overall_code": _overall_code(self),
            "answer": self.answer,
            "components": [c.to_dict() for c in self.components],
            "cameras": [c.to_dict() for c in self.cameras],
            "storage": disk,
            "projection_reason": self.projection.reason,
            "backlog": {
                "pending": self.backlog.pending,
                "uploading": self.backlog.uploading,
                "retry_wait": self.backlog.retry_wait,
                "failed_permanent": self.backlog.failed_permanent,
                "skipped_by_policy": self.backlog.skipped_by_policy,
                "uploaded_24h": self.backlog.uploaded_24h,
                "oldest_pending_age_s": self.backlog.oldest_pending_age_s,
            },
            "retention": {
                "local_days": self.local_days,
                "cloud_days": self.cloud_days,
                "next_cleanup_at": self.next_cleanup_at,
            },
            "unobserved": list(self.unobserved),
        }


def overall_state(summary: StatusSummary) -> str:
    """The contract's §2.3, top to bottom. Anything unrecognised is `UNKNOWN`, never healthy."""
    if summary.cameras_offline:
        return health.UNHEALTHY
    if any(c.state == health.UNHEALTHY for c in summary.components):
        return health.UNHEALTHY
    if summary.disk_state == health.UNHEALTHY:
        return health.UNHEALTHY
    stale = summary.storage_stale_s
    if stale is not None and summary.backlog.held and stale > summary.backup_critical_s:
        return health.UNHEALTHY

    if any(c.state == health.DEGRADED for c in summary.components):
        return health.DEGRADED
    if summary.cameras_quiet:
        return health.DEGRADED
    if summary.disk_state == health.DEGRADED:
        return health.DEGRADED
    if summary.backlog_stalled:
        return health.DEGRADED
    if summary.backlog.failed_permanent:
        return health.DEGRADED

    if any(c.state == health.UNKNOWN for c in summary.components):
        return health.UNKNOWN
    if summary.disk is None:
        return health.UNKNOWN
    return health.HEALTHY


def _overall_code(summary: StatusSummary) -> str | None:
    """The single code a human or an alert rule should act on first."""
    if summary.cameras_offline:
        return "CAMERA_OFFLINE"
    for component in summary.components:
        if component.state == health.UNHEALTHY and component.code:
            return component.code
    if summary.disk_state == health.UNHEALTHY:
        return "DISK_CRITICAL"
    if summary.backlog.failed_permanent:
        return "UPLOAD_FAILED_PERMANENT"
    if summary.backlog_stalled:
        return "STORAGE_STALE"
    for component in summary.components:
        if component.state == health.DEGRADED and component.code:
            return component.code
    if summary.cameras_quiet:
        return "CAMERA_QUIET"
    if summary.disk_state == health.DEGRADED:
        return "DISK_PRESSURE"
    return None


def answer(summary: StatusSummary) -> str:
    """The headline, composed in priority order (lld/06 §3.3).

    Never a bare "OK": it must say what is working, so a reader can spot the omission. The
    order matters — the first condition that is true is the one worth acting on.
    """
    if summary.cameras_offline:
        names = ", ".join(c.name for c in summary.cameras_offline)
        verb = "is" if len(summary.cameras_offline) == 1 else "are"
        return f"Recording is impacted. {names} {verb} offline."

    frigate = summary.component("frigate")
    if frigate is not None and (not frigate.observed or frigate.state == health.UNHEALTHY):
        return "Recording cannot be verified. Frigate did not answer."

    if summary.disk_state == health.UNHEALTHY and summary.disk is not None:
        free = _human_bytes(summary.disk.free_bytes)
        return f"Recording will stop soon. Only {free} of disk remains."

    if summary.backlog.failed_permanent:
        return (
            f"Clips are being produced, but {summary.backlog.failed_permanent} "
            f"{'is' if summary.backlog.failed_permanent == 1 else 'are'} not reaching the cloud."
        )

    if summary.backlog_stalled:
        stale = summary.storage_stale_s
        return (
            "Recording is working. Cloud backup has never succeeded."
            if stale is None
            else f"Recording is working. Cloud backup stalled {_human_age(stale)} ago."
        )

    worker = summary.component("clip-worker")
    if worker is not None and worker.state in {health.UNHEALTHY, health.UNKNOWN}:
        return "Cameras are recording, but clips are not being created."

    if any(c.name == "cleanup" and c.state == health.UNKNOWN for c in summary.components):
        return "Recording and backup are working. Retention has not run recently."

    return "Recording, clip creation, and cloud backup are all working."


# ---------------------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------------------


@dataclass
class StatusState:
    """Mutable state the collector keeps between polls.

    Kept out of `StatusSummary` so the served payload stays a value: the summary a request
    reads is the summary the loop produced, with no shared mutable structure behind it.
    """

    offline_since: dict[str, Instant] = field(default_factory=dict)
    misses: dict[str, int] = field(default_factory=dict)
    last_health: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    last_disk: DiskFacts | None = None
    last_disk_at: Instant | None = None
    last_collect_at: Instant | None = None
    last_error: str | None = None


def _http_json(url: str, *, timeout: float) -> Mapping[str, Any]:
    """GET a JSON object. Raises on anything unexpected so the caller can decide."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not return a JSON object.")
    return payload


def _disk_facts(data_dir: Path) -> DiskFacts:
    usage = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent)
    return DiskFacts(
        free_bytes=int(usage.free),
        total_bytes=int(usage.total),
        clips_bytes=_dir_bytes(data_dir / "clips"),
        media_bytes=_dir_bytes(data_dir / "media"),
    )


def _dir_bytes(path: Path) -> int:
    """Total size of a tree, missing directories counting as zero.

    Walked rather than inferred from `disk_usage`: the interesting number is how fast *our*
    footage grows, and the host's other residents should not appear in a projection about
    this system's own disk use.
    """
    total = 0
    if not path.exists():
        return total
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(Path(root, name))
            except OSError:
                # A file that vanished mid-walk is not a reason to fail a disk report.
                continue
    return total


def storage_projection(
    samples: list[db.DiskSample],
    *,
    free_bytes: int,
    min_days_history: float,
    window_days: float = 7.0,
) -> StorageProjection:
    """Days until full from the trailing growth rate (lld/06 §4).

    Suppressed rather than guessed below `min_days_history` of samples: a confident wrong
    number is worse than "not enough data yet", and the first days of a deployment are exactly
    when a wrong number would be read as fact.
    """
    if not samples:
        return StorageProjection(None, None, "no disk history yet")
    span_days = (samples[-1].at - samples[0].at) / 86400.0
    if span_days < min_days_history:
        return StorageProjection(
            None,
            None,
            f"only {span_days:.1f} days of history (need {min_days_history:g})",
        )

    first = samples[0]
    last = samples[-1]
    grown = (last.clips_bytes + last.media_bytes) - (first.clips_bytes + first.media_bytes)
    per_day = grown / span_days
    if per_day <= 0:
        return StorageProjection(None, per_day, "footage is not growing")
    return StorageProjection(free_bytes / per_day, per_day)


def _median_gap(times: list[Instant]) -> float | None:
    if len(times) < 2:
        return None
    ordered = sorted(times)
    gaps = [b - a for a, b in pairwise(ordered)]
    return statistics.median(gaps)


def camera_status(
    *,
    name: str,
    stats: Mapping[str, Any] | None,
    last_clip_at: Instant | None,
    clip_times: list[Instant],
    offline_since: Instant | None,
    now: Instant,
    settings: StatusSettings,
) -> CameraStatus:
    """One camera's state, from Frigate's view and the clip history.

    Frigate's `camera_fps` is the signal its own MQTT status messages are derived from, and
    the collector already polls `/api/stats`, so it is read there rather than opening a second
    subscription that could disagree with it.
    """
    camera_stats: Mapping[str, Any] = {}
    if stats is not None:
        cameras = stats.get("cameras")
        if isinstance(cameras, Mapping):
            entry = cameras.get(name)
            if isinstance(entry, Mapping):
                camera_stats = entry
    else:
        # Frigate did not answer, so nothing is known about this camera. Claiming `ONLINE`
        # from an absence of evidence is exactly the lie this module exists to prevent.
        return CameraStatus(name=name, state=UNKNOWN_CAMERA, last_clip_at=last_clip_at)

    camera_fps = _float_or_none(camera_stats.get("camera_fps"))
    detection_fps = _float_or_none(camera_stats.get("detection_fps"))
    live = camera_fps is None or camera_fps > 0.0

    if not live:
        since = offline_since if offline_since is not None else now
        offline_for = now - since
        if offline_for >= settings.camera_offline_grace_s:
            return CameraStatus(
                name=name,
                state=OFFLINE,
                last_clip_at=last_clip_at,
                detection_fps=detection_fps,
                offline_since=since,
            )
        # Within the grace window: still `ONLINE` on the page. Flap suppression is the point.
        return CameraStatus(
            name=name,
            state=ONLINE,
            last_clip_at=last_clip_at,
            detection_fps=detection_fps,
        )

    if last_clip_at is not None and len(clip_times) >= settings.camera_quiet_min_events_7d:
        gap = _median_gap(clip_times) or settings.camera_quiet_min_gap_s
        expected = max(gap, settings.camera_quiet_min_gap_s)
        if now - last_clip_at > max(settings.camera_quiet_multiplier * expected, 86400.0):
            return CameraStatus(
                name=name,
                state=QUIET,
                last_clip_at=last_clip_at,
                detection_fps=detection_fps,
            )

    return CameraStatus(
        name=name, state=ONLINE, last_clip_at=last_clip_at, detection_fps=detection_fps
    )


def _retention_days(config: CamerasConfig, *, key: str) -> int:
    """The strictest retention window across the cameras, for the page's one-line summary.

    Retention is per-camera in `cameras.yml` (a busy doorbell and a quiet shed may differ), and
    the contract's JSON has a single object, so the page reports the shortest window. That is
    the number an operator needs: it is the one that deletes footage first.
    """
    cameras = list(config.enabled_cameras().values())
    if not cameras:
        return 90 if key == "local_days" else 60
    return min(getattr(camera.retention, key) for camera in cameras)


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _first_code(payload: Mapping[str, Any]) -> str | None:
    """The first documented code in a component's `/health` report.

    Used only for the page's headline code. Codes stay a closed set (`health.CODES`), so an
    unknown string from a newer service is ignored rather than propagated into alert rules.
    """
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if isinstance(check, Mapping):
            code = check.get("code")
            if isinstance(code, str) and code in health.CODES:
                return code
    return None


@dataclass
class Collector:
    """Polls, derives, and returns one summary per call. No threads, no HTTP, no globals.

    Everything external is injected: the clock, the three probes, and the database path. That
    is what lets the whole decision surface be tested without a container, a broker or a
    camera — and it is also why `collect` is a pure-ish function of what the probes returned.
    """

    settings: StatusSettings
    config: CamerasConfig
    state: StatusState = field(default_factory=StatusState)
    clock: Callable[[], Instant] = timeutil.now
    probe_health: Callable[[str, str], Mapping[str, Any]] = field(
        default=lambda name, url: _http_json(url + "/health", timeout=5.0)
    )
    probe_stats: Callable[[str], Mapping[str, Any]] = field(
        default=lambda url: _http_json(url + "/api/stats", timeout=10.0)
    )
    probe_disk: Callable[[Path], DiskFacts] = _disk_facts
    probe_mqtt: Callable[[str, int], bool] = field(
        default=lambda host, port: _tcp_open(host, port, timeout=1.0)
    )

    # -- components ---------------------------------------------------------------------

    def _components(self, *, frigate: Mapping[str, Any] | None) -> tuple[ComponentStatus, ...]:
        out: list[ComponentStatus] = []
        for name in COMPONENT_NAMES:
            url = self.settings.component_urls.get(name)
            if not url:
                continue
            out.append(self._component(name, url))
        out.append(self._frigate_component(frigate))
        return tuple(out)

    def _component(self, name: str, url: str) -> ComponentStatus:
        try:
            payload = self.probe_health(name, url)
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self.state.misses[name] = self.state.misses.get(name, 0) + 1
            if self.state.misses[name] >= MISSES_BEFORE_UNKNOWN:
                return ComponentStatus(
                    name=name,
                    state=health.UNKNOWN,
                    summary=f"no report from {url} ({exc})",
                    observed=False,
                )
            # One miss is a blip: repeat the last honest report rather than flapping.
            last = self.state.last_health.get(name)
            if last is None:
                return ComponentStatus(
                    name=name,
                    state=health.UNKNOWN,
                    summary=f"no report from {url}",
                    observed=False,
                )
            return ComponentStatus(
                name=name,
                state=str(last.get("state", health.UNKNOWN)),
                summary=str(last.get("summary") or ""),
                version=last.get("version"),
            )

        self.state.misses[name] = 0
        self.state.last_health[name] = payload
        state = str(payload.get("state", health.UNKNOWN))
        if state not in health.ALL_STATES:
            state = health.UNKNOWN
        version = payload.get("version")
        summary = str(payload.get("summary") or "")
        code = _first_code(payload)

        # A mixed-version stack is a state we support during an upgrade and must therefore
        # make visible (service-contracts §8).
        if version and version != __version__:
            state = health.DEGRADED
            code = "FRIGATE_VERSION_MISMATCH"
            summary = f"version {version} (expected {__version__})" + (
                f"; {summary}" if summary else ""
            )
        return ComponentStatus(name=name, state=state, summary=summary, version=version, code=code)

    def _frigate_component(self, frigate: Mapping[str, Any] | None) -> ComponentStatus:
        if frigate is None:
            return ComponentStatus(
                name="frigate",
                state=health.UNHEALTHY,
                code="FRIGATE_UNREACHABLE",
                summary="Frigate did not answer /api/stats",
                observed=False,
            )
        cameras = frigate.get("cameras")
        count = len(cameras) if isinstance(cameras, Mapping) else 0
        return ComponentStatus(
            name="frigate",
            state=health.HEALTHY,
            summary=f"{count} camera(s) reporting",
        )

    def _mqtt_component(self) -> ComponentStatus:
        host = self.settings.mosquitto_host
        if host is None:
            return ComponentStatus(
                name="mosquitto",
                state=health.DISABLED,
                summary="MQTT is not configured",
            )
        try:
            reachable = self.probe_mqtt(host, self.settings.mosquitto_port)
        except OSError:
            reachable = False
        return ComponentStatus(
            name="mosquitto",
            state=health.HEALTHY if reachable else health.UNHEALTHY,
            code=None if reachable else "MQTT_DISCONNECTED",
            summary=(
                "connected" if reachable else f"cannot reach {host}:{self.settings.mosquitto_port}"
            ),
        )

    # -- one pass -----------------------------------------------------------------------

    def collect(self) -> StatusSummary:
        now = self.clock()
        self.state.last_collect_at = now

        errors: list[str] = []
        frigate: Mapping[str, Any] | None
        try:
            frigate = self.probe_stats(self.settings.frigate_url)
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            frigate = None
            errors.append(f"frigate: {exc}")
            self.state.last_error = str(exc)

        components = list(self._components(frigate=frigate))
        components.append(self._mqtt_component())

        conn = None
        backlog = db.QueueSummary(
            counts={},
            oldest_pending_age_s=None,
            uploaded_24h=0,
            last_successful_upload_at=None,
        )
        last_clip: dict[str, Instant] = {}
        clip_times: dict[str, list[Instant]] = {}
        next_cleanup_at: Instant | None = None
        try:
            conn = db.open_db(self.settings.db_path, readonly=True)
            try:
                backlog = db.queue_summary(conn, now=now)
                last_clip = db.last_clip_per_camera(conn)
                clip_times = db.clip_start_times_since(conn, since=now - 7 * 86400.0)
                last_cleanup = db.last_cleanup_run_at(conn)
                next_cleanup_at = (
                    None
                    if last_cleanup is None
                    else last_cleanup + self.settings.cleanup_interval_s
                )
            finally:
                conn.close()
        except (sqlite3.Error, db.DbError, OSError) as exc:
            conn = None
            components.append(
                ComponentStatus(
                    name="state-db",
                    state=health.UNHEALTHY,
                    code="DB_UNAVAILABLE",
                    summary=f"cannot read the state store: {exc}",
                    observed=False,
                )
            )
            errors.append(f"state-db: {exc}")

        disk: DiskFacts | None
        if (
            self.state.last_disk is not None
            and self.state.last_disk_at is not None
            and now - self.state.last_disk_at < self.settings.disk_interval_s
        ):
            disk = self.state.last_disk
        else:
            try:
                disk = self.probe_disk(self.settings.data_dir)
                self.state.last_disk = disk
                self.state.last_disk_at = now
            except OSError as exc:
                disk = self.state.last_disk
                errors.append(f"disk: {exc}")
            if disk is not None:
                self._record_disk_sample(now, disk)

        projection = StorageProjection(None, None, "no disk reading")
        if disk is not None:
            samples = self._disk_samples(now)
            projection = storage_projection(
                samples,
                free_bytes=disk.free_bytes,
                min_days_history=self.settings.disk_projection_min_days_history,
            )
            self._prune_disk_samples(now)

        cameras = tuple(
            camera_status(
                name=name,
                stats=frigate,
                last_clip_at=last_clip.get(name),
                clip_times=clip_times.get(name, []),
                offline_since=self.state.offline_since.get(name),
                now=now,
                settings=self.settings,
            )
            for name in sorted(self.config.enabled_cameras())
        )
        self._track_offline(cameras, now=now, stats=frigate)

        return StatusSummary(
            site=self.config.site.location,
            generated_at=now,
            components=tuple(components),
            cameras=cameras,
            disk=disk,
            projection=projection,
            backlog=backlog,
            local_days=_retention_days(self.config, key="local_days"),
            cloud_days=_retention_days(self.config, key="cloud_days"),
            next_cleanup_at=next_cleanup_at,
            data_dir=str(self.settings.data_dir),
            disk_warn_pct=self.settings.disk_warn_pct,
            disk_critical_pct=self.settings.disk_critical_pct,
            project_warn_days=self.settings.disk_project_warn_days,
            project_critical_days=self.settings.disk_project_critical_days,
            backup_warn_s=self.settings.backup_stale_warn_s,
            backup_critical_s=self.settings.backup_stale_critical_s,
            unobserved=tuple(errors),
        )

    def _track_offline(
        self,
        cameras: tuple[CameraStatus, ...],
        *,
        now: Instant,
        stats: Mapping[str, Any] | None,
    ) -> None:
        """Remember when each camera first looked disconnected, so the grace window is real."""
        for camera in cameras:
            entry: Mapping[str, Any] = {}
            if stats is not None and isinstance(stats.get("cameras"), Mapping):
                candidate = stats["cameras"].get(camera.name)
                if isinstance(candidate, Mapping):
                    entry = candidate
            fps = _float_or_none(entry.get("camera_fps"))
            if fps is not None and fps <= 0.0:
                self.state.offline_since.setdefault(camera.name, now)
            else:
                self.state.offline_since.pop(camera.name, None)

    # -- disk history -------------------------------------------------------------------

    def _record_disk_sample(self, now: Instant, disk: DiskFacts) -> None:
        try:
            conn = db.open_db(self.settings.db_path)
            try:
                db.record_disk_sample(
                    conn,
                    at=now,
                    free_bytes=disk.free_bytes,
                    clips_bytes=disk.clips_bytes,
                    media_bytes=disk.media_bytes,
                )
            finally:
                conn.close()
        except (db.DbError, sqlite3.Error, OSError):
            # The projection is a nicety; failing to store a sample must not fail the page.
            logger.warning("could not record a disk sample", exc_info=True)

    def _disk_samples(self, now: Instant) -> list[db.DiskSample]:
        try:
            conn = db.open_db(self.settings.db_path, readonly=True)
            try:
                return db.disk_samples_since(conn, since=now - 7 * 86400.0)
            finally:
                conn.close()
        except (db.DbError, sqlite3.Error, OSError):
            return []

    def _prune_disk_samples(self, now: Instant) -> None:
        try:
            conn = db.open_db(self.settings.db_path)
            try:
                db.prune_disk_samples(conn, before=now - 30 * 86400.0)
            finally:
                conn.close()
        except (db.DbError, sqlite3.Error, OSError):
            logger.debug("could not prune disk samples", exc_info=True)


def _tcp_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------


class Fmt:
    """Human-readable numbers, in one place so the page and the JSON agree."""

    @staticmethod
    def bytes(value: int) -> str:
        return _human_bytes(value)

    @staticmethod
    def age(seconds: float | None) -> str:
        return "never" if seconds is None else _human_age(seconds)


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover - unreachable


def _human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24.0:.0f} d"


def render_html(summary: StatusSummary, *, settings: StatusSettings) -> str:
    """The one-screen, read-only, JavaScript-free page (lld/06 §7).

    Inline CSS and no scripts: this is a diagnostics page, and it must render when everything
    else — including the network stack the browser normally relies on — is behaving badly.
    """
    esc = html.escape
    state_class = summary.overall.lower()

    chips = " ".join(
        f'<span class="chip {c.state.lower()}">{esc(c.name)} '
        f"{'●' if c.state == ONLINE else '○'}</span>"
        for c in summary.cameras
    )
    if not chips:
        chips = '<span class="muted">no cameras configured</span>'

    storage_line = "not measured"
    if summary.disk is not None:
        storage_line = (
            f"{Fmt.bytes(summary.disk.free_bytes)} free · "
            f"{Fmt.bytes(summary.disk.clips_bytes)} clips · "
            + (
                f"{summary.projection.days_to_full:.0f} days projected"
                if summary.projection.days_to_full is not None
                else "no projection yet"
            )
        )

    backup_line = (
        f"{summary.backlog.held} held · oldest {Fmt.age(summary.backlog.oldest_pending_age_s)} · "
        f"last success {Fmt.age(summary.storage_stale_s)} ago"
        if summary.backlog.last_successful_upload_at is not None
        else f"{summary.backlog.held} held · no successful upload yet"
    )

    retention_line = f"{summary.local_days}d local / {summary.cloud_days}d cloud"
    if summary.next_cleanup_at is not None:
        remaining = summary.next_cleanup_at - summary.generated_at
        retention_line += f" · next run in {Fmt.age(remaining)}"

    rows = "".join(
        f'<tr><td>{esc(c.name)}</td><td class="{c.state.lower()}">{esc(c.state)}</td>'
        f"<td>{esc(c.summary)}</td></tr>"
        for c in summary.components
    )

    links = [
        f'<a href="{esc(settings.frigate_ui_url)}">Frigate UI</a>',
    ]
    if settings.monitoring_url:
        links.append(f'<a href="{esc(settings.monitoring_url)}">Monitoring</a>')

    unobserved = ""
    if summary.unobserved:
        unobserved = (
            '<p class="unobserved">Not observed: ' + esc(", ".join(summary.unobserved)) + "</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>CCTV — {esc(summary.site)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 2rem auto;
          max-width: 46rem; padding: 0 1rem; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }}
  h1 {{ font-size: 1.1rem; margin: 0; letter-spacing: .02em; }}
  .badge {{ font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }}
  .healthy {{ color: #1a7f37; }} .degraded {{ color: #9a6700; }}
  .unhealthy {{ color: #cf222e; }} .unknown, .muted {{ color: #6e7781; }}
  .disabled {{ color: #6e7781; }}
  .answer {{ font-size: 1.35rem; margin: 1.25rem 0; }}
  .chip {{ border: 1px solid currentColor; border-radius: 1rem; padding: .05rem .6rem; }}
  .chip.online {{ color: #1a7f37; }} .chip.offline, .chip.unknown {{ color: #cf222e; }}
  .chip.quiet {{ color: #9a6700; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1.5rem; }}
  td {{ padding: .35rem .5rem; border-bottom: 1px solid #8884; }}
  td:first-child {{ white-space: nowrap; }}
  .unobserved {{ color: #9a6700; }}
  footer {{ margin-top: 2rem; display: flex; gap: 1.5rem; }}
</style>
</head>
<body>
<header>
  <h1>CCTV — {esc(summary.site)}</h1>
  <span class="badge {state_class}">● {esc(summary.overall)}</span>
</header>
<p class="answer">{esc(summary.answer)}</p>
<p><strong>Cameras</strong> &nbsp; {chips}</p>
<p><strong>Storage</strong> &nbsp; {esc(storage_line)}</p>
<p><strong>Backup</strong> &nbsp; {esc(backup_line)}</p>
<p><strong>Retention</strong> &nbsp; {esc(retention_line)}</p>
{unobserved}
<table>
<thead><tr><th>Component</th><th>State</th><th>Summary</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<footer>{" ".join(links)}</footer>
</body>
</html>
"""
