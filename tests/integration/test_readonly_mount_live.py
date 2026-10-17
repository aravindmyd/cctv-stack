"""M3.11, live: the uploader **container** cannot write to the clips mount.

`tests/test_uploader.py` asserts the mount flag in `compose.yml`; that proves the intent is
declared, not that the kernel enforces it. This test runs the real image with the real nested
mounts and attempts the write, so the guarantee is demonstrated rather than promised.

The nested mount is the whole trick, and it is easy to get wrong: `/data` must stay writable
(the database lives there) while `/data/clips` is read-only. The more specific path wins, and
this test is what shows it actually does.

Skipped when Docker, the daemon, or the built image are absent — building the image is a
multi-minute job and must never be triggered from a test run.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

IMAGE = "cctv-stack:local"

# Asked in the same process, with the same paths, as the container's own entrypoints.
PROBE = textwrap.dedent("""
    import errno
    import pathlib

    target = pathlib.Path("/data/clips/probe.txt")
    try:
        target.write_text("this must not be possible")
    except OSError as exc:
        if exc.errno == errno.EROFS:
            print("REFUSED_READ_ONLY")
        else:
            print(f"REFUSED_FOR_THE_WRONG_REASON errno={exc.errno}: {exc}")
    else:
        print("WRITABLE")
    """)


@pytest.fixture(scope="module")
def docker() -> str:
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("the docker CLI is not on PATH")
    if subprocess.run([executable, "info"], capture_output=True).returncode != 0:
        pytest.skip("the docker daemon is not reachable")
    if subprocess.run([executable, "image", "inspect", IMAGE], capture_output=True).returncode != 0:
        pytest.skip(f"{IMAGE} is not built; run `docker build -t {IMAGE} .` first")
    return executable


def test_the_clips_mount_is_read_only_at_the_kernel(docker: str, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "clips").mkdir(parents=True)

    result: Any = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{data}:/data",
            "-v",
            f"{data}/clips:/data/clips:ro",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            PROBE,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "REFUSED_READ_ONLY" in result.stdout, (
        f"expected EROFS writing under /data/clips, got: {result.stdout.strip()!r} "
        f"(stderr: {result.stderr.strip()!r})"
    )
    assert not (data / "clips" / "probe.txt").exists(), "and nothing escaped to the host"
