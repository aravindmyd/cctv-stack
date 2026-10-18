"""Reading what `ffprobe` says about a clip.

The output of this module is the evidence for INV-14
(../../../internal/architecture/invariants.md).
Without it, "are the clips right?" is an opinion; with it, it is a query against the
`clips` table's quality columns.

## `max_packet_gap_s` is advisory, and nothing may gate on it

The natural way to detect a hole in a clip is the largest gap between packet timestamps.
On this stack that detector **does not work**. Measured on a healthy 30-second clip cut
with `-c copy`: the largest gap was **3.55 s**, against a 2.55 s keyframe interval. The
likely cause is PTS discontinuities at the joins between recording segments, which
`-c copy` reproduces faithfully because it is not re-encoding anything.

So a threshold that looks reasonable — say 2.0 s — flags nearly every healthy clip. The
field is still recorded, because the data is real and a future detector may want it, but
**truncation is detected by coverage, not by gaps.**

See [ADR-0015](../../../internal/adr/0015-clip-boundaries-and-padding.md) for the three
candidate replacements, the preferred one being to read Frigate's own `recordings` table
rather than inferring anything from the media.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from .clip import DEFAULT_COVERAGE_OK

__all__ = [
    "ClipFacts",
    "ProbeError",
    "build_probe_command",
    "coverage_ratio",
    "looks_truncated",
    "max_packet_gap",
    "parse_probe",
    "run_probe",
]

#: The binary, overridable because a container's ffmpeg is rarely on `PATH`. Frigate
#: itself vendors several builds under `/usr/lib/ffmpeg/<version>/bin/`, and our own image
#: will have its own arrangement — so the path is configuration, not a constant.
FFPROBE_ENV = "FFPROBE_BIN"


class ProbeError(RuntimeError):
    """`ffprobe` could not read the file, or is not installed."""


@dataclass(frozen=True, slots=True)
class ClipFacts:
    """What we learned about a clip by inspecting it rather than trusting the fetch."""

    duration_s: float | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool
    format_name: str | None
    packet_count: int
    #: Advisory only — see the module docstring. Never gate on this.
    max_packet_gap_s: float | None
    keyframe_interval_s: float | None

    def coverage_against(self, requested_s: float) -> float | None:
        return coverage_ratio(self.duration_s, requested_s)


def build_probe_command(path: str) -> list[str]:
    """The `ffprobe` invocation, as a list so nothing is ever shell-interpreted.

    `-v error` keeps stderr useful: warnings about non-monotonic DTS would otherwise
    drown the actual error on a genuinely broken file.
    """
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_packets",
        "-of",
        "json",
        path,
    ]


def run_probe(
    path: str | Path,
    *,
    ffprobe_bin: str | None = None,
    timeout_s: float = 60.0,
) -> ClipFacts:
    """Run `ffprobe` on a finished file and return the facts.

    Separate from [`parse_probe`][cctv.ffprobe.parse_probe] so the parsing — where the
    interesting logic is — stays testable without a subprocess, and so every way the
    subprocess can fail is handled in one place:

    * the binary is missing (`FileNotFoundError`) — which is the normal case in a container
      unless the image installs it, hence `FFPROBE_BIN`
    * it hangs
    * it exits non-zero on a file that is not really a video

    Raises:
        ProbeError: for all of the above. A clip we cannot measure is a clip we cannot
            trust, so this is never swallowed.
    """
    binary = ffprobe_bin or os.environ.get(FFPROBE_ENV) or "ffprobe"
    command = build_probe_command(str(path))
    command[0] = binary

    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
    except FileNotFoundError as exc:
        raise ProbeError(
            f"{binary!r} is not installed or not on PATH. Set {FFPROBE_ENV} to its full "
            f"path: a container image rarely puts ffmpeg on PATH, and Frigate itself "
            f"vendors its builds under /usr/lib/ffmpeg/<version>/bin/."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{binary} did not finish within {timeout_s}s for {path}.") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise ProbeError(f"{binary} exited {result.returncode} for {path}: {stderr}")

    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{binary} produced output that is not JSON for {path}: {exc}") from exc

    return parse_probe(payload)


def parse_probe(payload: Mapping[str, Any]) -> ClipFacts:
    """Turn `ffprobe -of json` output into facts.

    Deliberately tolerant of missing fields. `ffprobe` omits keys rather than emitting
    nulls, and a clip with no audio stream has no audio fields at all — which must be
    distinguishable from "we failed to read the file", so a missing duration stays `None`
    rather than becoming `0.0`.
    """
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    packets = payload.get("packets") or []

    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")

    return ClipFacts(
        duration_s=_as_float(fmt.get("duration")),
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        has_audio=audio is not None,
        format_name=fmt.get("format_name"),
        packet_count=len(packets),
        max_packet_gap_s=max_packet_gap(_packet_times(packets)),
        keyframe_interval_s=_keyframe_interval(streams),
    )


def coverage_ratio(actual_s: float | None, requested_s: float) -> float | None:
    """`actual / requested`. Above 1.0 is normal, not an error — see `cctv.clip`."""
    if actual_s is None or requested_s <= 0:
        return None
    return actual_s / requested_s


def looks_truncated(
    facts: ClipFacts, requested_s: float, *, ok: float = DEFAULT_COVERAGE_OK
) -> bool:
    """Whether the clip is short enough to be worth another attempt.

    Coverage-based by construction. **Do not add a gap check here** — the module docstring
    explains why the gap detector flags healthy clips on this stack.
    """
    ratio = facts.coverage_against(requested_s)
    if ratio is None:
        return True  # unmeasurable is not the same as fine
    return ratio < ok


def max_packet_gap(times: Sequence[float]) -> float | None:
    """Largest gap between consecutive packet timestamps, in seconds.

    Advisory. Returns `None` for fewer than two packets, because a single packet has no
    gaps and reporting `0.0` would look like a passing check.
    """
    if len(times) < 2:
        return None
    # `pairwise`, not `zip(times, times[1:])` — the latter needs `strict=False`, and
    # `strict=True` on it raises because the second sequence is one element shorter. That
    # off-by-one has been written twice here; `pairwise` cannot express it.
    return max(b - a for a, b in pairwise(sorted(times)))


def _first_stream(streams: Sequence[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") == kind:
            return stream
    return None


def _packet_times(packets: Sequence[Mapping[str, Any]]) -> list[float]:
    times: list[float] = []
    for packet in packets:
        value = _as_float(packet.get("pts_time"))
        if value is None:
            value = _as_float(packet.get("dts_time"))
        if value is not None:
            times.append(value)
    return times


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # ffprobe emits "N/A" for an unreadable value.
        return None


def _keyframe_interval(streams: Sequence[Mapping[str, Any]]) -> float | None:
    """Mean spacing between video frames, used only to sanity-check a gap reading."""
    video = _first_stream(streams, "video")
    if not video:
        return None
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if not isinstance(rate, str) or "/" not in rate:
        return None
    numerator, _, denominator = rate.partition("/")
    try:
        frames = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    return None if frames <= 0 else 1.0 / frames
