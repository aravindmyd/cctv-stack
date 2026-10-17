"""Tests for crash-safe writes. INV-1 and INV-4 depend on a partial file being impossible."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from cctv import atomic


def test_write_atomic_creates_the_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "clip.mp4"
    atomic.write_atomic(target, b"payload")
    assert target.read_bytes() == b"payload"


def test_write_atomic_leaves_no_temporary_behind(tmp_path: Path) -> None:
    """A leftover `.tmp` would be reaped, but its existence would also mean the rename
    did not happen — so its absence is the assertion that matters."""
    target = tmp_path / "clip.mp4"
    atomic.write_atomic(target, b"payload")
    assert [p.name for p in tmp_path.iterdir()] == ["clip.mp4"]


def test_write_atomic_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    atomic.write_atomic(target, b"first")
    atomic.write_atomic(target, b"second")
    assert target.read_bytes() == b"second"


def test_a_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    """An interrupted materialisation must be a retry, not a truncated clip that every
    later check believes."""
    target = tmp_path / "clip.mp4"

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), atomic.atomic_writer(target) as handle:
        handle.write(b"half a clip")
        raise Boom

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_streaming_writes_are_all_kept(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    with atomic.atomic_writer(target) as handle:
        for chunk in range(64):
            handle.write(bytes([chunk % 256]) * 1024)
    assert target.stat().st_size == 64 * 1024


def test_the_file_is_readable_only_via_the_final_name(tmp_path: Path) -> None:
    """Nothing but `os.replace` publishes a clip, so a reader never sees a partial file."""
    target = tmp_path / "clip.mp4"
    with atomic.atomic_writer(target) as handle:
        handle.write(b"in progress")
        assert not target.exists()  # not visible under the real name yet
    assert target.read_bytes() == b"in progress"


def test_mode_is_applied(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    atomic.write_atomic(target, b"x", mode=0o600)
    assert (target.stat().st_mode & 0o777) == 0o600


def test_tmp_path_is_distinct_per_process() -> None:
    assert str(os.getpid()) in atomic.tmp_path_for(Path("/clips/a.mp4")).name
    assert atomic.is_tmp(atomic.tmp_path_for(Path("/clips/a.mp4"))) is True
    assert atomic.is_tmp(Path("/clips/a.mp4")) is False


# --------------------------------------------------------------------------------------
# The reaper, and why it is age-gated
# --------------------------------------------------------------------------------------


def make_tmp(directory: Path, name: str, *, age_s: float, now: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.mp4.tmp-1234"
    path.write_bytes(b"scratch")
    stamp = now - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_a_stale_temporary_is_removed(tmp_path: Path) -> None:
    now = time.time()
    stale = make_tmp(tmp_path, "stale", age_s=3600, now=now)
    removed = atomic.reap_tmp(tmp_path, max_age_s=600, now=now)
    assert removed == [stale]
    assert not stale.exists()


def test_a_fresh_temporary_is_left_alone(tmp_path: Path) -> None:
    """Deleting on sight would corrupt a fetch that is happening right now."""
    now = time.time()
    fresh = make_tmp(tmp_path, "fresh", age_s=5, now=now)
    assert atomic.reap_tmp(tmp_path, max_age_s=600, now=now) == []
    assert fresh.exists()


def test_the_reaper_does_not_touch_canonical_files(tmp_path: Path) -> None:
    now = time.time()
    real = tmp_path / "front-door" / "2026" / "09" / "12" / "18-45-00.mp4"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"a real clip")
    os.utime(real, (now - 100_000, now - 100_000))

    atomic.reap_tmp(tmp_path, max_age_s=600, now=now)
    assert real.exists()


def test_the_reaper_finds_temporaries_in_nested_camera_directories(tmp_path: Path) -> None:
    now = time.time()
    nested = tmp_path / "front-door" / "2026" / "09" / "12"
    stale = make_tmp(nested, "18-45-00", age_s=3600, now=now)
    assert atomic.reap_tmp(tmp_path, max_age_s=600, now=now) == [stale]


def test_a_zero_age_gate_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="races live fetches"):
        atomic.reap_tmp(tmp_path, max_age_s=0)


def test_reaping_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert atomic.reap_tmp(tmp_path / "nope", max_age_s=600) == []


def test_fsync_directory_tolerates_a_missing_directory(tmp_path: Path) -> None:
    """Best-effort: a durability improvement must not break a working system."""
    atomic.fsync_directory(tmp_path / "nope")
