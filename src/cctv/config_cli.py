"""`cctv-config` — validate our configuration, and generate Frigate's from it.

[INV-3](../../../docs/architecture/invariants.md) says camera configuration exists in
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

The generator emits a **generic/CPU host profile**: `ffmpeg` inputs from the camera URLs, a
CPU detector, detection resolution. Hardware acceleration profiles (`hwaccel_args`, Coral
detectors, model selection) belong to
[LLD 07](../../../docs/architecture/lld/07-installer-and-host-profiles.md) and are not
guessed at here. A generated config that is obviously missing a profile is better than one
that quietly picked the wrong one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import CamerasConfig, ConfigError, load_cameras_config, to_frigate

__all__ = ["build_frigate_config", "main", "render"]

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
# `auth.enabled: false` below is safe ONLY because Frigate's internal port (5000) is never
# published outside the compose network — Frigate resolves requests arriving on it as
# anonymous admin. Publishing that port would turn this file into a remote admin bypass.
"""


def build_frigate_config(config: CamerasConfig) -> dict[str, Any]:
    """Our config plus the v0.1 host profile, as Frigate's config. Deterministic."""
    projected = to_frigate(config)

    # Reassembled rather than appended to, so the emitted file reads top-down in the order an
    # operator wants to check it: process-wide settings first, then the cameras. `to_frigate`
    # emits `cameras` first because that is the order *it* settles them in, which is a
    # different question.
    cameras = projected.pop("cameras", {})

    tracked = {
        label for camera in config.enabled_cameras().values() for label in camera.detect.objects
    }

    out: dict[str, Any] = {
        # Frigate rewrites this itself. Emitted so the file is not read as an ancient config
        # on first start; revisit when the pinned Frigate version moves.
        "version": "0.18-0",
        "mqtt": {"enabled": True, "host": "mosquitto", "port": 1883},
        "auth": {"enabled": False},
        "detectors": {"cpu": {"type": "cpu", "num_threads": 2}},
        "objects": {"track": sorted(tracked)},
    }
    # No `timezone` here, and that is load-bearing: Frigate 0.18 rejects the whole config if
    # a top-level `timezone` is present and starts in safe mode with no cameras. See
    # `to_frigate` for why.
    out["cameras"] = cameras

    # Anything `to_frigate` learns to emit later still reaches the file. Dropping unknown
    # keys here would make this function silently lossy the next time that one grows.
    out.update(projected)

    for name, camera in config.enabled_cameras().items():
        entry: dict[str, Any] = cameras.setdefault(name, {})

        # A sub-stream, when the camera has one, is what detection should use: it is already
        # the low-resolution stream the camera produces for this purpose, so decoding it is
        # cheaper than downscaling the main one. With no sub-stream, the main input takes
        # both roles — one input, because two decoders reading the same URL is pure waste.
        inputs: list[dict[str, Any]] = [{"path": camera.rtsp_main, "roles": ["record"]}]
        if camera.rtsp_sub:
            inputs.append({"path": camera.rtsp_sub, "roles": ["detect"]})
        else:
            inputs[0]["roles"].append("detect")
        entry["ffmpeg"] = {"inputs": inputs}

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
        return 1

    # Projecting is the point: `to_frigate` enforces the retention-headroom rule (INV-9),
    # so validating without generating would let an unworkable site pass.
    try:
        build_frigate_config(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    names = sorted(config.enabled_cameras())
    if not names:
        print("error: no cameras are enabled", file=sys.stderr)
        return 1

    print(f"ok: {len(names)} camera(s) enabled: {', '.join(names)}")
    print(f"ok: timezone {config.site.timezone.key}")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    config = _load(args.cameras)
    if config is None:
        return 1

    try:
        text = render(config, cameras_path=args.cameras)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    # Permissions are tightened because the file holds camera credentials. This is not a
    # substitute for keeping it out of version control, and it is not claimed to be:
    # it only narrows who on the host can read it.
    out.chmod(0o600)
    print(f"wrote {out}")
    return 0


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
