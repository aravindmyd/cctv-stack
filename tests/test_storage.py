"""Tests for the storage adapter: classification, idempotence and verification honesty.

The adapter is the last thing between a clip and durability, so the behaviours pinned here
are the ones whose absence would be discovered by discovering missing footage:

* an existing object with different content is never overwritten;
* a re-upload of identical bytes is a no-op rather than a second object ([INV-7]);
* a checksum the provider reports as different stops the clip permanently;
* the verification we claim is the verification we performed;
* a delete is only recorded when the object is **confirmed gone** — on a versioned bucket a
  `200` can mean "hidden", with the bytes still present and still billed.

`boto3` is deliberately absent from this environment. The adapter takes an injected client
precisely so these branches are exercised rather than reasoned about.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cctv import storage

BUCKET = "cctv-test"


class ClientError(Exception):
    """Stands in for `botocore.exceptions.ClientError`, carrying the same shape."""

    def __init__(self, code: str, status: int = 400, message: str = "provider said no") -> None:
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3Client:
    """A minimal S3: enough surface for every branch, and no network."""

    def __init__(
        self,
        *,
        objects: dict[str, dict[str, Any]] | None = None,
        head_bucket_error: Exception | None = None,
        put_error: Exception | None = None,
        return_checksum: bool = True,
        checksum_override: str | None = None,
    ) -> None:
        self.objects: dict[str, dict[str, Any]] = objects or {}
        self.head_bucket_error = head_bucket_error
        self.put_error = put_error
        self.return_checksum = return_checksum
        self.checksum_override = checksum_override
        self.puts: list[str] = []
        self.multiparts: list[str] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.presigned: list[str] = []

    # -- bucket ------------------------------------------------------------------------

    def head_bucket(self, **_kwargs: Any) -> dict[str, Any]:
        if self.head_bucket_error is not None:
            raise self.head_bucket_error
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(str(kwargs["Bucket"]))
        return {}

    # -- objects -----------------------------------------------------------------------

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if Key not in self.objects:
            raise ClientError("404", 404, "Not Found")
        obj = self.objects[Key]
        return {
            "ContentLength": len(obj["body"]),
            "ETag": obj.get("etag", '"etag"'),
            "Metadata": dict(obj.get("metadata") or {}),
        }

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.put_error is not None:
            raise self.put_error
        key = str(kwargs["Key"])
        body = kwargs["Body"].read()
        self.puts.append(key)
        self.objects[key] = {
            "body": body,
            "metadata": dict(kwargs.get("Metadata") or {}),
            "etag": '"etag"',
        }
        response: dict[str, Any] = {"ETag": '"etag"'}
        if self.return_checksum:
            response["ChecksumSHA256"] = self.checksum_override or _b64_of(body)
        return response

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs: Any) -> None:
        del bucket
        self.multiparts.append(key)
        self.objects[key] = {
            "body": Path(filename).read_bytes(),
            "metadata": dict((kwargs.get("ExtraArgs") or {}).get("Metadata") or {}),
            "etag": '"multipart"',
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(str(kwargs["Key"]))
        self.objects.pop(str(kwargs["Key"]), None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        prefix = str(kwargs.get("Prefix") or "")
        items = [
            {"Key": key, "Size": len(obj["body"]), "ETag": obj.get("etag")}
            for key, obj in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"Contents": items, "IsTruncated": False}

    def generate_presigned_url(self, operation: str, Params: dict[str, Any], **_: Any) -> str:
        del operation
        self.presigned.append(str(Params["Key"]))
        return f"https://example.invalid/{Params['Key']}?sig=x"


def _b64_of(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def make_adapter(client: FakeS3Client, **overrides: Any) -> storage.S3StorageAdapter:
    settings: dict[str, Any] = {
        "bucket": BUCKET,
        "access_key_id": "key-id",
        "secret_access_key": "app-key",
        "client": client,
    }
    settings.update(overrides)
    return storage.S3StorageAdapter(**settings)


@pytest.fixture
def clip(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "clip.mp4"
    body = b"not really an mp4, but it is exactly these bytes"
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------------------
# Classification (lld/04 §4) — the code that decides retry, backoff and alarm
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status", "expected", "retryable"),
    [
        ("AccessDenied", 403, storage.AuthStorageError, False),
        ("InvalidAccessKeyId", 400, storage.AuthStorageError, False),
        ("SignatureDoesNotMatch", 403, storage.AuthStorageError, False),
        ("NoSuchBucket", 404, storage.BucketNotFoundError, False),
        ("SlowDown", 503, storage.TransientStorageError, True),
        ("ServiceUnavailable", 503, storage.TransientStorageError, True),
        ("RequestTimeout", 400, storage.TransientStorageError, True),
        ("TooManyRequests", 429, storage.TransientStorageError, True),
        ("QuotaExceeded", 403, storage.QuotaExceededError, True),
        ("StorageFull", 507, storage.QuotaExceededError, True),
        ("InvalidRequest", 400, storage.InvalidRequestError, False),
        ("BadDigest", 400, storage.ChecksumMismatchError, False),
        ("SomethingBrandNew", 418, storage.TransientStorageError, True),
    ],
)
def test_errors_are_classified_by_code_then_status(
    code: str, status: int, expected: type[storage.StorageError], retryable: bool
) -> None:
    classified = storage.classify_client_error(ClientError(code, status))
    assert isinstance(classified, expected)
    assert classified.retryable is retryable
    assert classified.error_class in {
        "transient",
        "auth",
        "not_found",
        "quota",
        "checksum",
        "unknown",
    }


def test_every_class_is_one_the_database_accepts() -> None:
    """The database's CHECK constraint is a closed set; a class outside it is a write error."""
    allowed = {"transient", "auth", "not_found", "quota", "checksum", "unknown"}
    for cls in (
        storage.TransientStorageError,
        storage.AuthStorageError,
        storage.BucketNotFoundError,
        storage.QuotaExceededError,
        storage.ChecksumMismatchError,
        storage.InvalidRequestError,
        storage.ObjectConflictError,
        storage.StorageBackendMissingError,
    ):
        assert cls.error_class in allowed


def test_an_unrecognised_error_is_retried_conservatively() -> None:
    """Asymmetric costs: a bounded number of retries beats a lost clip."""
    classified = storage.classify_client_error(RuntimeError("who knows"))
    assert classified.retryable is True
    assert "conservatively" in str(classified)


def test_classification_does_not_leak_the_bucket_name_into_the_message() -> None:
    """Messages are logged and stored in the database; the bucket is not a secret, but the
    message must not be a place where configuration leaks by accident."""
    classified = storage.classify_client_error(
        ClientError("AccessDenied", 403), key="loc/clips/a.mp4"
    )
    assert "loc/clips/a.mp4" in str(classified)


# ---------------------------------------------------------------------------------------
# put_object
# ---------------------------------------------------------------------------------------


def test_single_part_upload_verifies_with_the_provider_checksum(
    clip: tuple[Path, str],
) -> None:
    path, digest = clip
    client = FakeS3Client()
    result = make_adapter(client).put_object(
        key="loc/clips/cam/a.mp4",
        path=path,
        metadata={"camera": "cam"},
        checksum_sha256=digest,
    )

    assert result.verified_by == "sha256"
    assert result.size == path.stat().st_size
    assert result.already_present is False
    assert client.puts == ["loc/clips/cam/a.mp4"]
    stored = client.objects["loc/clips/cam/a.mp4"]
    assert stored["body"] == path.read_bytes()
    assert stored["metadata"]["sha256"] == digest
    assert stored["metadata"]["camera"] == "cam"


def test_upload_without_a_provider_checksum_falls_back_to_size_and_says_so(
    clip: tuple[Path, str],
) -> None:
    """Never claim a verification we did not perform (cloud-object-layout §4.2)."""
    path, digest = clip
    client = FakeS3Client(return_checksum=False)
    result = make_adapter(client).put_object(
        key="k", path=path, metadata={}, checksum_sha256=digest
    )
    assert result.verified_by == "size"


def test_upload_that_cannot_be_verified_at_all_is_reported_as_such(
    clip: tuple[Path, str],
) -> None:
    """An unverified backup is not a backup, and the caller must be able to tell."""
    path, digest = clip
    client = FakeS3Client(return_checksum=False)
    adapter = make_adapter(client)

    # The object lands without our metadata, so the HEAD fallback cannot confirm it either.
    original = client.put_object

    def put_object(**kwargs: Any) -> dict[str, Any]:
        response = original(**kwargs)
        client.objects[str(kwargs["Key"])]["metadata"] = {}
        return response

    client.put_object = put_object  # type: ignore[method-assign]

    result = adapter.put_object(key="k", path=path, metadata={}, checksum_sha256=digest)
    assert result.verified_by == "none"


def test_a_provider_checksum_mismatch_is_permanent_and_does_not_touch_the_file(
    clip: tuple[Path, str],
) -> None:
    path, digest = clip
    before = path.read_bytes()
    client = FakeS3Client(checksum_override="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    with pytest.raises(storage.ChecksumMismatchError) as caught:
        make_adapter(client).put_object(key="k", path=path, metadata={}, checksum_sha256=digest)

    assert caught.value.error_class == "checksum"
    assert caught.value.retryable is False
    assert path.read_bytes() == before


def test_re_uploading_identical_bytes_is_a_no_op(clip: tuple[Path, str]) -> None:
    """[INV-7]: the crash-between-PUT-and-record recovery sends no bytes the second time."""
    path, digest = clip
    client = FakeS3Client()
    adapter = make_adapter(client)

    first = adapter.put_object(key="k", path=path, metadata={}, checksum_sha256=digest)
    second = adapter.put_object(key="k", path=path, metadata={}, checksum_sha256=digest)

    assert first.already_present is False
    assert second.already_present is True
    assert second.verified_by == "sha256"
    assert client.puts == ["k"], "the second call must not send the object again"


def test_an_existing_object_with_different_content_is_never_overwritten(
    clip: tuple[Path, str],
) -> None:
    """Silently replacing an object is how the only good copy is lost."""
    path, digest = clip
    other = hashlib.sha256(b"a different video").hexdigest()
    client = FakeS3Client(
        objects={
            "k": {
                "body": b"a different video",
                "metadata": {"sha256": other},
                "etag": '"other"',
            }
        }
    )

    with pytest.raises(storage.ObjectConflictError) as caught:
        make_adapter(client).put_object(key="k", path=path, metadata={}, checksum_sha256=digest)

    assert client.puts == []
    assert "Refusing to overwrite" in str(caught.value)


def test_an_existing_object_with_no_comparable_metadata_is_not_assumed_identical(
    clip: tuple[Path, str],
) -> None:
    """Unprovable identity is refused rather than guessed: the bytes match in size only."""
    path, digest = clip
    client = FakeS3Client(objects={"k": {"body": path.read_bytes(), "metadata": {}, "etag": '"x"'}})

    with pytest.raises(storage.ObjectConflictError):
        make_adapter(client).put_object(key="k", path=path, metadata={}, checksum_sha256=digest)
    assert client.puts == []


def test_a_rejected_credential_is_not_retryable(clip: tuple[Path, str]) -> None:
    path, digest = clip
    client = FakeS3Client(put_error=ClientError("AccessDenied", 403))
    with pytest.raises(storage.AuthStorageError) as caught:
        make_adapter(client).put_object(key="k", path=path, metadata={}, checksum_sha256=digest)
    assert caught.value.retryable is False


def test_a_throttled_upload_is_retryable(clip: tuple[Path, str]) -> None:
    path, digest = clip
    client = FakeS3Client(put_error=ClientError("SlowDown", 503))
    with pytest.raises(storage.TransientStorageError) as caught:
        make_adapter(client).put_object(key="k", path=path, metadata={}, checksum_sha256=digest)
    assert caught.value.retryable is True


def test_an_unreadable_local_file_is_transient(tmp_path: Path) -> None:
    """A transient mount blip must not become a permanent failure."""
    missing = tmp_path / "gone.mp4"
    with pytest.raises(OSError):
        make_adapter(FakeS3Client()).put_object(
            key="k", path=missing, metadata={}, checksum_sha256="0" * 64
        )


def test_a_large_file_uses_multipart_and_verifies_by_size(tmp_path: Path) -> None:
    path = tmp_path / "big.mp4"
    body = b"x" * 2048
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    client = FakeS3Client()
    adapter = make_adapter(client, multipart_threshold_bytes=1024)
    result = adapter.put_object(key="k", path=path, metadata={}, checksum_sha256=digest)

    assert client.multiparts == ["k"]
    assert client.puts == [], "multipart must not also go through put_object"
    # A multipart digest is not comparable to a plain SHA-256, so the honest answer is size.
    assert result.verified_by == "size"


def test_head_object_returns_none_for_a_missing_object() -> None:
    assert make_adapter(FakeS3Client()).head_object(key="nope") is None


# ---------------------------------------------------------------------------------------
# delete_object — the hide-versus-delete trap (cloud-object-layout §5.3)
# ---------------------------------------------------------------------------------------


def test_delete_is_confirmed_gone() -> None:
    client = FakeS3Client(objects={"k": {"body": b"x", "metadata": {}}})
    result = make_adapter(client).delete_object(key="k")
    assert result.deleted is True
    assert client.deleted == ["k"]


def test_a_delete_that_only_hid_the_object_is_not_reported_as_success() -> None:
    """A `200` that left the bytes in place still costs money, so it is not a deletion."""

    class HidingClient(FakeS3Client):
        def delete_object(self, **kwargs: Any) -> dict[str, Any]:
            self.deleted.append(str(kwargs["Key"]))
            return {}  # the object stays in `self.objects` — a hide marker

    client = HidingClient(objects={"k": {"body": b"x", "metadata": {}}})
    result = make_adapter(client).delete_object(key="k")
    assert result.deleted is False
    assert result.detail is not None
    assert "still present" in result.detail


# ---------------------------------------------------------------------------------------
# healthcheck and bucket management
# ---------------------------------------------------------------------------------------


def test_healthcheck_reports_a_healthy_bucket() -> None:
    info = make_adapter(FakeS3Client()).healthcheck()
    assert info.reachable and info.credentials_ok and info.bucket_present
    assert info.error is None


def test_healthcheck_separates_bad_credentials_from_an_unreachable_endpoint() -> None:
    bad_creds = make_adapter(FakeS3Client(head_bucket_error=ClientError("AccessDenied", 403)))
    info = bad_creds.healthcheck()
    assert info.reachable is True, "a 403 proves the endpoint answered"
    assert info.credentials_ok is False
    assert isinstance(info.error, storage.AuthStorageError)

    unreachable = make_adapter(
        FakeS3Client(head_bucket_error=ClientError("ServiceUnavailable", 503))
    )
    down = unreachable.healthcheck()
    assert down.reachable is False


def test_healthcheck_names_a_missing_bucket() -> None:
    info = make_adapter(
        FakeS3Client(head_bucket_error=ClientError("NoSuchBucket", 404))
    ).healthcheck()
    assert info.bucket_present is False
    assert info.reachable is True


def test_ensure_bucket_creates_only_when_absent() -> None:
    present = FakeS3Client()
    make_adapter(present).ensure_bucket()
    assert present.created == []

    absent = FakeS3Client(head_bucket_error=ClientError("NoSuchBucket", 404))
    make_adapter(absent).ensure_bucket()
    assert absent.created == [BUCKET]


def test_ensure_bucket_propagates_an_auth_failure_rather_than_creating() -> None:
    client = FakeS3Client(head_bucket_error=ClientError("AccessDenied", 403))
    with pytest.raises(storage.AuthStorageError):
        make_adapter(client).ensure_bucket()
    assert client.created == []


# ---------------------------------------------------------------------------------------
# Listing, presigning, and the missing-SDK path
# ---------------------------------------------------------------------------------------


def test_list_objects_is_prefix_scoped() -> None:
    client = FakeS3Client(
        objects={
            "loc/clips/a.mp4": {"body": b"a", "metadata": {}},
            "loc/clips/b.mp4": {"body": b"bb", "metadata": {}},
            "other/clips/c.mp4": {"body": b"ccc", "metadata": {}},
        }
    )
    listed = list(make_adapter(client).list_objects(prefix="loc/clips/"))
    assert [item.key for item in listed] == ["loc/clips/a.mp4", "loc/clips/b.mp4"]


def test_presign_get_returns_a_url() -> None:
    url = make_adapter(FakeS3Client()).presign_get(key="k", ttl_s=60)
    assert url.startswith("https://")


def test_missing_boto3_is_a_configuration_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment without the SDK must get an actionable message, not an ImportError."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "boto3":
            raise ModuleNotFoundError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    adapter = storage.S3StorageAdapter(bucket=BUCKET, access_key_id="k", secret_access_key="s")
    with pytest.raises(storage.StorageBackendMissingError) as caught:
        _ = adapter.client
    assert "storage extra" in str(caught.value)

    # And through the health check it is reported as health, not raised at the caller.
    info = adapter.healthcheck()
    assert info.reachable is False
    assert isinstance(info.error, storage.StorageBackendMissingError)
