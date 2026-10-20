"""Configuration: one source of truth, and everything else generated.

`config/cameras.yml` is the single place a camera is defined
([INV-3](../../../internal/architecture/invariants.md)). Frigate's config, the monitoring
config, and the notify config are all *generated* from it — deterministically, same input
to byte-identical output. A hand edit to a generated file is lost on the next install.

The schema is [`internal/architecture/contracts/cameras-config-schema.md`]. Four rules from
there are worth stating because they are the ones that surprise people:

1. **Unknown keys are a hard error.** A typo that silently does nothing is worse than a
   failed install, because the operator believes a setting is in force when it is not.
2. **Lists are replaced, never concatenated.** `objects: [person]` on one camera replaces
   the default list entirely. Silent list merging is the top cause of "why is it detecting
   cars?".
3. **`null` means "unset, use the inherited value"** — not "set to nothing".
4. **A `${VAR}` that is not set is a hard error**, never an empty string. An empty camera
   password produces a confusing offline camera instead of an honest config failure.

This module is deliberately dependency-light: plain dataclasses and hand-written
validation, no schema framework. The error messages are the interface an operator actually
reads at install time, and a framework's generic errors are worse at that job than a dozen
explicit checks.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .paths import PathError, validate_camera_name, validate_location
from .timeutil import TimeConfigError, resolve_zone

__all__ = [
    "CameraConfig",
    "CamerasConfig",
    "ClipConfig",
    "ConfigError",
    "DetectConfig",
    "FrigateAuthConfig",
    "NotifyConfig",
    "RecordConfig",
    "RetentionConfig",
    "SiteConfig",
    "SnapshotsConfig",
    "load_cameras_config",
    "to_frigate",
]

RECORD_MODES = frozenset({"motion", "continuous", "alerts", "detections", "off"})
SEVERITIES = frozenset({"alert", "detection"})

#: Below this many days of Frigate-side recording, a clip-worker outage of the same
#: length causes permanent loss, because the footage is gone before we fetch it
#: ([INV-9](../../../internal/architecture/invariants.md)). The generator refuses to emit a
#: shorter window without an explicit override.
DEFAULT_MIN_HEADROOM_DAYS = 2


class ConfigError(ValueError):
    """Invalid configuration. Raised at load, never at run time — exit code 78."""


# ---------------------------------------------------------------------------------------
# The resolved model. Frozen: config is read once and never mutated by a worker.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteConfig:
    location: str
    timezone: ZoneInfo
    display_timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class DetectConfig:
    enabled: bool = True
    objects: tuple[str, ...] = ("person",)
    width: int = 1280
    height: int = 720
    fps: int = 5
    min_score: float = 0.4
    min_initialized: int = 2


@dataclass(frozen=True, slots=True)
class RecordConfig:
    """How Frigate retains *its* footage. Not our clip retention — see `RetentionConfig`.

    The default is continuous for two days (ADR-0022): motion-only footage leaves an
    incident with no motion — something left at a gate, a quiet corner at night — with
    nothing recorded at all. Two days costs disk and nothing else: Frigate's recordings are
    local-only and are never uploaded to the bucket.
    """

    mode: str = "continuous"


@dataclass(frozen=True, slots=True)
class ClipConfig:
    """Our clip shape. `pre_roll_s` and `post_roll_s` are ours alone; `visit_gap_s` is
    projected into Frigate because it defines what a visit *is*
    ([ADR-0016](../../../internal/adr/0016-clip-per-review-item.md))."""

    pre_roll_s: float = 5.0
    post_roll_s: float = 10.0
    #: `None` means "do not emit the key", so Frigate's own defaults apply — 40 s for
    #: alerts, 30 s for detections. Emitting our own default would silently change
    #: behaviour the operator did not ask to change.
    visit_gap_s: int | None = None
    severities: tuple[str, ...] = ("alert", "detection")
    objects: tuple[str, ...] = ("person",)


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    local_days: int = 90
    cloud_days: int = 60
    upload: bool = True


@dataclass(frozen=True, slots=True)
class SnapshotsConfig:
    enabled: bool = True
    retain_days: int = 30


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    enabled: bool = True
    labels: tuple[str, ...] = ("person",)


@dataclass(frozen=True, slots=True)
class CameraConfig:
    name: str
    display_name: str
    enabled: bool
    rtsp_main: str
    rtsp_sub: str | None
    detect: DetectConfig
    record: RecordConfig
    clip: ClipConfig
    retention: RetentionConfig
    snapshots: SnapshotsConfig
    notify: NotifyConfig


@dataclass(frozen=True, slots=True)
class FrigateAuthConfig:
    """Whether Frigate's own UI login is on (`frigate.auth.enabled`).

    Not camera data, so it sits beside `site` rather than under `defaults`. It is off by
    default because a fresh install has no stored password yet — Frigate generates one on
    the first start with auth enabled and prints it to the log — and because the generated
    config is only reachable through the loopback-bound authenticated port anyway. Turning
    it on is a one-line change; see the runbook `frigate-authentication.md` for the
    procedure.
    """

    enabled: bool = False


#: `frigate.hwaccel` → the `hwaccel_args` preset Frigate is given. `off` emits nothing, which
#: is the safe default: a preset on a machine without the accelerator fails every ffmpeg
#: input, and Frigate reports that as "no frames", not as "wrong hwaccel".
HWACCEL_PRESETS: dict[str, str] = {
    "vaapi": "preset-vaapi",
    "qsv": "preset-qsv",
}


#: `frigate.detector` values. `openvino` means Frigate's OpenVINO detector on the **iGPU** —
#: the device is fixed to GPU on purpose: OpenVINO on the CPU is strictly slower than the
#: built-in CPU detector it would replace, so offering that combination would only be a way to
#: make a machine worse while looking configured.
FRIGATE_DETECTORS: frozenset[str] = frozenset({"cpu", "openvino"})


@dataclass(frozen=True, slots=True)
class CamerasConfig:
    site: SiteConfig
    cameras: Mapping[str, CameraConfig]
    #: Non-fatal findings. Collected rather than logged so that the loader stays pure and
    #: the installer can present them all at once instead of one per re-run.
    warnings: tuple[str, ...] = ()
    min_headroom_days: int = DEFAULT_MIN_HEADROOM_DAYS
    frigate_auth: FrigateAuthConfig = FrigateAuthConfig()
    #: One of `off`, `vaapi`, `qsv`. Kept as the config word rather than the preset string so
    #: the operator writes what the hardware is, and the mapping to Frigate's spelling lives
    #: in one place (`HWACCEL_PRESETS`).
    frigate_hwaccel: str = "off"
    #: `cpu` or `openvino` (iGPU). Separate from `hwaccel` because they are different hardware
    #: paths with different failure modes: decode can work while the detector has no model.
    frigate_detector: str = "cpu"

    def hwaccel_args(self) -> str | None:
        """The `hwaccel_args` value to emit, or `None` when acceleration is off."""
        return HWACCEL_PRESETS.get(self.frigate_hwaccel)

    def enabled_cameras(self) -> Mapping[str, CameraConfig]:
        return {n: c for n, c in self.cameras.items() if c.enabled}


# ---------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")
_TOP_LEVEL_KEYS = frozenset({"site", "frigate", "defaults", "cameras", "min_headroom_days"})
_CAMERA_KEYS = frozenset(
    {
        "display_name",
        "enabled",
        "rtsp",
        "detect",
        "record",
        "snapshots",
        "clip",
        "retention",
        "notify",
    }
)


_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"


class _Yaml12Loader(yaml.SafeLoader):
    """`safe_load`, but with YAML 1.2 booleans instead of YAML 1.1's.

    PyYAML implements YAML 1.1, where `off`, `on`, `yes` and `no` are booleans. That makes
    `record: {mode: off}` load as `False` — a value *our own schema documents* silently
    becoming a boolean. The same trap would turn a camera named `no` or a location named
    `on` into a boolean, and the resulting error would point at the wrong place entirely.

    YAML 1.2 dropped those spellings, and every modern parser (ruamel, Go's `yaml.v3`,
    Kubernetes) follows it. This loader honours only `true` and `false`, so our config
    means what the schema says it means.

    Note the consequence: `enabled: no` is now the string `"no"` and is refused, because
    guessing that a string means a boolean is how these bugs start. Write `false`.
    """


# Copy before mutating: `yaml_implicit_resolvers` is shared, so editing it in place would
# change `SafeLoader` for everything else in the process, including libraries.
_Yaml12Loader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != _YAML_BOOL_TAG]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    _YAML_BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_cameras_config(
    path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> CamerasConfig:
    """Read, validate, and resolve `cameras.yml`.

    `environ` is injectable so tests never depend on the ambient environment, and so the
    installer can pass an explicit set rather than inheriting whatever the shell has.

    Raises:
        ConfigError: anything at all is wrong. There is no partial success.
    """
    environ = os.environ if environ is None else environ
    source = Path(path)

    try:
        raw = yaml.load(source.read_text(encoding="utf-8"), Loader=_Yaml12Loader)
    except FileNotFoundError as exc:
        raise ConfigError(f"{source}: not found.") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: not valid YAML — {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"{source}: the top level must be a mapping with `site`, `defaults` and `cameras` keys."
        )

    data = _interpolate(raw, environ, where=str(source))
    _check_keys(data, _TOP_LEVEL_KEYS, where=str(source))

    if "site" not in data:
        raise ConfigError(f"{source}: `site` is required — `location` and `timezone`.")
    if "cameras" not in data or not data["cameras"]:
        raise ConfigError(f"{source}: `cameras` must define at least one camera.")

    warnings: list[str] = []
    site = _parse_site(data["site"], warnings)
    frigate_raw = data.get("frigate")
    frigate_auth = _parse_frigate_auth(frigate_raw)
    frigate_hwaccel = _parse_frigate_hwaccel(frigate_raw)
    frigate_detector = _parse_frigate_detector(frigate_raw)
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ConfigError(f"{source}: `defaults` must be a mapping.")

    cameras_raw = data["cameras"]
    if not isinstance(cameras_raw, Mapping):
        raise ConfigError(f"{source}: `cameras` must be a mapping of name to settings.")

    cameras: dict[str, CameraConfig] = {}
    for name, override in cameras_raw.items():
        where = f"cameras.{name}"
        if not isinstance(override, Mapping):
            raise ConfigError(f"{where}: must be a mapping.")
        merged = _deep_merge(defaults, override)
        cameras[name] = _build_camera(name, merged, site, warnings, where)

    min_headroom = data.get("min_headroom_days", DEFAULT_MIN_HEADROOM_DAYS)
    if not isinstance(min_headroom, int) or isinstance(min_headroom, bool) or min_headroom < 1:
        raise ConfigError(f"{source}: `min_headroom_days` must be an integer >= 1.")

    return CamerasConfig(
        site=site,
        cameras=cameras,
        warnings=tuple(warnings),
        min_headroom_days=min_headroom,
        frigate_auth=frigate_auth,
        frigate_hwaccel=frigate_hwaccel,
        frigate_detector=frigate_detector,
    )


def _interpolate(value: Any, environ: Mapping[str, str], *, where: str) -> Any:
    """Resolve `${VAR}` from the environment, refusing to substitute an empty string."""

    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match["name"]
            if name not in environ:
                raise ConfigError(
                    f"{where}: ${{{name}}} is referenced but not set. Add it to `.env` "
                    f"(see `.env.example`). Substituting an empty string would produce a "
                    f"confusing failure later instead of an honest one now."
                )
            return environ[name]

        return _ENV_RE.sub(replace, value)
    if isinstance(value, Mapping):
        return {k: _interpolate(v, environ, where=f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, environ, where=f"{where}[]") for v in value]
    return value


def _check_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {unknown}. Allowed: {sorted(allowed)}. "
            f"Unknown keys are refused rather than ignored, because a typo that silently "
            f"does nothing means believing a setting is in force when it is not."
        )


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """`defaults` under a camera. Nested mappings merge; lists and scalars replace.

    `None` means "not set here" at **every** level, so it is dropped from the base as
    well as skipped in the override. That is what makes `null` consistent: below `defaults`
    there is something to inherit, and inside `defaults` there is not, so the built-in
    default applies. Treating the two differently would be a trap.
    """
    out: dict[str, Any] = {k: v for k, v in base.items() if v is not None}
    for key, value in override.items():
        if value is None:
            continue
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def _parse_frigate_detector(data: Any) -> str:
    """`frigate.detector` — where object detection runs.

    `cpu` (the default) emits Frigate's built-in CPU detector. `openvino` emits the OpenVINO
    detector pinned to `GPU`; the Value is deliberately not `auto`, because a silent fallback
    to CPU is the failure the whole host-profile design exists to avoid.
    """
    if data is None:
        return "cpu"
    if not isinstance(data, Mapping):
        raise ConfigError("frigate: must be a mapping with an `auth` key.")
    value = data.get("detector", "cpu")
    word = value.strip().lower() if isinstance(value, str) else ""
    if word not in FRIGATE_DETECTORS:
        allowed = ", ".join(sorted(FRIGATE_DETECTORS))
        raise ConfigError(f"frigate.detector: {value!r} is not one of {allowed}.")
    return word


def _parse_frigate_auth(data: Any) -> FrigateAuthConfig:
    """`frigate.auth` — whether Frigate's own UI login is on.

    It is deliberately not part of `defaults`: `defaults` is *camera* inheritance, and a
    camera cannot have its own copy of Frigate's process-wide login setting.
    """
    if data is None:
        return FrigateAuthConfig()
    if not isinstance(data, Mapping):
        raise ConfigError("frigate: must be a mapping with an `auth` key.")
    _check_keys(data, frozenset({"auth", "hwaccel", "detector"}), where="frigate")

    auth = data.get("auth") or {}
    if not isinstance(auth, Mapping):
        raise ConfigError("frigate.auth: must be a mapping with an `enabled` key.")
    _check_keys(auth, frozenset({"enabled"}), where="frigate.auth")

    return FrigateAuthConfig(enabled=_bool(auth.get("enabled", False), "frigate.auth.enabled"))


def _parse_frigate_hwaccel(data: Any) -> str:
    """`frigate.hwaccel` — the accelerator Frigate should decode with.

    Refused rather than guessed: a preset the machine cannot run makes every ffmpeg input
    fail, and Frigate reports that as a camera with no frames — the same silent-fallback
    trap `host.py` documents. The values are names for *hardware*, not presets; the mapping
    to Frigate's spelling is `HWACCEL_PRESETS`.
    """
    if data is None:
        return "off"
    if not isinstance(data, Mapping):
        raise ConfigError("frigate: must be a mapping with an `auth` key.")
    value = data.get("hwaccel", "off")
    if not isinstance(value, str):
        raise ConfigError("frigate.hwaccel: must be one of off, vaapi, qsv.")
    word = value.strip().lower()
    allowed = {"off", *HWACCEL_PRESETS}
    if word not in allowed:
        raise ConfigError(f"frigate.hwaccel: {value!r} is not one of {', '.join(sorted(allowed))}.")
    return word


def _parse_site(data: Mapping[str, Any], warnings: list[str]) -> SiteConfig:
    _check_keys(data, frozenset({"location", "timezone", "display_timezone"}), where="site")

    if "location" not in data:
        raise ConfigError("site.location is required.")
    if "timezone" not in data:
        raise ConfigError(
            "site.timezone is required and is never guessed from the host. A container's "
            "TZ is easy to get wrong across six services, and wrong here means silently "
            "misplaced clips (ADR-0017)."
        )

    try:
        location = validate_location(data["location"])
    except PathError as exc:
        raise ConfigError(f"site.location: {exc}") from exc

    timezone = _zone(data["timezone"], "site.timezone")

    if "display_timezone" in data:
        display = _zone(data["display_timezone"], "site.display_timezone")
        if display.key != timezone.key:
            warnings.append(
                f"site.display_timezone ({display.key}) differs from site.timezone "
                f"({timezone.key}): clip filenames and log timestamps will disagree."
            )
    else:
        display = timezone

    return SiteConfig(location=location, timezone=timezone, display_timezone=display)


def _zone(name: Any, where: str) -> ZoneInfo:
    try:
        return resolve_zone(name)
    except TimeConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _build_camera(
    name: str,
    data: Mapping[str, Any],
    site: SiteConfig,
    warnings: list[str],
    where: str,
) -> CameraConfig:
    try:
        validate_camera_name(name)
    except PathError as exc:
        raise ConfigError(f"cameras: {exc}") from exc

    _check_keys(data, _CAMERA_KEYS, where=where)

    enabled = _bool(data.get("enabled", True), f"{where}.enabled")
    display_name = data.get("display_name", name)
    if not isinstance(display_name, str) or not display_name:
        raise ConfigError(f"{where}.display_name must be a non-empty string.")

    detect = _build_detect(data.get("detect") or {}, where)

    rtsp = data.get("rtsp") or {}
    if not isinstance(rtsp, Mapping):
        raise ConfigError(f"{where}.rtsp must be a mapping with `main` and optional `sub`.")
    _check_keys(rtsp, frozenset({"main", "sub"}), where=f"{where}.rtsp")

    rtsp_main = rtsp.get("main")
    rtsp_sub = rtsp.get("sub")

    if enabled:
        if not rtsp_main:
            raise ConfigError(f"{where}.rtsp.main is required for an enabled camera.")
        _require_rtsp_scheme(rtsp_main, f"{where}.rtsp.main")
        if rtsp_sub:
            _require_rtsp_scheme(rtsp_sub, f"{where}.rtsp.sub")
        elif detect.enabled:
            warnings.append(
                f"{where}: no `rtsp.sub`, so detection runs on the main stream. That is "
                f"usually several times the pixels needed and shows up as CPU load."
            )
    clip = _build_clip(data.get("clip") or {}, detect, warnings, where)
    retention = _build_retention(data.get("retention") or {}, where)
    snapshots = _build_snapshots(data.get("snapshots") or {}, where)

    record_raw = data.get("record") or {}
    if not isinstance(record_raw, Mapping):
        raise ConfigError(f"{where}.record must be a mapping.")
    _check_keys(record_raw, frozenset({"mode"}), where=f"{where}.record")
    # The default lives in one place: `RecordConfig.mode`, so the dataclass and the parsed
    # model cannot disagree about what an omitted `record:` block means (ADR-0022).
    mode = record_raw.get("mode", RecordConfig().mode)
    if mode not in RECORD_MODES:
        raise ConfigError(f"{where}.record.mode: {mode!r} is not one of {sorted(RECORD_MODES)}.")

    notify = _build_notify(data.get("notify") or {}, detect, where)

    return CameraConfig(
        name=name,
        display_name=display_name,
        enabled=enabled,
        rtsp_main=rtsp_main or "",
        rtsp_sub=rtsp_sub or None,
        detect=detect,
        record=RecordConfig(mode=mode),
        clip=clip,
        retention=retention,
        snapshots=snapshots,
        notify=notify,
    )


def _build_detect(data: Mapping[str, Any], where: str) -> DetectConfig:
    _check_keys(
        data,
        frozenset(
            {
                "enabled",
                "objects",
                "width",
                "height",
                "fps",
                "min_score",
                "min_initialized",
            }
        ),
        where=f"{where}.detect",
    )
    enabled = _bool(data.get("enabled", True), f"{where}.detect.enabled")
    objects = _str_tuple(data.get("objects", ("person",)), f"{where}.detect.objects")

    if enabled and not objects:
        raise ConfigError(
            f"{where}.detect.objects is empty while detection is enabled: nothing would "
            f"ever be detected, and the camera would look healthy while doing nothing."
        )

    return DetectConfig(
        enabled=enabled,
        objects=objects,
        width=_int(data.get("width", 1280), f"{where}.detect.width", low=64, high=8192),
        height=_int(data.get("height", 720), f"{where}.detect.height", low=64, high=8192),
        fps=_int(data.get("fps", 5), f"{where}.detect.fps", low=1, high=15),
        min_score=_float(
            data.get("min_score", 0.4), f"{where}.detect.min_score", low=0.0, high=1.0
        ),
        min_initialized=_int(
            data.get("min_initialized", 2), f"{where}.detect.min_initialized", low=1
        ),
    )


def _build_clip(
    data: Mapping[str, Any],
    detect: DetectConfig,
    warnings: list[str],
    where: str,
) -> ClipConfig:
    _check_keys(
        data,
        frozenset({"pre_roll_s", "post_roll_s", "visit_gap_s", "severities", "objects"}),
        where=f"{where}.clip",
    )

    pre_roll = _float(data.get("pre_roll_s", 5.0), f"{where}.clip.pre_roll_s", low=0.0, high=60.0)
    if pre_roll > 30:
        warnings.append(
            f"{where}.clip.pre_roll_s is {pre_roll}s. Pre-roll costs storage on every "
            f"clip; above 30s it is usually a sign the value should be per-camera."
        )

    severities = _str_tuple(
        data.get("severities", ("alert", "detection")), f"{where}.clip.severities"
    )
    unknown = sorted(set(severities) - SEVERITIES)
    if unknown:
        raise ConfigError(f"{where}.clip.severities: {unknown} not in {sorted(SEVERITIES)}.")
    if not severities:
        raise ConfigError(
            f"{where}.clip.severities is empty: no severity would ever produce a clip."
        )

    objects = _str_tuple(data.get("objects", ("person",)), f"{where}.clip.objects")
    if not objects:
        raise ConfigError(f"{where}.clip.objects is empty: no review would be clipped.")
    not_tracked = sorted(set(objects) - set(detect.objects))
    if not_tracked:
        raise ConfigError(
            f"{where}.clip.objects: {not_tracked} is not in `detect.objects` "
            f"({list(detect.objects)}). Frigate cannot report an object it was never asked "
            f"to track, so a review containing it can never occur."
        )

    visit_gap_raw = data.get("visit_gap_s")
    visit_gap = None
    if visit_gap_raw is not None:
        visit_gap = _int(visit_gap_raw, f"{where}.clip.visit_gap_s", low=5, high=3600)

    return ClipConfig(
        pre_roll_s=pre_roll,
        post_roll_s=_float(
            data.get("post_roll_s", 10.0),
            f"{where}.clip.post_roll_s",
            low=0.0,
            high=120.0,
        ),
        visit_gap_s=visit_gap,
        severities=severities,
        objects=objects,
    )


def _build_retention(data: Mapping[str, Any], where: str) -> RetentionConfig:
    _check_keys(
        data,
        frozenset({"local_days", "cloud_days", "upload"}),
        where=f"{where}.retention",
    )
    return RetentionConfig(
        local_days=_int(
            data.get("local_days", 90),
            f"{where}.retention.local_days",
            low=1,
            high=3650,
        ),
        cloud_days=_int(
            data.get("cloud_days", 60),
            f"{where}.retention.cloud_days",
            low=1,
            high=3650,
        ),
        upload=_bool(data.get("upload", True), f"{where}.retention.upload"),
    )


def _build_snapshots(data: Mapping[str, Any], where: str) -> SnapshotsConfig:
    _check_keys(data, frozenset({"enabled", "retain_days"}), where=f"{where}.snapshots")
    return SnapshotsConfig(
        enabled=_bool(data.get("enabled", True), f"{where}.snapshots.enabled"),
        retain_days=_int(data.get("retain_days", 30), f"{where}.snapshots.retain_days", low=1),
    )


def _build_notify(data: Mapping[str, Any], detect: DetectConfig, where: str) -> NotifyConfig:
    _check_keys(data, frozenset({"enabled", "labels"}), where=f"{where}.notify")
    labels = _str_tuple(data.get("labels", ("person",)), f"{where}.notify.labels")
    not_tracked = sorted(set(labels) - set(detect.objects))
    if not_tracked:
        raise ConfigError(
            f"{where}.notify.labels: {not_tracked} is not in `detect.objects` "
            f"({list(detect.objects)}); nothing would ever trigger it."
        )
    return NotifyConfig(
        enabled=_bool(data.get("enabled", True), f"{where}.notify.enabled"),
        labels=labels,
    )


# ---------------------------------------------------------------------------------------
# Scalars. Every one reports where it came from, because that is the only useful thing
# a config error can say.
# ---------------------------------------------------------------------------------------


def _require_rtsp_scheme(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.startswith(("rtsp://", "rtsps://")):
        raise ConfigError(f"{where}: must be an rtsp:// or rtsps:// URL.")


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where}: must be `true` or `false`, got {value!r}. YAML 1.2 is in force, so "
            f"`yes`, `no`, `on` and `off` are ordinary strings here — that is deliberate, "
            f"because `record.mode: off` is a documented value and YAML 1.1 would have "
            f"read it as a boolean."
        )
    return value


def _int(value: Any, where: str, *, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: must be an integer, got {value!r}.")
    if value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        raise ConfigError(f"{where}: {value} is outside {bound}.")
    return value


def _float(value: Any, where: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: must be a number, got {value!r}.")
    if not low <= float(value) <= high:
        raise ConfigError(f"{where}: {value} is outside {low}..{high}.")
    return float(value)


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"{where}: must be a list, got {value!r}.")
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{where}: every entry must be a non-empty string.")
    return tuple(value)


# ---------------------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------------------


def to_frigate(config: CamerasConfig, *, allow_short_headroom: bool = False) -> dict[str, Any]:
    """Generate Frigate's config from ours. Deterministic: same input, same output.

    **Partial on purpose.** This emits what the design has actually settled — the review
    cutoff and Frigate-side `record:` retention. The parts that need the host profile
    ([`lld/07`](../../../internal/architecture/lld/07-installer-and-host-profiles.md))
    — `ffmpeg` inputs and `hwaccel_args`, `detectors`, `model`, `objects.track` — land with
    LLD 07, because emitting them requires knowing which accelerator exists. A generator
    that guessed would be worse than one that is obviously incomplete.

    **`site.timezone` is deliberately absent from the result.** Frigate 0.18 removed its
    top-level `timezone` key, and rejects the *entire* config when one is present
    (`Extra inputs are not permitted`) — after which it starts in safe mode with no cameras
    at all. Frigate's own zone comes from the container's `TZ`, and ADR-0017 already treats
    that as Frigate's business: our clip paths are rendered from `site.timezone`, and a
    disagreement between the two is a `doctor.sh` check rather than something this file can
    settle. Do not re-add it.
    """
    out: dict[str, Any] = {
        "cameras": {},
    }

    for name, camera in config.enabled_cameras().items():
        entry: dict[str, Any] = {}

        # The visit threshold is projected, not filtered, because it defines what a visit
        # is — by the time we see a review it is already too late to regroup it.
        if camera.clip.visit_gap_s is not None:
            entry["review"] = {
                "alerts": {"cutoff_time": camera.clip.visit_gap_s},
                "detections": {"cutoff_time": camera.clip.visit_gap_s},
            }

        record = _record_block(camera.record.mode)
        if record is not None:
            headroom = _min_retention_days(record)
            if headroom < config.min_headroom_days and not allow_short_headroom:
                raise ConfigError(
                    f"cameras.{name}.record.mode: {camera.record.mode!r} yields {headroom} "
                    f"day(s) of Frigate-side recording, below min_headroom_days "
                    f"({config.min_headroom_days}). That window is the clip-worker's "
                    f"recovery budget (INV-9): an outage of the same length loses footage "
                    f"permanently. Raise it, or pass allow_short_headroom to accept the risk."
                )
            entry["record"] = record

        out["cameras"][name] = entry

    return out


#: `record.mode` → Frigate retention blocks. From `cameras-config-schema.md` §4.1.
#:
#: Continuous is the default at **two** days. `motion` stays at seven even in that mode, so
#: footage attached to activity outlives the 24/7 churn by five days; alerts and detections
#: keep Frigate's own 30/14. `min_headroom_days` (default 2) is exactly satisfied, which is
#: deliberate — raising it without raising `continuous.days` is refused at generation time.
_RECORD_BLOCKS: dict[str, dict[str, Any]] = {
    "continuous": {
        "enabled": True,
        "continuous": {"days": 2},
        "motion": {"days": 7},
        "alerts": {"retain": {"days": 30}},
        "detections": {"retain": {"days": 14}},
    },
    "motion": {
        "enabled": True,
        "continuous": {"days": 0},
        "motion": {"days": 3},
        "alerts": {"retain": {"days": 30}},
        "detections": {"retain": {"days": 14}},
    },
    "alerts": {
        "enabled": True,
        "continuous": {"days": 0},
        "alerts": {"retain": {"days": 30}},
    },
    "detections": {
        "enabled": True,
        "continuous": {"days": 0},
        "detections": {"retain": {"days": 30}},
    },
    "off": {"enabled": False},
}


def _record_block(mode: str) -> dict[str, Any] | None:
    return _RECORD_BLOCKS[mode]


def _min_retention_days(record: Mapping[str, Any]) -> int:
    """The shortest non-zero retention window in a generated `record:` block.

    Zero means "off" for that category, so it does not bound anything — including it
    would make every mode look like a zero-day window.
    """
    days: list[int] = []
    for key in ("continuous", "motion", "alerts", "detections"):
        block = record.get(key)
        if not isinstance(block, Mapping):
            continue
        value = block.get("days") or (block.get("retain") or {}).get("days")
        if isinstance(value, int) and value > 0:
            days.append(value)
    return min(days) if days else 0
