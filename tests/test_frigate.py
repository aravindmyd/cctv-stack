"""Tests for the Frigate client, against a stub HTTP server.

Every test here runs with no Frigate, no Docker and no network. The stub exists so the
**error matrix** can be exercised exhaustively — and the error matrix is the part worth
testing, because it decides whether a clip gets retried or thrown away.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest

from cctv import frigate
from cctv.frigate import (
    ClipUnavailable,
    FrigateAuthError,
    FrigateClient,
    FrigateError,
    FrigateRequestError,
    FrigateUnavailable,
    NoRecordingsYet,
    ReviewNotFound,
)

NOW = 1_700_000_000.0


class Sink:
    """Collects writes, so a test can assert on both content and chunking."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    @property
    def content(self) -> bytes:
        return b"".join(self.writes)


class StubFrigate:
    """A one-route-per-path HTTP server. Records what it was asked for."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json",
        delay_s: float = 0.0,
    ) -> None:
        self.routes[(method.upper(), path)] = {
            "status": status,
            "body": body,
            "content_type": content_type,
            "delay_s": delay_s,
        }

    def add_json(self, method: str, path: str, payload: Any, *, status: int = 200) -> None:
        self.add(method, path, status=status, body=json.dumps(payload).encode())

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep pytest output clean
                pass

            def _serve(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                stub.requests.append((self.command, self.path))
                route = stub.routes.get((self.command, path))
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route["delay_s"]:
                    import time

                    time.sleep(route["delay_s"])
                self.send_response(route["status"])
                self.send_header("Content-Type", route["content_type"])
                self.send_header("Content-Length", str(len(route["body"])))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(route["body"])

            do_GET = _serve
            do_POST = _serve
            do_PUT = _serve
            do_DELETE = _serve

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stub() -> Iterator[StubFrigate]:
    server = StubFrigate()
    yield server
    server.close()


@pytest.fixture
def client(stub: StubFrigate) -> FrigateClient:
    return FrigateClient(base_url=stub.base_url, timeout_s=5.0)


RANGE = f"/api/front-door/start/{NOW - 5:.6f}/end/{NOW + 40:.6f}/clip.mp4"


# --------------------------------------------------------------------------------------
# Downloading — the happy path, including chunking
# --------------------------------------------------------------------------------------


def test_download_range_writes_the_body(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add("GET", RANGE, body=b"MP4DATA", content_type="video/mp4")
    sink = Sink()
    written = client.download_range("front-door", NOW - 5, NOW + 40, sink)
    assert written == 7
    assert sink.content == b"MP4DATA"


def test_download_streams_rather_than_buffering(stub: StubFrigate, client: FrigateClient) -> None:
    """Memory must not be a function of clip length."""
    payload = b"x" * (frigate.CHUNK_SIZE * 3 + 17)
    stub.add("GET", RANGE, body=payload, content_type="video/mp4")
    sink = Sink()
    client.download_range("front-door", NOW - 5, NOW + 40, sink)
    assert sink.content == payload
    assert len(sink.writes) == 4  # 3 full chunks and a short tail


def test_the_range_url_keeps_sub_second_precision(stub: StubFrigate, client: FrigateClient) -> None:
    """Rounding a range start *up* would shorten the pre-roll, which is the whole point."""
    stub.add("GET", RANGE, body=b"x", content_type="video/mp4")
    client.download_range("front-door", NOW - 5, NOW + 40, Sink())
    requested = stub.requests[0][1]
    assert f"{NOW - 5:.6f}" in requested
    assert f"{NOW + 40:.6f}" in requested


def test_the_camera_name_is_url_quoted(stub: StubFrigate, client: FrigateClient) -> None:
    """A camera name must never be concatenated into a URL unencoded.

    Config validation already forbids a slash in a camera name, so this is defence in
    depth: it asserts the URL is *built* safely, not that a bad name is reachable.
    """
    with pytest.raises(FrigateError):
        client.download_range("front/door", 1.0, 2.0, Sink())
    assert stub.requests[0][1] == "/api/front%2Fdoor/start/1.000000/end/2.000000/clip.mp4"


def test_an_empty_200_is_not_a_clip(stub: StubFrigate, client: FrigateClient) -> None:
    """Zero bytes is indistinguishable from a corrupt clip downstream, so it is refused."""
    stub.add("GET", RANGE, body=b"", content_type="video/mp4")
    with pytest.raises(ClipUnavailable, match="no body"):
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())


def test_iter_chunks_yields_the_same_bytes(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add("GET", RANGE, body=b"abcdef", content_type="video/mp4")
    assert b"".join(client.iter_chunks("front-door", NOW - 5, NOW + 40)) == b"abcdef"


def test_event_clip_fallback_downloads(stub: StubFrigate, client: FrigateClient) -> None:
    """The contract's one allowed fallback, when the range endpoint 404s.

    It is the wrong endpoint for us in general — symmetric padding, no control over
    pre-roll — but a slightly wrong clip beats no clip.
    """
    stub.add("GET", "/api/events/e-1/clip.mp4", body=b"EVENTCLIP", content_type="video/mp4")
    sink = Sink()
    assert client.download_event_clip("e-1", sink) == 9
    assert sink.content == b"EVENTCLIP"


def test_event_clip_fallback_refuses_an_empty_body(
    stub: StubFrigate, client: FrigateClient
) -> None:
    stub.add("GET", "/api/events/e-1/clip.mp4", body=b"", content_type="video/mp4")
    with pytest.raises(ClipUnavailable, match="no body"):
        client.download_event_clip("e-1", Sink())


# --------------------------------------------------------------------------------------
# The error matrix — contract §6, one test per row
# --------------------------------------------------------------------------------------


def test_no_recordings_is_retryable(stub: StubFrigate, client: FrigateClient) -> None:
    """The flush race. Treating this as permanent discards a clip that was almost ready."""
    stub.add_json(
        "GET",
        RANGE,
        {"success": False, "message": "No recordings found for the specified time range"},
        status=400,
    )
    with pytest.raises(NoRecordingsYet) as info:
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())
    assert info.value.retryable is True


def test_the_no_recordings_match_ignores_case(stub: StubFrigate, client: FrigateClient) -> None:
    """The wording is Frigate's, not ours, so the match must not depend on capitalisation."""
    stub.add_json("GET", RANGE, {"message": "NO RECORDINGS FOUND"}, status=400)
    with pytest.raises(NoRecordingsYet):
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())


def test_a_different_400_is_our_bug(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("GET", RANGE, {"message": "invalid start time"}, status=400)
    with pytest.raises(FrigateRequestError) as info:
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())
    assert info.value.retryable is False


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_stop_and_do_not_retry(
    stub: StubFrigate, client: FrigateClient, status: int
) -> None:
    stub.add_json("GET", RANGE, {"message": "denied"}, status=status)
    with pytest.raises(FrigateAuthError) as info:
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())
    assert info.value.retryable is False


def test_a_404_on_the_range_is_clip_unavailable(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("GET", RANGE, {"message": "not found"}, status=404)
    with pytest.raises(ClipUnavailable):
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retryable(stub: StubFrigate, client: FrigateClient, status: int) -> None:
    stub.add_json("GET", RANGE, {"message": "boom"}, status=status)
    with pytest.raises(FrigateUnavailable) as info:
        client.download_range("front-door", NOW - 5, NOW + 40, Sink())
    assert info.value.retryable is True


def test_a_timeout_is_retryable(stub: StubFrigate) -> None:
    stub.add("GET", RANGE, body=b"x", content_type="video/mp4", delay_s=2.0)
    impatient = FrigateClient(base_url=stub.base_url, timeout_s=0.2)
    with pytest.raises(FrigateUnavailable) as info:
        impatient.download_range("front-door", NOW - 5, NOW + 40, Sink())
    assert info.value.retryable is True


def test_a_refused_connection_is_retryable() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing is listening now

    with pytest.raises(FrigateUnavailable) as info:
        FrigateClient(base_url=f"http://127.0.0.1:{port}", timeout_s=1.0).list_reviews()
    assert info.value.retryable is True


def test_a_non_json_response_is_refused(stub: StubFrigate, client: FrigateClient) -> None:
    """Something else is answering — a proxy, or the wrong port."""
    stub.add("GET", "/api/stats", body=b"<html>", content_type="text/html")
    with pytest.raises(FrigateRequestError, match="expected JSON"):
        client.stats()


def test_every_error_class_declares_retryability() -> None:
    """No error may default to retryable or non-retryable by accident."""
    for cls in (
        FrigateUnavailable,
        NoRecordingsYet,
        FrigateRequestError,
        FrigateAuthError,
        ClipUnavailable,
        ReviewNotFound,
    ):
        assert isinstance(cls.retryable, bool)
    assert FrigateUnavailable.retryable and NoRecordingsYet.retryable
    assert not (FrigateAuthError.retryable or FrigateRequestError.retryable)


# --------------------------------------------------------------------------------------
# Pinning — INV-9
# --------------------------------------------------------------------------------------


def test_retain_uses_post(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("POST", "/api/events/e-1/retain", {"success": True, "message": "retained"})
    client.retain_event("e-1")
    assert stub.requests == [("POST", "/api/events/e-1/retain")]


def test_unretain_uses_delete(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("DELETE", "/api/events/e-1/retain", {"success": True})
    client.unretain_event("e-1")
    assert stub.requests == [("DELETE", "/api/events/e-1/retain")]


def test_a_200_with_success_false_is_still_a_failure(
    stub: StubFrigate, client: FrigateClient
) -> None:
    """Frigate reports some failures inside a 200. Trusting the status code alone would
    let us download without a pin, which is the case INV-9 exists to prevent."""
    stub.add_json("POST", "/api/events/e-1/retain", {"success": False, "message": "not found"})
    with pytest.raises(FrigateRequestError, match="success=false"):
        client.retain_event("e-1")


def test_retain_of_a_pruned_event_is_reported(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("POST", "/api/events/gone/retain", {"message": "not found"}, status=404)
    with pytest.raises(ReviewNotFound):
        client.retain_event("gone")


@pytest.mark.parametrize("method", ["retain_event", "unretain_event"])
def test_pinning_requires_an_id(client: FrigateClient, method: str) -> None:
    with pytest.raises(ValueError, match="needs an event id"):
        getattr(client, method)("")


# --------------------------------------------------------------------------------------
# Listing reviews — what makes the sweep possible
# --------------------------------------------------------------------------------------


def test_list_reviews_parses_a_list(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("GET", "/api/review", [{"id": "r-1", "camera": "front-door"}])
    assert client.list_reviews() == [{"id": "r-1", "camera": "front-door"}]


def test_list_reviews_sends_the_window_and_limit(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("GET", "/api/review", [])
    client.list_reviews(after=NOW, before=NOW + 60, limit=25)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(stub.requests[0][1]).query)
    assert query["after"] == [f"{NOW:.6f}"]
    assert query["before"] == [f"{NOW + 60:.6f}"]
    assert query["limit"] == ["25"]


def test_list_reviews_omits_the_camera_filter_by_default(
    stub: StubFrigate, client: FrigateClient
) -> None:
    stub.add_json("GET", "/api/review", [])
    client.list_reviews()
    assert "cameras" not in urllib.parse.urlsplit(stub.requests[0][1]).query


def test_list_reviews_rejects_a_non_list(stub: StubFrigate, client: FrigateClient) -> None:
    stub.add_json("GET", "/api/review", {"items": []})
    with pytest.raises(FrigateRequestError, match="did not return a list"):
        client.list_reviews()


def test_an_empty_review_list_is_not_an_error(stub: StubFrigate, client: FrigateClient) -> None:
    """A quiet period is normal, and must not look like a failure to the sweep."""
    stub.add_json("GET", "/api/review", [])
    assert client.list_reviews() == []


# --------------------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------------------


def test_stats_reads_the_version_from_stats_not_version_endpoint(
    stub: StubFrigate, client: FrigateClient
) -> None:
    """`/api/version` answers with an empty body, so it cannot be used as a readiness
    probe. This is the helper that replaces it."""
    stub.add_json("GET", "/api/stats", {"service": {"version": "0.18.0-77a66e7"}})
    assert client.stats()["service"]["version"] == "0.18.0-77a66e7"


def test_the_base_url_tolerates_a_trailing_slash(stub: StubFrigate) -> None:
    stub.add_json("GET", "/api/stats", {"service": {}})
    FrigateClient(base_url=stub.base_url + "/").stats()
    assert not stub.requests[0][1].startswith("//")
