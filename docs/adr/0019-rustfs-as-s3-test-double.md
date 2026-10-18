# ADR-0019: RustFS is the S3 test double for integration testing

- **Status:** Accepted
- **Date:** 2026-10-06
- **Deciders:** project owner
- **Related:** [`cloud-object-layout.md`](../contracts/cloud-object-layout.md) §6.1, [`lld/00-overview.md`](../lld/00-overview.md) §5, [`ADR-0005`](0005-b2-behind-storage-adapter.md), [`ADR-0014`](0014-pinned-image-digests.md), [`tests/integration/README.md`](../../tests/integration/README.md) (in the repo root)

---

## Context

M3's exit criteria include three things that cannot be certified with the in-process
`FakeAdapter` the unit suite uses: recovery from a provider outage without duplication
(M3.4), exactly-once upload under real concurrency (M3.12), and a live check that the uploader
container cannot write to its read-only clips mount (M3.11). The design also carries two
measurements that need a server which is not ours: what a single-part PUT returns for
`ChecksumSHA256` (§4.2), and what a delete leaves behind on a versioned bucket (§5.3).

The docs named **MinIO** as the CI stand-in, so the expectation was that getting one was a
`docker pull`. It is not. Measured on 2026-10-06, by registry metadata query (no images
pulled):

| Image | Result |
|---|---|
| `quay.io/minio/minio` | 401 Unauthorized |
| `docker.io/minio/minio` | 401 Unauthorized |
| `bitnami/minio` | gone (Bitnami moved to paid "Secure Images") |
| `bitnamilegacy/minio` | pullable — an archived mirror, "no longer updated", "may be removed" |
| `rustfs/rustfs` | pullable |
| `adobe/s3mock`, `localstack/localstack` | pullable |

So the plan had a dependency that no longer exists in the form the docs assumed. A
meaningful integration suite needs a server that (a) someone can pull without an account, and
(b) behaves enough like B2 to exercise authentication, checksums, metadata and versioning.

## Decision

**The bundled integration fixture is RustFS (`rustfs/rustfs`), started by
`tests/integration/compose.yml`. The suite itself stays endpoint-agnostic — it takes an
endpoint and credentials from `CCTV_TEST_STORAGE_*` — so the fixture is a convenience and
never a requirement.**

Measured against RustFS on 2026-10-06, `S3StorageAdapter`:

| Behaviour | Result |
|---|---|
| Single-part PUT checksum | `verified_by="sha256"` — a comparable `ChecksumSHA256` is returned |
| Object metadata round-trip | preserved |
| Re-PUT of identical bytes | `already_present=True`, one object |
| Bad credentials | `credentials_ok=False`, classified `auth` (reachable stays true) |
| Bucket versioning + delete | delete marker and prior version remain → the §5.3 trap reproduced |

## Consequences

### Positive

- Every M3 criterion that needed a real endpoint is now certified: 17 integration tests pass
  against RustFS, including the 8 × 500 exactly-once soak (M3.12).
- The checksum path is exercised for real rather than degrading to `verified_by="size"`.
- Authentication can be tested, so the "revoked credential stops instead of looping" path
  (M3.5) is verified against a server that actually rejects it.
- The suite runs against a real Backblaze B2 bucket by changing four environment variables,
  so no code change is needed when someone wants to settle §1.9 and §5.3 properly.

### Negative / accepted costs

- **`rustfs/rustfs:latest` is a moving tag.** Acceptable for a local fixture; this repo's
  digest pinning ([ADR-0014](0014-pinned-image-digests.md)) covers our own released images,
  not a throwaway test server. Pin a digest if this ever runs unattended in CI.
- **RustFS is a younger project than MinIO.** It is not a household name, and its
  compatibility surface is therefore less battle-tested. The mitigation is structural: the
  suite is endpoint-agnostic, so replacing it is a compose edit, not a rewrite.
- **It is still not Backblaze B2.** §4.2's real `ChecksumSHA256` semantics and §5.3's
  hide-versus-hard-delete behaviour remain measured only against a compatible server. Backlog
  §1.9 and §1.10 stay open.
- One more container image in the developer's life, and a pull on first use.

### Neutral

- The fixture publishes only the S3 API port (loopback). RustFS's console on 9001 is not
  published.
- The fixture keeps **no volume**, so each run starts empty — deliberate, so a leftover object
  cannot make a later test pass for the wrong reason.
- RustFS is reached through the one dependency we already have (`boto3`); no new SDK.

## Alternatives considered

| Alternative | Why not |
|---|---|
| `bitnamilegacy/minio` | Works today, but it is an archived mirror that upstream has said may be removed, and it is no longer updated. Choosing it knowingly for a permanent test dependency trades a solved problem for a scheduled one |
| `adobe/s3mock` | Pullable and purpose-built, but it does not enforce authentication (so M3.5's live path cannot be tested) and does not return a comparable checksum (so the §4.2 path degrades to size). It tests less of what M3 actually claims |
| `localstack/localstack` | Pullable and broad, but a much larger surface and slower start for a need that is only "an S3 endpoint with auth and versioning" |
| Mirror MinIO into a registry we control | Keeps MinIO, at the cost of a registry to run and a licence/compliance question for redistributing a vendor's image. Revisit if RustFS disappoints |
| No fixture — bring your own endpoint | The most honest option, and still supported, but it makes the integration suite opt-in for everyone, which in practice means it never runs |

## Revisit if

- RustFS changes its distribution model, or a compatibility bug is found that makes a test
  pass for the wrong reason.
- MinIO (or another double) becomes anonymously pullable again and is a better fit.
- This project gains its own CI runner that can host a pinned image digest — at which point
  the fixture should be pinned per [ADR-0014](0014-pinned-image-digests.md) and this decision
  largely stops mattering.
