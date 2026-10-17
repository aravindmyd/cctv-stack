# Packaging and client-site delivery

**Status:** 📐 proposed — approval needed before the release workflow is implemented
**Implements:** M6 (installer), M8 (public release)
**Decisions:** [ADR-0020](../adr/0020-release-artifacts-and-delivery-modes.md),
[ADR-0014](../adr/0014-pinned-image-digests.md)

---

## 1. The question this answers

> A client site has a mini-PC, a network of cameras, and someone who can follow a
> runbook but cannot read this repository. What exactly do we hand them, and what do
> they type?

The answer is **not** `git clone`. A client site receives a **versioned, signed release
bundle** plus a **digest-pinned container image**. The bundle is small and reviewable;
the image is the code. Neither contains a camera password, a site name, or a secret.

`git clone && ./install.sh` remains the development path. It is documented as such, and
it is what CI uses. It is not what an operator is told to do.

---

## 2. What a release is

One tag, `v<semver>`, produces one immutable artefact set:

| Artefact | Name | Contents | Typical size |
|---|---|---|---|
| Image (per arch) | `ghcr.io/<owner>/cctv-stack:<version>` | Python services, entry points, migrations, `ffmpeg` | ~300 MB compressed |
| Image index | the same tag, multi-arch | amd64 + arm64 manifest list, provenance, SBOM | — |
| Bundle | `cctv-stack-<version>.tar.gz` | compose, scripts, manifest, examples, docs, licence | < 200 KB |
| Checksums | `SHA256SUMS` | one line per artefact | < 1 KB |
| Signature | `SHA256SUMS.sig`, `SHA256SUMS.pem` | Sigstore keyless signature over the checksums | < 5 KB |
| Offline image archive (optional) | `cctv-stack-<version>-offline-<arch>.tar.zst` | `docker save` of the image **and** Frigate and mosquitto | 1–3 GB |

`latest` is not published. A site always knows its version because it is in the
filename.

### 2.1 Bundle layout

```text
cctv-stack-<version>/
├── VERSION                     # the same string as the tag, without the `v`
├── versions.yml                # image digests + schema versions (ADR-0014)
├── compose.yml                 # deployment boundary, WITHOUT `build:` (see §2.2)
├── .env.example                # location template, no values required to be real
├── config/
│   └── cameras.example.yml     # camera schema example, no credentials
├── install.sh                  # preflight → interview → generate → pull → start → verify
├── update.sh                   # acquire → back up → migrate → start → verify
├── uninstall.sh                # keeps data by default
├── scripts/
│   ├── detect-host.sh          # pure detection, JSON on stdout
│   └── doctor.sh               # read-only diagnosis
├── docs/
│   ├── runbooks/install.md
│   ├── runbooks/rotate-credentials.md
│   ├── runbooks/disaster-recovery.md
│   └── architecture/invariants.md
├── LICENSE
├── THIRD-PARTY-NOTICES
├── SHA256SUMS
└── SHA256SUMS.sig / SHA256SUMS.pem
```

The bundle is assembled from **tracked files only**, by an explicit allowlist. A
`git archive` of the whole repository would ship tests, the synthetic harness, and
whatever a future contributor accidentally commits; an allowlist fails closed.

**Never in the bundle:** `.env`, `config/cameras.yml`, `config/frigate/`, `data/`,
`spike/`, `dev-synthetic/`, any `.git` directory, any credential, any real hostname.

### 2.2 The release compose file has no `build:`

The repository's `compose.yml` has an `x-cctv-image` anchor with both `image:` and
`build:`. That is correct for development and wrong for a site: the bundle does not
contain a `Dockerfile` or `src/`, and a Compose file that can try to build source it does
not have turns a missing image into a confusing build failure instead of "the image this
release pins is not present".

The bundle therefore ships a generated `compose.yml` with the `build:` blocks removed and
the `image:` references left as `${CCTV_IMAGE}`. `install.sh` writes
`compose.override.yml` with the digest for the detected architecture. The service graph,
health checks, mounts and ports are identical; only the source of the image differs.
`install.sh` asserts that the image is present (or loads it in `--offline` mode) before
`docker compose up`, so a failed pull names the registry and digest rather than failing
inside a builder.

Keeping two compose files is a maintenance risk, so the release build derives one from the
other mechanically and a test asserts the service names and mounts match. The alternative —
one file with `--no-build` everywhere — is fragile because `docker compose run` and
`docker compose up` do not share every flag, and a forgotten flag reintroduces the build.

---

## 3. What the operator does

### 3.1 Connected site (default)

```sh
# 1. Download the four files for this release.
curl -fsSLO https://github.com/<owner>/cctv-stack/releases/download/v<version>/cctv-stack-<version>.tar.gz
curl -fsSLO https://github.com/<owner>/cctv-stack/releases/download/v<version>/SHA256SUMS
curl -fsSLO https://github.com/<owner>/cctv-stack/releases/download/v<version>/SHA256SUMS.sig
curl -fsSLO https://github.com/<owner>/cctv-stack/releases/download/v<version>/SHA256SUMS.pem

# 2. Verify BEFORE extracting or executing.
sha256sum -c SHA256SUMS --ignore-missing
cosign verify-blob \
  --certificate SHA256SUMS.pem \
  --signature SHA256SUMS.sig \
  --certificate-identity-regexp 'https://github.com/<owner>/cctv-stack/.github/workflows/release.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS

# 3. Extract into the install directory.
sudo mkdir -p /opt/cctv && sudo tar -xzf cctv-stack-<version>.tar.gz -C /opt/cctv --strip-components=1

# 4. Install.
cd /opt/cctv && sudo ./install.sh
```

`install.sh` then runs the ten phases from
[`lld/07`](../architecture/lld/07-installer-and-host-profiles.md): preflight, detect
profile, check storage, interview the operator, validate, generate, provision B2, pull
by digest, start, verify. It finishes by printing the URLs and writing a redacted
diagnostics bundle.

The release script never runs `docker compose build`. Phase 8 is a pull-and-verify against
`versions.yml`; the repository's script keeps the build step because a developer has
source. The phases, prompts and verification are shared, so "install" cannot mean two
different things depending on where the script came from.

**The install directory is a choice, not a hardcode.** `CCTV_HOME` defaults to
`/opt/cctv`; the data directory (`CCTV_DATA_DIR`) is separate and may be on another
filesystem. An operator who wants the bundle in `/srv/cctv` and the footage on a USB
disk can have that.

### 3.2 Mirrored site

The same bundle and the same digests; only the registry host differs. The site mirrors
by digest, not by tag:

```sh
skopeo copy --all \
  docker://ghcr.io/<owner>/cctv-stack@sha256:<digest-from-versions.yml> \
  docker://registry.site.local/cctv-stack@sha256:<same-digest>
```

`install.sh` accepts `--registry registry.site.local`, which rewrites the image
reference while keeping the digest. A mirror that cannot serve the exact digest is
rejected: a retagged "same version" is not the same bytes.

### 3.3 Air-gapped site

```sh
# On a connected machine, with the same release:
./scripts/fetch-images.sh --version <version> --arch amd64 --out .
# Produces cctv-stack-<version>-offline-amd64.tar.zst + SHA256SUMS.offline

# At the site: verify, load, install offline.
sha256sum -c SHA256SUMS.offline
zstd -d < cctv-stack-<version>-offline-amd64.tar.zst | docker load
cd /opt/cctv && sudo ./install.sh --offline
```

`--offline` does not skip verification. It skips the **pull**; every loaded image is
compared with `versions.yml` before a container starts. A mismatch aborts with `78`.

---

## 4. Network requirements

| Mode | Required outbound | Inbound |
|---|---|---|
| Connected | HTTPS 443 to `ghcr.io`, `github.com` (release download), the B2 endpoint, and the notification service | **None** |
| Mirrored | HTTPS to the site registry + B2 + notifications | **None** |
| Air-gapped | None during install; B2 and notifications are optional and may be disabled | **None** |

Every published port is bound to `127.0.0.1` (Frigate UI, uploader health, cleanup
health). Frigate's API port 5000 and MQTT are never published. This is the existing
compose contract, and the bundle does not add a port.

---

## 5. `versions.yml`

`versions.yml` is generated by CI at release time from the **actual digests that were
pushed**, not from tags. It is the only manifest an installer reads.

```yaml
cctv_stack: 0.6.0

schema:
  database: 2
  config: 1

frigate:
  version: "0.18.0"
  api_contract_verified: "0.18.0"
  images:
    amd64: ghcr.io/blakeblackshear/frigate@sha256:…
    arm64: ghcr.io/blakeblackshear/frigate@sha256:…

images:
  mosquitto: eclipse-mosquitto@sha256:…
  cctv:
    amd64: ghcr.io/<owner>/cctv-stack@sha256:…
    arm64: ghcr.io/<owner>/cctv-stack@sha256:…

python: "3.12"
```

Rules:

- `schema.database` is read from the migrations on disk at release time, not typed by
  hand.
- `frigate.api_contract_verified` must equal `frigate.version`; a difference fails the
  release ([`lld/09`](../architecture/lld/09-upgrade-and-versioning.md)).
- The amd64 and arm64 digests are recorded separately. They are different images.
- `install.sh` writes a generated `compose.override.yml` that pins every `image:` to the
  digest for the detected architecture. `.env` stays location configuration, so the
  two never contaminate each other.

---

## 6. Release pipeline

A release is a tag on a green commit. There is no manual upload step.

```text
git tag -s v0.6.0 -m "v0.6.0"
git push origin v0.6.0
        │
        ▼
.github/workflows/publish.yml
  1. assert the tag is on main, the tree is clean, and CI passed for that SHA
  2. buildx build --platform linux/amd64,linux/arm64
       --provenance=mode=max --sbom=true
       -t ghcr.io/<owner>/cctv-stack:<version> --push
  3. resolve the pushed index digest + per-arch digests
  4. resolve Frigate and mosquitto digests, assert the Frigate contract version
  5. generate versions.yml (schema versions read from the migrations)
  6. assemble the bundle from the allowlist, tar with the version prefix
  7. write SHA256SUMS; cosign sign-blob (keyless); copy the certificate
  8. optional: build the two offline archives with docker save | zstd
  9. create the GitHub Release and attach every asset
```

Gates inherited from CI (`.github/workflows/ci.yml`, `.github/workflows/e2e.yml`):

| Gate | Why it blocks a release |
|---|---|
| pre-commit (ruff + hygiene) | A release is not the place to discover a formatting error |
| unit tests, Python 3.12 and 3.13 | The floor and the next version |
| live S3 integration | The uploader is the part that touches the customer's cloud |
| synthetic e2e | Frigate → review → canonical clip, through the built image |
| arm64 build | A Pi site is a supported target, not a cross-compile hope |
| no secret in the bundle | Security property of the artefact, checked before upload |
| digest resolution | Every image reference in `versions.yml` must resolve |

The **test** workflows are implemented in
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (pre-commit, unit on 3.12 and
3.13, the pip fallback, and live integration) and
[`.github/workflows/e2e.yml`](../../.github/workflows/e2e.yml) (the synthetic pipeline).

### 6.1 Implemented publishing

[`.github/workflows/publish.yml`](../../.github/workflows/publish.yml) runs on every push
to `main` or `master`, on `v*` tags, and on demand:

| Trigger | Publishes |
|---|---|
| Every merge to `main`/`master` | `ghcr.io/<owner>/cctv-stack:edge` and `:sha-<short>`, multi-arch; wheel and sdist as a 30-day workflow artefact |
| Every `v*` tag | The same image under `:<version>` and `:<major>.<minor>`; the wheel and sdist to PyPI; a GitHub Release with both files attached |

Nothing is published unless the `verify` job passes: ruff check, ruff format check, and
the unit suite. The `dist` job also runs `uv lock --check`, validates the metadata with
`twine check`, and fails if the sdist contains any local-only path (`.env`, `config/`,
`data/`, `dev-synthetic/`, `spike/`).

PyPI is deliberately **not** published on every merge. PyPI versions are immutable, so the
second merge with the same `version` would fail. Per-merge installs use the workflow
artefact; immutable releases go to PyPI on a tag.

One-time setup, outside this repository:

1. **PyPI trusted publisher** — owner `aravindmyd`, repository `cctv-stack`, workflow
   `publish.yml`, environment `pypi`. No API token secret is stored.
2. **GHCR visibility** — after the repository is public, set the `cctv-stack` package to
   public in its package settings; otherwise anonymous `docker pull` returns unauthorized.

Still to implement for the full M6 release: `versions.yml` digest resolution, the bundle
allowlist and `compose.yml` without `build:`, cosign signing, and the offline archives.
Those need the installer phases, which is why the bundle is still marked planned.

---

## 7. Installation-time configuration

The split is already established and the bundle must not blur it:

| File | Owner | Contains | Lives in the bundle? |
|---|---|---|---|
| `cameras.yml` | the site | Camera structure, site name, timezone, retention policy | No — created from the example |
| `.env` | the site | Paths, RTSP credentials, B2 keys, ports | No — created from the example, `chmod 600` |
| `compose.override.yml` | the installer | Digest pins for this architecture | No — generated |
| `config/frigate/config.yml` | generated | Frigate's config, derived from `cameras.yml` | No — generated |
| `versions.yml` | the release | What the release is made of | Yes — read-only |

INV-2 applies to the install directory as much as to git: the installer creates `.env`
with `umask 077`, never prints a credential back, and the diagnostics bundle is
redacted. A site backup of `cameras.yml` alone is enough to rebuild the generated
configuration; a backup that includes `.env` is encrypted (M7).

---

## 8. Upgrade, rollback, uninstall

The client-site lifecycle is the bundle's scripts, not a `git pull`:

| Command | Behaviour |
|---|---|
| `./update.sh --to 0.7.0` | Download and verify the target bundle, back up, run the compatibility gate, pin the new digests, migrate, start, verify |
| `./update.sh --to 0.7.0 --offline-archive <file>` | Same, without network: load the archive, verify digests |
| `./update.sh --rollback` | Re-pin the previous `versions.yml`, restore the pre-upgrade database, start, verify |
| `./uninstall.sh` | Stop and remove containers; **keep** `config/` and `data/` |
| `./uninstall.sh --purge` | Also remove configuration and data, after the operator types the site name |

An upgrade is refused before anything is touched if the database schema was written by a
newer version, if the target MAJOR skips one, or if a required `cameras.yml` key is
missing. The exact sequence is [`lld/09`](../architecture/lld/09-upgrade-and-versioning.md).

**Downgrade is not supported.** Rollback is the supported path, because it restores a
known-good database and a known-good image together.

---

## 9. Packaging acceptance tests

These extend M6. They are tests of the **artefact**, not of the code inside it.

| # | Test | Method |
|---|---|---|
| P1 | The bundle installs on a clean Debian 12 amd64 host | Fresh VM, no checkout, no Python on the host |
| P2 | The bundle installs on a Raspberry Pi 5 (arm64) | Real hardware; asserts the `pi-cpu` profile warning is printed |
| P3 | A tampered bundle fails verification | Flip one byte; `sha256sum -c` and cosign both fail before extraction |
| P4 | A changed image digest aborts the install | Edit `versions.yml`; `install.sh` exits `78` and names the digest |
| P5 | Air-gapped install works with no egress | Network namespace with no route; `--offline` completes |
| P6 | The bundle contains no secrets or site data | Scan the tarball for `.env`, `cameras.yml`, RTSP URLs, B2 keys, and known fixture paths |
| P7 | The image contains no secrets | `docker history` + filesystem scan + `config/cameras.yml` absent |
| P8 | `install.sh` is idempotent | Run twice; the second run changes nothing and deletes nothing |
| P9 | `update.sh` from the previous release preserves every clip row and upload state | Automated, M7.11 |
| P10 | `uninstall.sh` leaves `DATA_DIR` byte-identical | Automated |
| P11 | The same `versions.yml` resolves on amd64 and arm64 | CI matrix |
| P12 | `doctor.sh` produces a redacted report on a broken install | Automated |

P1–P5 need real hardware or a network namespace and are manual gates; P6–P8, P10–P12
are automatable in CI once the installer exists.

---

## 10. What changes in the repository, in order

| Step | Change | Milestone |
|---|---|---|
| 1 | Add `.dockerignore` (no `.env`, config, data, or `.git` in the build context) | ✅ now |
| 2 | Add CI: pre-commit, unit, live integration, synthetic e2e | ✅ now |
| 3 | Add `scripts/detect-host.sh` and the install phases | M6 |
| 4 | Add `versions.yml` generation + `compose.override.yml` digest pinning | M6 |
| 5a | Publish the multi-arch image and Python package | ✅ now |
| 5b | Add `versions.yml`, the bundle allowlist, cosign and offline archives | M6 |
| 6 | Add `update.sh`, `uninstall.sh`, `doctor.sh` | M6/M7 |
| 7 | Add offline archive build + `--offline` install | M6 |
| 8 | Add bundle installation tests P1–P5 on real hardware | M6/M8 |
| 9 | Publish the first public release, with `SECURITY.md` and the signing policy | M8 |

---

## 11. Open questions

| # | Question | Blocks |
|---|---|---|
| PQ-1 | Registry and release host: GHCR + GitHub Releases, or a neutral registry with GitHub only as CI? | Release workflow |
| PQ-2 | Signing identity: keyless Sigstore only, or a long-lived key for offline verification? A site with no network cannot fetch a transparency log | Bundle verification |
| PQ-3 | Is a ~2 GB offline archive acceptable, or should we publish per-architecture deltas? | Air-gapped mode |
| PQ-4 | Do we support installing as a non-root user in the `docker` group, or require root? | `install.sh` |
| PQ-5 | How long are release artefacts retained? `versions.yml` from an old release must remain resolvable for rollback | Update policy |
| PQ-6 | Is the customer expected to have a domain/reverse proxy, or is loopback-only UI the product position? | Site network requirements |

These belong in [`open-questions.md`](open-questions.md) if they remain open after
review.
