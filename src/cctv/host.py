"""Host detection and profile selection (lld/07 §4-§5).

The installer has one job that cannot be rushed: decide what kind of machine this is and
which detector configuration will actually work on it. Two rules from the LLD are structural
here rather than aspirational:

**Detection degrades to `unknown`, never to a wrong answer.** If `vainfo` is absent we do not
claim VAAPI works; we say it could not be confirmed and pick the conservative profile. A wrong
acceleration profile produces a system that looks configured and silently falls back to CPU —
a slow, hot machine the operator has no reason to suspect.

**An unsupported host is refused before anything is started.** Matching a support row is a
fact, not an attempt: either the machine is one of the documented shapes or the installer says
so and stops, rather than failing halfway through a pull.

The probes are injected. Every decision below can therefore be tested on any machine, which is
the only way this code gets exercised at all: the CI runner is not a Raspberry Pi, and a
detector tested only on the host it runs on is tested exactly once.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CamerasConfig

__all__ = [
    "PROFILES",
    "Detection",
    "HostFacts",
    "HostProfile",
    "Probe",
    "detect",
    "render_host_yml",
    "shm_size_mb",
]

#: Normalised architecture names. Everything downstream uses these, never `uname`'s spellings.
AMD64 = "amd64"
ARM64 = "arm64"

#: Profiles from lld/07 §5. `camera_limit` is a validation cap, not a hard failure: exceeding
#: it warns, because a machine can be pushed and the operator should know the cost.
PROFILES: dict[str, dict[str, Any]] = {
    "intel-igpu": {
        "detector": "openvino",
        "hwaccel": "preset-vaapi",
        "camera_limit": 8,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-intel"],
    },
    "coral-usb": {
        "detector": "edgetpu",
        "hwaccel": None,
        "camera_limit": 12,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-coral"],
    },
    "coral-pcie": {
        "detector": "edgetpu",
        "hwaccel": None,
        "camera_limit": 16,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-coral"],
    },
    "hailo": {
        "detector": "hailo8l",
        "hwaccel": None,
        "camera_limit": 16,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-hailo"],
    },
    "cpu-x86": {
        "detector": "openvino",
        "hwaccel": None,
        "camera_limit": 4,
        "detect": {"width": 640, "height": 360, "fps": 5},
        "compose_profiles": [],
    },
    "pi-cpu": {
        "detector": "cpu",
        "hwaccel": None,
        "camera_limit": 2,
        "detect": {"width": 640, "height": 360, "fps": 3},
        "compose_profiles": [],
    },
    "rknn": {
        "detector": "rknn",
        "hwaccel": None,
        "camera_limit": 6,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-rknn"],
    },
    "nvidia": {
        "detector": "onnx",
        "hwaccel": "preset-nvidia-h264",
        "camera_limit": 12,
        "detect": {"width": 1280, "height": 720, "fps": 5},
        "compose_profiles": ["hwaccel-nvidia"],
    },
    "dev-mac": {
        "detector": "cpu",
        "hwaccel": None,
        "camera_limit": 2,
        "detect": {"width": 640, "height": 360, "fps": 5},
        "compose_profiles": ["dev-synthetic"],
    },
    "wsl2": {
        "detector": "cpu",
        "hwaccel": None,
        "camera_limit": 2,
        "detect": {"width": 640, "height": 360, "fps": 5},
        "compose_profiles": [],
    },
}


@dataclass(frozen=True, slots=True)
class HostProfile:
    """A support-matrix row, resolved."""

    name: str
    detector: str
    hwaccel: str | None
    camera_limit: int
    detect: dict[str, int]
    compose_profiles: tuple[str, ...]

    @classmethod
    def named(cls, name: str) -> HostProfile:
        row = PROFILES[name]
        return cls(
            name=name,
            detector=str(row["detector"]),
            hwaccel=row["hwaccel"],
            camera_limit=int(row["camera_limit"]),
            detect=dict(row["detect"]),
            compose_profiles=tuple(row["compose_profiles"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "detector": self.detector,
            "hwaccel": self.hwaccel,
            "camera_limit": self.camera_limit,
            "detect": dict(self.detect),
            "compose_profiles": list(self.compose_profiles),
        }


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What was actually observed, with `None` wherever nothing could be confirmed."""

    os_name: str
    arch: str
    model: str | None = None
    distro: str | None = None
    cpu_cores: int | None = None
    memory_bytes: int | None = None
    docker_present: bool = False
    docker_ostype: str | None = None
    dri_present: bool = False
    intel_gpu: bool | None = None
    nvidia: bool = False
    #: `None` means "we could not check", which is not the same as "no".
    vaapi_confirmed: bool | None = None
    coral_usb: bool = False
    coral_pcie: bool = False
    hailo: bool = False
    raspberry_pi: bool = False
    rockchip: bool = False
    apple_silicon: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "os": self.os_name,
            "arch": self.arch,
            "model": self.model,
            "distro": self.distro,
            "cpu_cores": self.cpu_cores,
            "memory_bytes": self.memory_bytes,
            "docker": {"present": self.docker_present, "ostype": self.docker_ostype},
            "gpu": {
                "dri_present": self.dri_present,
                "intel": self.intel_gpu,
                "nvidia": self.nvidia,
                "vaapi": self.vaapi_confirmed,
            },
            "accelerators": self.accelerators(),
        }

    def accelerators(self) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        if self.coral_usb:
            found.append({"type": "coral", "bus": "usb"})
        if self.coral_pcie:
            found.append({"type": "coral", "bus": "pcie"})
        if self.hailo:
            found.append({"type": "hailo", "device": "/dev/hailo0"})
        return found


@dataclass(frozen=True, slots=True)
class Detection:
    """The facts, the row they matched, and everything a human should be told."""

    facts: HostFacts
    profile: HostProfile | None
    warnings: tuple[str, ...] = ()
    unsupported_reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.profile is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.facts.to_dict(),
            "recommended_profile": None if self.profile is None else self.profile.name,
            "warnings": list(self.warnings),
            "unsupported_reason": self.unsupported_reason,
        }


def _normalise_arch(machine: str) -> str:
    machine = machine.strip().lower()
    if machine in {"x86_64", "amd64"}:
        return AMD64
    if machine in {"aarch64", "arm64"}:
        return ARM64
    return machine or "unknown"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return None


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _memory_bytes() -> int | None:
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else None
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else None
    if page_size and pages:
        return int(page_size) * int(pages)
    return None


@dataclass
class Probe:
    """Every machine observation the detector makes. Injectable, so tests are portable."""

    uname_system: Callable[[], str] = platform.system
    uname_machine: Callable[[], str] = platform.machine
    read: Callable[[Path], str | None] = _read
    exists: Callable[[Path], bool] = field(default=lambda path: Path(path).exists())
    which: Callable[[str], bool] = _which
    run: Callable[[list[str]], str | None] = _run
    cpu_count: Callable[[], int | None] = lambda: os.cpu_count()
    memory_bytes: Callable[[], int | None] = _memory_bytes


#: Coral USB PIDs. `1a6e:089a` is the pre-flash device; both count.
CORAL_USB_IDS = ("18d1:9302", "1a6e:089a")


def _docker_ostype(probe: Probe) -> str | None:
    output = probe.run(["docker", "info", "--format", "{{.OSType}}"])
    return None if output is None else output.strip().lower() or None


def _detect_coral_usb(probe: Probe) -> bool:
    listing = probe.run(["lsusb"])
    if listing is not None:
        lowered = listing.lower()
        if any(identifier in lowered for identifier in CORAL_USB_IDS):
            return True
    # Fallback for hosts without lsusb: the kernel exposes the same ids per device.
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.is_dir():
        return False
    for device in usb_root.iterdir():
        vendor = probe.read(device / "idVendor")
        product = probe.read(device / "idProduct")
        if vendor and product and f"{vendor.lower()}:{product.lower()}" in CORAL_USB_IDS:
            return True
    return False


def _detect_intel(probe: Probe) -> bool | None:
    """`True`/`False` from PCI, or `None` when `lspci` is not available to ask."""
    listing = probe.run(["lspci"])
    if listing is None:
        return None
    return "8086" in listing.lower() or "intel" in listing.lower()


def collect(probe: Probe | None = None) -> HostFacts:
    probe = Probe() if probe is None else probe
    system = probe.uname_system()
    distro_text = probe.read(Path("/etc/os-release")) or ""
    distro: str | None = None
    for line in distro_text.splitlines():
        if line.startswith("PRETTY_NAME="):
            distro = line.split("=", 1)[1].strip('"')
    model = probe.read(Path("/proc/device-tree/model")) or probe.read(
        Path("/sys/firmware/devicetree/base/model")
    )
    compatible = (
        probe.read(Path("/proc/device-tree/compatible"))
        or probe.read(Path("/sys/firmware/devicetree/base/compatible"))
        or ""
    )
    machine = _normalise_arch(probe.uname_machine())
    dri = probe.exists(Path("/dev/dri"))
    ostype = _docker_ostype(probe) if probe.which("docker") else None

    apple = system == "Darwin" and machine == ARM64
    if apple:
        brand = probe.run(["sysctl", "-n", "machdep.cpu.brand_string"]) or ""
        apple = "apple" in brand.lower()

    return HostFacts(
        os_name=system.lower(),
        arch=machine,
        model=model,
        distro=distro,
        cpu_cores=probe.cpu_count(),
        memory_bytes=probe.memory_bytes(),
        docker_present=probe.which("docker"),
        docker_ostype=ostype,
        dri_present=dri,
        intel_gpu=_detect_intel(probe) if dri else False,
        nvidia=probe.which("nvidia-smi"),
        # `vainfo` absent is *unknown*, not "no": claiming either way would be a guess.
        vaapi_confirmed=(probe.run(["vainfo"]) is not None) if probe.which("vainfo") else None,
        coral_usb=_detect_coral_usb(probe),
        coral_pcie=probe.exists(Path("/dev/apex_0")),
        hailo=probe.exists(Path("/dev/hailo0")) or probe.which("hailo"),
        raspberry_pi="raspberry pi" in (model or "").lower(),
        rockchip="rockchip" in compatible.lower(),
        apple_silicon=apple,
    )


def _profile_name(facts: HostFacts) -> str | None:
    """The support matrix, top to bottom. Order is the decision (lld/07 §2)."""
    if facts.os_name == "darwin":
        return "dev-mac" if facts.apple_silicon else None
    if facts.docker_ostype == "wsl" or "microsoft" in (facts.model or "").lower():
        return "wsl2"
    if facts.os_name != "linux":
        return None
    if facts.raspberry_pi:
        return "pi-cpu"
    if facts.rockchip:
        return "rknn"
    if facts.coral_usb:
        return "coral-usb"
    if facts.coral_pcie:
        return "coral-pcie"
    if facts.hailo:
        return "hailo"
    if facts.nvidia:
        return "nvidia"
    if facts.intel_gpu:
        return "intel-igpu"
    if facts.arch == AMD64:
        return "cpu-x86"
    return None


def _warnings_for(facts: HostFacts, profile: HostProfile) -> list[str]:
    warnings: list[str] = []
    if profile.name == "pi-cpu":
        cores = facts.cpu_cores or 4
        warnings.append(
            "No Coral Edge TPU or Hailo accelerator was found. Object detection will run on "
            f"the CPU. With more than 2 cameras this will likely be too slow to be useful "
            f"({cores} cores detected)."
        )
    if profile.name == "cpu-x86":
        warnings.append(
            "No hardware accelerator was found. Detection runs on the CPU; "
            f"{profile.camera_limit} cameras at low resolution is the documented limit."
        )
    if facts.dri_present and facts.vaapi_confirmed is None:
        warnings.append(
            "A graphics device exists at /dev/dri but `vainfo` is not installed, so hardware "
            "decoding could not be confirmed. The profile assumes none."
        )
    if facts.dri_present and facts.intel_gpu is None:
        warnings.append(
            "A graphics device exists but `lspci` is not available, so the GPU vendor could "
            "not be identified. The profile does not assume Intel acceleration."
        )
    if profile.name == "wsl2":
        warnings.append(
            "Windows/WSL2 support is best effort: hardware acceleration is unavailable and "
            "clip boundaries depend on the host clock."
        )
    if facts.accelerators() and profile.name in {"pi-cpu", "cpu-x86", "wsl2", "dev-mac"}:
        warnings.append(
            "An accelerator was detected but the selected profile does not use it. Choose a "
            "profile manually if you want to run detection on it."
        )
    return warnings


def detect(probe: Probe | None = None) -> Detection:
    facts = collect(probe)
    name = _profile_name(facts)
    if name is None:
        return Detection(
            facts=facts,
            profile=None,
            warnings=(),
            unsupported_reason=(
                f"This machine ({facts.os_name}/{facts.arch}) is not one of the supported "
                "hosts in lld/07 §2. The installer refuses rather than starting a stack that "
                "will not work."
            ),
        )
    profile = HostProfile.named(name)
    return Detection(facts=facts, profile=profile, warnings=tuple(_warnings_for(facts, profile)))


def shm_size_mb(cameras: int, *, width: int, height: int) -> int:
    """`shm_size` from camera count and detection resolution (lld/07 §5.1).

    Docker's 64 MB default is the classic Frigate deployment failure, so the formula is
    clamped to something that is never useless and never absurd.
    """
    if cameras <= 0:
        return 128
    scaled = 32 * cameras * (width * height) / (1280 * 720)
    return int(max(128, min(1024, 64 + scaled)))


def shm_for_config(config: CamerasConfig) -> int:
    """shm for a real config: one camera count, the largest detection area in it.

    Using the largest area rather than each camera's own is deliberate — the budget is shared,
    and sizing by the average is how a deployment works until the day it does not.
    """
    cameras = list(config.enabled_cameras().values())
    if not cameras:
        return shm_size_mb(0, width=640, height=360)
    width = max(camera.detect.width for camera in cameras)
    height = max(camera.detect.height for camera in cameras)
    return shm_size_mb(len(cameras), width=width, height=height)


def render_host_yml(detection: Detection, *, shm_mb: int) -> str:
    """`config/host.yml` — generated, with a header, and no secrets in it."""
    body = {
        "profile": None if detection.profile is None else detection.profile.to_dict(),
        "shm_size_mb": shm_mb,
        "warnings": list(detection.warnings),
        "unsupported_reason": detection.unsupported_reason,
        "facts": detection.facts.to_dict(),
    }
    header = (
        "# GENERATED FILE — DO NOT EDIT.\n"
        "# Written by `cctv-host` from probes of this machine. Editing it is lost the next\n"
        "# time the installer runs (INV-3: configuration has one source of truth).\n"
    )
    return header + json.dumps(body, indent=2, sort_keys=False) + "\n"
