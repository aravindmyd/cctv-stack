# ADR-0005: Backblaze B2 is the only implementation, behind a storage adapter

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [`contracts/cloud-object-layout.md`](../architecture/contracts/cloud-object-layout.md), PRD §8

---

## Context

PRD §8 requires that cloud storage be an adapter, not a coupling:

> "The application should not be tightly coupled to Backblaze B2… internally we should
> think in terms of Local Clip → Cloud Storage Adapter → B2."

It also states the initial implementation uses B2, with S3, Wasabi, and MinIO as
possible later targets.

B2 offers two interfaces: a native API (with `b2sdk`, file-ID-based deletes, and full
lifecycle-rule control) and an S3-compatible endpoint (works with any S3 client, but
with different checksum and versioning semantics). Lifecycle rules — which provide the
provider-side retention backstop PRD §10 asks for — are most completely expressed
through the native API.

The owner's direction: *"Let's have only B2 but have the code like a strategy so that
if we extend to other services, it will be easy."*

## Decision

**Define a narrow `StorageAdapter` protocol; implement exactly one provider (B2) in
v1; build against the S3-compatible endpoint so the interface stays provider-neutral.**

```python
class StorageAdapter(Protocol):
    def ensure_bucket(self, *, configure_lifecycle: LifecycleSpec | None) -> None: ...
    def put_object(self, *, key, path, metadata, checksum_sha256) -> PutResult: ...
    def head_object(self, *, key) -> ObjectInfo | None: ...
    def delete_object(self, *, key) -> DeleteResult: ...
    def list_objects(self, *, prefix, max_keys=1000) -> Iterator[ObjectInfo]: ...
    def presign_get(self, *, key, ttl_s) -> str: ...
    def healthcheck(self) -> AdapterHealth: ...
```

Supporting decisions:

- Object keys are provider-neutral (`<location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`)
  and mirror the local path exactly
- `STORAGE_ENDPOINT_URL` is overridable, so CI runs against a local S3-compatible
  double and the adapter is exercised without real credentials
- Provider-specific behaviour is confined to `ensure_bucket` and checksum handling, and
  both are documented as the places where a second provider will need work
- **No speculative abstraction.** One provider, one interface. The interface exists to
  keep provider concerns contained, not to support providers nobody has asked for

## Consequences

### Positive

- **The provider cannot leak into our logic.** Upload decisions, retry classification,
  key construction, and retention never mention B2
- **CI can test the whole pipeline** against a local S3-compatible server, with no
  cloud account, no credentials, and no egress cost
- **A second provider is a bounded piece of work**: implement the protocol, update
  `ensure_bucket` for lifecycle, document the checksum semantics
- **Cheap object storage that matches the retention model.** B2's lifecycle rules map
  cleanly onto "hide at N days, delete at N+1"
- Testable deletion semantics, which matters because of the hide-versus-delete trap
  ([`cloud-object-layout.md` §5.3](../architecture/contracts/cloud-object-layout.md#53-file-versions-and-the-deletion-trap))

### Negative / accepted costs

- **One provider in practice.** If B2 changes its API or pricing, migration means
  implementing a second provider, not switching a config value. Accepted: the
  interface makes that work bounded, but it is not free
- **Two interfaces to reason about.** B2's native API can do some things the
  S3-compatible endpoint cannot, particularly around lifecycle rules and
  version-specific deletes. Where the S3 endpoint is insufficient, a narrow native
  code path is needed — and that must be documented as provider-specific
- **Checksum semantics are the sharpest edge.** B2's native API reports SHA-1; the
  S3-compatible endpoint's ETag semantics differ for single-part and multipart uploads.
  The adapter must not claim a verification it did not perform, and the exact behaviour
  must be measured rather than assumed
- **A test double is not the real provider.** MinIO is S3-compatible but not
  behaviour-identical, so integration tests against real B2 remain necessary before a
  release
- **Versioning semantics can silently cost money.** An S3 `DELETE` against a
  versioned bucket may hide rather than delete. Handled explicitly, but it is a
  provider quirk that a "generic S3" design would have to solve per provider anyway

### Neutral

- `presign_get` is implemented but unused in v1. It exists so a future clip browser
  does not require adapter surgery
- The interface is small (7 methods) on purpose. Every method added is a method a
  second provider must implement correctly

## Alternatives considered

| Alternative | Why not |
|---|---|
| **rclone as a sidecar** | Least code, and handles many providers. Rejected: it moves upload state into rclone's own world, weakens the state machine PRD §9 requires, and makes per-clip verification and error classification much harder. We would be guessing at outcomes from log output |
| **Native `b2sdk` only** | Best B2 fidelity, including file-ID deletes and full lifecycle control. Rejected as the primary path because it drags B2 concepts (file IDs, versions) into the adapter interface, making a second provider harder — the opposite of what an adapter is for. Retained as a narrow escape hatch where the S3 endpoint is insufficient |
| **Abstract across multiple providers now** | Would produce an interface shaped by imagined requirements rather than real ones, and triple the testing surface with no user. Explicitly rejected; YAGNI |
| **No adapter — call S3 directly from the uploader** | Smallest amount of code, and would work. Rejected because PRD §8 asks for the adapter explicitly, and because it would put provider concerns into the retry-classification logic, which is exactly where they would do the most damage |
| **A local-only design with no cloud** | Contradicts PRD §8–§10 and the entire durability premise. However, `retention.upload: false` provides a supported local-only mode with an explicit `SKIPPED_BY_POLICY` state, so the capability exists without a separate architecture |

## Revisit if

- A second provider is genuinely needed (S3, Wasabi, MinIO for an on-prem site), at
  which point the interface should be reviewed against a real second implementation
- The S3-compatible endpoint cannot express something correctness-critical, forcing
  more native-API code than expected
- B2 changes pricing or lifecycle semantics materially
- Egress costs for restore become significant enough to prefer a provider with free
  egress
