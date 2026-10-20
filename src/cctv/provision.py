"""`cctv-provision` — make the bucket usable, and prove it (lld/07 §7).

Cloud backup works or it does not, and the difference is usually one of four things: the bucket
does not exist, the key cannot write to it, versioning turns every DELETE into a hide marker, or
a lifecycle rule everyone assumed was there was never written. All four are cheap to detect at
install time and expensive to diagnose months later, which is why this exists.

Seven steps, each reported separately, so a failure names the step rather than the feature:

1. reach the endpoint with these credentials;
2. create the bucket if it is absent;
3. make sure versioning will not turn deletes into hides;
4. merge our expiry rule into whatever rules are already there — never replacing the set;
5. read the rules back and assert ours is present with the values we asked for;
6. write a 1 KB probe object, read it back, verify its size, delete it, and confirm it is gone;
7. report.

**Step 5 is the one that matters.** Writing a lifecycle rule and assuming it applied is how a
retention policy silently does not exist.

**This is not cloud retention.** Expiring objects through a provider rule is the belt; the braces
are M4's J2, which is not built. Until then this rule is what stops the bucket growing forever,
and the database will not know an object expired — reconciliation (J3) is where that is squared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, load_cameras_config
from .storage import (
    BucketAdmin,
    StorageAdapter,
    StorageBackendMissingError,
    StorageError,
)
from .uploader import UploadSettings, build_adapter

__all__ = [
    "EXIT_CONFIG",
    "EXIT_FAILED",
    "EXIT_OK",
    "PROBE_KEY",
    "PROBE_SIZE",
    "RULE_ID",
    "Report",
    "Step",
    "find_our_rule",
    "main",
    "merge_rules",
    "our_rule",
    "provision",
    "remove_lifecycle",
    "target",
]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 78

#: Our rule's id. Both B2 and S3 key rules by `ID`, which is what makes a merge possible
#: without clobbering somebody else's rule: we replace ours, we keep theirs.
RULE_ID = "cctv-stack-expire-clips"

#: Outside the clips prefix, so no expiry rule can race the test, and named so an operator
#: finding it in a listing knows what it is.
PROBE_KEY = ".cctv-probe"

#: 1 KiB: big enough that a truncated or re-encoded body is caught, small enough to cost
#: nothing on any provider.
PROBE_SIZE = 1024

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

_MARK = {OK: "✓", WARN: "⚠", FAIL: "✗", SKIP: "·"}


@dataclass(frozen=True, slots=True)
class Step:
    """One step of the sequence, with the sentence an operator needs."""

    number: int
    name: str
    state: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "step": str(self.number),
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Report:
    steps: tuple[Step, ...]
    bucket: str = ""
    prefix: str = ""
    days: int = 0

    @property
    def failed(self) -> bool:
        return any(step.state == FAIL for step in self.steps)

    @property
    def exit_code(self) -> int:
        return EXIT_FAILED if self.failed else EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "expire_after_days": self.days,
            "ok": not self.failed,
            "steps": [step.to_dict() for step in self.steps],
        }

    def render(self) -> str:
        lines = [
            "════════════════════════════════════════════════════════════════════",
            f" Backblaze B2 provisioning — bucket {self.bucket}",
            f" Clips expire after {self.days} day(s) under {self.prefix}",
            "════════════════════════════════════════════════════════════════════",
            "",
        ]
        for step in self.steps:
            lines.append(f"  [{step.number}/7] {step.name} … {_MARK[step.state]}")
            if step.detail:
                lines.append(f"           {step.detail}")
        lines.append("")
        if self.failed:
            lines.append("  Cloud backup will not work until the ✗ above is fixed.")
            lines.append("  Local recording, clips and retention are unaffected.")
        else:
            lines.append("  Cloud backup is configured. Uploads start with the uploader.")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------


def our_rule(*, prefix: str, days: int) -> dict[str, Any]:
    """The expiry rule we own, in S3's shape.

    `Expiration.Days` counts from creation. With versioning on, an expired object is *hidden*
    and its versions still bill, so `NoncurrentVersionExpiration` is included: without it a
    versioned bucket keeps every expired object forever. That is also why step 3 runs before
    this rule is written.
    """
    return {
        "ID": RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
        "Expiration": {"Days": int(days)},
        "NoncurrentVersionExpiration": {"NoncurrentDays": int(days)},
    }


def merge_rules(
    existing: Sequence[Mapping[str, Any]], *, prefix: str, days: int
) -> list[dict[str, Any]]:
    """Our rule in place of ours; everybody else's rule untouched.

    Matching on `ID`, not on prefix or position. An operator may have their own rule for the same
    prefix — a longer expiry for one camera, say — and deleting it because it looked similar is
    exactly the surprise this module exists to prevent.
    """
    mine = our_rule(prefix=prefix, days=days)
    merged: list[dict[str, Any]] = []
    replaced = False
    for rule in existing:
        if str(rule.get("ID") or "") == RULE_ID:
            merged.append(mine)
            replaced = True
        else:
            merged.append(dict(rule))
    if not replaced:
        merged.append(mine)
    return merged


def find_our_rule(rules: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for rule in rules:
        if str(rule.get("ID") or "") == RULE_ID:
            return rule
    return None


def _rule_matches(rule: Mapping[str, Any] | None, *, prefix: str, days: int) -> bool:
    """Read the rule back the way an operator would: status, prefix, days."""
    if rule is None or str(rule.get("Status") or "") != "Enabled":
        return False
    filter_block = rule.get("Filter")
    if isinstance(filter_block, Mapping) and str(filter_block.get("Prefix") or "") != prefix:
        return False
    expiration = rule.get("Expiration")
    if not isinstance(expiration, Mapping):
        return False
    return int(expiration.get("Days") or 0) == days


# ---------------------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------------------


def provision(
    admin: BucketAdmin,
    adapter: StorageAdapter,
    *,
    bucket: str,
    prefix: str,
    days: int,
    dry_run: bool = False,
) -> Report:
    """Run all seven steps. A failure is a finding, never an exception."""
    steps: list[Step] = []
    health = admin.healthcheck()

    # 1 — endpoint and credentials
    if not health.reachable:
        steps.append(Step(1, "Endpoint reachable", FAIL, health.detail))
        return Report(tuple(steps), bucket, prefix, days)
    if not health.credentials_ok:
        steps.append(
            Step(
                1,
                "Credentials accepted",
                FAIL,
                f"{health.detail} Rotate them: docs/rotate-credentials.md.",
            )
        )
        return Report(tuple(steps), bucket, prefix, days)
    steps.append(Step(1, "Credentials accepted", OK, health.detail))

    # 2 — the bucket
    try:
        if dry_run:
            state = "present" if health.bucket_present else "absent"
            steps.append(Step(2, "Bucket available", OK, f"{state} (dry run, not created)"))
        else:
            outcome = admin.ensure_bucket()
            steps.append(
                Step(
                    2,
                    "Bucket available",
                    OK,
                    "created" if outcome == "created" else "already present",
                )
            )
    except StorageError as exc:
        steps.append(Step(2, "Bucket available", FAIL, str(exc)))
        return Report(tuple(steps), bucket, prefix, days)

    # 3 — versioning, because it decides what a delete means
    try:
        status = admin.versioning()
        if status == "Enabled":
            steps.append(
                Step(
                    3,
                    "Versioning",
                    OK,
                    "enabled; the rule expires noncurrent versions too",
                )
            )
        elif dry_run:
            steps.append(Step(3, "Versioning", OK, f"would enable (currently {status})"))
        else:
            admin.set_versioning(enabled=True)
            steps.append(Step(3, "Versioning", OK, f"enabled (was {status})"))
    except StorageError as exc:
        steps.append(
            Step(
                3,
                "Versioning",
                WARN,
                f"could not read or set versioning ({exc}). Deletes may hide rather than "
                f"delete; check the bucket in the B2 console.",
            )
        )

    # 4 — merge, never replace
    try:
        existing = admin.lifecycle_rules()
        ours = find_our_rule(existing)
        others = len(existing) - (1 if ours else 0)
        if _rule_matches(ours, prefix=prefix, days=days):
            steps.append(Step(4, "Lifecycle rule written", OK, "already correct, left alone"))
        elif dry_run:
            steps.append(
                Step(
                    4,
                    "Lifecycle rule written",
                    OK,
                    f"would write {others + 1} rule(s), keeping {others} existing",
                )
            )
        else:
            admin.put_lifecycle_rules(merge_rules(existing, prefix=prefix, days=days))
            steps.append(Step(4, "Lifecycle rule written", OK, f"ours plus {others} existing kept"))
    except StorageError as exc:
        steps.append(Step(4, "Lifecycle rule written", FAIL, str(exc)))
        return Report(tuple(steps), bucket, prefix, days)

    # 5 — read it back, because writing it is not evidence
    try:
        if dry_run:
            steps.append(Step(5, "Lifecycle rule read back", SKIP, "dry run"))
        else:
            read_back = find_our_rule(admin.lifecycle_rules())
            if _rule_matches(read_back, prefix=prefix, days=days):
                steps.append(
                    Step(
                        5,
                        "Lifecycle rule read back",
                        OK,
                        f"{prefix} expires after {days}d",
                    )
                )
            else:
                steps.append(
                    Step(
                        5,
                        "Lifecycle rule read back",
                        FAIL,
                        f"the provider kept something else: {dict(read_back or {})!r}",
                    )
                )
    except StorageError as exc:
        steps.append(Step(5, "Lifecycle rule read back", FAIL, str(exc)))

    # 6 — write, read, verify, delete, confirm
    steps.append(_probe(adapter, dry_run=dry_run))

    # 7 — verdict
    failed = [step for step in steps if step.state == FAIL]
    steps.append(
        Step(
            7,
            "Report",
            FAIL if failed else OK,
            (
                "failed: " + ", ".join(step.name for step in failed)
                if failed
                else "all steps passed"
            ),
        )
    )
    return Report(tuple(steps), bucket, prefix, days)


def _probe(adapter: StorageAdapter, *, dry_run: bool) -> Step:
    """1 KB up, verified, deleted, and the deletion confirmed.

    Catches a read-only key, a region mismatch, a key scoped to another bucket, and versioning
    that turns deletes into hides — each of which otherwise surfaces days later as an uploader
    stuck in `RETRY_WAIT`.
    """
    if dry_run:
        return Step(6, "Probe object", SKIP, "dry run")

    payload = (b"cctv-stack probe object\n" * 64)[:PROBE_SIZE]
    digest = hashlib.sha256(payload).hexdigest()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.bin"
            path.write_bytes(payload)
            adapter.put_object(
                key=PROBE_KEY,
                path=path,
                metadata={"probe": "1"},
                checksum_sha256=digest,
            )
            found = adapter.head_object(key=PROBE_KEY)
            if found is None or found.size != PROBE_SIZE:
                return Step(6, "Probe object", FAIL, "the object did not read back at its size")
            deleted = adapter.delete_object(key=PROBE_KEY)
            if not deleted.deleted:
                return Step(
                    6,
                    "Probe object",
                    WARN,
                    deleted.detail or "the DELETE was accepted but the object is still there",
                )
    except StorageError as exc:
        return Step(6, "Probe object", FAIL, str(exc))
    except OSError as exc:
        return Step(6, "Probe object", FAIL, f"could not write the local probe file: {exc}")
    return Step(6, "Probe object", OK, f"{PROBE_SIZE} B written, verified, deleted")


def remove_lifecycle(admin: BucketAdmin, *, dry_run: bool = False) -> Report:
    """Take our rule out and leave every other rule exactly as it is."""
    try:
        existing = admin.lifecycle_rules()
    except StorageError as exc:
        return Report((Step(1, "Lifecycle rules read", FAIL, str(exc)),))

    if find_our_rule(existing) is None:
        return Report((Step(1, "Lifecycle rule removed", SKIP, "no rule of ours to remove"),))

    remaining = [dict(rule) for rule in existing if str(rule.get("ID") or "") != RULE_ID]
    if dry_run:
        return Report((Step(1, "Lifecycle rule removed", OK, "would remove ours (dry run)"),))
    if not remaining:
        # Some providers reject an empty configuration. Say what to do instead of raising
        # their error code at somebody following a runbook.
        return Report(
            (
                Step(
                    1,
                    "Lifecycle rule removed",
                    WARN,
                    "ours was the only rule and an empty configuration cannot be written; "
                    "remove it in the B2 console if you want the bucket unmanaged",
                ),
            )
        )
    try:
        admin.put_lifecycle_rules(remaining)
    except StorageError as exc:
        return Report((Step(1, "Lifecycle rule removed", FAIL, str(exc)),))
    return Report((Step(1, "Lifecycle rule removed", OK, f"{len(remaining)} other rule(s) kept"),))


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def target(settings: UploadSettings, cameras: Path | None = None) -> tuple[str, int]:
    """The prefix and expiry: the site's own location and cloud retention.

    60 days is the documented default (`cameras-config-schema` §5) and matches what J2 will
    enforce when it exists, so the belt and the braces agree.
    """
    location = os.environ.get("CCTV_SITE") or "site"
    days = 60
    path = cameras or Path(os.environ.get("CCTV_CAMERAS") or "/config/cameras.yml")
    if path.exists():
        try:
            config = load_cameras_config(path)
        except ConfigError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        location = config.site.location
        days = min(camera.retention.cloud_days for camera in config.enabled_cameras().values())
    return f"{location}/clips/", int(days)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-provision",
        description="Create the bucket, set versioning, write the expiry rule and prove it.",
    )
    parser.add_argument("--cameras", type=Path, help="cameras.yml, for location and cloud_days")
    parser.add_argument(
        "--days",
        type=int,
        help="expire clips after this many days (default: cloud_days)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument(
        "--status",
        action="store_true",
        help="report only: reachability, versioning, our rule",
    )
    parser.add_argument(
        "--remove-lifecycle",
        action="store_true",
        help="remove our rule, keeping the others",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    settings = UploadSettings.from_environ()
    if not settings.storage_configured:
        print(
            "error: no bucket is configured, so there is nothing to provision. Leave "
            "CCTV_STORAGE_BUCKET unset to run without cloud backup deliberately.",
            file=sys.stderr,
        )
        return EXIT_OK
    if args.days is not None and args.days < 1:
        print("error: --days must be at least 1.", file=sys.stderr)
        return EXIT_CONFIG

    try:
        adapter = build_adapter(settings)
        if adapter is None:  # pragma: no cover - guarded by storage_configured
            raise ConfigError("no storage backend")
        prefix, days = target(settings, args.cameras)
    except (ConfigError, StorageBackendMissingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.days is not None:
        days = args.days

    # The adapter is both surfaces: `StorageAdapter` for the probe, `BucketAdmin` for the rest.
    admin: Any = adapter
    if args.remove_lifecycle:
        report = remove_lifecycle(admin, dry_run=args.dry_run)
    else:
        report = provision(
            admin,
            adapter,
            bucket=settings.bucket or "",
            prefix=prefix,
            days=days,
            dry_run=args.dry_run or args.status,
        )

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render(), end="")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
