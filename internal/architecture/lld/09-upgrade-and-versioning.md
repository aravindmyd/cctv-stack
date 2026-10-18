# LLD 09 — upgrade and versioning

**Implements:** PRD §13, [INV-8](../invariants.md)

Moving a machine from one version to the next without losing data, and without the
operator needing to understand the internals of the upgrade.

---

## 1. Version scheme

Semantic versioning, `MAJOR.MINOR.PATCH`, applied to the *packaging* — the
repository, the scripts, and the container image — because that is the unit an
operator installs.

| Bump | Means | Examples |
|---|---|---|
| **MAJOR** | An operator action is required, or a supported path is dropped | Removing a `cameras.yml` key; a rename migration; dropping arm64; a Frigate version bump that changes our integration contract |
| **MINOR** | New capability, fully additive | A new config key with a default; a new provider; a new health check; a new optional service |
| **PATCH** | Fix, no interface change | Bug fixes, performance, wording, dependency patch bumps |

### 1.1 What counts as a breaking change

The test is not "did code change" but **"can a running installation move forward
without a human deciding something?"**

| Change | Bump | Why |
|---|---|---|
| New optional `cameras.yml` key | MINOR | The generator emits it with a default |
| New *required* `cameras.yml` key | **MAJOR** | Every existing config becomes invalid; the operator must edit |
| Removing a `cameras.yml` key | **MAJOR** | Existing configs then fail validation |
| New database column with a default | MINOR | Old code still works, new code tolerates absence |
| Renaming a database column | **MAJOR** | Requires the two-release dance |
| New storage provider | MINOR | Additive behind the adapter |
| Changing the object key layout | **MAJOR** | Existing objects become unreachable; requires migration |
| Changing a `system_events` code | MINOR *in code*, **MAJOR** *in effect* | Alert rules reference codes — see §1.2 |
| Frigate version bump | **MINOR**, **unless** the integration contract changes | See §6 |
| Default retention value change | **MINOR**, with a loud release note | Retroactive deletion of footage is not acceptable behaviour for a patch |

### 1.2 Machine codes are an API

`system_events.code` values, health `code` values, and exit codes are consumed by
alerting rules and by operators' own scripts. Adding a code is additive; **removing or
renaming one is a breaking change** and must be listed explicitly in the release
notes, with the replacement.

A release-notes template has a mandatory "Breaking changes" section with a value of
`None` if there are none. An empty section is a decision; a missing section is an
oversight.

---

## 2. `versions.yml`

The single manifest of what this version of cctv-stack is made of.

```yaml
cctv_stack: 1.0.0

schema:
  database: 7              # migrations applied; used for compatibility gates
  config: 2                # cameras.yml schema version

frigate:
  version: "0.16.1"
  api_contract_verified: "0.16.1"     # the version the integration checklist was run against
  images:
    amd64: ghcr.io/blakeblackshear/frigate@sha256:…
    arm64: ghcr.io/blakeblackshear/frigate@sha256:…
    tensorrt: ghcr.io/blakeblackshear/frigate@sha256:…
    rocm: ghcr.io/blakeblackshear/frigate@sha256:…

images:
  mosquitto: eclipse-mosquitto@sha256:…
  monitoring:  twinproduction/gatus@sha256:…
  cctv:        ghcr.io/<owner>/cctv-stack@sha256:…

python: "3.12"
```

| Field | Purpose |
|---|---|
| `schema.database` | Compatibility gate: refuse to run against a newer schema |
| `schema.config` | Detect a `cameras.yml` that predates the current key set |
| `frigate.api_contract_verified` | Records which Frigate version the checklist in the integration contract was actually run against. **A mismatch between `version` and `api_contract_verified` blocks a release** |
| Digests | Every image pinned by digest ([ADR-0014](../../adr/0014-pinned-image-digests.md)) |

`versions.yml` is generated at release time and committed. It is not hand-edited.

---

## 3. The compatibility gate

`update.sh` refuses to proceed, before touching anything, if any of these hold:

| Gate | Message |
|---|---|
| Target MAJOR > current + 1 | "Upgrading from 1.x to 3.x must go through 2.x. Run `./update.sh --to 2.4.0` first" |
| Database schema is newer than the binary | "This database was written by a newer version. Downgrade is not supported" |
| `cameras.yml` contains keys removed in the target | "`detect.region` was removed in 2.0. Run `./update.sh --migrate-config` to convert, or edit manually" |
| `cameras.yml` is missing keys required in the target | Same |
| Frigate digest changed and the contract checklist is not marked complete | Release-process gate, enforced in CI rather than at runtime |
| Insufficient disk for the backup and the migration | "Need 2 GB free for the pre-upgrade backup; 400 MB available" |

Refusing early with a specific message is the whole point. An upgrade that fails
halfway leaves a machine in a state nobody designed.

### 3.1 Version skipping

Skipping MINOR and PATCH versions within a MAJOR is safe and supported — the migration
chain is cumulative.

Skipping a MAJOR is **not** supported. Not because the migrations could not be made
compatible, but because each MAJOR has a documented operator step, and silently
running three of them in sequence is how migrations are applied without the
understanding that makes them safe.

---

## 4. `update.sh`

### 4.1 Sequence

```text
 0. PREFLIGHT
      · current version, target version, git state
      · compatibility gate (§3) — abort here if anything fails
      · warn if any clip is PENDING/FAILED_PERMANENT: "back up before upgrading"
 1. ACQUIRE
      · git fetch, checkout the target tag (never a branch tip)
      · verify the working tree is clean; refuse otherwise
 2. BACK UP
      · state.db → db-backups/pre-upgrade-<from>-to-<to>.db   (always, automatically)
      · config bundle (encrypted if possible)
 3. GENERATE
      · regenerate config from cameras.yml + the new templates
      · show a diff of generated config; require confirmation if non-trivial
 4. VALIDATE
      · full config validation
      · ask Frigate to validate the newly generated config
      · abort on failure — nothing has been changed yet
 5. PULL
      · pull every image by digest
      · verify each digest matches versions.yml
      · abort on any mismatch: a tag that moved is not our version
 6. QUISCE
      · stop clip-worker and cleanup first (stop producing and stop deleting)
      · leave Frigate and mosquitto running — recording continues throughout
      · wait for in-flight uploads to finish, up to 60s
 7. MIGRATE
      · run migrations to completion
      · verify schema version == target
      · on failure: restore the pre-upgrade backup and abort with instructions
 8. START
      · docker compose up -d
      · recreate only containers whose image digest changed
 9. VERIFY
      · every component healthy within a time-boxed window
      · Frigate API version matches versions.yml
      · camera count matches configuration
      · a synthetic check that the pipeline can move: MQTT reachable, DB writable
10. REPORT
      · version, per-component health, and any warnings
      · on failure: the exact rollback command
```

### 4.2 Why this order

| Ordering choice | Reason |
|---|---|
| Back up **before** generating | Config generation could fail; the backup should precede anything that writes |
| Validate **before** pulling | Nothing should be downloaded for an upgrade that will be rejected |
| Stop clip-worker and cleanup **first** | Stop the producer and the destroyer before the schema changes. Frigate keeps recording, so no footage is lost during the window |
| Keep Frigate running | Recording continuity is more valuable than a fully quiesced stack |
| Migrate **before** new containers start | New code expects the new schema; old code must already be stopped |
| Digest verification | A mutable tag is a supply-chain hole ([ADR-0014](../../adr/0014-pinned-image-digests.md)) |

### 4.3 What the operator sees

```text
════════════════════════════════════════════════════════════════════
 CCTV Stack — upgrade 1.2.0 → 1.3.0
════════════════════════════════════════════════════════════════════

  Compatibility      ✓ supported
  Database schema    7 → 7 (no migration)
  Config schema      2 → 2 (no changes required)
  Frigate            0.16.1 → 0.16.1 (unchanged)
  Clips pending      0                    ✓ safe to upgrade
  Free space         318 GB               ✓

  Changes to generated config:
    config/frigate/config.yml
      + detect: { stationariy_threshold: 50 }

  [1/8] Pre-upgrade backup ………………………… db-backups/pre-upgrade-1.2.0-to-1.3.0.db  ✓
  [2/8] Regenerating configuration …………………………………………………………………… ✓
  [3/8] Validating ………………………………………………………………………………………………………… ✓
  [4/8] Pulling images ……………………………………………………………………………………… 2 updated ✓
  [5/8] Quiescing workers ……………………………………………… 2 pending uploads drained ✓
  [6/8] Migrating database ……………………………………………………… schema 7 ✓ no changes
  [7/8] Starting services ……………………………………………………………………………………………… ✓
  [8/8] Verifying health ………………………………………………………………………… all components ✓

  Upgrade complete. Running version 1.3.0.
  Rollback: ./update.sh --rollback
════════════════════════════════════════════════════════════════════
```

The pre-upgrade "clips pending" line is a deliberate safety prompt. Upgrading with a
non-empty upload backlog is possible but should be a *decision*, not an accident.

---

## 5. Rollback

Supported within a MAJOR, as a documented script rather than a documented procedure.

```text
./update.sh --rollback
  1. Stop services
  2. Restore db-backups/pre-upgrade-<from>-to-<to>.db
  3. git checkout <previous tag>
  4. Restore the pre-upgrade config, if config generation changed it
  5. Pull the previous digests
  6. Start and verify
```

| Rule | Detail |
|---|---|
| Same MAJOR only | A cross-MAJOR rollback is not offered |
| Automatic backup is the enabler | "Always back up before migrating" is what makes rollback possible; it is not optional |
| Clips recorded during the window | Their rows are lost by restoring the older database; the files remain on disk. The reconciliation job reports them as `unexpected_local_file`, and a re-run of the uploader re-uploads from the files — safe by [INV-7](../invariants.md) |
| Not attempted automatically | A failed upgrade stops and prints the rollback command. Silently rolling back could itself be the wrong decision |

### 5.1 The rollback asterisk, stated honestly

Restoring the pre-upgrade database discards the rows for clips materialised during the
upgrade window. The files survive, and a reconciliation pass plus the uploader
re-uploads them. **No clip is lost**, but the operator should know that the rollback
path depends on the reconciliation machinery working — which is another argument for
keeping it healthy and tested.

---

## 6. Frigate version bumps

Frigate's version is paced separately from ours, because it is a dependency whose
behaviour we depend on precisely.

```text
Frigate release → integration contract checklist re-run → versions.yml updated
                → cctv-stack MINOR release
```

| Rule | Detail |
|---|---|
| Never a PATCH | A Frigate bump changes an external dependency; it is never "a fix" for us |
| Checklist is mandatory | Every box in [`contracts/frigate-integration.md` §9](../contracts/frigate-integration.md#9-verification-checklist-must-be-completed-before-implementation-is-approved) is re-run against the new version, and the results recorded |
| `api_contract_verified` gates release | If it does not match `frigate.version`, CI fails |
| Runtime check | If the running Frigate reports a version different from `versions.yml`, health is `DEGRADED` with `FRIGATE_VERSION_MISMATCH` — visible, not fatal |
| Operator-frozen versions | An operator can pin an older Frigate digest. The mismatch is reported and otherwise tolerated |

The runtime check being non-fatal is deliberate: an operator who pinned a version
deliberately should see a visible notice, not a system that refuses to run.

---

## 7. Configuration schema migration

```text
./update.sh --migrate-config
```

| Behaviour | Detail |
|---|---|
| Shows a unified diff | The operator sees exactly what changes in their file |
| Backs up first | `cameras.yml.pre-migration-<schema>` |
| Comment-preserving | Migrations use `ruamel.yaml` round-trip mode; comments and ordering survive. Losing an operator's comments is losing their documentation |
| Idempotent | Running it twice changes nothing |
| Never automatic | Offered, with the diff, requiring confirmation |

**Never silently rewrite `cameras.yml`.** It is the one file the operator owns. A tool
that edits it unasked — even correctly — teaches operators to fear upgrades.

---

## 8. Release process

```text
1. Update versions.yml (schema, image digests, Frigate version)
2. If the database schema changed, add a migration and run it against
   every released snapshot in CI
3. If cameras.yml changed, add a config migration
4. Update docs/ — the changelog, ADRs for any new decision, release notes
5. Update CHANGELOG.md (Keep a Changelog format, with a Breaking Changes section)
6. Tag v<version>; CI builds and pushes images
7. CI verifies the digests published to versions.yml
8. CI runs the upgrade path from the previous MINOR and the previous MAJOR
9. Publish the release with migration notes and a rollback note
```

### 8.1 What CI must prove before a release

| Gate | Detail |
|---|---|
| All invariant tests pass | INV-1…INV-13, by name |
| Migration chain on every released snapshot | Row counts and state distribution preserved |
| Upgrade from the previous MINOR | Full `update.sh` on a synthetic installation |
| Upgrade from the previous MAJOR | With the documented operator step applied |
| Rollback | `--rollback` returns a working installation |
| Deterministic config generation | Regenerating an unchanged config is a no-op apart from the header timestamp |
| Contract checksum | `api_contract_verified` matches `frigate.version` |
| Digest verification | Every digest in `versions.yml` resolves and matches its tag |
| Secret scan | No credentials in the repository, images, or logs |
| Arm64 build | The image builds and runs on arm64 (`pi-cpu` profile smoke test) |

The two upgrade gates are the ones that catch the failures that hurt: a stack that
installs perfectly and cannot be upgraded is a stack operators stop upgrading, which
is worse than one that never worked.

---

## 9. Tests

| Test | Asserts |
|---|---|
| Compatibility gate | A MAJOR version skip is refused with the intermediate-step instruction |
| Schema gate | A database newer than the binary is refused; downgrade is not attempted |
| Config key gate | A config using a removed key is refused with the migration command |
| Disk gate | Insufficient space for the backup aborts before anything is written |
| Pre-upgrade backup | A backup is always taken before migration, and its path is printed |
| Pending-clip warning | A non-empty upload backlog produces an explicit warning before upgrade |
| Frigate continuity | Recording continues throughout an upgrade (Frigate is never stopped) |
| Digest verification | A tag whose digest changed aborts the upgrade |
| Migration failure | A deliberately failing migration restores the backup and prints the rollback command |
| **INV-8** | Upgrade from every released snapshot preserves clip rows, upload state, and configuration |
| Rollback | `--rollback` restores the previous version and database and passes health checks |
| Rollback clip safety | Clips materialised during the window survive the rollback and are re-uploaded by reconciliation |
| Config migration | `--migrate-config` preserves comments, is idempotent, and shows a diff first |
| Never auto-edits | `cameras.yml` is never modified without confirmation |
| Version mismatch | A running Frigate version differing from `versions.yml` is reported as `DEGRADED` |
| Determinism | A no-change upgrade regenerates byte-identical config apart from the header |
