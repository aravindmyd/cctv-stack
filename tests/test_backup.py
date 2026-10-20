"""Tests for backup and restore (M7, lld/08).

The behaviours worth pinning are the ones that fail quietly: a database copied unsafely, a
config bundle written unencrypted by accident, artefacts left world-readable, and a backup
nobody has ever restored. Where a test could pass for the wrong reason — "the file exists" —
it asserts the content instead.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from cctv import backup, db

NOW = 1_760_000_000.0
PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def site(tmp_path: Path) -> backup.BackupSettings:
    data = tmp_path / "data"
    config = tmp_path / "config"
    out = tmp_path / "backups"
    config.mkdir()
    data.mkdir()
    (config / "cameras.yml").write_text(
        "site: {location: home, timezone: UTC}\ncameras: {}\n", encoding="utf-8"
    )
    (config / "host.yml").write_text("# GENERATED FILE\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("CCTV_STORAGE_APPLICATION_KEY=topsecret\n", encoding="utf-8")

    db_path = data / "cctv.db"
    conn = db.open_db(db_path)
    try:
        db.migrate(conn)
        db.ensure_camera(conn, name="front-door", now=NOW)
        for index in range(3):
            conn.execute(
                "INSERT INTO clips (review_id, severity, camera, start_time, end_time, path, "
                "created_at, updated_at) "
                "VALUES (?, 'alert', 'front-door', ?, ?, ?, ?, ?)",
                (
                    f"review-{index}",
                    NOW + index,
                    NOW + index + 10,
                    f"front-door/2026/10/20/10-00-0{index}.mp4",
                    NOW,
                    NOW,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return backup.BackupSettings(
        data_dir=data,
        config_dir=config,
        db_path=db_path,
        out_dir=out,
        env_file=env_file,
        passphrase=PASSPHRASE,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------------------


def test_the_config_bundle_is_encrypted_by_default(site: backup.BackupSettings) -> None:
    result = backup.create(site, now=NOW)

    assert result.config_bundle.name.endswith(".tar.gz.enc")
    assert not (site.out_dir / f"config-{result.stamp}.tar.gz").exists()
    # Ciphertext, not a tarball with a misleading suffix.
    assert not result.config_bundle.read_bytes().startswith(b"\x1f\x8b")


def test_an_unencrypted_bundle_is_refused_without_explicit_consent(
    site: backup.BackupSettings,
) -> None:
    settings = backup.BackupSettings(
        data_dir=site.data_dir,
        config_dir=site.config_dir,
        db_path=site.db_path,
        out_dir=site.out_dir,
        env_file=site.env_file,
        passphrase=None,
    )

    with pytest.raises(backup.BackupError) as caught:
        backup.create(settings, now=NOW)

    assert "--unencrypted" in str(caught.value)
    assert "credentials" in str(caught.value)


def test_an_operator_can_choose_an_unencrypted_bundle(
    site: backup.BackupSettings,
) -> None:
    settings = backup.BackupSettings(
        data_dir=site.data_dir,
        config_dir=site.config_dir,
        db_path=site.db_path,
        out_dir=site.out_dir,
        env_file=site.env_file,
        unencrypted=True,
    )

    result = backup.create(settings, now=NOW)

    assert result.config_bundle.name.endswith(".tar.gz")
    with tarfile.open(result.config_bundle, "r:gz") as archive:
        assert "config/cameras.yml" in archive.getnames()


def test_every_artefact_is_owner_only(site: backup.BackupSettings) -> None:
    result = backup.create(site, now=NOW)

    for path in (
        result.config_bundle,
        result.state_db,
        result.manifest_csv,
        result.manifest,
    ):
        assert _mode(path) == 0o600, path


def test_the_database_is_snapshotted_with_the_backup_api(
    site: backup.BackupSettings,
) -> None:
    """The output is a real database, and reopening it is what proves it."""
    result = backup.create(site, now=NOW)

    conn = db.open_db(result.state_db, readonly=True)
    try:
        db.integrity_check(conn)
        assert conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"] == 3
    finally:
        conn.close()


def test_the_manifest_lists_every_clip_the_database_holds(
    site: backup.BackupSettings,
) -> None:
    result = backup.create(site, now=NOW)

    lines = result.manifest_csv.read_text(encoding="utf-8").splitlines()

    assert lines[0].startswith("event_id,camera,start_time,path")
    assert len(lines) == 4
    assert result.clips == 3
    assert any("front-door/2026/10/20/10-00-01.mp4" in line for line in lines)


# ---------------------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------------------


def test_verify_accepts_a_fresh_backup(site: backup.BackupSettings) -> None:
    backup.create(site, now=NOW)

    result = backup.verify(site, now=NOW + 60)

    assert result.ok, result.problems
    assert result.clips_in_backup == 3
    assert result.clips_live == 3
    assert result.config_files >= 2
    assert result.stale is False


def test_verify_detects_a_tampered_artefact(site: backup.BackupSettings) -> None:
    result = backup.create(site, now=NOW)
    with result.state_db.open("ab") as handle:
        handle.write(b"tampered")

    verified = backup.verify(site, now=NOW + 60)

    assert verified.ok is False
    assert any("checksum mismatch" in problem for problem in verified.problems)


def test_verify_does_not_claim_an_encrypted_bundle_it_could_not_open(
    site: backup.BackupSettings,
) -> None:
    backup.create(site, now=NOW)
    without_key = backup.BackupSettings(
        data_dir=site.data_dir,
        config_dir=site.config_dir,
        db_path=site.db_path,
        out_dir=site.out_dir,
        env_file=site.env_file,
        passphrase=None,
    )

    result = backup.verify(without_key, now=NOW + 60)

    assert result.ok is False
    assert any("passphrase" in problem for problem in result.problems)


def test_a_backup_that_stopped_being_taken_is_reported_stale(
    site: backup.BackupSettings,
) -> None:
    backup.create(site, now=NOW)
    conn = db.open_db(site.db_path)
    try:
        conn.execute(
            "INSERT INTO clips (review_id, severity, camera, start_time, end_time, path, "
            "created_at, updated_at) "
            "VALUES ('later', 'alert', 'front-door', ?, ?, 'front-door/x.mp4', ?, ?)",
            (NOW + 10, NOW + 20, NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()

    result = backup.verify(site, now=NOW + 2 * 86400)

    assert result.stale is True
    assert result.ok is False
    assert any("stale" in problem for problem in result.problems)


# ---------------------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------------------


def test_restore_state_refuses_to_overwrite_without_force(
    site: backup.BackupSettings,
) -> None:
    result = backup.create(site, now=NOW)

    with pytest.raises(backup.BackupError) as caught:
        backup.restore_state(site, source=result.state_db)

    assert "--force" in str(caught.value)


def test_restore_state_keeps_the_current_database_before_replacing_it(
    site: backup.BackupSettings,
) -> None:
    result = backup.create(site, now=NOW)
    conn = db.open_db(site.db_path)
    try:
        conn.execute("DELETE FROM clips WHERE review_id = 'review-2'")
        conn.commit()
    finally:
        conn.close()

    backup.restore_state(site, source=result.state_db, force=True)

    conn = db.open_db(site.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"] == 3
    finally:
        conn.close()
    pre = list(site.db_path.parent.glob("cctv.db.pre-restore-*"))
    assert len(pre) == 1
    assert _mode(pre[0]) == 0o600


def test_restore_config_recovers_the_files_and_keeps_aside_what_it_replaces(
    site: backup.BackupSettings,
) -> None:
    result = backup.create(site, now=NOW)
    (site.config_dir / "cameras.yml").write_text("site: {location: edited}\n", encoding="utf-8")

    written = backup.restore_config(site, source=result.config_bundle)

    names = {path.name for path in written}
    assert {"cameras.yml", ".env", "host.yml"} <= names
    assert "location: home" in (site.config_dir / "cameras.yml").read_text()
    assert (site.config_dir / "cameras.yml.pre-restore").exists()


def test_restore_config_refuses_a_bundle_that_escapes_the_target(
    site: backup.BackupSettings,
) -> None:
    evil = site.out_dir / "config-evil.tar.gz"
    site.out_dir.mkdir(parents=True, exist_ok=True)
    payload = site.data_dir / "payload"
    payload.write_text("owned", encoding="utf-8")
    with tarfile.open(evil, "w:gz") as archive:
        archive.add(payload, arcname="config/../../escape.txt")

    with pytest.raises(backup.BackupError) as caught:
        backup.restore_config(
            backup.BackupSettings(
                data_dir=site.data_dir,
                config_dir=site.config_dir,
                db_path=site.db_path,
                out_dir=site.out_dir,
                env_file=site.env_file,
                passphrase=None,
            ),
            source=evil,
        )

    assert "unsafe path" in str(caught.value) or "refusing to extract" in str(caught.value)


# ---------------------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------------------


def test_the_cli_creates_and_verifies(
    site: backup.BackupSettings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(site.data_dir))
    monkeypatch.setenv("CCTV_CONFIG_DIR", str(site.config_dir))
    monkeypatch.setenv("CCTV_DB", str(site.db_path))
    monkeypatch.setenv("CCTV_BACKUP_DIR", str(site.out_dir))
    monkeypatch.setenv("CCTV_ENV_FILE", str(site.env_file))
    monkeypatch.setenv("CCTV_BACKUP_PASSPHRASE", PASSPHRASE)
    # The CLI stamps with the wall clock; the fixture's rows are historical, which is fine.
    assert backup.main(["create"]) == 0
    capsys.readouterr()

    assert backup.main(["verify"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_the_cli_exits_78_when_a_backup_cannot_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CCTV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCTV_BACKUP_PASSPHRASE", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "cameras.yml").write_text("site: {}\n", encoding="utf-8")

    assert backup.main(["create"]) == 78
    assert "unencrypted" in capsys.readouterr().err


def test_the_cli_never_leaves_a_secret_in_its_output(
    site: backup.BackupSettings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(site.data_dir))
    monkeypatch.setenv("CCTV_CONFIG_DIR", str(site.config_dir))
    monkeypatch.setenv("CCTV_DB", str(site.db_path))
    monkeypatch.setenv("CCTV_BACKUP_DIR", str(site.out_dir))
    monkeypatch.setenv("CCTV_ENV_FILE", str(site.env_file))
    monkeypatch.setenv("CCTV_BACKUP_PASSPHRASE", PASSPHRASE)

    backup.main(["create"])
    backup.main(["verify"])
    output = capsys.readouterr()

    assert "topsecret" not in output.out + output.err
    assert PASSPHRASE not in output.out + output.err


def test_the_environment_is_the_only_configuration_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CCTV_BACKUP_PASSPHRASE", "x")
    settings = backup.BackupSettings.from_environ()

    assert settings.data_dir == tmp_path
    assert settings.db_path == tmp_path / "cctv.db"
    assert settings.out_dir == tmp_path / "backups"
    assert settings.passphrase == "x"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_restored_files_are_not_world_readable(site: backup.BackupSettings) -> None:
    result = backup.create(site, now=NOW)

    written = backup.restore_config(site, source=result.config_bundle)

    for path in written:
        assert _mode(path) != 0o644, path
