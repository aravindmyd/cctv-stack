"""HTTP client for Frigate.

Small on purpose. We call exactly four things, and the interesting part is not the
calling — it is the **classification**: which failures are worth retrying and which mean
something is actually wrong.

That distinction is where clips get lost. A `400 "No recordings found"` looks like a
malformed request and is in fact the *opposite*: it means the footage has not been flushed
to disk yet. Treating it as permanent discards a clip that was seconds away from being
retrievable. So the classification lives here, in one place, with the contract's matrix
next to it, rather than being re-derived at each call site.

```text
401 / 403                       -> FrigateAuthError     no retry, UNHEALTHY
400 "No recordings found"       -> NoRecordingsYet      retry, with a delay
400 (anything else)             -> FrigateRequestError  no retry, our bug
404 (range endpoint)            -> ClipUnavailable      caller falls back once
404 (review / event)            -> ReviewNotFound       no retry
5xx, connection error, timeout  -> FrigateUnavailable   retry with backoff
```

The client makes **one attempt per call and never sleeps**. Retrying is the caller's job,
because only the caller knows the attempt budget and whether a shorter timeout is
affordable — and a client that retries internally makes the caller's backoff meaningless.

Auth is a non-issue by construction: we talk to Frigate's internal port, which resolves
every request as `admin` (contract §2). The port is never published.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from .timeutil import Instant

__all__ = [
    "NO_RECORDINGS_MARKER",
    "ClipUnavailable",
    "FrigateAuthError",
    "FrigateClient",
    "FrigateError",
    "FrigateRequestError",
    "FrigateUnavailable",
    "NoRecordingsYet",
    "ReviewNotFound",
    "Sink",
]

#: Frigate's wording for the flush race. Matched case-insensitively, because the
#: classification must not depend on the exact capitalisation of a string we do not own.
NO_RECORDINGS_MARKER = "no recordings found"

#: Bytes per read when streaming a clip. Large enough that a long clip is not dominated by
#: syscalls, small enough that memory stays flat regardless of clip length.
CHUNK_SIZE = 256 * 1024


class Sink(Protocol):
    """Somewhere to write downloaded bytes. A file, or anything with `write`."""

    def write(self, data: bytes) -> int: ...


class FrigateError(Exception):
    """Base for every Frigate interaction failure.

    `retryable` is a class attribute rather than something the caller infers from the
    exception type, so adding a new error cannot silently default to "give up".
    """

    retryable: bool = False


class FrigateUnavailable(FrigateError):
    """Frigate or the network is unhealthy. Worth retrying: nothing is wrong with us."""

    retryable = True


class NoRecordingsYet(FrigateError):
    """`400 "No recordings found"` — the footage is not on disk yet, or has expired.

    **Retryable, and this is the single most important classification in the file.** It is
    the flush race, measured: at 5 s after a review ended, 5 of 6 fetches raced the segment
    flush and returned a short clip. The correct response is to wait and ask again.
    """

    retryable = True


class FrigateRequestError(FrigateError):
    """A `400` that is not the flush race. Our request is malformed — a bug, not weather."""

    retryable = False


class FrigateAuthError(FrigateError):
    """`401` / `403`. Configuration or a revoked credential. Stop and alert."""

    retryable = False


class ClipUnavailable(FrigateError):
    """`404` on the range endpoint. The caller may fall back to the event endpoint once."""

    retryable = False


class ReviewNotFound(FrigateError):
    """`404` for a review or event we asked about. It was pruned before we got there."""

    retryable = False


def _segment(value: str) -> str:
    """Escape one path segment.

    `safe=""` is the whole point. `urllib.parse.quote` leaves `/` unescaped by default,
    because `/` is in its default safe set — so a camera name containing a slash would
    silently inject an extra path segment rather than being encoded. Config validation
    already forbids that name, but building the URL safely should not depend on a
    validation rule enforced somewhere else.
    """
    return urllib.parse.quote(value, safe="")


def _timestamp(value: Instant) -> str:
    """Format an instant for a URL path.

    Six decimal places, not an integer: rounding a range start *up* would cut the pre-roll
    short, and the whole point of ADR-0015 is that boundaries err outward.
    """
    return f"{value:.6f}"


@dataclass(frozen=True, slots=True)
class FrigateClient:
    """A client for the four endpoints the pipeline uses.

    `base_url` defaults to the internal port. It is deliberately not configurable to the
    authenticated port: see contract §2, and note that publishing that port is the exact
    thing the contract forbids.
    """

    base_url: str = "http://frigate:5000"
    timeout_s: float = 30.0
    user_agent: str = "cctv-stack/0.1"

    # -----------------------------------------------------------------------------------
    # Transport
    # -----------------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float | None = None,
        accepts_json: bool = True,
        not_found: type[FrigateError] = ReviewNotFound,
    ) -> urllib.response.addinfourl:  # type: ignore[name-defined]
        """One attempt. Never retries, never sleeps — that is the caller's job.

        `not_found` exists because a 404 does not mean one thing. On a clip range it means
        the footage is not available and the caller may fall back; on an event it means the
        event was pruned. Collapsing the two would make the fallback path unreachable.
        """
        url = self.base_url.rstrip("/") + path
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=self.timeout_s if timeout_s is None else timeout_s
            )
        except urllib.error.HTTPError as exc:
            raise self._classify(exc, path, not_found) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # A connection refused, a DNS failure, a socket timeout — all "Frigate is not
            # answering", none of which we can fix by changing the request.
            raise FrigateUnavailable(f"{method} {path} failed: {exc}") from exc

        if accepts_json:
            content_type = response.headers.get("Content-Type", "")
            if "json" not in content_type:
                response.close()
                raise FrigateRequestError(
                    f"{method} {path} returned {content_type!r}, expected JSON. Reaching the "
                    f"wrong service, or a proxy is answering instead of Frigate."
                )
        return response

    def _classify(
        self, exc: urllib.error.HTTPError, path: str, not_found: type[FrigateError]
    ) -> FrigateError:
        """Turn an HTTP status into a decision. Contract §6, verbatim."""
        body = ""
        # A body we cannot read must not mask the status code, because the status code is
        # the part that decides whether we retry.
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")

        if exc.code in (401, 403):
            return FrigateAuthError(
                f"{path} returned {exc.code}. Frigate rejected us — a configuration or "
                f"credential problem, not a transient one. Stop and alert."
            )
        if exc.code == 404:
            return not_found(f"{path} returned 404.")
        if exc.code == 400:
            if NO_RECORDINGS_MARKER in body.lower():
                return NoRecordingsYet(
                    f"{path} returned 400 'No recordings found'. The footage is not on disk "
                    f"yet. This is the flush race, not a bad request — wait and ask again."
                )
            return FrigateRequestError(f"{path} returned 400: {body[:200]}")
        if exc.code >= 500:
            return FrigateUnavailable(f"{path} returned {exc.code}: {body[:200]}")
        return FrigateRequestError(f"{path} returned {exc.code}: {body[:200]}")

    # -----------------------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------------------

    def list_reviews(
        self,
        *,
        after: Instant | None = None,
        before: Instant | None = None,
        cameras: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Reviews Frigate has stored. This is what makes the sweep possible.

        The sweep exists because MQTT delivery is at-most-once from our side, and because
        a Frigate restart closes an in-progress review **without publishing `end` at all**
        — measured, not assumed. A review that MQTT never announced still appears here.
        """
        query: dict[str, str] = {"limit": str(limit)}
        if cameras != "all":
            query["cameras"] = cameras
        if after is not None:
            query["after"] = _timestamp(after)
        if before is not None:
            query["before"] = _timestamp(before)

        path = "/api/review?" + urllib.parse.urlencode(query)
        with self._request("GET", path) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise FrigateRequestError(
                f"{path} did not return a list. Got {type(payload).__name__}."
            )
        return payload

    def open_range(
        self,
        camera: str,
        start: Instant,
        end: Instant,
        *,
        timeout_s: float | None = None,
    ):
        """Open the range endpoint as a readable stream.

        The caller is expected to hand this to `shutil.copyfileobj`-style code writing into
        an [`atomic_writer`][cctv.atomic.atomic_writer], so a failed fetch leaves no
        half-written clip behind.
        """
        path = f"/api/{_segment(camera)}/start/{_timestamp(start)}/end/{_timestamp(end)}/clip.mp4"
        return self._request(
            "GET",
            path,
            timeout_s=timeout_s,
            accepts_json=False,
            not_found=ClipUnavailable,
        )

    def download_range(
        self,
        camera: str,
        start: Instant,
        end: Instant,
        sink: Sink,
        *,
        timeout_s: float | None = None,
    ) -> int:
        """Stream a clip range into `sink`. Returns the number of bytes written.

        Streamed rather than buffered: a long review can be hundreds of megabytes, and
        holding it whole would make memory a function of clip length.
        """
        written = 0
        with self.open_range(camera, start, end, timeout_s=timeout_s) as response:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                sink.write(chunk)
                written += len(chunk)

        if written == 0:
            # A 200 with an empty body is not a clip. Treated as unavailable rather than as
            # a zero-length clip, because a zero-length clip is indistinguishable from a
            # corrupt one downstream.
            raise ClipUnavailable(
                f"{camera} {start}..{end}: the range endpoint returned 200 with no body."
            )
        return written

    def iter_chunks(
        self,
        camera: str,
        start: Instant,
        end: Instant,
        *,
        timeout_s: float | None = None,
    ) -> Iterator[bytes]:
        """Chunk-wise form, for callers that would rather not pass a sink."""
        with self.open_range(camera, start, end, timeout_s=timeout_s) as response:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk

    # -----------------------------------------------------------------------------------
    # Pinning — INV-9
    # -----------------------------------------------------------------------------------

    def retain_event(self, event_id: str) -> None:
        """Pin an event so Frigate's retention cannot expire its footage before we copy it.

        **Failure here is fatal for that event** (contract §4.1). Downloading without a
        successful pin risks losing the footage entirely, which is worse than delaying the
        clip — so the caller must not proceed. Treated as idempotent: a repeated pin of an
        already-pinned event is not an error.
        """
        if not event_id:
            raise ValueError("retain_event needs an event id.")
        path = f"/api/events/{_segment(event_id)}/retain"
        with self._request("POST", path) as response:
            self._require_success(response, path)

    def unretain_event(self, event_id: str) -> None:
        """Release a pin after our copy is durable.

        **Failure here is not fatal** (contract §4.1). A stuck pin wastes Frigate's disk; it
        does not lose data, and the pin reaper will collect it later.
        """
        if not event_id:
            raise ValueError("unretain_event needs an event id.")
        path = f"/api/events/{_segment(event_id)}/retain"
        with self._request("DELETE", path) as response:
            self._require_success(response, path)

    def _require_success(self, response: Any, path: str) -> None:
        payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, Mapping) and payload.get("success") is False:
            # Frigate answers 200 with `"success": false` for some failures, so the status
            # code alone is not enough. That is the trap this check exists for.
            raise FrigateRequestError(
                f"{path} returned 200 but success=false: {payload.get('message')!r}"
            )

    # -----------------------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """`/api/stats`. Used by the health check and, notably, by anything that needs the
        Frigate version — `/api/version` answers with an empty body, which is why a
        readiness probe against it never passes."""
        with self._request("GET", "/api/stats") as response:
            return json.loads(response.read().decode("utf-8"))

    def download_event_clip(
        self, event_id: str, sink: Sink, *, timeout_s: float | None = None
    ) -> int:
        """The event-scoped fallback, used once when the range endpoint 404s.

        Kept because the contract says to fall back once, and because it needs no time
        range: it is the wrong endpoint for us in general (symmetric padding, no pre-roll
        control) but a better answer than no clip at all.
        """
        path = f"/api/events/{_segment(event_id)}/clip.mp4"
        written = 0
        with self._request("GET", path, timeout_s=timeout_s, accepts_json=False) as response:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                sink.write(chunk)
                written += len(chunk)
        if written == 0:
            raise ClipUnavailable(f"event {event_id}: returned 200 with no body.")
        return written


def write_to(sink: BinaryIO) -> Sink:
    """Identity helper, so `download_range(..., write_to(handle))` reads clearly."""
    return sink
