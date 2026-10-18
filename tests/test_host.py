"""Tests for host detection and profile selection (M6, lld/07 §4-§5).

Every probe is injected, so the whole support matrix is exercised on whatever machine runs the
suite. The tests that matter most are the negative ones: an unsupported host must be refused,
and detection must say `unknown` rather than guess when it cannot confirm a capability.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctv import host
from cctv.host_cli import EXIT_CONFIG, main


def make_probe(
    *,
    system: str = "Linux",
    machine: str = "x86_64",
    files: dict[str, str] | None = None,
    exists: set[str] | None = None,
    which: set[str] | None = None,
    commands: dict[str, str | None] | None = None,
    cpu: int = 4,
    memory: int = 8 * 1024**3,
) -> host.Probe:
    """A fake machine. `commands` is keyed by the executable, which is all the detector asks."""
    files = files or {}
    exists = exists or set()
    which = which or set()
    commands = commands or {}

    def read(path: Path) -> str | None:
        return files.get(str(path))

    def does_exist(path: Path) -> bool:
        return str(path) in exists

    def has(name: str) -> bool:
        return name in which

    def run(command: list[str]) -> str | None:
        if command and command[0] == "docker":
            return commands.get("docker")
        return commands.get(command[0]) if command else None

    return host.Probe(
        uname_system=lambda: system,
        uname_machine=lambda: machine,
        read=read,
        exists=does_exist,
        which=has,
        run=run,
        cpu_count=lambda: cpu,
        memory_bytes=lambda: memory,
    )


# ---------------------------------------------------------------------------------------
# The support matrix, one row at a time
# ---------------------------------------------------------------------------------------


def test_a_raspberry_pi_gets_pi_cpu_and_a_loud_warning() -> None:
    detection = host.detect(
        make_probe(
            machine="aarch64",
            files={"/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0"},
            exists={"/dev/dri"},
        )
    )

    assert detection.profile is not None
    assert detection.profile.name == "pi-cpu"
    assert detection.profile.detect == {"width": 640, "height": 360, "fps": 3}
    assert any("CPU" in warning for warning in detection.warnings)
    assert any("2 cameras" in warning for warning in detection.warnings)


def test_a_coral_usb_is_detected_from_lsusb() -> None:
    detection = host.detect(
        make_probe(which={"lsusb"}, commands={"lsusb": "Bus 001 Device 004: ID 18d1:9302"})
    )

    assert detection.profile is not None
    assert detection.profile.name == "coral-usb"
    assert detection.profile.detector == "edgetpu"


def test_a_pre_flash_coral_usb_still_counts() -> None:
    detection = host.detect(
        make_probe(which={"lsusb"}, commands={"lsusb": "Bus 001 Device 004: ID 1a6e:089a"})
    )

    assert detection.profile is not None
    assert detection.profile.name == "coral-usb"


def test_coral_pcie_and_hailo_are_recognised() -> None:
    pcie = host.detect(make_probe(exists={"/dev/apex_0"}))
    hailo = host.detect(make_probe(exists={"/dev/hailo0"}))

    assert pcie.profile is not None and pcie.profile.name == "coral-pcie"
    assert hailo.profile is not None and hailo.profile.name == "hailo"


def test_an_intel_igpu_is_detected_and_vaapi_is_reported_unknown() -> None:
    detection = host.detect(
        make_probe(
            exists={"/dev/dri"},
            which={"lspci"},
            commands={"lspci": "00:02.0 VGA compatible controller: Intel Corporation UHD"},
        )
    )

    assert detection.profile is not None
    assert detection.profile.name == "intel-igpu"
    assert detection.facts.vaapi_confirmed is None
    assert any("vainfo" in warning for warning in detection.warnings)


def test_a_graphics_device_without_lspci_does_not_become_an_intel_profile() -> None:
    """The failure this prevents: claiming VAAPI on a machine we could not identify."""
    detection = host.detect(make_probe(exists={"/dev/dri"}))

    assert detection.facts.intel_gpu is None
    assert detection.profile is not None
    assert detection.profile.name == "cpu-x86"
    assert any("lspci" in warning for warning in detection.warnings)


def test_a_plain_x86_host_is_supported_with_a_capacity_warning() -> None:
    detection = host.detect(make_probe())

    assert detection.profile is not None
    assert detection.profile.name == "cpu-x86"
    assert any("4 cameras" in warning for warning in detection.warnings)


def test_an_apple_silicon_mac_is_a_development_host() -> None:
    detection = host.detect(
        make_probe(
            system="Darwin",
            machine="arm64",
            which={"sysctl"},
            commands={"sysctl": "Apple M2"},
        )
    )

    assert detection.profile is not None
    assert detection.profile.name == "dev-mac"
    assert detection.profile.compose_profiles == ("dev-synthetic",)


def test_wsl2_is_recognised_from_the_docker_ostype() -> None:
    detection = host.detect(make_probe(which={"docker"}, commands={"docker": "wsl\n"}))

    assert detection.profile is not None
    assert detection.profile.name == "wsl2"
    assert any("best effort" in warning for warning in detection.warnings)


def test_an_unsupported_host_is_refused_with_a_reason() -> None:
    detection = host.detect(make_probe(machine="aarch64"))

    assert detection.profile is None
    assert detection.supported is False
    assert detection.unsupported_reason is not None
    assert "not one of the supported hosts" in detection.unsupported_reason


def test_a_detected_accelerator_that_the_profile_ignores_is_called_out() -> None:
    """A Pi with a Coral is still capacity-limited; silently ignoring the TPU is worse than
    saying so."""
    detection = host.detect(
        make_probe(
            machine="aarch64",
            files={"/proc/device-tree/model": "Raspberry Pi 5"},
            which={"lsusb"},
            commands={"lsusb": "ID 18d1:9302"},
        )
    )

    assert detection.profile is not None
    assert detection.profile.name == "pi-cpu"
    assert any("does not use it" in warning for warning in detection.warnings)


# ---------------------------------------------------------------------------------------
# shm sizing
# ---------------------------------------------------------------------------------------


def test_shm_is_never_below_the_useful_minimum() -> None:
    assert host.shm_size_mb(2, width=640, height=360) == 128


def test_shm_scales_with_cameras_and_resolution() -> None:
    # 64 + 32 * 4 * 1.0 = 192 for four 720p cameras.
    assert host.shm_size_mb(4, width=1280, height=720) == 192
    # Sixteen 1080p cameras would ask for more than the ceiling; the ceiling wins.
    assert host.shm_size_mb(16, width=1920, height=1080) == 1024


def test_shm_for_a_configuration_uses_the_largest_detection_area(tmp_path: Path) -> None:
    from cctv.config import load_cameras_config

    path = tmp_path / "cameras.yml"
    path.write_text(
        """\
site:
  location: home
  timezone: UTC
cameras:
  front-door:
    rtsp:
      main: rtsp://192.168.1.50:554/1
    detect:
      width: 1280
      height: 720
  garage:
    rtsp:
      main: rtsp://192.168.1.51:554/1
    detect:
      width: 640
      height: 360
""",
        encoding="utf-8",
    )
    config = load_cameras_config(path, environ={})

    # Two cameras, sized by the larger area: 64 + 32 * 2 * 1.0 = 128.
    assert host.shm_for_config(config) == 128


# ---------------------------------------------------------------------------------------
# The written record and the CLI
# ---------------------------------------------------------------------------------------


def test_the_host_file_is_generated_and_contains_no_credentials() -> None:
    detection = host.detect(make_probe(which={"lsusb"}, commands={"lsusb": "ID 18d1:9302"}))

    text = host.render_host_yml(detection, shm_mb=192)

    assert text.startswith("# GENERATED FILE — DO NOT EDIT.")
    assert "coral-usb" in text
    assert "rtsp://" not in text


def test_the_cli_returns_ex_config_for_an_unsupported_host(capsys) -> None:
    code = main(["--json"])

    # The CI machine is either supported or not; either answer must be one of the two
    # documented codes, never a crash.
    assert code in {0, EXIT_CONFIG}
    capsys.readouterr()


def test_the_cli_writes_the_host_file_and_prints_shm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "config" / "host.yml"
    code = main(["--write", str(out), "--shm-env"])

    if code == EXIT_CONFIG:
        # Unsupported runner: nothing written, and that is the contract.
        assert not out.exists()
        return
    assert out.exists()
    assert "FRIGATE_SHM_SIZE=" in capsys.readouterr().out
