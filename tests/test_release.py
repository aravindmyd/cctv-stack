"""Tests for the release bundle (`packaging.md` §2, §5).

The bundle is the artefact a customer receives, so the tests are about what it *cannot* contain as
much as what it does:

* no `.env`, no `config/cameras.yml`, no `data/`, no `src/`, no tests — even if they are present
  in the repository;
* no `build:` block, because a release pulls and never builds;
* no unpinned image tag, because a tag can move and a digest cannot;
* no Frigate version whose integration checklist was never run.

The last section runs against the real repository, because an allowlist that has drifted from the
tree is the failure this file exists to catch and a fake repo cannot catch it.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from cctv import release
from cctv.release import ReleaseError

REPO = Path(__file__).resolve().parents[1]

DIGEST_AMD64 = "sha256:" + "a" * 64
DIGEST_ARM64 = "sha256:" + "b" * 64
FRIGATE_AMD64 = "ghcr.io/blakeblackshear/frigate@sha256:" + "c" * 64
FRIGATE_ARM64 = "ghcr.io/blakeblackshear/frigate@sha256:" + "d" * 64
MOSQUITTO = "eclipse-mosquitto@sha256:" + "e" * 64

COMPOSE = """\
# A comment an operator reads, and which must survive.

name: cctv-stack

x-cctv-image: &cctv-image
  image: ${CCTV_IMAGE:-cctv-stack:local}
  build:
    context: .

services:
  mosquitto:
    image: eclipse-mosquitto:2
    volumes:
      - ./data/mosquitto:/mosquitto/data

  worker:
    <<: *cctv-image
    command: ["cctv-clip-worker"]
    volumes:
      - ./config:/config:ro
"""


def images(**overrides: Any) -> dict[str, str]:
    base = {
        "amd64": f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}",
        "arm64": f"ghcr.io/x/cctv-stack@{DIGEST_ARM64}",
    }
    base.update(overrides)
    return base


#: The services in the fixture's `compose.yml` that run `${CCTV_IMAGE}`.
FIXTURE_SERVICES = ("worker",)


def assemble(repo: Path, out: Path, **overrides: Any) -> release.BundleResult:
    kwargs: dict[str, Any] = {
        "repo": repo,
        "out_dir": out,
        "images": images(),
        "frigate_version": "0.18.0",
        "frigate_contract_verified": "0.18.0",
        "frigate_images": {"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        "mosquitto_image": MOSQUITTO,
    }
    kwargs.update(overrides)
    return release.assemble(**kwargs)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal repository that satisfies the allowlist, plus the files that must not ship."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "cctv-stack"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    migrations = root / "src" / "cctv" / "migrations"
    migrations.mkdir(parents=True)
    for name in ("0001_init.sql", "0002_more.sql", "0007_last.sql"):
        (migrations / name).write_text("-- migration\n", encoding="utf-8")

    for relative in release.BUNDLE_ALLOWLIST:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            COMPOSE if relative == "compose.yml" else f"# {relative}\n", encoding="utf-8"
        )

    # The things that must never ship, present in the repository on purpose.
    (root / ".env").write_text("CCTV_STORAGE_APPLICATION_KEY=super-secret\n", encoding="utf-8")
    (root / "config" / "cameras.yml").write_text("site:\n  location: real\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "cctv.db").write_bytes(b"sqlite")
    (root / "src" / "cctv" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_thing.py").write_text("# a test\n", encoding="utf-8")
    return root


def _canonical_name(source: Path, directory: Path, sub: str) -> Path:
    """A tampered copy of `source`, in its own directory, under the *same* filename.

    `inspect` derives the top-level directory from the filename, because the filename is the
    contract (`cctv-stack-<version>.tar.gz`). A rewrite under any other name would be testing a
    different rule and passing for the wrong reason.
    """
    target = directory / sub / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def contents(archive: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            assert handle is not None
            found[str(Path(member.name).relative_to(Path(member.name).parts[0]))] = handle.read()
    return found


# ---------------------------------------------------------------------------------------
# What ships
# ---------------------------------------------------------------------------------------


def test_the_bundle_holds_the_allowlist_and_the_generated_files(repo: Path, tmp_path: Path) -> None:
    result = assemble(repo, tmp_path / "dist")

    shipped = contents(result.archive)
    expected = set(release.BUNDLE_ALLOWLIST) - {"compose.yml"}
    expected |= {"VERSION", "compose.yml", "versions.yml", "SHA256SUMS", *release.OVERRIDE_FILES}

    assert shipped.keys() == expected


def test_version_is_the_filename_and_the_file(repo: Path, tmp_path: Path) -> None:
    result = assemble(repo, tmp_path / "dist")

    assert result.version == "9.9.9"
    assert result.archive.name == "cctv-stack-9.9.9.tar.gz"
    assert contents(result.archive)["VERSION"].decode().strip() == "9.9.9"


def test_a_secret_present_in_the_repository_never_reaches_the_bundle(
    repo: Path, tmp_path: Path
) -> None:
    result = assemble(repo, tmp_path / "dist")

    shipped = contents(result.archive)
    assert ".env" not in shipped
    assert "config/cameras.yml" not in shipped
    assert "data/cctv.db" not in shipped
    assert not any(name.startswith("src/") for name in shipped)
    assert not any(name.startswith("tests/") for name in shipped)
    for body in shipped.values():
        assert b"super-secret" not in body


def test_a_missing_allowlisted_file_aborts_rather_than_shipping_a_hole(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "docs" / "health.md").unlink()

    with pytest.raises(ReleaseError) as caught:
        assemble(repo, tmp_path / "dist")

    assert "docs/health.md" in str(caught.value)


# ---------------------------------------------------------------------------------------
# compose.yml
# ---------------------------------------------------------------------------------------


def test_the_release_compose_has_no_build_block() -> None:
    derived = release.derive_compose(COMPOSE)

    assert "build:" not in derived
    assert "context: ." not in derived


def test_the_release_compose_keeps_its_comments() -> None:
    derived = release.derive_compose(COMPOSE)

    # The comments are the documentation someone reads when a mount does not appear, so losing
    # them to a YAML round-trip would be a real loss.
    assert "# A comment an operator reads" in derived


def test_the_release_compose_names_the_same_services_and_mounts() -> None:
    derived = yaml.safe_load(release.derive_compose(COMPOSE))
    original = yaml.safe_load(COMPOSE)

    assert set(derived["services"]) == set(original["services"])
    for name, body in original["services"].items():
        assert derived["services"][name]["volumes"] == body["volumes"]
        assert derived["services"][name].get("image") == body.get("image")


def test_a_build_block_on_a_service_is_removed_too() -> None:
    with_build = COMPOSE.replace(
        '    command: ["cctv-clip-worker"]',
        '    command: ["cctv-clip-worker"]\n    build:\n      context: ./worker',
    )

    derived = release.derive_compose(with_build)

    assert "build:" not in derived
    assert "context: ./worker" not in derived


def test_deriving_refuses_when_the_service_graph_would_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural comparison is what makes a text edit safe.

    `strip_build_blocks` walks lines; a future edit to it (or to the compose file's shape) could
    quietly take a mount with it. Injecting exactly that bug proves the guard is load-bearing.
    """

    genuine = release.strip_build_blocks

    def also_drops_a_mount(text: str) -> str:
        return genuine(text).replace("      - ./config:/config:ro\n", "")

    monkeypatch.setattr(release, "strip_build_blocks", also_drops_a_mount)

    with pytest.raises(ReleaseError) as caught:
        release.derive_compose(COMPOSE)

    assert "more than build" in str(caught.value)


# ---------------------------------------------------------------------------------------
# versions.yml
# ---------------------------------------------------------------------------------------


def test_the_schema_version_is_read_from_the_migrations_on_disk(repo: Path) -> None:
    assert release.database_schema_version(repo) == 7


def test_the_manifest_records_both_architectures_separately(repo: Path) -> None:
    manifest = release.versions_manifest(
        version="9.9.9",
        repo=repo,
        images=images(),
        frigate_version="0.18.0",
        frigate_contract_verified="0.18.0",
        frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        mosquitto_image=MOSQUITTO,
    )

    assert manifest["images"]["cctv"]["amd64"] != manifest["images"]["cctv"]["arm64"]
    assert manifest["schema"]["database"] == 7
    assert manifest["cctv_stack"] == "9.9.9"


def test_a_frigate_version_the_checklist_never_ran_against_blocks_the_release(repo: Path) -> None:
    with pytest.raises(ReleaseError) as caught:
        release.versions_manifest(
            version="9.9.9",
            repo=repo,
            images=images(),
            frigate_version="0.19.0",
            frigate_contract_verified="0.18.0",
            frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
            mosquitto_image=MOSQUITTO,
        )

    assert "fr igate-integration" not in str(caught.value)
    assert "0.19.0" in str(caught.value)


def test_an_image_pinned_by_tag_is_refused(repo: Path) -> None:
    with pytest.raises(ReleaseError) as caught:
        release.versions_manifest(
            version="9.9.9",
            repo=repo,
            images=images(arm64="ghcr.io/x/cctv-stack:9.9.9"),
            frigate_version="0.18.0",
            frigate_contract_verified="0.18.0",
            frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
            mosquitto_image=MOSQUITTO,
        )

    assert "digest" in str(caught.value)


def test_a_missing_architecture_is_refused(repo: Path) -> None:
    with pytest.raises(ReleaseError) as caught:
        release.versions_manifest(
            version="9.9.9",
            repo=repo,
            images={"amd64": f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}"},
            frigate_version="0.18.0",
            frigate_contract_verified="0.18.0",
            frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
            mosquitto_image=MOSQUITTO,
        )

    assert "arm64" in str(caught.value)


# ---------------------------------------------------------------------------------------
# The override file
# ---------------------------------------------------------------------------------------


def test_an_arm64_site_gets_the_arm64_digest(repo: Path) -> None:
    manifest = release.versions_manifest(
        version="9.9.9",
        repo=repo,
        images=images(),
        frigate_version="0.18.0",
        frigate_contract_verified="0.18.0",
        frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        mosquitto_image=MOSQUITTO,
    )

    text = release.override_for(manifest, arch="arm64", services=FIXTURE_SERVICES)

    assert DIGEST_ARM64 in text
    assert DIGEST_AMD64 not in text


def test_moving_to_a_mirror_keeps_the_digest() -> None:
    assert (
        release.target_from(
            f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}", "registry.site.local/cctv-stack"
        )
        == f"registry.site.local/cctv-stack@{DIGEST_AMD64}"
    )


def test_a_reference_without_a_digest_cannot_be_mirrored() -> None:
    with pytest.raises(ReleaseError):
        release.target_from("ghcr.io/x/cctv-stack:9.9.9", "registry.site.local/cctv-stack")


# ---------------------------------------------------------------------------------------
# inspect — the check on the built artefact
# ---------------------------------------------------------------------------------------


def test_a_clean_bundle_inspects_clean(repo: Path, tmp_path: Path) -> None:
    result = assemble(repo, tmp_path / "dist")

    checked = release.inspect(result.archive)

    assert checked.ok, checked.problems
    assert checked.version == "9.9.9"


def test_inspect_catches_a_forbidden_path_that_got_in(repo: Path, tmp_path: Path) -> None:
    result = assemble(repo, tmp_path / "dist")
    tampered = _canonical_name(result.archive, tmp_path, "tampered")
    with tarfile.open(result.archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            handle = source.extractfile(member) if member.isfile() else None
            target.addfile(member, handle)
        payload = b"CCTV_STORAGE_APPLICATION_KEY=leaked\n"
        info = tarfile.TarInfo("cctv-stack-9.9.9/.env")
        info.size = len(payload)
        target.addfile(info, io.BytesIO(payload))

    checked = release.inspect(tampered)

    assert not checked.ok
    assert any(".env" in problem for problem in checked.problems)


def test_inspect_catches_a_file_that_does_not_match_its_checksum(
    repo: Path, tmp_path: Path
) -> None:
    result = assemble(repo, tmp_path / "dist")
    tampered = _canonical_name(result.archive, tmp_path, "changed")
    with tarfile.open(result.archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            if member.name.endswith("install.sh"):
                payload = b"# a different installer\n"
                info = tarfile.TarInfo(member.name)
                info.size = len(payload)
                info.mode = member.mode
                target.addfile(info, io.BytesIO(payload))
                continue
            handle = source.extractfile(member) if member.isfile() else None
            target.addfile(member, handle)

    checked = release.inspect(tampered)

    assert not checked.ok
    assert any("does not match its checksum" in problem for problem in checked.problems)


def test_inspect_catches_a_bundle_that_still_builds(repo: Path, tmp_path: Path) -> None:
    result = assemble(repo, tmp_path / "dist")
    tampered = _canonical_name(result.archive, tmp_path, "building")
    with tarfile.open(result.archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            handle = source.extractfile(member) if member.isfile() else None
            if member.name.endswith("compose.yml"):
                payload = COMPOSE.encode()
                info = tarfile.TarInfo(member.name)
                info.size = len(payload)
                info.mode = member.mode
                target.addfile(info, io.BytesIO(payload))
                continue
            target.addfile(member, handle)

    checked = release.inspect(tampered)

    assert not checked.ok
    assert any("build" in problem for problem in checked.problems)


def test_inspect_reports_a_missing_archive(tmp_path: Path) -> None:
    checked = release.inspect(tmp_path / "nothing.tar.gz")

    assert not checked.ok
    assert "no such archive" in checked.problems[0]


# ---------------------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------------------


def test_the_real_repository_can_be_bundled(tmp_path: Path) -> None:
    """The allowlist is a hand-written list, so it can drift from the tree.

    This is the test that notices: every allowlisted path must exist in *this* repository, and the
    real `compose.yml` must derive cleanly.
    """
    result = assemble(REPO, tmp_path / "dist")

    checked = release.inspect(result.archive)
    assert checked.ok, checked.problems
    assert checked.version == release.bundle_version(REPO)
    assert "versions.yml" in checked.files
    assert "docs/disaster-recovery.md" in checked.files


def test_the_real_compose_file_only_loses_its_build_block() -> None:
    source = (REPO / "compose.yml").read_text(encoding="utf-8")

    derived = release.derive_compose(source)

    removed = [line for line in source.splitlines() if line not in derived.splitlines()]
    assert removed == ["  build:", "    context: ."]


def test_the_cli_bundles_and_inspects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "dist"

    code = release.main(
        [
            "bundle",
            "--repo",
            str(REPO),
            "--out",
            str(out),
            "--frigate-version",
            "0.18.0",
            "--frigate-contract-verified",
            "0.18.0",
            "--frigate-amd64",
            FRIGATE_AMD64,
            "--frigate-arm64",
            FRIGATE_ARM64,
            "--mosquitto",
            MOSQUITTO,
            "--image-amd64",
            f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}",
            "--image-arm64",
            f"ghcr.io/x/cctv-stack@{DIGEST_ARM64}",
        ]
    )
    archive = out / f"cctv-stack-{release.bundle_version(REPO)}.tar.gz"

    assert code == 0
    assert archive.is_file()
    assert release.main(["inspect", str(archive)]) == 0


def test_the_cli_exits_78_on_a_bad_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    code = release.main(
        [
            "bundle",
            "--repo",
            str(empty),
            "--out",
            str(tmp_path / "dist"),
            "--frigate-version",
            "0.18.0",
            "--frigate-contract-verified",
            "0.18.0",
            "--frigate-amd64",
            FRIGATE_AMD64,
            "--frigate-arm64",
            FRIGATE_ARM64,
            "--mosquitto",
            MOSQUITTO,
            "--image-amd64",
            f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}",
            "--image-arm64",
            f"ghcr.io/x/cctv-stack@{DIGEST_ARM64}",
        ]
    )

    assert code in {74, 78}
    assert "cctv-release:" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------
# The release workflow
# ---------------------------------------------------------------------------------------

PUBLISH = REPO / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))


def test_the_bundle_job_runs_only_on_a_tag() -> None:
    job = _workflow()["jobs"]["bundle"]

    assert "refs/tags/v" in job["if"]
    # It needs the image it pins, and a tree that passed verification.
    assert "image" in job["needs"]
    assert "verify" in job["needs"]


def test_the_release_attaches_the_signature_not_just_the_bundle() -> None:
    """A release without the signature is a release nobody can verify.

    The `release` job downloads a named artefact, so a bundle that is uploaded but whose
    signature is not uploaded *with it* would silently produce an unverifiable release.
    """
    jobs = _workflow()["jobs"]
    upload = next(
        step
        for step in jobs["bundle"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )
    paths = str(upload["with"]["path"])

    assert "SHA256SUMS" in paths
    assert "SHA256SUMS.sig" in paths
    assert "SHA256SUMS.pem" in paths
    assert "bundle" in jobs["release"]["needs"]


def test_the_checksums_are_signed_keyless_with_the_workflow_identity() -> None:
    job = _workflow()["jobs"]["bundle"]

    assert job["permissions"].get("id-token") == "write"
    signing = [step for step in job["steps"] if "cosign" in str(step.get("uses", ""))]
    assert signing, "no cosign-installer step: the checksums would ship unsigned"
    sign = next(step for step in job["steps"] if "sign-blob" in str(step.get("run", "")))
    # Keyless means no long-lived key to leak; the certificate is what a verifier checks.
    assert "--output-certificate" in sign["run"]
    assert "--output-signature" in sign["run"]


def test_the_frigate_version_and_the_verified_version_are_the_same_in_ci() -> None:
    env = _workflow()["jobs"]["bundle"]["env"]

    # `cctv-release bundle` enforces this too, but a mismatch here would fail a release that
    # was otherwise ready, so it is worth catching in the cheaper place first.
    assert env["FRIGATE_VERSION"] == env["FRIGATE_CONTRACT_VERIFIED"]


def test_the_embedded_digest_script_parses() -> None:
    """The heredoc only runs on a tag, so a syntax error in it waits for a release to appear.

    Parsed here with `ast`, which is the cheapest possible guard against that.
    """
    import ast

    job = _workflow()["jobs"]["bundle"]
    scripts = [step.get("run") or "" for step in job["steps"]]
    heredocs = [script for script in scripts if "<<'PY'" in script]

    assert heredocs, "the digest resolution script is missing"
    for script in heredocs:
        body = script.split("<<'PY'", 1)[1].rsplit("PY", 1)[0]
        ast.parse(body)


def test_the_workflow_bundles_and_inspects_before_it_signs() -> None:
    steps = _workflow()["jobs"]["bundle"]["steps"]
    scripts = [str(step.get("run") or "") for step in steps]

    bundle_at = next(i for i, script in enumerate(scripts) if "cctv-release bundle" in script)
    inspect_at = next(i for i, script in enumerate(scripts) if "cctv-release inspect" in script)
    sign_at = next(i for i, script in enumerate(scripts) if "sign-blob" in script)

    # Verify before signing: a signature over an artefact nobody checked is a signed mistake.
    assert bundle_at < inspect_at < sign_at


# ---------------------------------------------------------------------------------------
# install.sh consuming a bundle
# ---------------------------------------------------------------------------------------

INSTALL = REPO / "install.sh"


def _mirror_awk() -> str:
    """The `--registry` rewrite, lifted out of install.sh.

    Coupled to the script's shape on purpose: the point is to test the program that actually
    runs, and the failure mode of that coupling (the script is edited and this stops finding it)
    is a loud test failure rather than a silently unverified installer.
    """
    text = INSTALL.read_text(encoding="utf-8")
    start = text.find("awk -v target=")
    assert start != -1, "install.sh no longer rewrites the registry with awk"
    end = text.find('\' "$override"', start)
    assert end != -1, "the awk program's terminator changed"
    program = text[start + len("awk ") : end]
    # Drop the `-v target=...` prefix; the test supplies its own target.
    return program.split("\n", 1)[1]


def test_the_first_image_line_of_an_override_is_ours(repo: Path) -> None:
    """install.sh rewrites the first `image:` it finds, so the order is load-bearing."""
    manifest = release.versions_manifest(
        version="9.9.9",
        repo=repo,
        images=images(),
        frigate_version="0.18.0",
        frigate_contract_verified="0.18.0",
        frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        mosquitto_image=MOSQUITTO,
    )

    text = release.override_for(manifest, arch="amd64", services=FIXTURE_SERVICES)
    image_lines = [line for line in text.splitlines() if line.strip().startswith("image:")]

    assert len(image_lines) == 3
    assert DIGEST_AMD64 in image_lines[0]
    # The other two follow it, in the order the generated file writes them.
    assert MOSQUITTO in image_lines[1]
    assert FRIGATE_AMD64 in image_lines[2]


def test_the_registry_flag_moves_the_host_and_keeps_the_digest(repo: Path, tmp_path: Path) -> None:
    import subprocess

    manifest = release.versions_manifest(
        version="9.9.9",
        repo=repo,
        images=images(),
        frigate_version="0.18.0",
        frigate_contract_verified="0.18.0",
        frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        mosquitto_image=MOSQUITTO,
    )
    source = tmp_path / "override.yml"
    source.write_text(
        release.override_for(manifest, arch="amd64", services=FIXTURE_SERVICES),
        encoding="utf-8",
    )

    program = _mirror_awk()
    mirrored = subprocess.run(
        ["awk", "-v", "target=registry.site.local/cctv-stack", program, str(source)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert f"registry.site.local/cctv-stack@{DIGEST_AMD64}" in mirrored
    # Everything else is untouched: a site may mirror only what this project publishes.
    assert FRIGATE_AMD64 in mirrored
    assert MOSQUITTO in mirrored
    assert "ghcr.io/x/cctv-stack" not in mirrored


def test_the_installer_detects_a_bundle_rather_than_being_told() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    # One file's presence decides it, so there is no flag to get wrong and a bundle can never
    # silently try to build source it does not contain.
    assert "if [[ -f versions.yml && -f compose.override.amd64.yml ]]" in text
    assert "docker compose build" in text  # still there for a repository checkout


def test_the_installer_documents_its_flags() -> None:
    import subprocess

    help_text = subprocess.run(
        ["bash", str(INSTALL), "--help"], check=True, capture_output=True, text=True
    ).stdout

    assert "--offline" in help_text
    assert "--registry" in help_text


def test_the_installer_refuses_an_unknown_argument() -> None:
    import subprocess

    result = subprocess.run(["bash", str(INSTALL), "--nonsense"], capture_output=True, text=True)

    assert result.returncode != 0
    assert "unknown argument" in result.stderr


def test_offline_mode_still_verifies_every_pinned_image() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    # `--offline` skips the pull, never the check: an unloaded image is a clear failure rather
    # than a Compose error three steps later.
    assert "docker image inspect" in text
    assert "is not loaded" in text


def test_every_cctv_service_is_named_in_the_override(repo: Path) -> None:
    """The bug this test exists for: an override that pins nothing at all.

    `x-cctv-image` is an extension key Compose ignores, and a YAML anchor does not resolve across
    two merged files. So an override that only redefined the anchor would leave every container
    running whatever `cctv-stack:local` was on the host — and nothing else would complain.
    """
    manifest = release.versions_manifest(
        version="9.9.9",
        repo=repo,
        images=images(),
        frigate_version="0.18.0",
        frigate_contract_verified="0.18.0",
        frigate_images={"amd64": FRIGATE_AMD64, "arm64": FRIGATE_ARM64},
        mosquitto_image=MOSQUITTO,
    )

    derived = yaml.safe_load(
        release.override_for(manifest, arch="amd64", services=FIXTURE_SERVICES)
    )

    # The anchor resolves here, because it is defined in the same file the services merge from.
    assert derived["services"]["worker"]["image"] == f"ghcr.io/x/cctv-stack@{DIGEST_AMD64}"


def test_the_cctv_services_are_derived_from_compose_not_listed() -> None:
    source = (REPO / "compose.yml").read_text(encoding="utf-8")

    found = release.cctv_services(source)

    # Adding a service to compose.yml cannot leave it unpinned: it is found by its image
    # reference, not by appearing in a list someone has to remember to update.
    assert found
    assert "clip-worker" in found
    assert "mosquitto" not in found
    assert "frigate" not in found


def test_inspect_catches_an_override_that_pins_nothing(tmp_path: Path) -> None:
    """The half-fix: the anchor present, no service merged from it."""
    result = assemble(REPO, tmp_path / "dist")
    tampered = _canonical_name(result.archive, tmp_path, "unpinned")
    with tarfile.open(result.archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            handle = source.extractfile(member) if member.isfile() else None
            if member.name.endswith("compose.override.amd64.yml"):
                payload = (
                    "x-cctv-image-override: &cctv-image-override\n"
                    "  image: ghcr.io/x/cctv-stack@sha256:" + "a" * 64 + "\n"
                    "services:\n"
                    "  mosquitto:\n"
                    "    image: eclipse-mosquitto@sha256:" + "e" * 64 + "\n"
                ).encode()
                info = tarfile.TarInfo(member.name)
                info.size = len(payload)
                info.mode = member.mode
                target.addfile(info, io.BytesIO(payload))
                continue
            target.addfile(member, handle)

    checked = release.inspect(tampered)

    assert not checked.ok
    assert any("does not pin every cctv service" in problem for problem in checked.problems)
