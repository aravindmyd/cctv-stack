"""`cctv-status` — the aggregate status collector (M5).

The service skeleton from
[LLD 00 §3](../../../internal/architecture/lld/00-service-skeleton.md): configure, validate,
serve health, then work. Its work is a poll loop; everything it *decides* lives in
[`cctv.status`](status.py), which is why this module is mostly wiring.

What it adds to the shared health surface is `/status`: the aggregate page and its JSON. Both
come from one `StatusSummary`, so the page and the machine-readable view cannot disagree — a
dashboard that contradicts its own JSON is worse than no dashboard.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import db, health, status, timeutil
from .config import CamerasConfig, ConfigError, load_cameras_config
from .logging import configure_logging
from .monitoring import GatusAdapter, heartbeats_for, monitors_for, push_heartbeat
from .timeutil import Instant

__all__ = ["EXIT_CONFIG", "EXIT_OK", "EXIT_UNAVAILABLE", "StatusService", "main"]

logger = logging.getLogger(__name__)

#: `EX_CONFIG` / `EX_TEMPFAIL` from `sysexits.h`, per LLD 00 §3.
EXIT_OK = 0
EXIT_CONFIG = 78
EXIT_UNAVAILABLE = 75

#: How long past its own cadence the collector's readings may be before the component says so.
FRESHNESS_MULTIPLIER = 2.0


class StatusService:
    """Holds the latest summary and answers both the health and the status surfaces.

    `latest()` never re-collects on a request: a page view must not trigger four network
    calls, and a slow source would otherwise turn every browser refresh into a stall. The
    loop is the only producer.
    """

    def __init__(
        self,
        *,
        settings: status.StatusSettings,
        config: CamerasConfig,
        collector: status.Collector | None = None,
        clock: Callable[[], Instant] = timeutil.now,
    ) -> None:
        self._settings = settings
        self._config = config
        self._collector = collector or status.Collector(settings=settings, config=config)
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: status.StatusSummary | None = None
        self._started_at: Instant = clock()
        self._last_error: str | None = None

    # -- collection ---------------------------------------------------------------------

    def collect_once(self) -> status.StatusSummary:
        summary = self._collector.collect()
        with self._lock:
            self._latest = summary
            self._last_error = None
        self._push_heartbeats(summary)
        return summary

    def write_monitoring_config(self) -> bool:
        """Render the monitoring tool's config from `cameras.yml`.

        Written here rather than by the installer because the installer does not exist yet
        (M6). When it does, this becomes the installer's call and the path is unchanged: the
        file is still generated from one source of truth (INV-3), never hand-edited.
        """
        path = self._settings.gatus_config_path
        if path is None:
            return False
        adapter = GatusAdapter(
            ntfy_url=self._settings.ntfy_url,
            ntfy_topic=self._settings.ntfy_topic,
            token=self._settings.monitoring_token,
            status_page_url=self._settings.monitoring_url,
        )
        monitors = monitors_for(
            self._config,
            frigate_url=self._settings.frigate_url,
            component_urls=dict(self._settings.component_urls),
            mosquitto_host=self._settings.mosquitto_host,
            mosquitto_port=self._settings.mosquitto_port,
            collect_interval_s=self._settings.collect_interval_s,
        )
        adapter.write_config(path, monitors)
        logger.info(
            "monitoring config written",
            extra={"path": str(path), "count": len(monitors)},
        )
        return True

    def _push_heartbeats(self, summary: status.StatusSummary) -> None:
        """Tell the monitoring tool what we saw. Failure is logged, never raised."""
        base = self._settings.gatus_url
        token = self._settings.monitoring_token
        if not base or not token:
            return
        for key, (ok, detail) in heartbeats_for(summary).items():
            push_heartbeat(base_url=base, key=key, token=token, ok=ok, detail=detail)

    def latest(self) -> status.StatusSummary | None:
        with self._lock:
            return self._latest

    def run(self, stop: threading.Event) -> None:
        """Poll until asked to stop. The first collection happens immediately."""
        while not stop.is_set():
            try:
                summary = self.collect_once()
                logger.debug(
                    "collected",
                    extra={
                        "state": summary.overall,
                        "cameras": len(summary.cameras),
                        "unobserved": len(summary.unobserved),
                    },
                )
            except Exception as exc:
                # A collector that dies because one source misbehaved is worse than one that
                # reports `UNKNOWN` about that source. The next pass retries.
                with self._lock:
                    self._last_error = str(exc)
                logger.error("collection failed", extra={"reason": str(exc)})
            stop.wait(self._settings.collect_interval_s)

    # -- surfaces -----------------------------------------------------------------------

    def report(self) -> health.HealthReport:
        """The collector's own `/health`, from the same summary the page uses.

        The check names are the contract's (`state_db`, `frigate_api`, `mqtt`,
        `collector_freshness`). Frigate unreachable is reported as this component's problem
        because it is: without it the collector cannot answer questions 1 to 3 at all.
        """
        summary = self.latest()
        checks: list[health.Check] = []
        if summary is None:
            checks.append(
                health.Check(
                    name="collector_freshness",
                    state=health.UNHEALTHY,
                    code="COLLECTOR_STALE",
                    detail="no collection has completed yet",
                )
            )
            return health.HealthReport(
                component=status.COMPONENT,
                checks=tuple(checks),
                summary="the collector has not completed a pass",
                since=self._started_at,
                uptime_s=max(0.0, self._clock() - self._started_at),
            )

        checks.append(self._check("state_db", summary, "state-db", "DB_UNAVAILABLE"))
        checks.append(self._check("frigate_api", summary, "frigate", "FRIGATE_UNREACHABLE"))
        checks.append(self._check("mqtt", summary, "mosquitto", "MQTT_DISCONNECTED"))

        age = max(0.0, self._clock() - summary.generated_at)
        limit = FRESHNESS_MULTIPLIER * self._settings.collect_interval_s
        checks.append(
            health.Check(
                name="collector_freshness",
                state=health.HEALTHY if age <= limit else health.UNHEALTHY,
                code=None if age <= limit else "COLLECTOR_STALE",
                detail=f"last reading {age:.0f}s old (limit {limit:.0f}s)",
                observed_at=summary.generated_at,
            )
        )

        return health.HealthReport(
            component=status.COMPONENT,
            checks=tuple(checks),
            summary=summary.answer,
            since=self._started_at,
            uptime_s=max(0.0, self._clock() - self._started_at),
        )

    @staticmethod
    def _check(name: str, summary: status.StatusSummary, component: str, code: str) -> health.Check:
        found = summary.component(component)
        if found is None:
            return health.Check(
                name=name,
                state=health.UNKNOWN,
                code=code,
                detail=f"no observation of {component}",
            )
        if found.state == health.UNHEALTHY:
            return health.Check(name=name, state=health.UNHEALTHY, code=code, detail=found.summary)
        if found.state == health.DEGRADED:
            return health.Check(name=name, state=health.DEGRADED, code=code, detail=found.summary)
        if found.state == health.DISABLED:
            return health.Check(name=name, state=health.DISABLED, detail=found.summary)
        if found.state == health.UNKNOWN:
            return health.Check(name=name, state=health.UNKNOWN, code=code, detail=found.summary)
        return health.Check(name=name, state=health.HEALTHY, detail=found.summary)

    def status_json(self) -> dict[str, Any]:
        summary = self.latest()
        if summary is None:
            return {
                "site": self._config.site.location,
                "generated_at": None,
                "overall": health.UNKNOWN,
                "overall_code": "COLLECTOR_STALE",
                "answer": "The status collector has not completed a pass yet.",
                "components": [],
                "cameras": [],
                "unobserved": ["status-collector"],
            }
        return summary.to_dict()

    def status_html(self) -> str:
        summary = self.latest()
        if summary is None:
            return (
                "<!doctype html><meta charset='utf-8'><title>CCTV status</title>"
                "<p>The status collector has not completed a pass yet.</p>"
            )
        return status.render_html(summary, settings=self._settings)

    def gauges(self) -> dict[str, float | int]:
        """Bounded-cardinality metrics (service-contracts §3). No path or event-id labels."""
        summary = self.latest()
        if summary is None:
            return {}
        out: dict[str, float | int] = {}
        if summary.disk is not None:
            out["cctv_disk_free_bytes"] = summary.disk.free_bytes
        days = summary.projection.days_to_full
        out["cctv_disk_projected_days_to_full"] = -1 if days is None else days
        age = summary.backlog.oldest_pending_age_s
        out["cctv_oldest_pending_age_seconds"] = -1 if age is None else age
        for camera in summary.cameras:
            out[f'cctv_camera_online{{camera="{camera.name}"}}'] = (
                1 if camera.state == status.ONLINE else 0
            )
        return out


def _install_signal_handlers(stop: threading.Event) -> None:
    """SIGTERM/SIGINT become a cooperative shutdown, as in the other services."""

    def request_stop(signum: int, _frame: object) -> None:
        if not stop.is_set():
            logger.info("shutdown requested", extra={"signal": signum})
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            logger.debug("could not install a signal handler", exc_info=True)


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # every input is an environment variable, as everywhere else

    try:
        settings = status.StatusSettings.from_environ()
    except ValueError as exc:
        print(f"cctv-status: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        configure_logging(os.environ.get("CCTV_LOG_LEVEL", "INFO"))
    except ValueError as exc:
        print(f"cctv-status: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    cameras_path = Path(os.environ.get("CCTV_CONFIG_DIR", "/config")) / "cameras.yml"
    try:
        config = load_cameras_config(cameras_path)
    except (ConfigError, OSError) as exc:
        logger.error(
            "configuration is unusable",
            extra={"path": str(cameras_path), "reason": str(exc)},
        )
        return EXIT_CONFIG

    for warning in config.warnings:
        logger.warning(warning)

    conn = None
    try:
        conn = db.open_db(settings.db_path)
        version = db.migrate(conn)
        db.integrity_check(conn)
    except (db.DbError, sqlite3.Error) as exc:
        logger.error(
            "state store is unusable",
            extra={"path": str(settings.db_path), "reason": str(exc)},
        )
        return EXIT_UNAVAILABLE
    finally:
        if conn is not None:
            conn.close()
    logger.info("state store ready", extra={"state": f"schema-{version}"})

    service = StatusService(settings=settings, config=config)

    if settings.gatus_config_path is not None:
        try:
            service.write_monitoring_config()
        except OSError as exc:
            # The monitoring tool is a convenience; the status page is the product. A
            # read-only mount is a deployment mistake worth logging, not a reason to exit.
            logger.error(
                "could not write the monitoring config",
                extra={"path": str(settings.gatus_config_path), "reason": str(exc)},
            )

    stop = threading.Event()
    _install_signal_handlers(stop)

    server = None
    if settings.health_port > 0:
        server, _thread = health.start_health_server(
            port=settings.health_port,
            host=settings.health_host,
            snapshot=service.report,
            gauges=service.gauges,
            status=service.status_json,
            status_html=service.status_html,
        )
        logger.info(
            "status endpoint listening",
            extra={"host": settings.health_host, "count": settings.health_port},
        )
    else:
        logger.warning("no status endpoint: CCTV_STATUS_HEALTH_PORT is 0")

    try:
        service.run(stop)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        logger.info("stopped")

    return EXIT_OK
