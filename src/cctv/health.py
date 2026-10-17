"""The health contract, and a small HTTP surface that serves it.

[INV-6](../../../docs/architecture/invariants.md) is the reason this module exists: a
component reported as `HEALTHY` must genuinely be able to do its job. Two rules make that
structural rather than aspirational, and both are here rather than in each service:
1. **`state` is derived, never assigned.** [`derive_state`][cctv.health.derive_state] is the
   only thing that produces an overall state, and it takes the checks as its only input. A
   service cannot return a literal `HEALTHY`; it can only supply checks that add up to one.
2. **A check that has not been evaluated is `UNKNOWN`, not healthy.** The fallback is
   `UNKNOWN`, which never renders as `200` in strict mode, so an absence of information
   cannot be mistaken for good news.

The HTTP layer is a thin shell: it does no health logic of its own, so every rule above is
testable without a socket, and the socket can be tested separately with one request.

Endpoints, per
[`contracts/service-contracts.md`](../../../docs/architecture/contracts/service-contracts.md):

```text
GET  /health          JSON report;  ?strict=1 makes DEGRADED/UNKNOWN return 503
GET  /metrics         Prometheus text exposition
GET  /admin/queue     backlog summary (requires INTERNAL_API_TOKEN)
POST /admin/retry     requeue failed clips (requires INTERNAL_API_TOKEN and a reason)
```

Admin routes are only mounted when a token is configured, and they are never published on a
host port — the compose network is the boundary.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .timeutil import Instant
from .timeutil import now as _now

__all__ = [
    "CODES",
    "DEGRADED",
    "DISABLED",
    "HEALTHY",
    "UNHEALTHY",
    "UNKNOWN",
    "AdminRoutes",
    "Check",
    "HealthReport",
    "HealthServer",
    "derive_state",
    "render_metrics",
    "start_health_server",
]

logger = logging.getLogger(__name__)

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNHEALTHY = "UNHEALTHY"
UNKNOWN = "UNKNOWN"
DISABLED = "DISABLED"

ALL_STATES = frozenset({HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN, DISABLED})

#: The documented machine codes (service-contracts §1.6). Codes are a stable API because
#: alerting rules reference them, so this set is deliberately closed: a check names one of
#: these or it names nothing.
CODES = frozenset(
    {
        "BACKLOG_AGING",
        "CAMERA_OFFLINE",
        "CLEANUP_STALE",
        "CLIPS_OVERDUE_RETENTION",
        "CLIP_COVERAGE_LOW",
        "CLIP_FLUSH_PRESSURE",
        "CLIP_HAS_GAPS",
        "DB_INTEGRITY_FAILED",
        "DB_UNAVAILABLE",
        "DETECTION_FALLING_BEHIND",
        "DISK_CRITICAL",
        "DISK_PRESSURE",
        "FRIGATE_AUTH_FAILED",
        "FRIGATE_UNREACHABLE",
        "FRIGATE_VERSION_MISMATCH",
        "MQTT_DISCONNECTED",
        "RETENTION_HEADROOM_LOW",
        "STORAGE_AUTH_FAILED",
        "STORAGE_STALE",
        "STORAGE_UNREACHABLE",
        "SWEEP_LAG_HIGH",
        "UPLOAD_FAILED_PERMANENT",
        "UPLOAD_LOCAL_FILE_MISSING",
        "UPLOAD_UNVERIFIED",
    }
)

#: Overall state → HTTP status, by mode. The table in service-contracts §1.4 omits `UNKNOWN`,
#: which is an omission rather than a licence: "we cannot tell" must never be a `200`, or a
#: component that has never evaluated a single check looks fine to every monitor.
_STATUS_CODES: Mapping[str, tuple[int, int]] = {
    HEALTHY: (200, 200),
    DISABLED: (200, 200),
    DEGRADED: (200, 503),
    UNHEALTHY: (503, 503),
    UNKNOWN: (503, 503),
}


@dataclass(frozen=True, slots=True)
class Check:
    """One dependency or condition, evaluated now.

    `code` is required whenever the state is not `HEALTHY`: an unhealthy check with no
    machine code cannot be alerted on, and a human reading the dashboard has to guess what
    to do about it.
    """

    name: str
    state: str
    detail: str | None = None
    code: str | None = None
    latency_ms: int | None = None
    observed_at: Instant | None = None

    def __post_init__(self) -> None:
        if self.state not in ALL_STATES:
            raise ValueError(f"{self.name}: unknown health state {self.state!r}.")
        # `DISABLED` is exempt: a component that is deliberately off is not a condition any
        # alerting rule can act on, so demanding a code would only invite a meaningless one.
        if self.state not in {HEALTHY, DISABLED} and not self.code:
            raise ValueError(
                f"{self.name}: a {self.state} check must carry a machine code, because "
                f"alerting rules reference codes rather than prose."
            )
        if self.code is not None and self.code not in CODES:
            raise ValueError(
                f"{self.name}: {self.code!r} is not a documented code. Codes are a stable "
                f"API (service-contracts §1.6); adding one is a deliberate act."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "code": self.code,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "observed_at": self.observed_at,
        }


def derive_state(checks: Sequence[Check | str]) -> str:
    """The overall state, derived from the checks and nothing else.

    The order is the contract's (§1.3), evaluated top to bottom, with one refinement the
    contract's list does not cover: **`DISABLED` checks are neutral**. A component with a
    deliberately-off dependency (cloud backup not configured, for instance) is otherwise
    neither all-`HEALTHY` nor all-`DISABLED`, and would fall through to `UNKNOWN` — which in
    strict mode is a `503`, so Docker would mark a perfectly intentional deployment
    permanently unhealthy.

    The final `UNKNOWN` is the important one: anything the rules do not positively cover —
    including an empty check list — is *unknown*, never healthy.
    """
    states = [check.state if isinstance(check, Check) else check for check in checks]

    if not states or all(state == DISABLED for state in states):
        return DISABLED if states else UNKNOWN
    if UNHEALTHY in states:
        return UNHEALTHY
    if DEGRADED in states:
        return DEGRADED
    if all(state in {HEALTHY, DISABLED} for state in states):
        return HEALTHY
    return UNKNOWN


@dataclass(frozen=True, slots=True)
class HealthReport:
    """One component's answer to "are you actually working?"."""

    component: str
    checks: tuple[Check, ...]
    summary: str = ""
    version: str = __version__
    since: Instant | None = None
    observed_at: Instant = field(default_factory=_now)
    uptime_s: float = 0.0

    @property
    def state(self) -> str:
        return derive_state(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "version": self.version,
            "state": self.state,
            "since": self.since,
            "observed_at": self.observed_at,
            "uptime_s": self.uptime_s,
            "checks": [check.to_dict() for check in self.checks],
            "summary": self.summary,
        }

    def http_status(self, *, strict: bool = False) -> int:
        return _STATUS_CODES[self.state][1 if strict else 0]


def render_metrics(report: HealthReport, *, gauges: Mapping[str, float | int] | None = None) -> str:
    """Prometheus text exposition (service-contracts §3).

    Only bounded-cardinality labels are emitted — a component name and a state, never a clip
    path or an event id, which would make the series count grow with the number of clips.
    """
    lines = [
        "# HELP cctv_component_state The component's derived health state, 1 for the current one.",
        "# TYPE cctv_component_state gauge",
    ]
    for state in sorted(ALL_STATES):
        value = 1 if state == report.state else 0
        lines.append(
            f'cctv_component_state{{component="{report.component}",state="{state}"}} {value}'
        )

    for name, value in sorted((gauges or {}).items()):
        lines.append(f"{name} {value}")

    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class AdminRoutes:
    """Callables the HTTP layer exposes under `/admin`.

    Injected rather than imported so the health server has no idea what an uploader is, and
    so a test can exercise the routing and the token check without a database.
    """

    queue: Callable[[], Mapping[str, Any]]
    retry: Callable[[Mapping[str, Any]], Mapping[str, Any]]


class _Handler(BaseHTTPRequestHandler):
    server_version = "cctv-health"
    protocol_version = "HTTP/1.1"

    # A request line per scrape is noise; only failures are worth a log line.
    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("http request", extra={"topic": self.requestline})

    # -- helpers -----------------------------------------------------------------------

    @property
    def _config(self) -> HealthServer:
        server = self.server
        assert isinstance(server, HealthServer)
        return server

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._write(
            status,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            "application/json",
        )

    def _authorised(self) -> bool:
        token = self._config.token
        if not token:
            return False
        header = self.headers.get("Authorization") or ""
        presented = (
            header.removeprefix("Bearer ").strip()
            or (self.headers.get("X-Internal-Token") or "").strip()
        )
        # Compared as strings; the token never appears in a log line (INV-2).
        return presented == token

    # -- routes ------------------------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path)
        if path.path == "/health":
            self._health(path.query)
            return
        if path.path == "/metrics":
            self._metrics()
            return
        if path.path == "/admin/queue":
            self._admin_queue()
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path)
        if path.path == "/admin/retry":
            self._admin_retry()
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _health(self, query: str) -> None:
        strict = parse_qs(query).get("strict", ["0"])[0] in {"1", "true", "yes"}
        report = self._config.snapshot()
        self._json(report.http_status(strict=strict), report.to_dict())

    def _metrics(self) -> None:
        report = self._config.snapshot()
        gauges = self._config.gauges() if self._config.gauges else None
        self._write(200, render_metrics(report, gauges=gauges).encode("utf-8"), "text/plain")

    def _admin_guard(self) -> bool:
        if self._config.admin is None or not self._config.token:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "admin endpoints are not enabled (INTERNAL_API_TOKEN is unset)"},
            )
            return False
        if not self._authorised():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid token"})
            return False
        return True

    def _admin_queue(self) -> None:
        if not self._admin_guard():
            return
        assert self._config.admin is not None
        self._json(200, dict(self._config.admin.queue()))

    def _admin_retry(self) -> None:
        if not self._admin_guard():
            return
        assert self._config.admin is not None
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"body is not JSON: {exc}"})
            return
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
            return
        try:
            result = self._config.admin.retry(body)
        except ValueError as exc:
            # A missing `reason`, or no filter. Both are operator mistakes, not crashes.
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(200, dict(result))


class HealthServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying the snapshot callables.

    `daemon_threads` matters: a slow scrape must not hold up shutdown, and a service that
    will not exit on `SIGTERM` is a service Docker eventually `SIGKILL`s mid-upload.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        snapshot: Callable[[], HealthReport],
        admin: AdminRoutes | None = None,
        token: str | None = None,
        gauges: Callable[[], Mapping[str, float | int]] | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.snapshot = snapshot
        self.admin = admin
        self.token = token
        self.gauges = gauges


def start_health_server(
    *,
    port: int,
    host: str = "0.0.0.0",
    snapshot: Callable[[], HealthReport],
    admin: AdminRoutes | None = None,
    token: str | None = None,
    gauges: Callable[[], Mapping[str, float | int]] | None = None,
) -> tuple[HealthServer, threading.Thread]:
    """Start serving in a background thread. Returns the server and its thread.

    Port `0` means "disabled" — a deployment that does not want the surface pays nothing for
    it, and the caller logs that choice rather than leaving it implicit.
    """
    server = HealthServer((host, port), snapshot=snapshot, admin=admin, token=token, gauges=gauges)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    return server, thread
