"""Tests for the instant/rendering split. INV-12."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import pytest

from cctv import timeutil
from cctv.timeutil import TimeConfigError

UTC = dt.UTC


def instant(*args: int) -> float:
    """An instant from a UTC wall-clock time, for readability in tests."""
    return dt.datetime(*args, tzinfo=UTC).timestamp()


# --------------------------------------------------------------------------------------
# resolve_zone — the installer never guesses, and an offset is not a timezone
# --------------------------------------------------------------------------------------


def test_resolves_the_first_deployment_zone() -> None:
    zone = timeutil.resolve_zone("Asia/Kolkata")
    assert zone.key == "Asia/Kolkata"
    # The half-hour offset is the thing that will surprise someone reading a path.
    assert zone.utcoffset(dt.datetime(2026, 9, 12)) == dt.timedelta(hours=5, minutes=30)


def test_kolkata_has_no_dst_so_the_fallback_collision_cannot_arise() -> None:
    assert timeutil.has_dst(timeutil.resolve_zone("Asia/Kolkata")) is False


def test_sydney_does_have_dst() -> None:
    # The example config in cameras-config-schema.md uses Sydney, so this matters.
    assert timeutil.has_dst(timeutil.resolve_zone("Australia/Sydney")) is True


@pytest.mark.parametrize("offset", ["+05:30", "-0800", "+05", "-08"])
def test_rejects_bare_utc_offsets(offset: str) -> None:
    with pytest.raises(TimeConfigError, match="UTC offset, not a timezone"):
        timeutil.resolve_zone(offset)


@pytest.mark.parametrize("name", ["", "   ", "Not/AZone", "Asia/Kolkatta"])
def test_rejects_unknown_or_empty_zones(name: str) -> None:
    with pytest.raises(TimeConfigError, match=r"Unknown timezone|required"):
        timeutil.resolve_zone(name)


# --------------------------------------------------------------------------------------
# to_instant — refuse to guess between UTC and local
# --------------------------------------------------------------------------------------


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        timeutil.to_instant(dt.datetime(2026, 9, 12, 13, 15))


def test_aware_datetime_round_trips() -> None:
    aware = dt.datetime(2026, 9, 12, 13, 15, tzinfo=UTC)
    assert timeutil.to_instant(aware) == instant(2026, 9, 12, 13, 15)


# --------------------------------------------------------------------------------------
# render — total in the direction we actually use
# --------------------------------------------------------------------------------------


def test_render_is_total_across_a_spring_forward() -> None:
    """Every instant renders, including the ones spanning a skipped hour.

    Sydney moves 02:00 → 03:00 on 2026-10-04, so the wall time 02:30 does not exist.
    Rendering instant → local never has to produce it, which is the whole reason we
    store instants: the partial direction (local → instant) is the one that breaks.
    """
    zone = timeutil.resolve_zone("Australia/Sydney")
    start = instant(2026, 10, 3, 14, 0)  # 00:00 local, the morning of the transition

    rendered = [timeutil.render(start + 60 * i, zone) for i in range(180)]

    assert len(rendered) == 180
    # Monotonic in local time too, and the skip shows up as a jump, not an error.
    pairs = list(pairwise(rendered))
    assert all(a < b for a, b in pairs)
    # 01:59 AEST is followed by 03:00 AEDT. 02:xx never renders, and never has to.
    assert any(a.hour == 1 and a.minute == 59 and b.hour == 3 for a, b in pairs)


def test_rfc3339_rendering_carries_its_offset() -> None:
    zone = timeutil.resolve_zone("Asia/Kolkata")
    rendered = timeutil.format_rfc3339(instant(2026, 9, 12, 13, 15), zone)
    assert rendered.startswith("2026-09-12T18:45:00")
    assert rendered.endswith("+05:30")
