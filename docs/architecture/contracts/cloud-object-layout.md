# Contract: cloud object layout

**Parties:** uploader, cleanup, `install.sh` → Backblaze B2

Defines what we write to the cloud, under what keys, with what guarantees, and how
it is removed. The storage adapter ([ADR-0005](../../adr/0005-b2-behind-storage-adapter.md))
hides the provider, but the *key layout* is provider-independent and is a contract
in its own right, because objects outlive our code.

---

## 1. Bucket topology

| Decision | Value |
|---|---|
| Granularity | **One bucket per site** |
| Naming convention | `cctv-<location>` — e.g. `cctv-parents-home` |
| Visibility | **Private**, always. No public buckets, ever |
| Encryption | Provider-side encryption enabled; documented in the install runbook |
| Versioning | Configured to keep only the relevant version (see §5) |

### 1.1 Why one bucket per site, when prefixes alone would do

The PRD's example is `B2_BUCKET=cctv-parents-home`, and it is the right call for
reasons beyond matching the PRD:

| Reason | Detail |
|---|---|
| Blast radius | A lifecycle rule with a mistaken prefix cannot reach another site's data, because it is a different bucket |
| Credentials | Each site's application key can be scoped to its own bucket only, so a compromised site cannot read another's footage |
| Cost attribution | Per-site billing is inherent |
| Deletion | Decommissioning a site is deleting a bucket, not a prefix-surgery exercise |

**Prefixes are used anyway, inside the bucket.** Both belts, because
[INV-10](../invariants.md) fails silently when it fails.

---

## 2. Object keys

```text
<location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
<location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
```

Example:

```text
cctv-parents-home
├── parents-home/clips/front-door/2026/09/11/18-32-45.mp4
├── parents-home/clips/front-door/2026/09/11/18-32-45.mp4.json
├── parents-home/clips/front-door/2026/09/11/18-47-12.mp4
├── parents-home/clips/garage/2026/09/11/19-03-51.mp4
└── parents-home/clips/garage/2026/09/11/19-03-51.mp4.json
```

| Element | Rule | Why |
|---|---|---|
| `<location>` | First path segment, from `site.location` | Makes prefix-scoped lifecycle rules and IAM trivial. A rule at `<location>/clips/` cannot touch anything else |
| `clips/` | Namespace segment | Reserves room for future namespaces (`snapshots/`, `exports/`) without a migration |
| `<camera>` | Same key as `cameras.yml` | Identical to the local path; no translation layer |
| `YYYY/MM/DD/HH-MM-SS` | **Mirrors the local path exactly** | One path function ([INV-5](../invariants.md)); the cloud layout is provably a prefix of the local one |
| `.mp4.json` | Sidecar, written **after** the clip | The clip's existence is the signal; sidecar ordering means a crash leaves a clip without metadata rather than metadata without a clip |

### 2.1 Key equality with the local path

The object key is `<location>/clips/` + the clip's path **relative to `data/clips/`**.

This is deliberate and load-bearing:

- Round-trip verification is a string comparison, not a mapping table.
- Restore is "download prefix, strip `<location>/clips/`, write into `data/clips/`".
- A bug in key construction is immediately visible as a path that does not match.

A test asserts this equality for every clip row.

### 2.2 Why not include the event id in the key

Tempting for uniqueness, rejected because it breaks human browsability and the
PRD's stated layout (§7). Collisions within a camera-second are resolved by the
deterministic suffix rule in
[`on-disk-layout.md`](on-disk-layout.md#3-clip-path-contract), which produces the
same key locally and remotely.

---

## 3. Object properties

### 3.1 Content

| Property | Value |
|---|---|
| Content-Type | `video/mp4` |
| Body | The exact bytes of the canonical clip file; **never re-encoded or transformed on upload** |
| Size | Typically 1–50 MB; single-part PUT |
| Multipart | Used above `multipart_threshold_bytes` (default 100 MB). Beyond B2's single-request limits this is required, and it changes ETag semantics (§4.2) |

Uploading the file's exact bytes matters: it makes the local `sha256` a meaningful
statement about the cloud object, which is what makes
[INV-7](../invariants.md) and [INV-10](../invariants.md) verifiable.

### 3.2 Object metadata

Sent as provider metadata on the PUT so an object is self-describing even if
`state.db` is lost:

| Key | Value |
|---|---|
| `event-id` | Frigate event id |
| `camera` | Camera name |
| `location` | Site location |
| `start-time` | UTC epoch, as a string |
| `end-time` | UTC epoch |
| `duration-s` | From `ffprobe` |
| `sha256` | Lowercase hex of the local file |
| `label` | Primary label |
| `zones` | Comma-separated |
| `cctv-version` | Version that produced the clip |

This is the recovery path of last resort: with the bucket listing and object
metadata alone, a rebuild can reconstruct most of `clips` — though **not** upload
state, which is why [`lld/08`](../lld/08-backup-and-restore.md) still centres on
`state.db`.

Metadata must not contain credentials or anything derived from them.

---

## 4. Integrity verification

### 4.1 What is checked

```text
1. Local:   sha256 of the file is computed before upload and stored in state.db
2. Upload:  the exact bytes are PUT
3. Verify:  the provider-reported checksum for the stored object is compared
4. Record:  only after step 3 matches is the row moved to UPLOADED
```

A mismatch is `checksum_mismatch` — a **permanent** error class. It is never
retried blindly; retrying an upload that produces a different checksum on the same
input indicates corruption, not a transient fault. The clip is escalated to
`FAILED_PERMANENT` with a `critical` audit event, and the local file is
**preserved** ([INV-1](../invariants.md)).

### 4.2 Provider checksum semantics

| Situation | Verification available |
|---|---|
| Single-part PUT | The provider reports a content checksum for the stored object. Compare against local `sha256` |
| Multipart PUT | The reported value is typically a composite digest and is **not** comparable to a plain SHA-256 of the file |

> **VERIFY AT IMPLEMENTATION.** Confirm the checksum algorithm and encoding that the
> S3-compatible B2 endpoint returns for single-part and multipart PUTs in the pinned
> provider version, and record it here. B2's native API reports SHA-1; the
> S3-compatible API's ETag semantics differ. Do not assume; measure.

**Rule:** if a comparable checksum is unavailable for a given upload shape, do not
claim verification. Either fall back to a size comparison plus a documented
`VERIFIED_BY_SIZE` state, or perform a `HEAD` and compare size and metadata. Claiming
a checksum match we did not perform is worse than admitting we only checked the size.

### 4.3 Re-verification

The `verify` admin endpoint
([`service-contracts.md`](service-contracts.md#4-admin-endpoints)) re-downloads and
re-hashes a clip on demand. It is not run automatically: egress costs money, and the
provider already guarantees durability.

---

## 5. Lifecycle rules (the provider-side backstop)

Per PRD §10, cloud retention uses **both** application cleanup and the provider's
native lifecycle mechanism. The application is exact and runs frequently; the
lifecycle rule catches anything bookkeeping missed.

### 5.1 The rule

Applied to the bucket by `install.sh`, scoped to this site's prefix:

```json
[
  {
    "fileNamePrefix": "<location>/clips/",
    "daysFromUploadingToHiding": <cloud_days + grace_days>,
    "daysFromHidingToDeleting": 1,
    "daysFromStartingToCancelingUnfinishedLargeFiles": 7
  }
]
```

With the defaults (`cloud_days = 60`, `grace_days = 7`):

```json
[
  {
    "fileNamePrefix": "parents-home/clips/",
    "daysFromUploadingToHiding": 67,
    "daysFromHidingToDeleting": 1,
    "daysFromStartingToCancelingUnfinishedLargeFiles": 7
  }
]
```

| Field | Reason |
|---|---|
| `fileNamePrefix` | Scopes the rule to this site's clips. **Never an empty prefix** — an empty prefix can delete everything in the bucket |
| `daysFromUploadingToHiding` = `cloud_days + grace` | The application deletes at exactly `cloud_days`. The extra grace means the application normally wins, and the rule only catches objects our code failed to remove |
| `daysFromHidingToDeleting: 1` | See §5.3 — this is what actually frees the space |
| `daysFromStartingToCancelingUnfinishedLargeFiles: 7` | Reaps aborted multipart uploads, which otherwise occupy storage invisibly |

### 5.2 Constraints imposed by the provider

| Constraint | Consequence for us |
|---|---|
| Applied **once per day** | Cloud retention is accurate to ±1 day. Acceptable, and documented so it is not reported as a bug |
| At most 100 rules per bucket | One bucket per site keeps us far from this, but the generator must still **merge** rather than append on re-install, or repeated installs would exhaust the limit |
| Overlapping prefixes resolve to the **smallest** non-null value per property | We use exactly one rule per site prefix, so this cannot bite. If a second rule is ever added, this becomes a real hazard |
| Rules are evaluated per file version | §5.3 |

### 5.3 File versions and the deletion trap

> ⚠️ **This is the easiest way to pay for deleted data.**

Backblaze B2 retains file versions. A plain `DELETE` on an object whose bucket keeps
all versions creates a **hide marker**; the previous version still exists and
**still counts toward storage**. An application that "deletes" expired clips and
reports success would be hiding them forever, at full cost.

Three mitigations, all applied:

1. **Configure the bucket** to keep only the relevant version, so a delete actually
   removes the current version. `install.sh` sets this and verifies it.
2. **`daysFromHidingToDeleting: 1`** on the site prefix, so any hidden version is
   purged the next day regardless. This is the backstop for the backstop.
3. **Verify after delete.** `cleanup` confirms the object is gone (a `HEAD` that
   returns not-found). A delete that leaves the object visible is recorded as
   `EXPIRED_CLOUD` only when confirmed; otherwise the row goes to a
   `cloud_delete_failed` condition and the health degrades.

> **VERIFY AT IMPLEMENTATION.** Confirm how the S3-compatible endpoint expresses
> hiding versus hard deletion in the pinned provider version, and whether the
> bucket's versioning setting can be configured through that endpoint or requires
> the native API. If the native API is required, the adapter needs a second, narrow
> code path for bucket configuration — and that is a finding worth recording here.

### 5.4 Lifecycle rule management

| Operation | Behaviour |
|---|---|
| Install | Read existing rules; merge ours by prefix; write back. Never clobber another tool's rules |
| Update | Same merge. If `cloud_days` changed, the rule is rewritten |
| Verify | After writing, read back and assert our rule is present with the expected values. Failure is a non-fatal warning at install and a `DEGRADED` health check at runtime |
| Uninstall | Rules are **left in place** by default. Removing them silently would strand objects forever. `uninstall.sh --remove-lifecycle` does it explicitly |

---

## 6. Adapter interface

The provider-specific surface is deliberately tiny. Anything larger would leak the
provider into our logic, defeating [ADR-0005](../../adr/0005-b2-behind-storage-adapter.md).

```python
class StorageAdapter(Protocol):
    def ensure_bucket(self, *, configure_lifecycle: LifecycleSpec | None) -> None: ...
    def put_object(self, *, key: str, path: Path, metadata: dict[str, str],
                   checksum_sha256: str) -> PutResult: ...
    def head_object(self, *, key: str) -> ObjectInfo | None: ...
    def delete_object(self, *, key: str) -> DeleteResult: ...
    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Iterator[ObjectInfo]: ...
    def presign_get(self, *, key: str, ttl_s: int) -> str: ...
    def healthcheck(self) -> AdapterHealth: ...
```

| Method | Notes |
|---|---|
| `put_object` | Must be idempotent for identical content. Overwriting an existing object with identical bytes is a success, not an error |
| `delete_object` | Must report whether the object is *actually gone*, not merely that a request succeeded (§5.3) |
| `list_objects` | Used **only** by reconciliation and restore — never to decide what to delete ([INV-10](../invariants.md)) |
| `presign_get` | Not used in v1; present so a future clip browser does not need adapter surgery |
| `healthcheck` | Must distinguish unreachable / auth failure / bucket missing. These have different operator fixes |

Provider-specific concerns are confined to `ensure_bucket` and the checksum
semantics in §4.2.

### 6.1 The test double

Because the interface is S3-shaped, CI uses an S3-compatible server (MinIO) with
`STORAGE_ENDPOINT_URL` overridden. Requirements this places on the adapter:

- No B2-specific SDK calls in the hot path.
- No B2-specific behaviour in key construction.
- Lifecycle configuration, being the one genuinely provider-specific feature, is
  optional: its absence degrades to "no provider backstop" with a warning, not a
  failure. Tests assert exactly that.

---

## 7. Naming and validation

| Rule | Enforcement |
|---|---|
| `<location>` must match `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` | Hard error at config validation |
| Bucket name must be a valid provider bucket name | Hard error at install |
| Bucket name must contain the location, or an explicit override must be recorded | Warning; the override is written to `system_events` |
| Keys contain no leading `/`, no `//`, no `..`, no backslashes, no control characters | Hard error at key construction |
| Key length ≤ 1024 bytes | Hard error |
| No key contains a camera display name, only the camera key | [INV-3](../invariants.md) — display names may contain characters hostile to keys |

---

## 8. Failure and recovery matrix

| Condition | Behaviour |
|---|---|
| Endpoint unreachable | `transient_error`, exponential backoff, indefinite retry. Clips never deleted locally ([INV-1](../invariants.md)) |
| Credentials rejected | `auth` class, **no retry**, immediate `critical` alert naming the fix |
| Bucket missing | `not_found` class, no retry, alert with the exact expected name |
| Quota / cap exceeded | `quota` class, backoff with a longer ceiling, alert. This is a "call the operator" condition |
| Checksum mismatch | `checksum_mismatch`, no retry, local file preserved, alert |
| Delete fails | Row is **not** marked `EXPIRED_CLOUD`; retried next run; health degrades after N failures |
| Object exists with different content | Treated as a corrupt state, not an overwrite. `critical` event, no automatic overwrite — silently replacing an object is how you lose the only good copy |
| Object missing but row says `UPLOADED` | Reconciliation reports `dangling_row`; the next upload attempt re-uploads the local file if it still exists, or the row is flagged for the operator if it does not |

---

## 9. Cost notes

Recorded here because they are design constraints, not trivia.

| Factor | Implication |
|---|---|
| Storage billed per byte-month, including hidden versions | §5.3 exists to stop silent cost growth |
| Uploads are free; **downloads are billed** | Verification re-downloads (§4.3) are opt-in, never automatic |
| Deletes are free | Cleanup can be aggressive about actually removing objects |
| Object count matters as much as bytes | One object per clip is far more efficient than one per segment, which is another reason we materialise whole clips |
| 60-day clip retention at ~1 GB/day ≈ 60 GB | Roughly the cost of a coffee per month, which is what makes the whole design viable |

At the default retentions, cloud cost scales with *events*, not with *cameras*,
which is the property that makes this system deployable at a family home without a
financial discussion. It is worth stating explicitly in the README, because
"back up my CCTV to the cloud" sounds expensive and is not.
