"""Tests for `cctv-provision`.

A provisioning script is judged by what it does to a bucket somebody else also uses, so most of
these tests are about *not* clobbering: a foreign lifecycle rule survives a merge, a correct rule
is not rewritten, an unreadable versioning setting does not become "enabled". The rest are about
the honest failure — a read-only key, a delete that only hides.

Every test drives a fake that implements both surfaces, so none of it needs boto3 or a network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cctv import provision
from cctv.provision import FAIL, OK, SKIP, WARN, remove_lifecycle
from cctv.provision import provision as run
from cctv.storage import (
    AdapterHealth,
    AuthStorageError,
    BucketAdmin,
    DeleteResult,
    ObjectInfo,
    PutResult,
    StorageAdapter,
    TransientStorageError,
)


class FakeBucket:
    """A bucket that behaves like a provider, including the parts that cause trouble."""

    def __init__(self, *, present: bool = True, reachable: bool = True) -> None:
        self.present = present
        self.reachable = reachable
        self.credentials_ok = True
        self.versioning_status = "Unversioned"
        self.rules: list[dict[str, Any]] = []
        self.versioning_writes: list[bool] = []
        self.lifecycle_writes = 0
        self.objects: dict[str, bytes] = {}
        self.delete_leaves_object = False
        self.fail_on: str | None = None

    # -- BucketAdmin ----------------------------------------------------------------

    def healthcheck(self) -> AdapterHealth:
        return AdapterHealth(
            reachable=self.reachable,
            credentials_ok=self.credentials_ok,
            bucket_present=self.present,
            detail="ok" if self.reachable else "connection refused",
        )

    def ensure_bucket(self) -> str:
        self._maybe_fail("ensure_bucket")
        if self.present:
            return "existing"
        self.present = True
        return "created"

    def versioning(self) -> str:
        self._maybe_fail("versioning")
        return self.versioning_status

    def set_versioning(self, *, enabled: bool) -> None:
        self._maybe_fail("set_versioning")
        self.versioning_writes.append(enabled)
        self.versioning_status = "Enabled" if enabled else "Suspended"

    def lifecycle_rules(self) -> tuple[dict[str, Any], ...]:
        self._maybe_fail("lifecycle_rules")
        return tuple(dict(rule) for rule in self.rules)

    def put_lifecycle_rules(self, rules: Any) -> None:
        self._maybe_fail("put_lifecycle_rules")
        if not rules:
            raise AssertionError("an empty configuration must never be written")
        self.lifecycle_writes += 1
        self.rules = [dict(rule) for rule in rules]

    # -- StorageAdapter ------------------------------------------------------------

    def put_object(self, *, key: str, path: Path, metadata: Any, checksum_sha256: str) -> PutResult:
        self._maybe_fail("put_object")
        self.objects[key] = path.read_bytes()
        return PutResult(key=key, size=path.stat().st_size, verified_by="sha256")

    def head_object(self, *, key: str) -> ObjectInfo | None:
        size = len(self.objects.get(key, b""))
        return ObjectInfo(key=key, size=size) if key in self.objects else None

    def delete_object(self, *, key: str) -> DeleteResult:
        self._maybe_fail("delete_object")
        if self.delete_leaves_object:
            return DeleteResult(key=key, deleted=False, detail="hidden, not deleted")
        self.objects.pop(key, None)
        return DeleteResult(key=key, deleted=True)

    def list_objects(self, *, prefix: str, max_keys: int = 1000):  # type: ignore[no-untyped-def]
        return iter(())

    def presign_get(self, *, key: str, ttl_s: int) -> str:
        return f"https://example.invalid/{key}"

    # -- helpers -------------------------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        if self.fail_on == method:
            raise TransientStorageError(f"{method} failed on purpose")


BUCKET = "cctv-parents-home"
PREFIX = "parents-home/clips/"


def _run(admin: FakeBucket, **kwargs: Any) -> provision.Report:
    kwargs.setdefault("bucket", BUCKET)
    kwargs.setdefault("prefix", PREFIX)
    kwargs.setdefault("days", 60)
    return run(admin, admin, **kwargs)


def _states(report: provision.Report) -> dict[str, str]:
    return {step.name: step.state for step in report.steps}


# --- the sequence ----------------------------------------------------------------------


def test_a_fresh_bucket_is_created_configured_and_probed() -> None:
    admin = FakeBucket(present=False)
    report = _run(admin)

    assert report.exit_code == 0
    assert admin.present is True
    assert admin.versioning_status == "Enabled"
    assert provision.find_our_rule(admin.rules) is not None
    assert admin.objects == {}, "the probe must be cleaned up"


def test_every_step_is_reported() -> None:
    report = _run(FakeBucket())
    assert [step.number for step in report.steps] == [1, 2, 3, 4, 5, 6, 7]
    assert all(step.state in {OK, WARN, FAIL, SKIP} for step in report.steps)


def test_an_existing_bucket_is_left_alone() -> None:
    admin = FakeBucket(present=True)
    report = _run(admin)
    assert "already present" in report.steps[1].detail


# --- rules ------------------------------------------------------------------------------


def test_the_rule_is_scoped_to_the_site_prefix_and_expiry() -> None:
    admin = FakeBucket()
    _run(admin, days=30)
    rule = provision.find_our_rule(admin.rules)
    assert rule is not None
    assert rule["Filter"]["Prefix"] == PREFIX
    assert rule["Expiration"]["Days"] == 30
    assert rule["Status"] == "Enabled"


def test_a_foreign_rule_survives_the_merge() -> None:
    admin = FakeBucket()
    admin.rules = [
        {
            "ID": "someone-elses",
            "Status": "Enabled",
            "Filter": {"Prefix": "logs/"},
            "Expiration": {"Days": 7},
        }
    ]
    _run(admin)
    assert len(admin.rules) == 2
    assert any(rule["ID"] == "someone-elses" for rule in admin.rules)
    assert provision.find_our_rule(admin.rules) is not None


def test_a_foreign_rule_on_the_same_prefix_survives_too() -> None:
    """Matching on ID, not on prefix: an operator's longer expiry for one camera stays."""
    admin = FakeBucket()
    theirs = {
        "ID": "keep-front-door-longer",
        "Status": "Enabled",
        "Filter": {"Prefix": PREFIX},
        "Expiration": {"Days": 365},
    }
    admin.rules = [theirs]
    _run(admin)
    assert theirs in admin.rules


def test_a_correct_rule_is_not_rewritten() -> None:
    admin = FakeBucket()
    _run(admin)
    writes_after_first_run = admin.lifecycle_writes
    _run(admin)
    assert admin.lifecycle_writes == writes_after_first_run, "idempotent: no second write"


def test_a_rule_with_the_wrong_days_is_corrected() -> None:
    admin = FakeBucket()
    admin.rules = [provision.our_rule(prefix=PREFIX, days=5)]
    _run(admin, days=60)
    assert provision.find_our_rule(admin.rules)["Expiration"]["Days"] == 60


def test_a_rule_the_provider_does_not_keep_fails_the_read_back() -> None:
    class Forgets(FakeBucket):
        def put_lifecycle_rules(self, rules: Any) -> None:
            super().put_lifecycle_rules(rules)
            self.rules = []  # accepted the write, kept nothing

    report = _run(Forgets())
    assert _states(report)["Lifecycle rule read back"] == FAIL
    assert report.exit_code == 1


# --- versioning -------------------------------------------------------------------------


def test_unversioned_buckets_get_versioning_enabled() -> None:
    admin = FakeBucket()
    _run(admin)
    assert admin.versioning_writes == [True]


def test_versioning_already_enabled_is_not_touched() -> None:
    admin = FakeBucket()
    admin.versioning_status = "Enabled"
    _run(admin)
    assert admin.versioning_writes == []


def test_an_unreadable_versioning_setting_warns_but_continues() -> None:
    admin = FakeBucket()
    admin.fail_on = "versioning"
    report = _run(admin)
    states = _states(report)
    assert states["Versioning"] == WARN
    assert states["Lifecycle rule written"] == OK


# --- failures ---------------------------------------------------------------------------


def test_unreachable_endpoint_stops_at_step_one() -> None:
    report = _run(FakeBucket(reachable=False))
    assert [step.number for step in report.steps] == [1]
    assert report.exit_code == 1


def test_bad_credentials_name_the_fix() -> None:
    admin = FakeBucket()
    admin.credentials_ok = False
    report = _run(admin)
    assert report.steps[0].state == FAIL
    assert "rotate-credentials" in report.steps[0].detail


def test_a_failed_lifecycle_write_stops_before_the_probe() -> None:
    admin = FakeBucket()
    admin.fail_on = "put_lifecycle_rules"
    report = _run(admin)
    assert _states(report)["Lifecycle rule written"] == FAIL
    assert admin.objects == {}, "nothing should have been uploaded"


def test_a_read_only_key_is_caught_by_the_probe() -> None:
    admin = FakeBucket()
    admin.fail_on = "put_object"
    report = _run(admin)
    assert _states(report)["Probe object"] == FAIL
    assert report.exit_code == 1


def test_a_delete_that_only_hides_is_a_warning_not_a_pass() -> None:
    admin = FakeBucket()
    admin.delete_leaves_object = True
    report = _run(admin)
    probe = next(step for step in report.steps if step.name == "Probe object")
    assert probe.state == WARN
    assert "hidden" in probe.detail


# --- dry run and status -----------------------------------------------------------------


def test_dry_run_writes_nothing() -> None:
    admin = FakeBucket(present=False)
    report = _run(admin, dry_run=True)

    assert admin.present is False, "no bucket was created"
    assert admin.lifecycle_writes == 0
    assert admin.versioning_writes == []
    assert admin.objects == {}
    assert _states(report)["Probe object"] == SKIP


# --- removal ----------------------------------------------------------------------------


def test_removal_keeps_other_rules() -> None:
    admin = FakeBucket()
    admin.rules = [
        provision.our_rule(prefix=PREFIX, days=60),
        {
            "ID": "theirs",
            "Status": "Enabled",
            "Filter": {"Prefix": "x/"},
            "Expiration": {"Days": 1},
        },
    ]
    report = remove_lifecycle(admin)
    assert report.exit_code == 0
    assert [rule["ID"] for rule in admin.rules] == ["theirs"]


def test_removal_of_a_bucket_with_nothing_of_ours_is_a_no_op() -> None:
    admin = FakeBucket()
    report = remove_lifecycle(admin)
    assert report.steps[0].state == SKIP
    assert admin.lifecycle_writes == 0


def test_removal_of_our_only_rule_explains_what_to_do_instead() -> None:
    admin = FakeBucket()
    admin.rules = [provision.our_rule(prefix=PREFIX, days=60)]
    report = remove_lifecycle(admin)
    assert report.steps[0].state == WARN
    assert "B2 console" in report.steps[0].detail


# --- the report -------------------------------------------------------------------------


def test_json_and_terminal_agree() -> None:
    report = _run(FakeBucket())
    payload = report.to_dict()
    assert payload["ok"] is True
    assert [step["name"] for step in payload["steps"]] == [step.name for step in report.steps]
    assert "Cloud backup is configured" in report.render()


def test_the_failure_text_says_local_recording_is_unaffected() -> None:
    report = _run(FakeBucket(reachable=False))
    assert "Local recording" in report.render()


# --- the real adapter is both surfaces ---------------------------------------------------


def test_the_s3_adapter_satisfies_both_protocols() -> None:
    from cctv.storage import S3StorageAdapter

    adapter = S3StorageAdapter(
        bucket=BUCKET, access_key_id="k", secret_access_key="s", client=object()
    )
    assert isinstance(adapter, BucketAdmin)
    assert isinstance(adapter, StorageAdapter)


def test_an_auth_error_type_is_available_to_callers() -> None:
    """`main` catches this to exit 78 rather than 1; it must stay exported."""
    assert issubclass(AuthStorageError, Exception)
