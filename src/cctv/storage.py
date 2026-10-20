"""The storage adapter — the only module that knows what a "cloud" is.

[ADR-0005](../../../internal/adr/0005-b2-behind-storage-adapter.md) puts the provider behind a
deliberately tiny interface. This module is that interface, plus the one implementation we
ship (S3-compatible, which is how Backblaze B2 is reached and how RustFS stands in for it in
CI — [ADR-0019](../../../internal/adr/0019-rustfs-as-s3-test-double.md)).

Three decisions here are worth stating, because each of them is a place where a plausible
simplification causes data loss:

1. **An existing object with different content is never overwritten.** Silently replacing an
   object is how the only good copy is lost. The adapter raises, the clip stays `PENDING`,
   and a human decides ([INV-7](../../../internal/architecture/invariants.md)).
2. **A verification we did not perform is never claimed.** `PutResult.verified_by` says
   exactly what was checked — `sha256`, `size`, or `none` — and `none` makes the uploader
   degrade rather than declare an unverified backup a backup (lld/04 §3 step 5).
3. **`delete_object` reports whether the object is *gone*, not whether a request
   succeeded.** B2 keeps versions, so a plain delete can leave a hidden object still
   billing for storage. Claiming success would be a lie that costs money.

`boto3` is imported lazily, for the same reason `paho` is in [`cctv.mqtt`][cctv.mqtt]: a
deployment with no cloud backup should not have to install an SDK it will never call, and a
missing library must produce an actionable message rather than an `ImportError` at start-up.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Protocol, runtime_checkable

__all__ = [
    "CONTENT_TYPE_MP4",
    "DEFAULT_MULTIPART_THRESHOLD_BYTES",
    "AdapterHealth",
    "AuthStorageError",
    "BucketAdmin",
    "BucketNotFoundError",
    "ChecksumMismatchError",
    "DeleteResult",
    "InvalidRequestError",
    "ObjectConflictError",
    "ObjectInfo",
    "ObjectReader",
    "PutResult",
    "QuotaExceededError",
    "S3StorageAdapter",
    "StorageAdapter",
    "StorageBackendMissingError",
    "StorageError",
    "TransientStorageError",
    "classify_client_error",
]

CONTENT_TYPE_MP4 = "video/mp4"

#: At or above this size the adapter switches to a multipart upload. Below it a single PUT
#: is used, which is the only shape that gives us a provider checksum comparable to a plain
#: SHA-256 of the file (cloud-object-layout §4.2).
DEFAULT_MULTIPART_THRESHOLD_BYTES = 100 * 1024 * 1024


# ---------------------------------------------------------------------------------------
# Errors — classification drives retry, backoff and alerting (lld/04 §4)
# ---------------------------------------------------------------------------------------


class StorageError(RuntimeError):
    """A storage operation failed, classified.

    `error_class` is one of the six the database's `last_error_class` CHECK accepts
    (contract §2.3), and `retryable` is the decision that actually matters to the caller.
    Keeping both on the exception means the uploader never has to re-derive "is a 403 worth
    retrying?" from a message string.
    """

    error_class: ClassVar[str] = "unknown"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class TransientStorageError(StorageError):
    """A `5xx`, a reset connection, a DNS failure or a timeout. Retried indefinitely."""

    error_class: ClassVar[str] = "transient"
    retryable: ClassVar[bool] = True


class AuthStorageError(StorageError):
    """Credentials were rejected. Never retried: retrying cannot succeed.

    Retrying a `403` produces thousands of failed requests that bury the real signal and
    make a broken system look busy. The fix is a credential rotation, and the runbook is
    [`rotate-credentials.md`](../../../docs/rotate-credentials.md).
    """

    error_class: ClassVar[str] = "auth"


class BucketNotFoundError(StorageError):
    """The bucket does not exist. Never retried; the message names the expected bucket."""

    error_class: ClassVar[str] = "not_found"


class QuotaExceededError(StorageError):
    """A provider cap, a payment problem, or a full account. Retried with a longer ceiling.

    Retryable because caps are sometimes transient (a daily counter that resets), but at a
    much longer interval — this one needs a person either way.
    """

    error_class: ClassVar[str] = "quota"
    retryable: ClassVar[bool] = True


class ChecksumMismatchError(StorageError):
    """The provider's checksum for the stored object does not match the local file.

    Never retried. Re-uploading identical bytes that produce a different stored checksum is
    evidence of corruption, not of a transient fault, and retrying it just burns time.
    """

    error_class: ClassVar[str] = "checksum"


class InvalidRequestError(StorageError):
    """A `400`. Our bug or a configuration error, so retrying is pointless.

    Carries the database's `unknown` class rather than a class of its own because the
    `last_error_class` CHECK accepts six values and adding a seventh would mean a table
    rebuild for a label. The distinction the operator needs — *no retry* — is carried by
    `retryable`, and the message says what the provider objected to.
    """

    error_class: ClassVar[str] = "unknown"


class ObjectConflictError(StorageError):
    """An object already exists at this key with content we cannot prove identical.

    The most dangerous possible state: overwriting could destroy the only good copy, and
    assuming identity could mark a corrupt object as a verified backup. So neither happens —
    the clip stays `PENDING`, the health check degrades, and a human resolves it.
    """

    error_class: ClassVar[str] = "unknown"


class StorageBackendMissingError(StorageError):
    """`boto3` is not installed. A configuration error, not a runtime one."""

    error_class: ClassVar[str] = "unknown"


# ---------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PutResult:
    """What actually happened during an upload.

    `verified_by` is the honest record of the verification performed, and it is deliberately
    a string rather than a bool: `"sha256"` and `"size"` are not interchangeable, and the
    uploader reports a `"none"` as `DEGRADED` because an unverified backup is not a backup.
    """

    key: str
    size: int
    verified_by: str  # "sha256" | "size" | "none"
    etag: str | None = None
    #: True when the object already existed with identical content, so no bytes were sent.
    #: This is what makes a re-run after a crash a no-op rather than a second upload.
    already_present: bool = False


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """An object as the provider describes it.

    `last_modified` is the provider's own modification time as a UTC epoch, and it exists for
    one caller: reconstruction ([`recover`][cctv.recover]). Cloud retention decides expiry from
    `uploaded_at` (`lld/05` §4.3), so a rebuild that guessed `uploaded_at = now` would restart
    every object's cloud life and silently extend it by `retention.cloud_days`. The provider
    knows when the bytes arrived; a rebuild should ask rather than invent.

    `None` where the provider or the call shape does not supply it — a listing that omits it is
    still a usable listing.
    """

    key: str
    size: int
    etag: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    last_modified: float | None = None


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Whether the object is *actually gone* — not whether a request was accepted.

    `deleted=False` on a `200` response is a real and expected outcome on B2 when the bucket
    keeps versions: the delete wrote a hide marker and the bytes are still there, still
    billed. Callers must treat `deleted=False` as a failure to delete.
    """

    key: str
    deleted: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """`healthcheck()`'s answer, split so an operator gets the right fix.

    Unreachable, credentials rejected and bucket missing have three different remedies, and
    collapsing them into one boolean is how "storage is down" sends someone to the wrong
    runbook.
    """

    reachable: bool
    credentials_ok: bool
    bucket_present: bool
    detail: str
    latency_ms: int | None = None
    error: StorageError | None = None


# ---------------------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------------------


@runtime_checkable
class StorageAdapter(Protocol):
    """The provider-specific surface (cloud-object-layout §6).

    Anything larger than this would leak the provider into the uploader's logic and defeat
    [ADR-0005](../../../internal/adr/0005-b2-behind-storage-adapter.md).
    """

    def put_object(
        self,
        *,
        key: str,
        path: Path,
        metadata: Mapping[str, str],
        checksum_sha256: str,
    ) -> PutResult: ...

    def head_object(self, *, key: str) -> ObjectInfo | None: ...

    def delete_object(self, *, key: str) -> DeleteResult: ...

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Iterator[ObjectInfo]: ...

    def presign_get(self, *, key: str, ttl_s: int) -> str: ...

    def healthcheck(self) -> AdapterHealth: ...


@runtime_checkable
class ObjectReader(Protocol):
    """Read-only object access, for recovery alone (`lld/08` §6.2, §6.3).

    Separate from `StorageAdapter` for the same reason `BucketAdmin` is: a test double for the
    uploader should not have to implement downloading, and the uploader must not be able to pull
    an object back over the local file it is about to send. Recovery is the only caller, and it
    wants the other direction of the arrows.
    """

    def head_object(self, *, key: str) -> ObjectInfo | None: ...

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Iterator[ObjectInfo]: ...

    def download_object(self, *, key: str, destination: BinaryIO) -> int:
        """Stream the object's bytes into `destination`. Returns the number written.

        Takes a handle rather than a path so that the caller owns the atomically: recovery
        writes into a scratch file and renames it into place ([`atomic`][cctv.atomic]), and an
        adapter that chose its own destination would make that impossible.
        """
        ...


@runtime_checkable
class BucketAdmin(Protocol):
    """Bucket-level configuration: existence, versioning, lifecycle rules.

    Separate from `StorageAdapter` on purpose. The uploader must not be able to rewrite a
    lifecycle rule, and a test double for the uploader should not have to implement bucket
    administration. Provisioning (lld/07 §7) is the only caller.

    Rules are passed and returned as plain dicts in S3's own shape, because that is the
    interchange format both B2 and the lifecycle console speak; wrapping it in our own model
    would mean translating twice and losing the fields we did not think of.
    """

    def ensure_bucket(self) -> str:
        """Create the bucket if it is absent. Returns `"created"` or `"existing"`."""
        ...

    def versioning(self) -> str:
        """`"Enabled"`, `"Suspended"` or `"Unversioned"`."""
        ...

    def set_versioning(self, *, enabled: bool) -> None: ...

    def lifecycle_rules(self) -> tuple[Mapping[str, Any], ...]: ...

    def put_lifecycle_rules(self, rules: Sequence[Mapping[str, Any]]) -> None: ...

    def healthcheck(self) -> AdapterHealth: ...


# ---------------------------------------------------------------------------------------
# Classification — the code that decides retry, backoff and alarm
# ---------------------------------------------------------------------------------------

#: Provider error codes, grouped by what the operator should do about them. Matched against
#: `ClientError.response["Error"]["Code"]` and, as a fallback, the HTTP status. Both,
#: because S3-compatible providers are inconsistent about which they populate.
_AUTH_CODES = frozenset(
    {
        "AccessDenied",
        "AccountProblem",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidToken",
        "SignatureDoesNotMatch",
        "UnauthorizedAccess",
    }
)
_NOT_FOUND_CODES = frozenset({"NoSuchBucket", "NotFound"})
_QUOTA_CODES = frozenset(
    {"InsufficientStorage", "QuotaExceeded", "ServiceQuotaExceeded", "StorageFull"}
)
_THROTTLE_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "TooManyRequests",
    }
)
_SERVER_CODES = frozenset({"InternalError", "RequestTimeout", "ServiceUnavailable", "SlowDown"})
_INVALID_CODES = frozenset(
    {
        "EntityTooLarge",
        "InvalidArgument",
        "InvalidBucketName",
        "InvalidRequest",
        "KeyTooLong",
    }
)
_CHECKSUM_CODES = frozenset({"BadDigest", "InvalidDigest"})

_AUTH_STATUSES = frozenset({401, 403})
_NOT_FOUND_STATUSES = frozenset({404})
_QUOTA_STATUSES = frozenset({507})
_THROTTLE_STATUSES = frozenset({429})
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
_INVALID_STATUSES = frozenset({400})


def _epoch(value: Any) -> float | None:
    """A provider timestamp as a UTC epoch, or `None` when it is absent or unreadable.

    Providers disagree about the type: boto3 hands back a `datetime`, a fake client in a test
    hands back whatever the test wrote, and some S3-compatible servers return a string. All
    three are accepted, and anything else becomes `None` rather than an exception — a listing
    without modification times is still a listing, and reconstruction can fall back to the
    other side of the comparison.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    timestamp = getattr(value, "timestamp", None)
    if timestamp is None:
        return None
    try:
        return float(timestamp())
    except (TypeError, ValueError, OSError):
        return None


def classify_client_error(exc: BaseException, *, key: str | None = None) -> StorageError:
    """Turn a provider exception into one of our classified errors.

    The classification is the whole point: it decides whether we retry (and how long) or
    stop and shout. `unknown` defaults to **retryable-transient**, because the costs are
    asymmetric — retrying something permanent costs a bounded number of attempts and some
    noise, while not retrying something transient costs a clip.
    """
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or exc)
    where = f" for {key!r}" if key else ""

    if code in _CHECKSUM_CODES:
        return ChecksumMismatchError(
            f"The provider rejected our checksum{where}: {message}. "
            f"The local file is preserved and the clip is not retried.",
            http_status=status,
        )
    if code in _AUTH_CODES:
        return AuthStorageError(
            f"Storage rejected the credentials{where}: {message}. "
            f"Rotate them (docs/rotate-credentials.md); retrying will not help.",
            http_status=status,
        )
    if code in _NOT_FOUND_CODES:
        return BucketNotFoundError(
            f"Storage could not find the bucket or object{where}: {message}.",
            http_status=status,
        )
    if code in _QUOTA_CODES:
        return QuotaExceededError(
            f"Storage is over quota or out of space{where}: {message}. This needs a person.",
            http_status=status,
        )
    if code in _THROTTLE_CODES or code in _SERVER_CODES:
        return TransientStorageError(
            f"Storage is temporarily unavailable{where}: {message}.",
            http_status=status,
        )
    if code in _INVALID_CODES:
        return InvalidRequestError(
            f"Storage rejected the request{where}: {message}. "
            f"This is our bug or a configuration error, so it is not retried.",
            http_status=status,
        )

    # No code we recognise, so fall back to the status — which is at least unambiguous.
    if status in _AUTH_STATUSES:
        return AuthStorageError(
            f"Storage rejected the credentials{where}: {message}. "
            f"Rotate them (docs/rotate-credentials.md); retrying will not help.",
            http_status=status,
        )
    if status in _NOT_FOUND_STATUSES:
        return BucketNotFoundError(
            f"Storage could not find the bucket or object{where}: {message}.",
            http_status=status,
        )
    if status in _QUOTA_STATUSES:
        return QuotaExceededError(
            f"Storage is over quota or out of space{where}: {message}. This needs a person.",
            http_status=status,
        )
    if status in _THROTTLE_STATUSES or status in _TRANSIENT_STATUSES:
        return TransientStorageError(
            f"Storage is temporarily unavailable{where}: {message}.",
            http_status=status,
        )
    if status in _INVALID_STATUSES:
        return InvalidRequestError(
            f"Storage rejected the request{where}: {message}. "
            f"This is our bug or a configuration error, so it is not retried.",
            http_status=status,
        )
    return TransientStorageError(
        f"Unexpected storage failure{where}: {type(exc).__name__}: {message}. "
        f"Treated as transient, conservatively — a bounded number of retries is cheaper "
        f"than a lost clip.",
        http_status=status,
    )


# ---------------------------------------------------------------------------------------
# The S3-compatible implementation
# ---------------------------------------------------------------------------------------


class S3StorageAdapter:
    """Backblaze B2 (and MinIO, and anything else S3-compatible) behind `StorageAdapter`.

    The client is constructible from an injected object so tests can drive every branch
    without a `boto3` install and without a network — the classification, the conflict rules
    and the checksum comparison are exactly the code that must not be tested only by
    inspection.
    """

    def __init__(
        self,
        *,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str | None = None,
        region: str = "us-west-004",
        multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("A bucket name is required.")
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._multipart_threshold = multipart_threshold_bytes
        self._client = client

    # -- client ------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The `boto3` client, created on first use.

        `boto3` is imported here rather than at module import so that a deployment without
        cloud backup never needs the SDK installed.
        """
        if self._client is None:
            self._client = self._new_client()
        return self._client

    def _new_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
            raise StorageBackendMissingError(
                "boto3 is not installed, so no cloud backup can happen. Install the "
                "storage extra (`uv sync --extra storage`), or unset CCTV_STORAGE_BUCKET "
                "to run without cloud backup deliberately."
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            config=Config(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    # -- operations --------------------------------------------------------------------

    def ensure_bucket(self) -> str:
        """Create the bucket if it is absent, and say which of the two happened."""
        try:
            self.client.head_bucket(Bucket=self._bucket)
            return "existing"
        except StorageError:
            raise
        except Exception as exc:
            classified = classify_client_error(exc)
            if not isinstance(classified, BucketNotFoundError):
                raise classified from exc
        try:
            self.client.create_bucket(Bucket=self._bucket)
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc) from exc
        return "created"

    # -- bucket administration (lld/07 §7) ---------------------------------------------

    def versioning(self) -> str:
        """`Enabled`, `Suspended` or `Unversioned`. Unreadable is unversioned, never "on"."""
        try:
            response = self.client.get_bucket_versioning(Bucket=self._bucket)
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc) from exc
        return str(response.get("Status") or "Unversioned")

    def set_versioning(self, *, enabled: bool) -> None:
        try:
            self.client.put_bucket_versioning(
                Bucket=self._bucket,
                VersioningConfiguration={"Status": "Enabled" if enabled else "Suspended"},
            )
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc) from exc

    def lifecycle_rules(self) -> tuple[Mapping[str, Any], ...]:
        """Existing rules, or none. `NoSuchLifecycleConfiguration` is an answer, not an error."""
        try:
            response = self.client.get_bucket_lifecycle_configuration(Bucket=self._bucket)
        except StorageError:
            raise
        except Exception as exc:
            # A bucket with no rules answers with an error code, and that is the normal state
            # of a fresh bucket. Read the code from the provider's response rather than from
            # the message, which is the provider's to reword.
            provider = getattr(exc, "response", None)
            code = ""
            if isinstance(provider, Mapping):
                error = provider.get("Error")
                if isinstance(error, Mapping):
                    code = str(error.get("Code") or "")
            if code.startswith("NoSuchLifecycle"):
                return ()
            raise classify_client_error(exc) from exc
        return tuple(dict(rule) for rule in (response.get("Rules") or ()))

    def put_lifecycle_rules(self, rules: Sequence[Mapping[str, Any]]) -> None:
        self._require_rules(rules)
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self._bucket,
                LifecycleConfiguration={"Rules": [dict(rule) for rule in rules]},
            )
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc) from exc

    @staticmethod
    def _require_rules(rules: Sequence[Mapping[str, Any]]) -> None:
        """Refuse an empty configuration with a sentence, not a provider error code."""
        if not rules:
            raise InvalidRequestError(
                "Refusing to write an empty lifecycle configuration: some providers reject it "
                "outright, and one that accepted it would remove every rule this call did "
                "not name."
            )

    def put_object(
        self,
        *,
        key: str,
        path: Path,
        metadata: Mapping[str, str],
        checksum_sha256: str,
    ) -> PutResult:
        """Upload `path` to `key`, idempotently, verifying what we can and saying what.

        Order matters and is deliberate:

        1. `HEAD` first. An object already there with a matching checksum means the bytes
           need not be sent at all — this is the crash-between-PUT-and-record recovery
           ([INV-7](../../../internal/architecture/invariants.md)).
        2. If it is there with *unprovable* content, refuse. Never overwrite.
        3. Upload, then compare the provider's checksum when it gives us a comparable one.
        4. When it does not (multipart), fall back to size plus our own recorded metadata
           and record `verified_by="size"` — never `"sha256"`.
        """
        path = Path(path)
        size = path.stat().st_size
        digest_b64 = _b64(checksum_sha256)
        stored_metadata = {str(k): str(v) for k, v in metadata.items()} | {
            "sha256": checksum_sha256
        }

        existing = self.head_object(key=key)
        if existing is not None:
            recorded = existing.metadata.get("sha256")
            if recorded == checksum_sha256 and existing.size == size:
                return PutResult(
                    key=key,
                    size=existing.size,
                    verified_by="sha256",
                    etag=existing.etag,
                    already_present=True,
                )
            raise ObjectConflictError(
                f"An object already exists at {key!r} with different content "
                f"(local {size} bytes / sha256 {checksum_sha256[:12]}…, "
                f"remote {existing.size} bytes / sha256 "
                f"{(recorded or 'unrecorded')[:12]}…). Refusing to overwrite it: silently "
                f"replacing an object is how the only good copy is lost. Inspect the object "
                f"in the bucket and resolve it by hand."
            )

        try:
            if size >= self._multipart_threshold:
                self._multipart_upload(key=key, path=path, metadata=stored_metadata)
                return self._verify_by_head(key=key, size=size, checksum_sha256=checksum_sha256)

            with open(path, "rb") as handle:
                response = self.client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=handle,
                    ContentLength=size,
                    ContentType=CONTENT_TYPE_MP4,
                    Metadata=stored_metadata,
                    ChecksumSHA256=digest_b64,
                )
        except StorageError:
            raise
        except OSError as exc:
            raise TransientStorageError(f"Could not read {path} to upload it: {exc}") from exc
        except Exception as exc:
            raise classify_client_error(exc, key=key) from exc

        returned = response.get("ChecksumSHA256")
        if returned:
            if returned != digest_b64:
                raise ChecksumMismatchError(
                    f"The provider stored different bytes for {key!r}: it reports checksum "
                    f"{returned!r}, we sent {digest_b64!r}. The local file is preserved and "
                    f"the clip will not be retried blindly."
                )
            return PutResult(
                key=key,
                size=size,
                verified_by="sha256",
                etag=response.get("ETag"),
            )

        # No comparable checksum came back. Do not claim one: fall back to a HEAD and
        # compare size and our own recorded metadata.
        return self._verify_by_head(key=key, size=size, checksum_sha256=checksum_sha256)

    def head_object(self, *, key: str) -> ObjectInfo | None:
        try:
            response = self.client.head_object(Bucket=self._bucket, Key=key)
        except StorageError:
            raise
        except Exception as exc:
            classified = classify_client_error(exc, key=key)
            if isinstance(classified, BucketNotFoundError):
                # A missing *object* is not an error, it is the answer to the question.
                return None
            raise classified from exc

        return ObjectInfo(
            key=key,
            size=int(response.get("ContentLength") or 0),
            etag=response.get("ETag"),
            metadata=dict(response.get("Metadata") or {}),
            last_modified=_epoch(response.get("LastModified")),
        )

    def delete_object(self, *, key: str) -> DeleteResult:
        """Delete, then **confirm** it is gone.

        The confirmation is not belt-and-braces: on a versioned bucket a successful DELETE
        response can mean "hidden", with the bytes still present and still billed
        (cloud-object-layout §5.3).
        """
        try:
            self.client.delete_object(Bucket=self._bucket, Key=key)
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc, key=key) from exc

        remaining = self.head_object(key=key)
        if remaining is None:
            return DeleteResult(key=key, deleted=True)
        return DeleteResult(
            key=key,
            deleted=False,
            detail=(
                f"The DELETE was accepted but the object is still present ({remaining.size} "
                f"bytes). On a versioned bucket this is a hide marker: the bytes still count "
                f"toward storage. Configure the bucket to keep only the current version, or "
                f"rely on the lifecycle rule's daysFromHidingToDeleting."
            ),
        )

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Iterator[ObjectInfo]:
        """List objects. Reconciliation and restore only — never to decide what to delete.

        `INV-10 <../../../../internal/architecture/invariants.md>` forbids acting on a listing.
        """
        token: str | None = None
        while True:
            parameters: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": min(max_keys, 1000),
            }
            if token:
                parameters["ContinuationToken"] = token
            try:
                response = self.client.list_objects_v2(**parameters)
            except StorageError:
                raise
            except Exception as exc:
                raise classify_client_error(exc) from exc

            for item in response.get("Contents") or []:
                yield ObjectInfo(
                    key=str(item["Key"]),
                    size=int(item.get("Size") or 0),
                    etag=item.get("ETag"),
                    last_modified=_epoch(item.get("LastModified")),
                )
            if not response.get("IsTruncated"):
                return
            token = response.get("NextContinuationToken")
            if not token:
                return

    def download_object(self, *, key: str, destination: BinaryIO) -> int:
        """Stream an object's bytes into an open handle, and return how many arrived.

        Streamed rather than read whole so that a 66 GB recovery is a sequence of small
        buffers instead of a memory figure. The caller owns durability: it passes a handle
        inside an atomic writer, so a download that dies halfway leaves no file at the
        canonical path — which matters because the alternative is a truncated clip that looks
        like a complete one and uploads back over itself.
        """
        try:
            response = self.client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc, key=key) from exc

        written = 0
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                written += len(chunk)
        except StorageError:
            raise
        except Exception as exc:
            raise TransientStorageError(
                f"Downloading {key!r} failed after {written} bytes: {exc}",
                http_status=None,
            ) from exc
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()

        expected = int(response.get("ContentLength") or -1)
        if expected >= 0 and written != expected:
            raise TransientStorageError(
                f"{key!r} is {expected} bytes according to the provider but {written} arrived. "
                f"The download was truncated; nothing was written to the canonical path."
            )
        return written

    def presign_get(self, *, key: str, ttl_s: int) -> str:
        """A time-limited GET URL. Unused in v1; present so a clip browser needs no surgery."""
        try:
            return str(
                self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=int(ttl_s),
                )
            )
        except StorageError:
            raise
        except Exception as exc:
            raise classify_client_error(exc, key=key) from exc

    def healthcheck(self) -> AdapterHealth:
        """Is the endpoint reachable, are the credentials good, does the bucket exist?"""
        started = time.perf_counter()
        try:
            self.client.head_bucket(Bucket=self._bucket)
        except StorageError as exc:
            return self._unhealthy(exc, started)
        except Exception as exc:
            return self._unhealthy(classify_client_error(exc), started)
        return AdapterHealth(
            reachable=True,
            credentials_ok=True,
            bucket_present=True,
            detail=f"bucket {self._bucket!r} reachable",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _unhealthy(self, exc: StorageError, started: float) -> AdapterHealth:
        # A missing SDK was never able to reach anything, so all three answers are "no"
        # rather than the optimistic defaults a failure to *contact* would imply.
        missing_backend = isinstance(exc, StorageBackendMissingError)
        return AdapterHealth(
            # An auth failure proves the endpoint is reachable — we are just not allowed in.
            reachable=not isinstance(exc, TransientStorageError) and not missing_backend,
            credentials_ok=not isinstance(exc, AuthStorageError) and not missing_backend,
            bucket_present=not isinstance(exc, BucketNotFoundError) and not missing_backend,
            detail=str(exc),
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=exc,
        )

    # -- internals ---------------------------------------------------------------------

    def _multipart_upload(self, *, key: str, path: Path, metadata: Mapping[str, str]) -> None:
        config = self._transfer_config()
        extra: dict[str, Any] = {} if config is None else {"Config": config}
        self.client.upload_file(
            str(path),
            self._bucket,
            key,
            ExtraArgs={"ContentType": CONTENT_TYPE_MP4, "Metadata": dict(metadata)},
            **extra,
        )

    def _transfer_config(self) -> Any | None:
        """Tune the multipart thresholds, or fall back to boto3's own defaults.

        Imported here rather than at module scope so that the adapter is usable — and
        testable — without `boto3` installed at all; the multipart path is driven through
        `upload_file` by a fake client in the tests.
        """
        try:
            from boto3.s3.transfer import TransferConfig
        except ModuleNotFoundError:  # pragma: no cover - depends on the install
            return None
        return TransferConfig(
            multipart_threshold=self._multipart_threshold,
            multipart_chunksize=16 * 1024 * 1024,
            use_threads=True,
        )

    def _verify_by_head(self, *, key: str, size: int, checksum_sha256: str) -> PutResult:
        info = self.head_object(key=key)
        if (
            info is not None
            and info.size == size
            and info.metadata.get("sha256") == checksum_sha256
        ):
            return PutResult(key=key, size=size, verified_by="size", etag=info.etag)
        return PutResult(key=key, size=size, verified_by="none", etag=info.etag if info else None)


def _b64(hex_digest: str) -> str:
    """Hex SHA-256 → the base64 form S3's `ChecksumSHA256` expects."""
    try:
        return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
    except (ValueError, binascii.Error) as exc:  # pragma: no cover - defensive
        raise ValueError(f"Not a hex SHA-256 digest: {hex_digest!r}") from exc
