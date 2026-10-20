"""`cctv-config` — validate our configuration, and generate Frigate's from it.

[INV-3](../../../internal/architecture/invariants.md) says camera configuration exists in
exactly one place, `config/cameras.yml`, and that everything else is generated. This is the
"everything else".

```bash
cctv-config validate --cameras config/cameras.yml
cctv-config generate --cameras config/cameras.yml --out config/frigate/config.yml
```

`validate` runs the same projection as `generate`, so the retention-headroom rule is
enforced at validation time rather than at install time — a site that would lose footage
should find out before it is running, not after an outage.

## What is v0.1 and what is not

The generator emits a **generic/CPU host profile**: `ffmpeg` inputs from the local go2rtc
restream, a CPU detector, detection resolution. Hardware acceleration is opt-in per site
(`frigate.hwaccel: vaapi|qsv`), because only the operator knows whether the device was passed
through; it emits `ffmpeg.hwaccel_args` and nothing else. The rest of the host-profile work
(Coral detectors, OpenVINO, model selection) belongs to
[LLD 07](../../../internal/architecture/lld/07-installer-and-host-profiles.md) and is not
guessed at here. A generated config that is obviously missing a profile is better than one
that quietly picked the wrong one.

The one piece of plumbing that is not optional is `go2rtc`. Frigate's live view only uses a
go2rtc stream; without one it falls back to jsmpeg, which is fed from the `detect` role —
the sub stream. Emitting `go2rtc.streams` plus `live.streams` is what makes the UI show the
main stream, and pointing the `ffmpeg` inputs at the local restream keeps the camera's
connection count where it was.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import CamerasConfig, ConfigError, load_cameras_config, to_frigate

__all__ = ["EXIT_CONFIG", "EXIT_OK", "build_frigate_config", "main", "render"]

#: `EX_OK` / `EX_CONFIG` from `sysexits.h`, per LLD 00 §3 — the same pair the three long-running
#: services use. An invalid configuration is not an internal error: the caller should not
#: retry, and a script must be able to tell the two apart without parsing stderr.
EXIT_OK = 0
EXIT_CONFIG = 78

#: Written into the file itself, not just into this module. Two invariants are at stake and
#: neither survives as a comment in a code file that a site operator will never read:
#: INV-3 (this file is derived, so editing it is wasted work) and INV-2 (it carries
#: credentials, so it must not be committed).
_HEADER = """\
# GENERATED FILE — DO NOT EDIT.
#
# Written by `cctv-config generate` from {cameras_path}.
# Camera configuration lives in exactly one place (INV-3). This file is derived from it and
# any change made here is lost the next time it is generated.
#
# THIS FILE CONTAINS CAMERA CREDENTIALS.
# RTSP URLs carry a username and password, so this file must never enter version control
# (INV-2). It is listed in .gitignore; keep it there.
#
# Frigate's own UI login is OFF unless `frigate.auth.enabled: true` is set in cameras.yml.
# While it is off, that is safe ONLY because Frigate's internal port (5000) is never
# published outside the compose network — Frigate resolves requests arriving on it as
# anonymous admin. Publishing that port would turn this file into a remote admin bypass.
# To turn the login on: docs/frigate-ui.md
"""

#: go2rtc's RTSP restream inside Frigate's container. The camera's ffmpeg inputs read from
#: here rather than from the camera, so Frigate still opens exactly two connections per
#: camera (main + sub) — the budget `docs/install.md` documents — instead of four.
#: The same two streams also serve the Web UI.
_RESTREAM_BASE = "rtsp://127.0.0.1:8554"


def _sub_stream(camera_name: str) -> str:
    """go2rtc stream name for a camera's sub stream. Deterministic, never user-supplied."""
    return f"{camera_name}_sub"


def build_frigate_config(config: CamerasConfig) -> dict[str, Any]:
    """Our config plus the v0.1 host profile, as Frigate's config. Deterministic."""
    projected = to_frigate(config)

    # Reassembled rather than appended to, so the emitted file reads top-down in the order an
    # operator wants to check it: process-wide settings first, then the cameras. `to_frigate`
    # emits `cameras` first because that is the order *it* settles them in, which is a
    # different question.
    cameras = projected.pop("cameras", {})

    enabled = config.enabled_cameras()
    tracked = {label for camera in enabled.values() for label in camera.detect.objects}

    # One go2rtc stream per camera input. Frigate's live view can only use a go2rtc stream:
    # with none configured it falls back to jsmpeg, which is fed from the *detect* role — the
    # low-resolution sub stream. Registering the main stream here is what makes the UI show
    # the high-quality feed. It costs no extra camera connection because the ffmpeg inputs
    # below read this restream instead of the camera.
    streams: dict[str, str] = {}
    for name, camera in enabled.items():
        streams[name] = camera.rtsp_main
        if camera.rtsp_sub:
            streams[_sub_stream(name)] = camera.rtsp_sub

    out: dict[str, Any] = {
        # Frigate rewrites this itself. Emitted so the file is not read as an ancient config
        # on first start; revisit when the pinned Frigate version moves.
        "version": "0.18-0",
        "mqtt": {"enabled": True, "host": "mosquitto", "port": 1883},
        "go2rtc": {"streams": streams},
        "auth": {"enabled": config.frigate_auth.enabled},
        "detectors": {"cpu": {"type": "cpu", "num_threads": 2}},
        "objects": {"track": sorted(tracked)},
    }

    # Global, not per input: recording, detection and the live view all decode the same
    # streams, and Frigate applies `ffmpeg.hwaccel_args` to every role. Absent entirely when
    # acceleration is off, so a machine without the device keeps the CPU path it had.
    hwaccel_args = config.hwaccel_args()
    if hwaccel_args is not None:
        out["ffmpeg"] = {"hwaccel_args": hwaccel_args}
    # No `timezone` here, and that is load-bearing: Frigate 0.18 rejects the whole config if
    # a top-level `timezone` is present and starts in safe mode with no cameras. See
    # `to_frigate` for why.
    out["cameras"] = cameras

    # Anything `to_frigate` learns to emit later still reaches the file. Dropping unknown
    # keys here would make this function silently lossy the next time that one grows.
    out.update(projected)

    for name, camera in enabled.items():
        entry: dict[str, Any] = cameras.setdefault(name, {})

        # A sub-stream, when the camera has one, is what detection should use: it is already
        # the low-resolution stream the camera produces for this purpose, so decoding it is
        # cheaper than downscaling the main one. With no sub-stream, the main input takes
        # both roles — one input, because two decoders reading the same URL is pure waste.
        #
        # Both inputs read the local restream rather than the camera URL, so the camera sees
        # two connections (one per go2rtc stream) instead of four, and the same feed serves
        # recording, detection and the live UI.
        inputs: list[dict[str, Any]] = [
            {
                "path": f"{_RESTREAM_BASE}/{name}",
                "input_args": "preset-rtsp-restream",
                "roles": ["record"],
            }
        ]
        if camera.rtsp_sub:
            inputs.append(
                {
                    "path": f"{_RESTREAM_BASE}/{_sub_stream(name)}",
                    "input_args": "preset-rtsp-restream",
                    "roles": ["detect"],
                }
            )
        else:
            inputs[0]["roles"].append("detect")
        entry["ffmpeg"] = {"inputs": inputs}

        # Which go2rtc streams the UI may play. Frigate's default dashboard always plays the
        # *first* entry, so `Main` has to come first; `Sub` is a per-device choice in the Live
        # view dropdown. Without this block the UI has no go2rtc stream to play at all and
        # stays on the jsmpeg fallback above.
        live_streams: dict[str, str] = {"Main": name}
        if camera.rtsp_sub:
            live_streams["Sub"] = _sub_stream(name)
        entry["live"] = {"streams": live_streams}

        entry["detect"] = {
            "enabled": camera.detect.enabled,
            "width": camera.detect.width,
            "height": camera.detect.height,
            "fps": camera.detect.fps,
            "min_initialized": camera.detect.min_initialized,
        }
        entry["objects"] = {
            "track": list(camera.detect.objects),
            "filters": {
                label: {"min_score": camera.detect.min_score} for label in camera.detect.objects
            },
        }
        entry["snapshots"] = {
            "enabled": camera.snapshots.enabled,
            "retain": {"default": camera.snapshots.retain_days},
        }

    return out


def render(config: CamerasConfig, *, cameras_path: Path) -> str:
    """The full text of the generated config, header included."""
    body = yaml.safe_dump(build_frigate_config(config), sort_keys=False, default_flow_style=False)
    return _HEADER.format(cameras_path=cameras_path) + body


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def _load(path: Path) -> CamerasConfig | None:
    try:
        config = load_cameras_config(path)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return config


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args.cameras)
    if config is None:
        return EXIT_CONFIG

    # Projecting is the point: `to_frigate` enforces the retention-headroom rule (INV-9),
    # so validating without generating would let an unworkable site pass.
    try:
        build_frigate_config(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    names = sorted(config.enabled_cameras())
    if not names:
        print("error: no cameras are enabled", file=sys.stderr)
        return EXIT_CONFIG

    print(f"ok: {len(names)} camera(s) enabled: {', '.join(names)}")
    print(f"ok: timezone {config.site.timezone.key}")
    return EXIT_OK


def _cmd_generate(args: argparse.Namespace) -> int:
    config = _load(args.cameras)
    if config is None:
        return EXIT_CONFIG

    try:
        text = render(config, cameras_path=args.cameras)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    # Permissions are tightened because the file holds camera credentials. This is not a
    # substitute for keeping it out of version control, and it is not claimed to be:
    # it only narrows who on the host can read it.
    out.chmod(0o600)
    print(f"wrote {out}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-config",
        description="Validate cameras.yml and generate Frigate's config from it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check the configuration without writing anything")
    validate.add_argument("--cameras", type=Path, required=True, help="path to cameras.yml")
    validate.set_defaults(func=_cmd_validate)

    generate = sub.add_parser("generate", help="write Frigate's config")
    generate.add_argument("--cameras", type=Path, required=True, help="path to cameras.yml")
    generate.add_argument("--out", type=Path, required=True, help="where to write it")
    generate.set_defaults(func=_cmd_generate)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
