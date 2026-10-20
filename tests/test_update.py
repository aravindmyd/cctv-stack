"""Tests for `update.sh`.

An upgrade script is only trustworthy if the destructive parts are ordered, so these run it
against a throwaway installation with a stub `docker` that records what it was asked to do.
The assertions are about *sequence* and *refusals* rather than about Docker: that the backup
happens before `.env` changes, that nothing runs when the target is wrong, and that a rollback
without a previous version stops immediately.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "update.sh"


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    (root / "config").mkdir(parents=True)
    (root / "data" / "backups").mkdir(parents=True)
    (root / "data" / "backups" / "state-20261020T101010Z.db").write_text("db", encoding="utf-8")
    shutil.copy(SCRIPT, root / "update.sh")
    (root / "update.sh").chmod(0o755)
    shutil.copy(REPO / "compose.yml", root / "compose.yml")
    (root / ".env").write_text(
        "CCTV_SITE=parents-home\nCCTV_DATA_DIR=./data\nCCTV_IMAGE=cctv-stack:0.5.1\n",
        encoding="utf-8",
    )

    bin_dir = root / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return root


def _run(site: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{site / 'bin'}:{os.environ['PATH']}",
        "DOCKER_LOG": str(site / "docker.log"),
        "HOME": str(site / "home"),
    }
    return subprocess.run(
        ["bash", str(site / "update.sh"), *args],
        cwd=site,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _calls(site: Path) -> list[str]:
    log = site / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _env(site: Path) -> str:
    return (site / ".env").read_text(encoding="utf-8")


def test_target_is_required(site: Path) -> None:
    result = _run(site)
    assert result.returncode == 1
    assert "no target image" in result.stderr
    assert _calls(site) == []


def test_same_image_is_refused(site: Path) -> None:
    result = _run(site, "--to", "cctv-stack:0.5.1")
    assert result.returncode == 1
    assert "nothing to do" in result.stderr
    assert _calls(site) == []


def test_target_can_come_from_env(site: Path) -> None:
    (site / ".env").write_text(
        "CCTV_SITE=parents-home\nCCTV_DATA_DIR=./data\n"
        "CCTV_IMAGE=cctv-stack:0.5.1\nCCTV_IMAGE_NEXT=cctv-stack:0.6.0\n",
        encoding="utf-8",
    )
    result = _run(site, "--dry-run")
    assert result.returncode == 0
    assert "0.5.1 → cctv-stack:0.6.0" in result.stdout


def test_dry_run_changes_nothing(site: Path) -> None:
    result = _run(site, "--to", "cctv-stack:0.6.0", "--dry-run")
    assert result.returncode == 0
    assert _calls(site) == []
    assert "CCTV_IMAGE=cctv-stack:0.5.1" in _env(site)
    assert not list(site.glob(".env.pre-upgrade-*"))


def test_upgrade_backs_up_before_touching_env(site: Path) -> None:
    result = _run(site, "--to", "cctv-stack:0.6.0")
    assert result.returncode == 0, result.stderr
    calls = _calls(site)
    backup_index = next(i for i, c in enumerate(calls) if "cctv-backup create" in c)
    pull_index = next(i for i, c in enumerate(calls) if c.startswith("pull "))
    assert backup_index < pull_index
    assert list((site / "data" / "db-backups").glob("pre-upgrade-*.db"))


def test_upgrade_records_the_previous_image(site: Path) -> None:
    _run(site, "--to", "cctv-stack:0.6.0")
    env = _env(site)
    assert "CCTV_IMAGE=cctv-stack:0.6.0" in env
    assert "CCTV_IMAGE_PREVIOUS=cctv-stack:0.5.1" in env
    assert list(site.glob(".env.pre-upgrade-*")), "the old .env is kept as evidence"


def test_workers_stop_before_the_migration(site: Path) -> None:
    _run(site, "--to", "cctv-stack:0.6.0")
    calls = _calls(site)
    stop_index = next(i for i, c in enumerate(calls) if c.startswith("compose stop"))
    migrate_index = next(i for i, c in enumerate(calls) if "cctv-migrate" in c)
    up_index = next(i for i, c in enumerate(calls) if c.startswith("compose up"))
    assert stop_index < migrate_index < up_index


def test_upgrade_verifies_at_the_end(site: Path) -> None:
    result = _run(site, "--to", "cctv-stack:0.6.0")
    assert "cctv-doctor" in _calls(site)[-1]
    assert "Upgrade complete" in result.stdout
    assert "./update.sh --rollback" in result.stdout


def test_failed_backup_aborts_before_env_changes(site: Path) -> None:
    docker = site / "bin" / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$*" == *"cctv-backup"* ]]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = _run(site, "--to", "cctv-stack:0.6.0")
    assert result.returncode == 1
    assert "pre-upgrade backup failed" in result.stderr
    assert "CCTV_IMAGE=cctv-stack:0.5.1" in _env(site)


def test_failed_migration_says_how_to_roll_back(site: Path) -> None:
    docker = site / "bin" / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$*" == *"cctv-migrate"* ]]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = _run(site, "--to", "cctv-stack:0.6.0")
    assert result.returncode == 1
    assert "./update.sh --rollback" in result.stderr
    assert not any(c.startswith("compose up") for c in _calls(site))


def test_rollback_without_a_previous_image_is_refused(site: Path) -> None:
    result = _run(site, "--rollback")
    assert result.returncode == 1
    assert "nothing to roll back to" in result.stderr
    assert _calls(site) == []


def test_rollback_restores_the_newest_pre_upgrade_backup(site: Path) -> None:
    _run(site, "--to", "cctv-stack:0.6.0")
    (site / "docker.log").unlink()

    result = _run(site, "--rollback")
    assert result.returncode == 0, result.stderr
    calls = _calls(site)
    restore = next(c for c in calls if "restore-state" in c)
    assert "pre-upgrade-" in restore
    assert "CCTV_IMAGE=cctv-stack:0.5.1" in _env(site)


def test_help_states_the_digest_free_contract(site: Path) -> None:
    result = _run(site, "--help")
    assert result.returncode == 0
    assert "--rollback" in result.stdout
