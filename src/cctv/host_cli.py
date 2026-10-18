"""`cctv-host` — report what this machine is, and which profile it should run.

The installer runs this inside the image, because installing must not need Python on the host
(lld/07 §3). Three outputs, each for a different consumer:

* the human summary, which is shown and confirmed by the operator (§4.2);
* `--json`, a single object, which is what a script or a bug report reads;
* `--shm-env`, one line for `.env`, because Frigate's shared-memory size is a deployment
  parameter that must be computed rather than guessed (§5.1).

Exit `78` when the host is unsupported. That is the same `EX_CONFIG` code the services use,
and it is deliberately not `1`: "this machine is not supported" is a decision, not a crash.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import host
from .config import ConfigError, load_cameras_config

__all__ = ["main"]

EXIT_OK = 0
EXIT_CONFIG = 78


def _human(detection: host.Detection, *, shm_mb: int) -> str:
    facts = detection.facts
    lines = [
        "════════════════════════════════════════════════════════════════════",
        " Detected host",
        "════════════════════════════════════════════════════════════════════",
        f"  Machine        {facts.model or 'not reported'}",
        f"  Architecture   {facts.arch} ({facts.os_name})",
    ]
    if facts.memory_bytes:
        lines.append(f"  Memory         {facts.memory_bytes / 1024**3:.1f} GiB")
    if facts.cpu_cores:
        lines.append(f"  CPU cores      {facts.cpu_cores}")
    accelerators = facts.accelerators()
    lines.append(
        "  Accelerator    "
        + (
            ", ".join(f"{a['type']} ({a.get('bus', a.get('device'))})" for a in accelerators)
            if accelerators
            else "none detected"
        )
    )
    if facts.dri_present:
        lines.append("  Video decode   graphics device present (/dev/dri)")
    lines.append(f"  shm_size       {shm_mb} MB")
    lines.append("")

    if detection.profile is None:
        lines.append(f"  ERROR  {detection.unsupported_reason}")
        return "\n".join(lines) + "\n"

    for warning in detection.warnings:
        lines.append(f"  ⚠️  {warning}")
    if detection.warnings:
        lines.append("")
    profile = detection.profile
    lines.append(f"  Recommended profile:  {profile.name}")
    lines.append(f"    · detector: {profile.detector}")
    lines.append(
        f"    · up to {profile.camera_limit} cameras at {profile.detect['width']}"
        f"x{profile.detect['height']}, {profile.detect['fps']} fps"
    )
    lines.append("")
    lines.append("  Confirm this in the installer before anything starts.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-host",
        description="Detect the host profile and the Frigate shared-memory size.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--cameras", type=Path, help="cameras.yml, used to size shm; optional")
    parser.add_argument("--write", type=Path, help="write config/host.yml here")
    parser.add_argument(
        "--shm-env",
        action="store_true",
        help="print only FRIGATE_SHM_SIZE=<n>mb for .env",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the human summary")
    args = parser.parse_args(argv)

    detection = host.detect()

    shm_mb = host.shm_size_mb(0, width=640, height=360)
    if args.cameras is not None and args.cameras.exists():
        try:
            shm_mb = host.shm_for_config(load_cameras_config(args.cameras))
        except (ConfigError, OSError) as exc:
            # Sizing is a convenience here; validation is what refuses a bad config.
            print(f"warning: could not read {args.cameras}: {exc}", file=sys.stderr)

    if args.write is not None and detection.supported:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(host.render_host_yml(detection, shm_mb=shm_mb), encoding="utf-8")

    if args.shm_env:
        print(f"FRIGATE_SHM_SIZE={shm_mb}mb")
    elif args.json:
        print(json.dumps({**detection.to_dict(), "shm_size_mb": shm_mb}, indent=2))
    elif not args.quiet:
        print(_human(detection, shm_mb=shm_mb), end="")

    if not detection.supported:
        return EXIT_CONFIG
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
