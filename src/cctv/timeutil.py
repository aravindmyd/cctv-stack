"""Instants and their rendering.

Two kinds of time value exist in this system and confusing them is the single most
common bug of this kind. See [ADR-0017](../../../internal/adr/0017-time-handling.md) and
[INV-12](../../../internal/architecture/invariants.md).

* An **instant** is epoch seconds — a bare `float`. It is the same instant everywhere
  on earth, so it carries no timezone and cannot be "in UTC" or "in IST". This is what
  Frigate sends, what the database stores, and what every comparison uses.
* A **rendered** time is a wall-clock value, produced from an instant plus a timezone.
  This is what a human reads: path components, log lines, the status page.

**Storage is never rendered.** Rendering happens only at the edges, only through this
module and [`cctv.paths`][cctv.paths], and always with an explicitly supplied timezone.

Why not just store local-time strings: an instant cannot be wrong and cannot lose
information, so any timezone stays derivable from it. A rendered string *can* be wrong,
and during a DST fallback two distinct instants render to the same string — which would
mean a silent overwrite.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "Instant",
    "TimeConfigError",
    "format_rfc3339",
    "has_dst",
    "now",
    "render",
    "resolve_zone",
    "to_instant",
]

#: Epoch seconds. An alias, not a class — the point is that a bare `float` is the
#: correct representation, and wrapping it would invite arithmetic on the wrapper.
type Instant = float


class TimeConfigError(ValueError):
    """A timezone name that cannot be used. Raised at config load, never at request time."""


# `+05:30`, `-0800`, `+05` — a fixed offset. These parse as "valid" but carry no DST
# history, so a site that observes DST would render wrongly for part of the year.
_OFFSET_LIKE = re.compile(r"^[+-]\d{2}:?\d{2}$|^[+-]\d{2}$")


def resolve_zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, once, at config load.

    Resolving here and passing `ZoneInfo` around — rather than passing a string and
    resolving per call — is what makes "resolved once at config load" enforceable
    ([INV-12](../../../internal/architecture/invariants.md)) instead of a convention.

    Rejects bare offsets, because they cannot express DST history. Rejects names
    absent from tzdata, because `ZoneInfo` would otherwise fail deep inside a worker
    on the first clip of the day rather than at start-up.

    Raises:
        TimeConfigError: the name is missing, is an offset, or is unknown.
    """
    if not isinstance(name, str) or not name.strip():
        raise TimeConfigError(
            "site.timezone is required and must be a non-empty IANA name. "
            "It is never guessed from the host: a container's TZ is easy to get "
            "wrong across six services, and wrong here means silently misplaced clips."
        )

    name = name.strip()

    if _OFFSET_LIKE.match(name):
        raise TimeConfigError(
            f"{name!r} is a UTC offset, not a timezone. Offsets carry no DST history, "
            f"so a site that observes DST would render wrongly for part of the year. "
            f"Use an IANA name such as 'Asia/Kolkata'."
        )

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeConfigError(
            f"Unknown timezone {name!r}. It must be an IANA name present in tzdata — "
            f"either the system zoneinfo database or the 'tzdata' package."
        ) from exc


def now() -> Instant:
    """The current instant.

    Use this instead of `datetime.now()`. `datetime.now()` without a timezone returns a
    naive local value, which is neither an instant nor a rendering — it is a wart, and
    a lint rule forbids it in service code.
    """
    return time.time()


def is_aware(value: _dt.datetime) -> bool:
    """Whether a datetime carries timezone information."""
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def to_instant(value: _dt.datetime) -> Instant:
    """Convert an **aware** datetime to an instant.

    A naive datetime is rejected rather than assumed to be UTC or local. Both
    assumptions have shipped as production bugs; refusing to guess is cheaper.
    """
    if not is_aware(value):
        raise ValueError(
            "A naive datetime has no defined instant. Attach a timezone (usually "
            "`cctv.timeutil.UTC`) before converting. Guessing between UTC and local "
            "time is the bug this check exists to prevent."
        )
    return value.timestamp()


def render(instant: Instant, zone: ZoneInfo) -> _dt.datetime:
    """Render an instant as an aware wall-clock value in `zone`.

    Total: every instant renders. That is the direction that matters, because
    instants are what we start from. Going the other way — a wall-clock value to an
    instant — is *partial*, since a DST spring-forward skips an hour entirely and a
    fallback repeats one. Nothing in this system does it.
    """
    return _dt.datetime.fromtimestamp(instant, tz=zone)


def format_rfc3339(instant: Instant, zone: ZoneInfo) -> str:
    """Render an instant for a human: logs, the status page, notifications."""
    return render(instant, zone).isoformat(timespec="milliseconds")


def has_dst(zone: ZoneInfo, year: int = 2026) -> bool:
    """Whether `zone` observes DST in `year`.

    Used by tests and by an installer warning. A DST-observing site cannot be treated
    as though it behaves like IST: Indian Standard Time is UTC+05:30 with no DST, so
    the fallback-collision case in
    [ADR-0006](../../../internal/adr/0006-canonical-clip-layout.md) cannot arise there —
    but it certainly can in Sydney, which is the value in the example config.
    """
    early = render(_dt.datetime(year, 1, 15, 12, tzinfo=_dt.UTC).timestamp(), zone)
    mid = render(_dt.datetime(year, 7, 15, 12, tzinfo=_dt.UTC).timestamp(), zone)
    return early.utcoffset() != mid.utcoffset()


#: Convenience alias, since a great deal of test and generator code needs it.
UTC = _dt.UTC
