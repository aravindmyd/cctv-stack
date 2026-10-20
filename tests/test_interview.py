"""Tests for the install interview.

The prompts are driven by scripted answers, so every scenario here is a real conversation: a
typo that gets re-prompted, a password that must not be echoed, a second camera, a cloud
answer of "no". What is asserted is the two files that come out and — repeatedly — what must
*not* come out: a secret in the output, an unvalidated config on disk, a `.env` readable by
other users.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from cctv import interview
from cctv.config import load_cameras_config
from cctv.interview import Answers, CameraAnswer, Interview, InterviewError, redact

RTSP = "rtsp://user:s3cret@10.0.0.5:554/Streaming/Channels/101"
SUB = "rtsp://user:s3cret@10.0.0.5:554/Streaming/Channels/102"


class Script:
    """Answers in order, recording every prompt it was asked."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self._answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self._answers.pop(0)


class Secrets:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self._answers:
            raise AssertionError(f"unexpected secret prompt: {prompt!r}")
        return self._answers.pop(0)


@pytest.fixture()
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".env", tmp_path / "config" / "cameras.yml"


def _interview(
    paths: tuple[Path, Path],
    *answers: str,
    secrets: tuple[str, ...] = (),
    out: list[str] | None = None,
) -> tuple[Interview, Script, list[str]]:
    env_path, cameras_path = paths
    script = Script(*answers)
    lines: list[str] = [] if out is None else out
    obj = Interview(
        env_path=env_path,
        cameras_path=cameras_path,
        ask=script,
        secret=Secrets(*secrets),
        out=lines.append,
        environ={"TZ": "UTC"},
        hostname="Parents-Home.local",
    )
    return obj, script, lines


# --- the common path ------------------------------------------------------------------


def test_one_camera_and_no_cloud(paths: tuple[Path, Path]) -> None:
    # location (default), timezone (default), camera name (default), main url,
    # sub-stream no, another camera no, cloud no
    obj, _, _ = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    answers = obj.run()
    obj.apply(answers)

    env_path, cameras_path = paths
    env = interview.read_env(env_path)
    assert env["TZ"] == "UTC"
    assert env["CCTV_SITE"] == "parents-home"
    assert env["CAMERA_FRONT_DOOR_RTSP"] == RTSP
    assert "CCTV_STORAGE_BUCKET" not in env

    config = load_cameras_config(cameras_path, environ=env)
    assert list(config.enabled_cameras()) == ["front-door"]
    assert config.site.location == "parents-home"


def test_env_is_written_six_hundred(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    obj.apply(obj.run())
    mode = stat.S_IMODE(paths[0].stat().st_mode)
    assert mode == 0o600


def test_existing_env_keys_are_preserved(paths: tuple[Path, Path]) -> None:
    env_path, _ = paths
    env_path.write_text(
        "# operator comment\nCCTV_IMAGE=cctv-stack:0.6.0\nFRIGATE_SHM_SIZE=512mb\nTZ=Europe/Paris\n",
        encoding="utf-8",
    )
    obj, _, _ = _interview(paths, "", "Australia/Sydney", "", RTSP, "n", "n", "n")
    obj.apply(obj.run())

    text = env_path.read_text(encoding="utf-8")
    assert "# operator comment" in text
    assert "CCTV_IMAGE=cctv-stack:0.6.0" in text
    assert "FRIGATE_SHM_SIZE=512mb" in text
    assert "TZ=Australia/Sydney" in text
    assert "TZ=Europe/Paris" not in text


def test_existing_location_is_offered_as_the_default(paths: tuple[Path, Path]) -> None:
    _, cameras_path = paths
    cameras_path.parent.mkdir(parents=True)
    cameras_path.write_text(
        "site:\n  location: grandparents\n  timezone: Asia/Tokyo\ncameras: {}\n", encoding="utf-8"
    )
    obj, script, _ = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    obj.run()
    assert any("[grandparents]" in prompt for prompt in script.asked)
    assert any("[Asia/Tokyo]" in prompt for prompt in script.asked)


# --- validation as you go ---------------------------------------------------------------


def test_invalid_location_is_re_prompted(paths: tuple[Path, Path]) -> None:
    obj, _, lines = _interview(paths, "Parents Home", "", "", "", RTSP, "n", "n", "n")
    answers = obj.run()
    assert answers.location == "parents-home"  # fell back to the default on the retry
    assert any("lowercase" in line for line in lines)


def test_invalid_timezone_is_re_prompted(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(paths, "", "Mars/Olympus", "Australia/Sydney", "", RTSP, "n", "n", "n")
    answers = obj.run()
    assert answers.timezone == "Australia/Sydney"


def test_invalid_rtsp_scheme_is_re_prompted(paths: tuple[Path, Path]) -> None:
    obj, _, lines = _interview(paths, "", "", "", "http://10.0.0.5/stream", RTSP, "n", "n", "n")
    answers = obj.run()
    assert answers.cameras[0].main == RTSP
    assert any("rtsp://" in line for line in lines)


def test_invalid_camera_name_is_re_prompted(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(paths, "", "", "FrontDoor", "", RTSP, "n", "n", "n")
    answers = obj.run()
    assert answers.cameras[0].name == "front-door"


def test_giving_up_is_an_error_not_a_loop(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(paths, "Bad Name", "Bad Name", "Bad Name")
    with pytest.raises(InterviewError, match="no valid location"):
        obj.run()


# --- cameras ---------------------------------------------------------------------------


def test_two_cameras_each_get_their_own_variables(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(
        paths,
        "",  # location
        "",  # timezone
        "",  # name
        RTSP,  # main
        "y",  # has sub
        SUB,  # sub
        "y",  # another camera
        "side-gate",  # name
        RTSP.replace("101", "201"),  # main
        "n",  # no sub
        "n",  # done
        "n",  # no cloud
    )
    answers = obj.run()
    obj.apply(answers)

    env = interview.read_env(paths[0])
    assert env["CAMERA_FRONT_DOOR_RTSP"] == RTSP
    assert env["CAMERA_FRONT_DOOR_SUB"] == SUB
    assert env["CAMERA_SIDE_GATE_RTSP"].endswith("201")
    assert "CAMERA_SIDE_GATE_SUB" not in env

    config = load_cameras_config(paths[1], environ=env)
    assert sorted(config.enabled_cameras()) == ["front-door", "side-gate"]


def test_duplicate_camera_name_is_refused(paths: tuple[Path, Path]) -> None:
    obj, _, lines = _interview(
        paths,
        "",
        "",
        "",
        RTSP,
        "n",  # first camera
        "y",
        "front-door",  # duplicate name, which is refused
        "side-gate",
        RTSP,
        "n",  # the corrected second camera
        "n",  # done
        "n",  # no cloud
    )
    answers = obj.run()
    assert [camera.name for camera in answers.cameras] == ["front-door", "side-gate"]
    assert any("already defined" in line for line in lines)


# --- secrets ---------------------------------------------------------------------------


def test_secret_is_never_printed(paths: tuple[Path, Path]) -> None:
    obj, _, lines = _interview(
        paths, "", "", "", RTSP, "n", "n", "y", "cctv-parents-home", "keyid", secrets=("appkey",)
    )
    answers = obj.run()
    obj.apply(answers)
    output = "\n".join(lines)
    assert "appkey" not in output
    assert "s3cret" not in output
    assert interview.read_env(paths[0])["CCTV_STORAGE_APPLICATION_KEY"] == "appkey"


def test_summary_shows_the_host_but_not_the_password() -> None:
    answers = Answers(
        location="home",
        timezone="UTC",
        cameras=(CameraAnswer(name="front-door", main=RTSP),),
    )
    rendered = "\n".join(answers.summary())
    assert "10.0.0.5" in rendered
    assert "s3cret" not in rendered


def test_redact_keeps_the_path() -> None:
    assert redact(RTSP) == "rtsp://***@10.0.0.5:554/Streaming/Channels/101"


def test_missing_secret_is_re_prompted(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(
        paths,
        "",
        "",
        "",
        RTSP,
        "n",
        "n",
        "y",
        "cctv-parents-home",
        "keyid",
        secrets=("", "appkey"),
    )
    answers = obj.run()
    assert answers.application_key == "appkey"


# --- cloud -----------------------------------------------------------------------------


def test_cloud_answers_become_env_keys(paths: tuple[Path, Path]) -> None:
    obj, _, _ = _interview(
        paths,
        "",
        "",
        "",
        RTSP,
        "n",
        "n",
        "y",
        "cctv-parents-home",
        "keyid",
        secrets=("appkey",),
    )
    answers = obj.run()
    assert answers.cloud_enabled
    obj.apply(answers)
    env = interview.read_env(paths[0])
    assert env["CCTV_STORAGE_BUCKET"] == "cctv-parents-home"
    assert env["CCTV_STORAGE_KEY_ID"] == "keyid"


# --- writing ---------------------------------------------------------------------------


def test_generated_config_is_validated_before_it_replaces_the_real_one(
    paths: tuple[Path, Path],
) -> None:
    """A config that would not load must never reach the path the stack reads."""
    _, cameras_path = paths
    cameras_path.parent.mkdir(parents=True)
    cameras_path.write_text("# the operator's own file\n", encoding="utf-8")

    obj, _, _ = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    answers = obj.run()

    def refuse(_text: str, _env: dict[str, str]) -> tuple[str, ...]:
        raise InterviewError("the configuration this produced is invalid: rejected")

    obj._validate = refuse  # type: ignore[method-assign]
    with pytest.raises(InterviewError):
        obj.apply(answers)
    assert cameras_path.read_text(encoding="utf-8") == "# the operator's own file\n"


def test_dry_run_writes_nothing(paths: tuple[Path, Path]) -> None:
    obj, _, lines = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    answers = obj.run()
    obj.apply(answers, dry_run=True)
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert any("Would write" in line for line in lines)


def test_warnings_from_the_loader_are_surfaced(paths: tuple[Path, Path]) -> None:
    """No sub-stream is legal but costs CPU, and the operator should hear it now."""
    obj, _, lines = _interview(paths, "", "", "", RTSP, "n", "n", "n")
    obj.apply(obj.run())
    assert any("sub" in line and "⚠️" in line for line in lines)


def test_rendered_yaml_names_the_camera(paths: tuple[Path, Path]) -> None:
    answers = Answers(
        location="parents-home",
        timezone="UTC",
        cameras=(CameraAnswer(name="front-door", main=RTSP),),
    )
    text = interview.render_cameras_yml(answers)
    assert "  front-door:" in text
    assert "display_name: Front Door" in text
    assert "main: ${CAMERA_FRONT_DOOR_RTSP}" in text


# --- CLI -------------------------------------------------------------------------------


def test_cli_refuses_to_overwrite_without_force(
    paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, cameras_path = paths
    cameras_path.parent.mkdir(parents=True)
    cameras_path.write_text("site: {}\n", encoding="utf-8")

    code = interview.main(["--env", str(paths[0]), "--cameras", str(cameras_path)])
    assert code == interview.EXIT_CONFIG
    assert "already exists" in capsys.readouterr().err


def test_non_interactive_refuses_a_missing_config(
    paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = interview.main(["--env", str(paths[0]), "--cameras", str(paths[1]), "--non-interactive"])
    assert code == interview.EXIT_CONFIG
    assert "will not create it" in capsys.readouterr().err


def test_non_interactive_refuses_placeholders(
    paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env_path, cameras_path = paths
    cameras_path.parent.mkdir(parents=True)
    cameras_path.write_text(
        "site:\n  location: home\n  timezone: UTC\n"
        "cameras:\n  front-door:\n    rtsp:\n      main: ${CAMERA_FRONT_DOOR_RTSP}\n",
        encoding="utf-8",
    )
    env_path.write_text("CAMERA_FRONT_DOOR_RTSP=rtsp://user:password@1.2.3.4/s\n", encoding="utf-8")

    code = interview.main(
        ["--env", str(env_path), "--cameras", str(cameras_path), "--non-interactive"]
    )
    assert code == interview.EXIT_CONFIG
    assert "placeholder" in capsys.readouterr().err


def test_non_interactive_accepts_a_complete_site(
    paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env_path, cameras_path = paths
    cameras_path.parent.mkdir(parents=True)
    cameras_path.write_text(
        "site:\n  location: home\n  timezone: UTC\n"
        "cameras:\n  front-door:\n    rtsp:\n      main: ${CAMERA_FRONT_DOOR_RTSP}\n",
        encoding="utf-8",
    )
    env_path.write_text(f"CAMERA_FRONT_DOOR_RTSP={RTSP}\n", encoding="utf-8")

    code = interview.main(
        ["--env", str(env_path), "--cameras", str(cameras_path), "--non-interactive"]
    )
    assert code == interview.EXIT_OK
    assert "1 camera(s) enabled" in capsys.readouterr().out
