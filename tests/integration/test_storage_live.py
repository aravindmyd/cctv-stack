"""The storage adapter against a **real** S3 endpoint (M3.1 - M3.3, cloud-object-layout §4.2).

The unit suite drives an in-process fake, so it proves what we do with what a provider
reports but not that a provider reports it. These tests speak real HTTP to a real server
with real `boto3`: the checksum response header, the object metadata round-trip, the
conflict refusal, the delete confirmation, and the versioned-delete trap are all facts about
a running server, not about our double.

> **These results are evidence about the test double, not about Backblaze B2.** RustFS is
> S3-compatible, not B2. `docs/plans/backlog.md` §1.9 stays open until the same measurement
> is taken against the real provider; the value of running it here is that a *broken code
> path* now fails loudly before anyone spends money on egress.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from cctv import storage

pytestmark = pytest.mark.integration

CLIP_KEY = "integration/clips/front-door/2026/09/12/18-45-00.mp4"
BODY = b"pretend this is a 30 second h264 clip" * 64


def write_clip(tmp_path: Path, body: bytes = BODY) -> tuple[Path, str]:
    path = tmp_path / "clip.mp4"
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------------------
# Put, head, and what the provider actually returned
# ---------------------------------------------------------------------------------------


def test_a_put_round_trips_with_its_metadata(adapter: Any, tmp_path: Path) -> None:
    path, digest = write_clip(tmp_path)

    result = adapter.put_object(
        key=CLIP_KEY,
        path=path,
        metadata={"camera": "front-door", "location": "integration", "label": "person"},
        checksum_sha256=digest,
    )

    assert result.key == CLIP_KEY
    assert result.size == len(BODY)
    assert result.already_present is False

    info = adapter.head_object(key=CLIP_KEY)
    assert info is not None
    assert info.size == len(BODY)
    assert info.metadata["sha256"] == digest, "the self-describing metadata survives a round trip"
    assert info.metadata["camera"] == "front-door"


def test_the_provider_tells_us_which_verification_was_possible(
    adapter: Any, tmp_path: Path
) -> None:
    """cloud-object-layout §4.2: what a real endpoint returns for a single-part PUT.

    The assertion is deliberately loose — ``{"sha256", "size"}`` — because the honest
    requirement is *"the adapter reports the verification it performed"*, not *"this provider
    returns a comparable checksum"*. The measured value is printed so it can be recorded in
    the contract: it is the RustFS answer, and B2 still has to be measured separately.
    """
    path, digest = write_clip(tmp_path)

    result = adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    print(f"\n[MEASURED] RustFS single-part PUT verified_by={result.verified_by!r}")
    assert result.verified_by in {"sha256", "size"}, (
        "an upload is either checksum-verified or size+metadata verified; 'none' would mean "
        "the object is stored with no verification at all, which the uploader reports as "
        "DEGRADED"
    )


def test_a_second_put_of_identical_bytes_is_idempotent(
    adapter: Any, admin: Any, bucket: str, tmp_path: Path
) -> None:
    """[INV-7] re-running an upload never creates a second object or re-sends the bytes."""
    path, digest = write_clip(tmp_path)
    first = adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)
    second = adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    assert first.already_present is False
    assert second.already_present is True, "the bytes were already there; nothing was sent"

    listing = admin.list_objects_v2(Bucket=bucket, Prefix="integration/")
    keys = [item["Key"] for item in listing.get("Contents") or []]
    assert keys == [CLIP_KEY], "one object at the key, however many times we upload"


def test_a_put_over_different_content_is_refused(
    adapter: Any, admin: Any, bucket: str, tmp_path: Path
) -> None:
    """The adapter's most important refusal: never overwrite an object we cannot prove is ours.

    Silently replacing an object is how the only good copy is lost (ADR-0005), so the clip
    stays `PENDING` and a human resolves it.
    """
    path, digest = write_clip(tmp_path)
    adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    other = tmp_path / "other.mp4"
    other.write_bytes(b"completely different bytes")
    other_digest = hashlib.sha256(b"completely different bytes").hexdigest()

    with pytest.raises(storage.ObjectConflictError):
        adapter.put_object(key=CLIP_KEY, path=other, metadata={}, checksum_sha256=other_digest)

    stored = admin.get_object(Bucket=bucket, Key=CLIP_KEY)
    assert stored["Body"].read() == BODY, "the original object is untouched"


# ---------------------------------------------------------------------------------------
# Delete — the trap in cloud-object-layout §5.3
# ---------------------------------------------------------------------------------------


def test_delete_removes_the_object_and_says_it_is_gone(adapter: Any, tmp_path: Path) -> None:
    path, digest = write_clip(tmp_path)
    adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    result = adapter.delete_object(key=CLIP_KEY)

    assert result.deleted is True
    assert adapter.head_object(key=CLIP_KEY) is None


def test_deleting_something_absent_is_still_success(adapter: Any) -> None:
    """Idempotent by design: the desired end state — "not there" — has been reached."""
    assert adapter.delete_object(key="integration/clips/never/existed.mp4").deleted is True


def test_a_versioned_bucket_keeps_the_bytes_after_a_delete(
    adapter: Any, admin: Any, bucket: str, tmp_path: Path
) -> None:
    """cloud-object-layout §5.3 — the deletion trap, **demonstrated** rather than assumed.

    On a versioned bucket a plain ``DELETE`` writes a delete marker: the object stops being
    *visible* while the bytes remain and keep being billed. The adapter reports the only thing
    a HEAD can see (no visible object), and this test records what is underneath — which is
    exactly the evidence M4 needs before it is allowed to record `EXPIRED_CLOUD`.

    It also fixes the boundary of the adapter's honest claim: `delete_object` reports whether
    the object is *visible*, never whether the storage was released.
    """
    admin.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    path, digest = write_clip(tmp_path)
    adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    result = adapter.delete_object(key=CLIP_KEY)

    assert result.deleted is True, "the adapter reports what a HEAD can see, and that is honest"
    assert adapter.head_object(key=CLIP_KEY) is None

    versions = admin.list_object_versions(Bucket=bucket, Prefix=CLIP_KEY)
    remainder = list(versions.get("Versions") or []) + list(versions.get("DeleteMarkers") or [])
    print(f"\n[MEASURED] versioned delete left {len(remainder)} version/delete-marker rows")
    assert remainder, (
        "a versioned delete leaves a delete marker and a non-current version: the bytes are "
        "still billable. This is why M4.6 requires a deletion to be confirmed, not assumed."
    )


# ---------------------------------------------------------------------------------------
# Multipart — backlog §1.10, which had only ever been driven through a fake client
# ---------------------------------------------------------------------------------------


def test_a_multipart_upload_is_verified_by_size_and_metadata(
    bucket: str,
    endpoint: str,
    region: str,
    credentials: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Above `multipart_threshold_bytes` the adapter switches to `upload_file`, where the
    provider reports a composite digest and the only check available is a `HEAD` comparing
    size and our own `sha256` metadata.

    Lowering the threshold lets a small file take that path for real, so the wiring between
    `TransferConfig` and a live server is exercised without generating a 100 MB clip. The
    claim under test is the adapter's *honesty*: a multipart upload must never be reported as
    checksum-verified, because it was not.
    """
    key, secret = credentials
    threshold = 5 * 1024 * 1024
    adapter = storage.S3StorageAdapter(
        bucket=bucket,
        access_key_id=key,
        secret_access_key=secret,
        endpoint_url=endpoint,
        region=region,
        multipart_threshold_bytes=threshold,
    )
    body = b"x" * (threshold + 1024)
    path, digest = write_clip(tmp_path, body=body)

    result = adapter.put_object(key=CLIP_KEY, path=path, metadata={}, checksum_sha256=digest)

    print(f"\n[MEASURED] RustFS multipart PUT verified_by={result.verified_by!r}")
    assert result.verified_by == "size", "a multipart PUT cannot claim a sha256 comparison"
    assert result.size == len(body)

    info = adapter.head_object(key=CLIP_KEY)
    assert info is not None
    assert info.size == len(body)
    assert info.metadata["sha256"] == digest, "the fallback check is size **and** our metadata"


# ---------------------------------------------------------------------------------------
# Health — three different problems with three different fixes
# ---------------------------------------------------------------------------------------


def test_healthcheck_reports_a_healthy_bucket(adapter: Any) -> None:
    health = adapter.healthcheck()

    assert health.reachable is True
    assert health.credentials_ok is True
    assert health.bucket_present is True
    assert health.latency_ms is not None


def test_healthcheck_reports_a_missing_bucket_as_its_own_problem(
    endpoint: str, region: str, credentials: tuple[str, str]
) -> None:
    key, secret = credentials
    absent = storage.S3StorageAdapter(
        bucket=f"cctv-absent-{uuid.uuid4().hex[:10]}",
        access_key_id=key,
        secret_access_key=secret,
        endpoint_url=endpoint,
        region=region,
    )

    health = absent.healthcheck()

    assert health.reachable is True, "the endpoint answered; the bucket is what is missing"
    assert health.bucket_present is False


def test_healthcheck_reports_rejected_credentials_as_its_own_problem(
    bucket: str, endpoint: str, region: str
) -> None:
    rejected = storage.S3StorageAdapter(
        bucket=bucket,
        access_key_id="not-a-real-key",
        secret_access_key="not-a-real-secret",
        endpoint_url=endpoint,
        region=region,
    )

    health = rejected.healthcheck()

    assert health.credentials_ok is False
    assert health.error is not None
    assert health.error.error_class == "auth", (
        "a rejected credential must classify as auth so the uploader stops instead of "
        "retrying a 403 forever"
    )
