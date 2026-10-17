"""Tests for the single clip-path function. INV-5, and the DST half of INV-12."""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath

import pytest

from cctv import paths, timeutil
from cctv.paths import PathError

UTC = dt.UTC


def instant(*args: int) -> float:
    return dt.datetime(*args, tzinfo=UTC).timestamp()


@pytest.fixture
def kolkata():
    return timeutil.resolve_zone("Asia/Kolkata")


@pytest.fixture
def sydney():
    return timeutil.resolve_zone("Australia/Sydney")


# --------------------------------------------------------------------------------------
# The layout, exactly as PRD §7 and ADR-0006 specify
# --------------------------------------------------------------------------------------


def test_primary_relpath_matches_the_prd_layout(kolkata) -> None:
    got = paths.primary_relpath("front-door", instant(2026, 9, 12, 13, 15), kolkata)
    assert got == PurePosixPath("front-door/2026/09/12/18-45-00.mp4")


def test_explicit_collision_path_is_suffixed(kolkata) -> None:
    got = paths.colliding_relpath(
        "front-door", instant(2026, 9, 12, 13, 15), kolkata, "1718987129.308396-fqk5ka"
    )
    assert got == PurePosixPath("front-door/2026/09/12/18-45-00-fqk5ka.mp4")


def test_sidecar_sits_beside_the_clip(kolkata) -> None:
    clip = paths.primary_relpath("front-door", instant(2026, 9, 12, 13, 15), kolkata)
    assert paths.sidecar_relpath(clip) == PurePosixPath("front-door/2026/09/12/18-45-00.mp4.json")


def test_cloud_key_mirrors_the_local_path(kolkata) -> None:
    rel = paths.primary_relpath("front-door", instant(2026, 9, 12, 13, 15), kolkata)
    assert (
        paths.clip_cloud_key("parents-home", rel)
        == "parents-home/clips/front-door/2026/09/12/18-45-00.mp4"
    )


def test_components_are_non_posix_separated_even_on_windows(kolkata) -> None:
    """A clip path is also an object key, so it is always POSIX."""
    rel = paths.primary_relpath("front-door", instant(2026, 9, 12, 13, 15), kolkata)
    assert "\\" not in rel.as_posix()
    assert rel.as_posix().count("/") == 4


# --------------------------------------------------------------------------------------
# The half-hour offset, which is the thing most likely to be got wrong by hand
# --------------------------------------------------------------------------------------


def test_kolkata_offset_moves_the_hour_and_the_minute(kolkata) -> None:
    # 13:15 UTC is 18:45 IST — the example in ADR-0017.
    assert paths.date_parts(instant(2026, 9, 12, 13, 15), kolkata) == (
        "2026",
        "09",
        "12",
        "18-45-00",
    )


def test_kolkata_offset_can_roll_the_date_forward(kolkata) -> None:
    # 19:00 UTC on the 12th is 00:30 IST on the 13th. A whole-offset bug would miss this.
    assert paths.date_parts(instant(2026, 9, 12, 19, 0), kolkata) == (
        "2026",
        "09",
        "13",
        "00-30-00",
    )


# --------------------------------------------------------------------------------------
# Purity — the property INV-5 actually relies on
# --------------------------------------------------------------------------------------


def test_same_metadata_always_gives_the_same_path(kolkata) -> None:
    """Idempotent materialisation depends on this, so sweep a real span of instants."""
    base = instant(2026, 1, 1, 0, 0)
    for offset in range(0, 400_000, 37):  # ~4.6 days, prime-ish stride
        start = base + offset
        first = paths.primary_relpath("front-door", start, kolkata)
        assert paths.primary_relpath("front-door", start, kolkata) == first
        assert paths.date_parts(start, kolkata) == paths.date_parts(start, kolkata)


def test_every_generated_path_parses_back_to_its_components(kolkata) -> None:
    base = instant(2026, 3, 1, 0, 0)
    for offset in range(0, 200_000, 53):
        start = base + offset
        rel = paths.primary_relpath("garage", start, kolkata)
        parsed = paths.parse_clip_relpath(rel)
        year, month, day, hms = paths.date_parts(start, kolkata)

        assert parsed.camera == "garage"
        assert parsed.date_parts == (year, month, day)
        assert "-".join(parsed.time_parts) == hms
        assert parsed.collided is False


# --------------------------------------------------------------------------------------
# DST fallback — why the path cannot be inverted to an instant
# --------------------------------------------------------------------------------------


def test_fallback_hour_produces_identical_primary_paths(sydney) -> None:
    """Sydney repeats 02:00-03:00 on 2026-04-05. Two instants, one rendered name.

    This is the concrete proof that a path is not invertible: without the database's
    instant, these two clips are indistinguishable from the path alone. The collision
    rule is what stops one silently overwriting the other.
    """
    early = instant(2026, 4, 4, 15, 30)  # 02:30 AEDT (UTC+11)
    late = instant(2026, 4, 4, 16, 30)  # 02:30 AEST (UTC+10)

    assert early != late
    assert timeutil.render(early, sydney).hour == 2
    assert timeutil.render(late, sydney).hour == 2

    assert paths.primary_relpath("gate", early, sydney) == paths.primary_relpath(
        "gate", late, sydney
    )
    # …and the collision rule separates them, deterministically.
    a = paths.colliding_relpath("gate", early, sydney, "review-aaaaaaaaaa-bbbbbb")
    b = paths.colliding_relpath("gate", late, sydney, "review-cccccccccc-dddddd")
    assert a != b


def test_kolkata_cannot_produce_a_fallback_collision(kolkata) -> None:
    """IST has no DST, so the collision case above simply cannot arise for this site."""
    assert timeutil.has_dst(kolkata) is False


# --------------------------------------------------------------------------------------
# Collision suffix — deterministic, and independent of Frigate's id format
# --------------------------------------------------------------------------------------


def test_suffix_is_the_last_six_alphanumerics_of_a_frigate_id() -> None:
    assert paths.collision_suffix("1718987129.308396-fqk5ka") == "fqk5ka"


def test_suffix_is_deterministic() -> None:
    for review_id in (
        "a",
        "1718987129.308396-fqk5ka",
        "!!weird!!id!!",
        "12345678901234567890",
    ):
        assert paths.collision_suffix(review_id) == paths.collision_suffix(review_id)


def test_suffix_separates_two_reviews_in_the_same_second(kolkata) -> None:
    start = instant(2026, 9, 12, 13, 15)
    first, second = (
        paths.candidate_relpaths("front-door", start, kolkata, "1718987129.308396-fqk5ka")[1],
        paths.candidate_relpaths("front-door", start, kolkata, "1718987129.308396-zz99zz")[1],
    )
    assert first != second


def test_short_ids_fall_back_to_a_hash_rather_than_a_counter() -> None:
    """A stateful suffix would not be derivable from metadata, breaking INV-5."""
    short = paths.collision_suffix("ab")
    assert len(short) == paths.COLLISION_SUFFIX_LEN
    assert short == paths.collision_suffix("ab")  # stable across calls
    assert short != paths.collision_suffix("ba")


def test_empty_review_id_is_refused() -> None:
    with pytest.raises(PathError, match="non-empty review id"):
        paths.collision_suffix("")


def test_candidate_relpaths_returns_primary_then_colliding(kolkata) -> None:
    primary, colliding = paths.candidate_relpaths(
        "front-door", instant(2026, 9, 12, 13, 15), kolkata, "x" * 12
    )
    assert primary.name == "18-45-00.mp4"
    assert colliding.name.startswith("18-45-00-")


# --------------------------------------------------------------------------------------
# Validation — cameras are data, and they become paths
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["front-door", "garage", "a", "cam1", "a-b-c-1"])
def test_accepts_valid_camera_names(name: str) -> None:
    assert paths.validate_camera_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "FrontDoor",  # case collision on macOS
        "-leading",
        "trailing-",
        "",
        "has space",
        "has_underscore",
        "a" * 33,  # too long for a comfortable object key prefix
        "../escape",
        "café",  # non-ASCII
    ],
)
def test_rejects_invalid_camera_names(name: str) -> None:
    with pytest.raises(PathError):
        paths.validate_camera_name(name)


def test_camera_name_is_never_silently_lowercased() -> None:
    with pytest.raises(PathError):
        paths.validate_camera_name("FrontDoor")


# --------------------------------------------------------------------------------------
# Parsing — restore walks trees it did not write
# --------------------------------------------------------------------------------------


def test_parses_a_colliding_path() -> None:
    parsed = paths.parse_clip_relpath("front-door/2026/09/12/18-45-00-a1b2c3.mp4")
    assert parsed.collided is True
    assert parsed.collision_suffix == "a1b2c3"
    assert parsed.time_parts == ("18", "45", "00")


@pytest.mark.parametrize(
    "relpath",
    [
        "front-door/2026/09/12/18-45-00.mkv",
        "front-door/2026/09/18-45-00.mp4",  # missing a level
        "front-door/2026/09/12/18-45.mp4",
        "FrontDoor/2026/09/12/18-45-00.mp4",
        "front-door/2026/13/12/18-45-00.mp4",  # month 13
        "front-door/2026/09/31/18-45-00.mp4",  # September has 30 days
        "front-door/2026/02/30/18-45-00.mp4",  # never existed
        "front-door/2026/09/12/29-45-00.mp4",  # hour 29
        "front-door/2026/09/12/18-60-00.mp4",  # minute 60
        "front-door/2026/09/12/18-45-00.mp4.json",  # sidecar, not a clip
        "random.mp4",
    ],
)
def test_rejects_non_canonical_paths(relpath: str) -> None:
    with pytest.raises(PathError):
        paths.parse_clip_relpath(relpath)


def test_is_canonical_relpath_is_the_non_raising_form() -> None:
    assert paths.is_canonical_relpath("front-door/2026/09/12/18-45-00.mp4") is True
    assert paths.is_canonical_relpath("nope.mp4") is False


def test_leap_day_is_accepted_only_in_a_leap_year() -> None:
    assert paths.is_canonical_relpath("gate/2028/02/29/00-00-00.mp4") is True
    assert paths.is_canonical_relpath("gate/2026/02/29/00-00-00.mp4") is False
