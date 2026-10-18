"""The version exists twice, so the two must be compared rather than trusted.

`cctv.__version__` is reported by every `/health` response and written into each uploaded
object's metadata; `pyproject.toml` is what PyPI and GHCR tag. Bumping one and forgetting the
other is invisible at runtime — the uploader would keep claiming the old version in the
objects it stores — so the drift is caught here instead.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

from cctv import __version__


def test_the_reported_version_matches_the_installed_distribution() -> None:
    try:
        installed = version("cctv-stack")
    except PackageNotFoundError:  # pragma: no cover - only when run outside an install
        pytest.skip("cctv-stack is not installed; `uv run` installs it")

    assert installed == __version__
