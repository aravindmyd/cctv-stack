# Integration suite

Live tests that need a **real S3-compatible endpoint**. They exist because the unit suite
drives an in-process `FakeAdapter`: it can prove what we do with what a provider says, but
not that a provider says it, that concurrent claims survive real HTTP, or that a container
cannot write to a read-only mount. Those are exactly the M3 exit criteria that were still
open (`docs/plans/milestones.md`).

```text
tests/integration/
├── run.sh                       # start RustFS, run the suite, tear it down
├── compose.yml                  # the RustFS fixture (loopback, ephemeral, not a deployment)
├── conftest.py                  # fixtures: a raw boto3 client, a fresh bucket, a real adapter
├── test_storage_live.py         # the adapter against a real endpoint
├── test_uploader_live.py        # the state machine against a real endpoint
└── test_readonly_mount_live.py  # the container cannot write to the clips mount (M3.11)
```

## Running it

```bash
./tests/integration/run.sh                 # everything that needs an endpoint
./tests/integration/run.sh -k outage       # a subset
CCTV_TEST_SLOW=1 ./tests/integration/run.sh   # include the 8 x 500 soak (M3.12)
```

The default `uv run pytest` **skips** everything here, so the ordinary suite stays green with
no containers and no network. The `test_readonly_mount_live.py` test needs Docker and the
built `cctv-stack:local` image but no endpoint; it skips if either is absent.

To run against something other than the bundled fixture — RustFS from elsewhere, or a **real
Backblaze B2 bucket** — set these and run pytest directly:

| Variable | Meaning |
|---|---|
| `CCTV_TEST_STORAGE_ENDPOINT_URL` | S3 endpoint; unset ⇒ the directory is skipped |
| `CCTV_TEST_STORAGE_KEY_ID` | access key id |
| `CCTV_TEST_STORAGE_APPLICATION_KEY` | secret |
| `CCTV_TEST_STORAGE_REGION` | optional, default `us-east-1` |
| `CCTV_TEST_SLOW` | `1` to include the full-scale soak |
| `CCTV_TEST_STORAGE_IMAGE` | override the fixture image |
| `CCTV_TEST_STORAGE_PORT` | host port for the fixture, default `19000` |

```bash
CCTV_TEST_STORAGE_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com \
CCTV_TEST_STORAGE_KEY_ID=… CCTV_TEST_STORAGE_APPLICATION_KEY=… \
CCTV_TEST_STORAGE_REGION=eu-central-003 \
  uv run --extra storage pytest tests/integration -m integration -s -ra
```

> Use a **throwaway bucket** for this. The suite creates and deletes buckets and objects.

## Why RustFS, and not MinIO

The design docs originally named MinIO as CI's S3 stand-in. As of 2026-10-06 it is no longer
pullable without an account:

| Image | Status |
|---|---|
| `quay.io/minio/minio` | 401 Unauthorized |
| `docker.io/minio/minio` | 401 Unauthorized |
| `bitnami/minio` | gone (Bitnami moved to paid "Secure Images") |
| `bitnamilegacy/minio` | pullable — archived mirror, "may be removed" |
| `rustfs/rustfs` | **pullable, actively maintained — the default here** |
| `adobe/s3mock`, `localstack/localstack` | pullable — viable alternatives |

RustFS was chosen because it pulls without an account and behaves like a real server.
Measured against it on 2026-10-06, `S3StorageAdapter`:

- receives a comparable `ChecksumSHA256` (`verified_by="sha256"`), so the checksum path is
exercised rather than falling back to size;
- authenticates, and classifies a bad key as `auth` rather than `transient`;
- supports bucket versioning, and reproduces the §5.3 delete-marker trap.

`adobe/s3mock` could do none of the first three, which is why it was not chosen. See
ADR-0019.

The fixture is only a convenience: the suite is endpoint-agnostic, so the same variables can
point at any S3-compatible server — including a real Backblaze B2 bucket — and this file can
be skipped entirely.

## What this proves — and what it does not

**Proves** (against a real server): the object key equals `<location>/clips/` + the local
path; a repeat PUT of identical bytes sends nothing; a PUT over different content is refused
rather than overwritten; a delete is confirmed; an outage recovers with no duplicate objects;
a killed worker's lease is reclaimed; a restart resumes without re-uploading; a revoked
credential stops instead of looping; concurrent workers upload each clip exactly once; the
container's `clips` mount is read-only at the kernel.

**Does not prove**: anything specific to **Backblaze B2**. RustFS is S3-compatible, not B2.
The two measurements the contract still flags — the real `ChecksumSHA256` semantics
(`cloud-object-layout §4.2`) and hiding versus hard deletion on a versioned bucket (`§5.3`) —
stay open until the same run is done against the real provider. Backlog §1.9 and §1.10 remain
the authority on that, and `test_a_versioned_bucket_keeps_the_bytes_after_a_delete` is the
evidence M4 will need before it may record `EXPIRED_CLOUD`.
