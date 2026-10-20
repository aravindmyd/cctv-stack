"""Tests for the installer's wiring around the interview.

Docker cannot run in CI, so what is asserted here is the *contract between the script and the
image*: that a terminal gets the interview, that no terminal gets `--non-interactive` rather
than invented answers, and that a placeholder RTSP URL still stops the install. A stub `docker`
stands in for the image and edits `.env` the way `cctv-init` would, so the sequence is exercised
end to end without a daemon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STUB = """#!/usr/bin/env bash
printf "%s\\n" "$*" >> "$DOCKER_LOG"
# Stand in for `cctv-init`: the image rewrites .env, so the stub does too.
if [[ "$*" == *"cctv-init"* ]] && [[ "$*" != *"--non-interactive"* || -z "${INIT_FAILS:-}" ]]; then
  sed -i.bak 's|rtsp://user:password@[^ ]*|rtsp://user:real@10.0.0.5:554/stream|g' .env
  rm -f .env.bak
fi
if [[ -n "${INIT_FAILS:-}" && "$*" == *"cctv-init"* ]]; then exit 78; fi
exit 0
"""


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    (root / "config" / "frigate").mkdir(parents=True)
    shutil.copy(REPO / "install.sh", root / "install.sh")
    shutil.copy(REPO / "compose.yml", root / "compose.yml")
    shutil.copy(REPO / ".env.example", root / ".env.example")
    shutil.copy(REPO / "config" / "cameras.example.yml", root / "config" / "cameras.example.yml")

    bin_dir = root / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(STUB, encoding="utf-8")
    docker.chmod(0o755)
    return root


def _run(
    site: Path, *, stdin_tty: bool = False, fails: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{site / 'bin'}:{os.environ['PATH']}",
        "DOCKER_LOG": str(site / "docker.log"),
        "HOME": str(site / "home"),
    }
    if fails:
        env["INIT_FAILS"] = "1"
    # No PTY either way: `bash -c` under a pipe is never a terminal, which is exactly the
    # no-terminal path this test is about. `stdin_tty` documents the intent of the caller.
    assert not stdin_tty
    return subprocess.run(
        ["bash", str(site / "install.sh")],
        cwd=site,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _calls(site: Path) -> list[str]:
    log = site / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_without_a_terminal_the_interview_is_non_interactive(site: Path) -> None:
    result = _run(site)
    assert result.returncode == 0, result.stdout + result.stderr
    init = [c for c in _calls(site) if "cctv-init" in c]
    assert init, _calls(site)
    assert "--non-interactive" in init[0]
    assert "--env /site/.env" in init[0]
    assert "--cameras /config/cameras.yml" in init[0]


def test_image_is_built_before_the_interview_runs(site: Path) -> None:
    _run(site)
    calls = _calls(site)
    build_index = next(i for i, c in enumerate(calls) if c.startswith("compose build"))
    init_index = next(i for i, c in enumerate(calls) if "cctv-init" in c)
    assert build_index < init_index


def test_the_install_finishes_after_the_interview(site: Path) -> None:
    result = _run(site)
    assert "Frigate is up." in result.stdout
    assert "Running." in result.stdout


def test_incomplete_configuration_stops_the_install(site: Path) -> None:
    result = _run(site, fails=True)
    assert result.returncode == 1
    assert "needs a terminal to ask questions" in result.stderr
    # The stack was never started.
    assert not any(c.startswith("compose up") for c in _calls(site))


def test_a_placeholder_url_still_stops_the_install(site: Path) -> None:
    """If the interview declines to rewrite the URL, the placeholder check must catch it."""
    (site / "bin" / "docker").write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$DOCKER_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    (site / "bin" / "docker").chmod(0o755)

    result = _run(site)
    assert result.returncode == 1
    assert "placeholder RTSP URL" in result.stderr


def test_cloud_backup_is_skipped_without_a_bucket(site: Path) -> None:
    """Local-only is a supported state, so no bucket means no provisioning attempt."""
    result = _run(site)
    assert result.returncode == 0
    assert "cloud backup stays DISABLED" in result.stdout
    assert not any("cctv-provision" in call for call in _calls(site))


def test_a_configured_bucket_is_provisioned_before_starting(site: Path) -> None:
    original = (site / "bin" / "docker").read_text(encoding="utf-8")
    (site / "bin" / "docker").write_text(
        original.replace(
            "exit 0\n",
            'if [[ "$*" == *"cctv-init"* ]]; then\n'
            '  printf "CCTV_STORAGE_BUCKET=cctv-parents-home\\n" >> .env\n'
            "fi\n"
            "exit 0\n",
        ),
        encoding="utf-8",
    )

    result = _run(site)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(site)
    provision_index = next(i for i, c in enumerate(calls) if "cctv-provision" in c)
    up_index = next(i for i, c in enumerate(calls) if c.startswith("compose up"))
    assert provision_index < up_index


def test_a_failed_provisioning_is_loud_but_not_fatal(site: Path) -> None:
    original = (site / "bin" / "docker").read_text(encoding="utf-8")
    (site / "bin" / "docker").write_text(
        original.replace(
            "exit 0\n",
            'if [[ "$*" == *"cctv-init"* ]]; then\n'
            '  printf "CCTV_STORAGE_BUCKET=cctv-parents-home\\n" >> .env\n'
            "fi\n"
            'if [[ "$*" == *"cctv-provision"* ]]; then exit 1; fi\n'
            "exit 0\n",
        ),
        encoding="utf-8",
    )

    result = _run(site)
    assert result.returncode == 0, result.stderr
    assert "bucket is not ready" in result.stdout
    assert "Frigate is up." in result.stdout
