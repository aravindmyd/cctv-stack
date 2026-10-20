"""`cctv-init` — the install interview: location, timezone, cameras, cloud backup.

Installing is the one moment where the operator knows everything the system needs and has not
yet touched a file. This turns those answers into the only two files they would otherwise have
to hand-edit — `cameras.yml` and `.env` — and validates the result before it replaces anything.

Four rules shape it, all from [lld/07 §6.1]:

**Validate as you go.** A bad location or camera name is re-prompted immediately. Discovering it
at the end of a seven-step install is the cost this avoids.

**Never echo a secret.** B2 keys go through `getpass`, are never printed back, and `.env` is
written `0600`. Camera passwords are *inside* the RTSP URL, which is why the URL itself is
treated as a secret: the summary shows the camera name and host, never the credentials.

**Validate the file, not the intention.** The generated `cameras.yml` is written to a temporary
file in the same directory and loaded through the real loader before it replaces the real one.
"Valid" then means the same thing here as it does in the `config` service.

**Defaults everywhere, and honest refusal without a terminal.** Every prompt has a default, so
the common path is pressing Enter. `--non-interactive` never guesses: it validates what is
already on disk and exits `78` if that is incomplete, which is exactly what automation needs.

It runs in the image (`docker compose run --rm --no-deps init cctv-init …`), so installing still
does not require Python on the host.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import paths, timeutil
from .config import ConfigError, load_cameras_config
from .logging import redact_url
from .storage import StorageError

__all__ = [
    "EXIT_CONFIG",
    "EXIT_OK",
    "Answers",
    "CameraAnswer",
    "Interview",
    "InterviewError",
    "main",
]

EXIT_OK = 0
#: The same `EX_CONFIG` code the services use: "this configuration is incomplete" rather than
#: "this program crashed".
EXIT_CONFIG = 78

#: Retries per prompt before giving up. Three is enough for a typo and short enough that a
#: wrong question does not trap an operator in a loop.
ATTEMPTS = 3

#: The RTSP variables are named from the camera name; this is the only piece of naming the
#: interview and `.env.example` share, so it lives in one place.
_CAMERA_VAR = "CAMERA_{name}_RTSP"

_ENV_HEADER = "# Written by cctv-init. Hand edits are preserved; these keys are managed."


class InterviewError(RuntimeError):
    """The answers are missing or wrong and the operator is not there to fix them."""


# ---------------------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CameraAnswer:
    """One camera as the operator described it."""

    name: str
    main: str
    sub: str | None = None
    objects: tuple[str, ...] = ("person",)

    @property
    def main_var(self) -> str:
        return _CAMERA_VAR.format(name=self.name.upper().replace("-", "_"))

    @property
    def sub_var(self) -> str:
        return _CAMERA_VAR.format(name=self.name.upper().replace("-", "_")).replace("_RTSP", "_SUB")


@dataclass(frozen=True, slots=True)
class Answers:
    """Everything the interview collects, before it becomes two files."""

    location: str
    timezone: str
    cameras: tuple[CameraAnswer, ...]
    bucket: str | None = None
    key_id: str | None = None
    application_key: str | None = None

    @property
    def cloud_enabled(self) -> bool:
        return bool(self.bucket and self.key_id and self.application_key)

    def env_values(self) -> dict[str, str]:
        """The keys this interview writes, in a stable order."""
        values: dict[str, str] = {"TZ": self.timezone, "CCTV_SITE": self.location}
        for camera in self.cameras:
            values[camera.main_var] = camera.main
            if camera.sub:
                values[camera.sub_var] = camera.sub
        if self.cloud_enabled:
            assert self.bucket and self.key_id and self.application_key
            values["CCTV_STORAGE_BUCKET"] = self.bucket
            values["CCTV_STORAGE_KEY_ID"] = self.key_id
            values["CCTV_STORAGE_APPLICATION_KEY"] = self.application_key
        return values

    def summary(self) -> list[str]:
        """What to show back to the operator. Hosts only: never credentials."""
        lines = [
            f"  Location       {self.location}",
            f"  Timezone       {self.timezone}",
        ]
        for camera in self.cameras:
            lines.append(f"  Camera         {camera.name} → {redact(camera.main)}")
        lines.append(
            "  Cloud backup   "
            + (f"Backblaze B2, bucket {self.bucket}" if self.cloud_enabled else "disabled")
        )
        return lines


def redact(rtsp_url: str) -> str:
    """A URL safe to print: the userinfo removed, host and path kept.

    Reuses the logging redactor rather than a second regex, so a password containing `@`
    cannot slip through one and not the other.
    """
    return redact_url(rtsp_url)


# ---------------------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------------------


class Interview:
    """Prompts, validates, and writes. Every side effect is injectable for tests."""

    def __init__(
        self,
        *,
        env_path: Path,
        cameras_path: Path,
        ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] = print,
        environ: Mapping[str, str] | None = None,
        hostname: str | None = None,
    ) -> None:
        self.env_path = Path(env_path)
        self.cameras_path = Path(cameras_path)
        self._ask = ask
        self._secret = secret
        self._out = out
        self._environ = os.environ if environ is None else environ
        self._hostname = hostname if hostname is not None else socket.gethostname()

    # -- prompting ---------------------------------------------------------------------

    def _prompt(
        self,
        label: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str] | None = None,
        hint: str = "",
    ) -> str:
        """Ask until the answer validates. The prompt shows the default, never a secret."""
        for _ in range(ATTEMPTS):
            suffix = f" [{default}]" if default else ""
            raw = self._ask(f"{label}{suffix}: ").strip()
            value = raw or (default or "")
            if not value:
                self._out(f"  {label} is required.")
                continue
            if validate is None:
                return value
            try:
                return validate(value)
            except ValueError as exc:
                self._out(f"  {exc}{(' ' + hint) if hint else ''}")
        raise InterviewError(f"no valid {label.lower()} after {ATTEMPTS} attempts.")

    def _confirm(self, label: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        for _ in range(ATTEMPTS):
            raw = self._ask(f"{label} {suffix}: ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            self._out("  Answer y or n.")
        return default

    def _hidden(self, label: str) -> str:
        for _ in range(ATTEMPTS):
            value = self._secret(f"{label}: ").strip()
            if value:
                return value
            self._out("  A value is required, and it is not echoed.")
        raise InterviewError(f"no {label.lower()} given.")

    # -- the questions -----------------------------------------------------------------

    def _ask_location(self, existing: str | None) -> str:
        default = existing or _slug(self._hostname)
        self._out("\nThe location name appears in every clip path and cloud object key.")
        return self._prompt(
            "Location",
            default=default,
            validate=paths.validate_location,
            hint="Lowercase, 1-32 characters: a-z, 0-9 and interior hyphens.",
        )

    def _ask_timezone(self, existing: str | None) -> str:
        default = existing or self._detect_timezone()
        self._out("\nThe timezone decides which directory a clip is filed under. It is never")
        self._out("guessed from the container, so confirm it.")
        return self._prompt(
            "Timezone", default=default, validate=_valid_zone, hint="Example: Australia/Sydney."
        )

    def _ask_cameras(self) -> tuple[CameraAnswer, ...]:
        self._out("\nCameras. The RTSP URL is written to .env and referenced from cameras.yml as")
        self._out("${CAMERA_<NAME>_RTSP}; it is never printed back with its password.")
        cameras: list[CameraAnswer] = []
        while True:
            index = len(cameras) + 1
            self._out(f"\n  Camera {index}")
            name = self._prompt(
                "    Name",
                default="front-door" if index == 1 else None,
                validate=paths.validate_camera_name,
                hint="Lowercase kebab-case, e.g. front-door.",
            )
            if any(camera.name == name for camera in cameras):
                self._out(f"  {name} is already defined.")
                continue
            main = self._prompt("    Main RTSP URL", validate=_valid_rtsp)
            sub = ""
            if self._confirm("    Is there a sub-stream URL (cheaper detection)?", default=False):
                sub = self._prompt("    Sub RTSP URL", validate=_valid_rtsp)
            cameras.append(CameraAnswer(name=name, main=main, sub=sub or None))
            if not self._confirm("\nAdd another camera?", default=False):
                return tuple(cameras)

    def _ask_cloud(self, existing: Mapping[str, str]) -> tuple[str | None, str | None, str | None]:
        self._out("\nCloud backup is optional. Without it the uploader runs and reports DISABLED,")
        self._out("and local retention never deletes a clip, because nothing is backed up.")
        if not self._confirm("Enable Backblaze B2 backup?", default=bool(existing.get("bucket"))):
            return None, None, None

        bucket = self._prompt(
            "  Bucket", default=existing.get("bucket") or None, validate=_valid_bucket
        )
        key_id = self._prompt("  Key ID", default=existing.get("key_id") or None)
        self._out("  Application key (not echoed):")
        application_key = self._hidden("  Application key")
        return bucket, key_id, application_key

    def _detect_timezone(self) -> str:
        candidate = (self._environ.get("TZ") or "").strip()
        if candidate:
            try:
                _valid_zone(candidate)
            except ValueError:
                pass
            else:
                return candidate
        zoneinfo = Path("/etc/timezone")
        if zoneinfo.exists():
            value = zoneinfo.read_text(encoding="utf-8").strip()
            if value:
                return value
        return "UTC"

    # -- writing -----------------------------------------------------------------------

    def run(self) -> Answers:
        existing_env = read_env(self.env_path)
        existing_site = read_site(self.cameras_path)

        self._out("\n════════════════════════════════════════════════════════════════════")
        self._out(" CCTV Stack — site configuration")
        self._out("════════════════════════════════════════════════════════════════════")

        location = self._ask_location(existing_site.get("location"))
        timezone = self._ask_timezone(existing_site.get("timezone"))
        cameras = self._ask_cameras()
        bucket, key_id, application_key = self._ask_cloud(
            {
                "bucket": existing_env.get("CCTV_STORAGE_BUCKET"),
                "key_id": existing_env.get("CCTV_STORAGE_KEY_ID"),
            }
        )

        answers = Answers(
            location=location,
            timezone=timezone,
            cameras=cameras,
            bucket=bucket,
            key_id=key_id,
            application_key=application_key,
        )
        self._out("\n────────────────────────────────────────────────────────────────────")
        for line in answers.summary():
            self._out(line)
        self._out("────────────────────────────────────────────────────────────────────")
        return answers

    def apply(self, answers: Answers, *, dry_run: bool = False) -> list[str]:
        """Write both files, validating the generated config before it replaces anything.

        Returns the warnings the real loader raised, so the operator hears them now rather
        than from `doctor` later.
        """
        env_values = answers.env_values()
        cameras_text = render_cameras_yml(answers)
        warnings = self._validate(cameras_text, env_values)

        if dry_run:
            self._out(f"\nWould write {self.env_path} ({len(env_values)} keys, mode 0600)")
            self._out(f"Would write {self.cameras_path}:")
            self._out(cameras_text)
            return list(warnings)

        self._write_env(env_values)
        self._write_cameras(cameras_text)
        self._out(f"\n  Wrote {self.env_path} (mode 0600)")
        self._out(f"  Wrote {self.cameras_path}")
        for warning in warnings:
            self._out(f"  ⚠️  {warning}")
        return list(warnings)

    def _validate(self, cameras_text: str, env_values: Mapping[str, str]) -> tuple[str, ...]:
        scratch = self.cameras_path.with_name(self.cameras_path.name + ".init-tmp")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(cameras_text, encoding="utf-8")
        try:
            config = load_cameras_config(scratch, environ={**self._environ, **env_values})
        except ConfigError as exc:
            raise InterviewError(f"the configuration this produced is invalid: {exc}") from exc
        finally:
            scratch.unlink(missing_ok=True)
        return config.warnings

    def _write_env(self, values: Mapping[str, str]) -> None:
        lines: list[str] = []
        seen: set[str] = set()
        for line in (
            self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
        ):
            stripped = line.lstrip()
            is_assignment = "=" in line and not stripped.startswith("#")
            key = line.split("=", 1)[0].strip() if is_assignment else None
            if key and key in values:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(line)
        missing = [key for key in values if key not in seen]
        if missing:
            lines.append("")
            lines.append(_ENV_HEADER)
            lines.extend(f"{key}={values[key]}" for key in missing)

        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 *before* the content lands, so the window in which a key is world-readable
        # does not exist (INV-2).
        fd = os.open(self.env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip("\n") + "\n")
        os.chmod(self.env_path, 0o600)

    def _write_cameras(self, text: str) -> None:
        self.cameras_path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.cameras_path.with_name(self.cameras_path.name + ".init-tmp")
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, self.cameras_path)


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------


def render_cameras_yml(answers: Answers) -> str:
    """The generated configuration, with a header that says who wrote it and why.

    Hand-written rather than `yaml.dump`: the file is the operator's to edit afterwards, and
    a stable, commented, diff-friendly rendering is worth more than an arbitrary one.
    """
    lines = [
        "# cameras.yml — the single source of truth for cameras (ADR-0003).",
        "#",
        "# Written by cctv-init on first install. Edit it freely: re-running cctv-init only",
        "# overwrites this file with --force, and ./update.sh never touches it.",
        "#",
        "# Secrets do not belong here. ${VAR} is substituted from .env, and an unset variable",
        "# is a hard error rather than an empty string.",
        "",
        "site:",
        f"  location: {answers.location}",
        f"  timezone: {answers.timezone}",
        "",
        "min_headroom_days: 2",
        "",
        "frigate:",
        "  auth:",
        "    enabled: false",
        "",
        "defaults:",
        "  record:",
        "    mode: motion",
        "",
        "cameras:",
    ]
    for camera in answers.cameras:
        lines.append(f"  {camera.name}:")
        lines.append(f"    display_name: {_title(camera.name)}")
        lines.append("    enabled: true")
        lines.append("    rtsp:")
        lines.append(f"      main: ${{{camera.main_var}}}")
        if camera.sub:
            lines.append(f"      sub: ${{{camera.sub_var}}}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _title(name: str) -> str:
    return " ".join(word.capitalize() for word in name.split("-"))


def read_env(path: Path) -> dict[str, str]:
    """The `.env` as a mapping. Absent file is an empty mapping, not an error."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_site(path: Path) -> dict[str, str]:
    """`site.location` / `site.timezone` from an existing `cameras.yml`, if readable.

    Deliberately not a full parse: this runs before validation, when the file may be the
    unmodified example or half-edited. A YAML error here must not stop the interview.
    """
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    in_site = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^site:\s*$", line):
            in_site = True
            continue
        if in_site and line and not line.startswith((" ", "\t")):
            break
        if in_site:
            match = re.match(r"^\s+(location|timezone):\s*(\S+)\s*$", line)
            if match:
                found[match.group(1)] = match.group(2)
    return found


# ---------------------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------------------


def _valid_zone(value: str) -> str:
    try:
        timeutil.resolve_zone(value)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, KeyError
        raise ValueError(f"{value!r} is not a timezone in tzdata.") from exc
    return value


def _valid_rtsp(value: str) -> str:
    if not re.match(r"^rtsps?://", value):
        raise ValueError("The URL must start with rtsp:// (or rtsps://).")
    if any(ch in value for ch in " \t\n#"):
        raise ValueError("The URL cannot contain whitespace or '#'.")
    if len(value) < len("rtsp://x"):
        raise ValueError("That is not a complete URL.")
    return value


def _valid_bucket(value: str) -> str:
    if not re.match(r"^[a-z0-9][a-z0-9-]{4,48}[a-z0-9]$", value):
        raise ValueError(
            "A B2 bucket name is 6-50 characters: lowercase letters, digits and hyphens."
        )
    return value


def _slug(value: str) -> str:
    # The first label only: a hostname is `parents-home.lan`, and the domain is not part of
    # where clips are filed.
    slug = re.sub(r"[^a-z0-9]+", "-", value.split(".", 1)[0].lower()).strip("-")
    return slug[:32].strip("-") or "cctv-site"


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def _test_storage(answers: Answers, out: Callable[[str], None]) -> str:
    """Live credential test (lld/07 §6.1). One request, and it says which thing is wrong."""
    from .uploader import UploadSettings, build_adapter

    assert answers.bucket and answers.key_id and answers.application_key
    environ = {
        "CCTV_STORAGE_BUCKET": answers.bucket,
        "CCTV_STORAGE_KEY_ID": answers.key_id,
        "CCTV_STORAGE_APPLICATION_KEY": answers.application_key,
    }
    try:
        settings = UploadSettings.from_environ(environ)
        adapter = build_adapter(settings)
    except (ConfigError, StorageError) as exc:
        return f"WARN could not test the credentials: {exc}"
    if adapter is None:
        return "WARN could not test the credentials: no storage backend is installed"

    try:
        health = adapter.healthcheck()
    except StorageError as exc:
        return f"WARN could not test the credentials: {exc}"

    if not health.reachable:
        return f"WARN {health.detail}"
    if not health.credentials_ok:
        return f"FAIL {health.detail} Rotate them: docs/rotate-credentials.md."
    if not health.bucket_present:
        return (
            f"WARN credentials work, but bucket {answers.bucket!r} does not exist yet. "
            "Create it in the Backblaze console, then run ./scripts/doctor.sh."
        )
    out(f"  B2 reachable, credentials accepted, bucket {answers.bucket!r} present.")
    return "OK"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-init",
        description="Answer the install questions and write cameras.yml and .env.",
    )
    parser.add_argument("--env", type=Path, default=Path("/site/.env"), help="the .env to write")
    parser.add_argument(
        "--cameras", type=Path, default=Path("/config/cameras.yml"), help="the cameras.yml to write"
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing cameras.yml")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="do not prompt: validate what is on disk and exit 78 if it is incomplete",
    )
    parser.add_argument("--no-test", action="store_true", help="skip the live B2 credential test")
    parser.add_argument("--print", dest="dry_run", action="store_true", help="show, do not write")
    args = parser.parse_args(argv)

    if args.non_interactive:
        return _non_interactive(args)

    if args.cameras.exists() and not args.force and not args.dry_run:
        print(
            f"error: {args.cameras} already exists.\n"
            f"       Re-run with --force to replace it, or edit it by hand.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    interview = Interview(env_path=args.env, cameras_path=args.cameras)
    try:
        answers = interview.run()
        interview.apply(answers, dry_run=args.dry_run)
    except InterviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was written.", file=sys.stderr)
        return EXIT_CONFIG

    if answers.cloud_enabled and not args.no_test and not args.dry_run:
        outcome = _test_storage(answers, print)
        verb, _, detail = outcome.partition(" ")
        if verb == "FAIL":
            print(f"  ✗ {detail}", file=sys.stderr)
            print("    The files were written, but the credentials were rejected.", file=sys.stderr)
            return 1
        if verb == "WARN":
            print(f"  ⚠️  {detail}")
        return EXIT_OK
    return EXIT_OK


def _non_interactive(args: argparse.Namespace) -> int:
    """Validate what is already on disk; never invent an answer."""
    if not args.cameras.exists():
        print(
            f"error: {args.cameras} does not exist and --non-interactive will not create it.\n"
            f"       Copy config/cameras.example.yml and edit it, or run without "
            f"--non-interactive.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    env = read_env(args.env)
    placeholders = sorted(
        key for key, value in env.items() if "user:password@" in value or not value
    )
    if placeholders:
        print(
            f"error: {args.env} still has placeholder or empty values: "
            f"{', '.join(placeholders)}\n"
            f"       Set them, or run without --non-interactive to answer the questions.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        config = load_cameras_config(args.cameras, environ={**os.environ, **env})
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{args.cameras}: valid, {len(config.enabled_cameras())} camera(s) enabled")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
