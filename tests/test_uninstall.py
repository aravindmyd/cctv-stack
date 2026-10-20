"""Tests for `uninstall.sh`.

The script's whole value is what it *does not* delete, so these run it against a throwaway
installation with a stub `docker` on PATH. That stub records what it was asked to do, which is
the only way to assert both halves: that the default path stops containers, and that it leaves
`config/` and the data directory alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "uninstall.sh"


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    """A minimal installation: the script, an `.env`, config and data."""
    root = tmp_path / "site"
    (root / "config").mkdir(parents=True)
    (root / "data" / "clips" / "front-door").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "uninstall.sh")
    (root / "uninstall.sh").chmod(0o755)
    shutil.copy(REPO / "compose.yml", root / "compose.yml")
    (root / ".env").write_text(
        "CCTV_SITE=parents-home\nCCTV_DATA_DIR=./data\n", encoding="utf-8"
    )
    (root / "config" / "cameras.yml").write_text(
        "site: {location: parents-home}\n", encoding="utf-8"
    )
    (root / "data" / "cctv.db").write_text("db", encoding="utf-8")

    bin_dir = root / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$*" == *"config --images"* ]]; then printf "ghcr.io/jothikaraja/cctv:0.5.1\\n"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return root


def _run(
    site: Path, *args: str, stdin: str = "", data_dir: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{site / 'bin'}:{os.environ['PATH']}",
        "DOCKER_LOG": str(site / "docker.log"),
        "HOME": str(site / "home"),
    }
    if data_dir is not None:
        (site / ".env").write_text(
            f"CCTV_SITE=parents-home\nCCTV_DATA_DIR={data_dir}\n", encoding="utf-8"
        )
    return subprocess.run(
        ["bash", str(site / "uninstall.sh"), *args],
        cwd=site,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _docker_calls(site: Path) -> list[str]:
    log = site / "docker.log"
    if not log.exists():
        return []
    return [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_default_keeps_everything(site: Path) -> None:
    result = _run(site)
    assert result.returncode == 0, result.stderr
    assert (site / "config" / "cameras.yml").exists()
    assert (site / "data" / "cctv.db").exists()
    assert any("down" in call for call in _docker_calls(site))
    assert not any("rm -rf" in call for call in _docker_calls(site))
    assert "Nothing was deleted" in result.stdout


def test_default_does_not_remove_volumes(site: Path) -> None:
    _run(site)
    assert not any("--volumes" in call for call in _docker_calls(site))


def test_dry_run_runs_nothing(site: Path) -> None:
    result = _run(site, "--dry-run")
    assert result.returncode == 0
    assert "would run:" in result.stdout
    assert _docker_calls(site) == []


def test_images_flag_removes_the_image(site: Path) -> None:
    result = _run(site, "--images", "--dry-run")
    assert result.returncode == 0
    assert "docker rmi ghcr.io/jothikaraja/cctv:0.5.1" in result.stdout


def test_purge_needs_the_location_name_typed(site: Path) -> None:
    result = _run(site, "--purge", stdin="wrong-site\n")
    assert result.returncode == 1
    assert (site / "config").exists()
    assert (site / "data").exists()
    assert "Not confirmed" in result.stdout


def test_purge_removes_config_and_data_when_confirmed(site: Path) -> None:
    result = _run(site, "--purge", "--dry-run", stdin="parents-home\n")
    assert result.returncode == 0
    assert "rm -rf config" in result.stdout
    assert "rm -rf ./data" in result.stdout
    # Cloud footage is not this script's business, and it says so.
    assert "bucket is untouched" in result.stdout


def test_purge_refuses_a_home_directory(site: Path) -> None:
    result = _run(site, "--purge", "--dry-run", data_dir=str(site / "home"))
    assert result.returncode == 1
    assert "refusing to purge" in result.stderr


def test_purge_refuses_the_root_directory(site: Path) -> None:
    result = _run(site, "--purge", "--dry-run", data_dir="/")
    assert result.returncode == 1
    assert "refusing to purge" in result.stderr


def test_missing_env_changes_nothing(site: Path) -> None:
    (site / ".env").unlink()
    result = _run(site)
    assert result.returncode == 1
    assert "Nothing has been changed" in result.stderr
    assert (site / "config").exists()


def test_unknown_option_is_rejected(site: Path) -> None:
    result = _run(site, "--nuke-everything")
    assert result.returncode == 64
    assert "unknown option" in result.stderr
    assert (site / "config").exists()


def test_help_lists_the_destructive_flags(site: Path) -> None:
    result = _run(site, "--help")
    assert result.returncode == 0
    assert "--purge" in result.stdout
    assert "--remove-lifecycle" in result.stdout


def test_lifecycle_flag_is_honest_about_doing_nothing(site: Path) -> None:
    result = _run(site, "--remove-lifecycle", "--dry-run")
    assert result.returncode == 0
    assert "creates no lifecycle rule" in result.stdout
