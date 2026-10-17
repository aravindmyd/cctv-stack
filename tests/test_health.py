"""Tests for the health contract.

[INV-6] says health never lies. These tests pin the two mechanisms that make that true
rather than intended: the overall state is *derived* from the checks, and anything not
positively covered — including an empty check list — is `UNKNOWN`, which never renders as
`200` in strict mode.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

import pytest

from cctv import health

DEGRADED_CODE = "BACKLOG_AGING"


def check(name: str = "dep", state: str = health.HEALTHY, **kwargs: Any) -> health.Check:
    kwargs.setdefault("code", None if state == health.HEALTHY else DEGRADED_CODE)
    return health.Check(name=name, state=state, **kwargs)


# ---------------------------------------------------------------------------------------
# Derivation (service-contracts §1.3)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([health.HEALTHY], health.HEALTHY),
        ([health.HEALTHY, health.HEALTHY], health.HEALTHY),
        ([health.DEGRADED], health.DEGRADED),
        ([health.HEALTHY, health.DEGRADED], health.DEGRADED),
        ([health.UNHEALTHY], health.UNHEALTHY),
        ([health.HEALTHY, health.DEGRADED, health.UNHEALTHY], health.UNHEALTHY),
        ([health.DISABLED], health.DISABLED),
        ([health.DISABLED, health.DISABLED], health.DISABLED),
        # A deliberately-off dependency is neutral: it must not turn a working component
        # into UNKNOWN, which in strict mode is a 503 forever.
        ([health.HEALTHY, health.DISABLED], health.HEALTHY),
        ([health.UNKNOWN], health.UNKNOWN),
        ([health.HEALTHY, health.UNKNOWN], health.UNKNOWN),
        ([], "UNKNOWN"),
    ],
)
def test_state_is_derived_from_the_checks(states: list[str], expected: str) -> None:
    assert health.derive_state(states) == expected


def test_no_checks_is_unknown_not_healthy() -> None:
    """The absence of information must never be good news."""
    assert health.derive_state([]) == health.UNKNOWN


def test_derive_accepts_check_objects() -> None:
    assert health.derive_state([check("a"), check("b")]) == health.HEALTHY


# ---------------------------------------------------------------------------------------
# Check validation
# ---------------------------------------------------------------------------------------


def test_a_non_healthy_check_must_carry_a_machine_code() -> None:
    with pytest.raises(ValueError, match="machine code"):
        health.Check(name="x", state=health.DEGRADED)


def test_a_disabled_check_needs_no_code() -> None:
    assert health.Check(name="x", state=health.DISABLED).code is None


def test_an_undocumented_code_is_refused() -> None:
    """Codes are a stable API that alerting rules reference."""
    with pytest.raises(ValueError, match="not a documented code"):
        health.Check(name="x", state=health.DEGRADED, code="MADE_UP")


def test_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown health state"):
        health.Check(name="x", state="FINE")


# ---------------------------------------------------------------------------------------
# Report shape and status codes (service-contracts §1.2, §1.4)
# ---------------------------------------------------------------------------------------


def report(*checks: health.Check, **kwargs: Any) -> health.HealthReport:
    kwargs.setdefault("component", "uploader")
    return health.HealthReport(checks=checks, **kwargs)


def test_report_serialises_the_documented_fields() -> None:
    payload = report(check("state_db")).to_dict()
    assert set(payload) == {
        "component",
        "version",
        "state",
        "since",
        "observed_at",
        "uptime_s",
        "checks",
        "summary",
    }
    assert payload["checks"][0]["name"] == "state_db"
    assert set(payload["checks"][0]) == {
        "name",
        "state",
        "code",
        "detail",
        "latency_ms",
        "observed_at",
    }


@pytest.mark.parametrize(
    ("state", "default", "strict"),
    [
        (health.HEALTHY, 200, 200),
        (health.DISABLED, 200, 200),
        (health.DEGRADED, 200, 503),
        (health.UNHEALTHY, 503, 503),
        (health.UNKNOWN, 503, 503),
    ],
)
def test_http_status_table(state: str, default: int, strict: int) -> None:
    """`DEGRADED` returning 200 by default is deliberate: a backlog is worth surfacing but
    is not a reason for Docker to consider the container dead."""
    assert report(check("dep", state)).http_status() == default
    assert report(check("dep", state)).http_status(strict=True) == strict


def test_metrics_expose_a_one_hot_state_gauge() -> None:
    text = health.render_metrics(report(check("dep")), gauges={"cctv_clips_pending": 3})
    assert 'cctv_component_state{component="uploader",state="HEALTHY"} 1' in text
    assert 'cctv_component_state{component="uploader",state="DEGRADED"} 0' in text
    assert "cctv_clips_pending 3" in text


# ---------------------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------------------


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[health.HealthServer] = []

    def start(**kwargs: Any) -> health.HealthServer:
        server = health.HealthServer(("127.0.0.1", 0), **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def base_url(server: health.HealthServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def call(
    url: str, *, method: str = "GET", body: Any = None, token: str | None = None
) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a result, not a test failure
        return exc.code, exc.read().decode()


def test_health_endpoint_returns_the_report(serve: Any) -> None:
    server = serve(snapshot=lambda: report(check("state_db"), summary="all good"))
    status, text = call(f"{base_url(server)}/health")
    assert status == 200
    payload = json.loads(text)
    assert payload["component"] == "uploader"
    assert payload["state"] == "HEALTHY"
    assert payload["summary"] == "all good"


def test_strict_mode_turns_a_degraded_report_into_a_503(serve: Any) -> None:
    server = serve(snapshot=lambda: report(check("storage", health.DEGRADED)))
    assert call(f"{base_url(server)}/health")[0] == 200
    assert call(f"{base_url(server)}/health?strict=1")[0] == 503


def test_metrics_endpoint_renders_gauges(serve: Any) -> None:
    server = serve(
        snapshot=lambda: report(check("state_db")),
        gauges=lambda: {"cctv_clips_pending": 2},
    )
    status, text = call(f"{base_url(server)}/metrics")
    assert status == 200
    assert "cctv_clips_pending 2" in text


def test_an_unknown_path_is_a_404(serve: Any) -> None:
    server = serve(snapshot=lambda: report(check("state_db")))
    assert call(f"{base_url(server)}/nothing")[0] == 404


def test_admin_endpoints_are_not_mounted_without_a_token(serve: Any) -> None:
    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=lambda body: {}),
    )
    status, text = call(f"{base_url(server)}/admin/queue")
    assert status == HTTPStatus.NOT_FOUND
    assert "not enabled" in text


def test_admin_endpoints_require_the_token(serve: Any) -> None:
    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=lambda body: {}),
        token="s3cret",
    )
    assert call(f"{base_url(server)}/admin/queue")[0] == HTTPStatus.UNAUTHORIZED
    assert call(f"{base_url(server)}/admin/queue", token="wrong")[0] == HTTPStatus.UNAUTHORIZED
    status, text = call(f"{base_url(server)}/admin/queue", token="s3cret")
    assert status == 200
    assert json.loads(text) == {}


def test_the_token_is_also_accepted_as_a_header(serve: Any) -> None:
    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=lambda body: {}),
        token="s3cret",
    )
    request = urllib.request.Request(f"{base_url(server)}/admin/queue")
    request.add_header("X-Internal-Token", "s3cret")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_retry_refuses_a_missing_reason(serve: Any) -> None:
    def retry(body: Any) -> Any:
        raise ValueError("A reason is required to requeue failed uploads.")

    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=retry),
        token="s3cret",
    )
    status, text = call(f"{base_url(server)}/admin/retry", method="POST", body={}, token="s3cret")
    assert status == HTTPStatus.BAD_REQUEST
    assert "reason" in text


def test_retry_passes_the_body_through(serve: Any) -> None:
    seen: dict[str, Any] = {}

    def retry(body: Any) -> Any:
        seen.update(body)
        return {"requeued": 1}

    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=retry),
        token="s3cret",
    )
    status, text = call(
        f"{base_url(server)}/admin/retry",
        method="POST",
        body={"camera": "front-door", "reason": "credentials rotated"},
        token="s3cret",
    )
    assert status == 200
    assert json.loads(text)["requeued"] == 1
    assert seen["camera"] == "front-door"


def test_retry_rejects_a_non_json_body(serve: Any) -> None:
    server = serve(
        snapshot=lambda: report(check("state_db")),
        admin=health.AdminRoutes(queue=dict, retry=lambda body: {}),
        token="s3cret",
    )
    request = urllib.request.Request(
        f"{base_url(server)}/admin/retry", data=b"not json", method="POST"
    )
    request.add_header("Authorization", "Bearer s3cret")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == HTTPStatus.BAD_REQUEST


def test_start_health_server_runs_in_a_background_thread() -> None:
    server, thread = health.start_health_server(
        port=0, host="127.0.0.1", snapshot=lambda: report(check("state_db"))
    )
    try:
        assert thread.daemon is True
        status, _ = call(f"http://127.0.0.1:{server.server_address[1]}/health")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
