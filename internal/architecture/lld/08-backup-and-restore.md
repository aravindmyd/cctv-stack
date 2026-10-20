# LLD 08 — backup and restore

> **Implementation status (2026-10-20).** §2.1 (encrypted config bundle), §2.2 (online database
> backup, reopened and checked), §2.3 (manifest CSV), §4.1 (`backup.sh` sequence) and §5
> (`--verify`, including staleness) are built in `src/cctv/backup.py`, with `./backup.sh` and
> `./restore.sh` running it in the image. §2.4 (clips tarball), cloud offsite copy (§4.2),
> scheduling (§4.3), restore drill (§5.1) and scenarios B/C (§6.2–§6.3) are **not built**.

**Implements:** PRD §14, [INV-4](../invariants.md), [INV-8](../invariants.md)

Answering the question the PRD asks in §14: what must survive, what need not, and how
do we know it actually works.

---

## 1. What must survive, and why

The PRD's distinction is correct and worth restating precisely, because the
interesting part is *why* each item is on its side of the line.

| P0 — irreplaceable | Why it cannot be rebuilt |
|---|---|
| `config/cameras.yml` | Operator knowledge: RTSP URLs, credentials, which objects each camera should detect. Nobody recreates this from memory |
| `.env` | Secrets, location identity |
| `config/app.yml` overrides | Operator tuning |
| `state.db` | **Upload state.** Whether a clip reached the cloud, how many times it failed, what the errors were. Nothing else knows this |
| `config/host.yml` | Detected hardware profile; re-detectable, but cheap to keep |

| P1 — useful, rebuildable | How to rebuild |
|---|---|
| `clips/` on disk | Download from B2 |
| `config/frigate/config.yml` and other generated files | Regenerate from `cameras.yml` |

| P2 — deliberately excluded | Why |
|---|---|
| `data/media/recordings/` | Huge. Retained briefly by design. Losing it loses recent continuous footage, which is an accepted trade |
| `data/frigate/frigate.db` | Frigate's own cache; it rebuilds |
| `data/frigate/model_cache/` | Re-downloaded |
| Docker images | Re-pulled |

### 1.1 The asymmetry that defines the design

**Clips are recoverable from the cloud. Upload state is not recoverable from
anything.**

A restore without `state.db` means we do not know which clips are already in B2. The
recovery path degrades to: list the bucket, download everything, and rebuild rows
from object metadata — recovering the *clips* but losing the history of what
happened. That is survivable once, and it is why `state.db` is the centre of the
backup strategy and why it is small enough to back up constantly.

---

## 2. Backup sets

`backup.sh` produces independent artefacts, each small and separately restorable.

### 2.1 `config-<ts>.tar.gz` — the configuration set

```text
config/
├── cameras.yml
├── app.yml
├── host.yml
└── mosquitto/mosquitto.conf
.env
versions.yml
```

| Property | Value |
|---|---|
| Typical size | < 100 KB |
| Contains secrets | **Yes** — `.env` and `cameras.yml` may both hold credentials |
| Encryption | **Required by default.** `backup.sh` refuses to write an unencrypted config backup without `--unencrypted` |
| Permissions | `chmod 600` on every output |

Encryption uses `age` if available, falling back to `openssl enc -aes-256-cbc -pbkdf2`.
The recipient key is configured once in `.env` (`BACKUP_AGE_RECIPIENT`) or supplied
per-run. If no key is configured, `backup.sh` still works but prints, in capitals, the
exact command to generate one — because the alternative is operators emailing
themselves a tarball with a B2 application key inside it.

### 2.2 `state-<ts>.db` — the state set

```bash
sqlite3 "$STATE_DB" ".backup '$OUT/state-<ts>.db'"
```

| Property | Value |
|---|---|
| Method | SQLite **online backup API** |
| Never | `cp` — copying a live WAL database produces a corrupt file, and this is the single most likely operator mistake |
| Typical size | A few MB per year of operation |
| Integrity | `.db` is written, then reopened and `PRAGMA integrity_check` run on it before the backup is considered complete |
| Contains secrets | No — checksums, paths, timestamps, and error strings only |

The integrity check on the *output* is not paranoia. A backup that cannot be opened
is worse than no backup, because it is believed.

### 2.3 `state-<ts>.manifest.csv` — the human-readable insurance

```csv
event_id,camera,start_time,path,size_bytes,sha256,upload_state,cloud_key,uploaded_at
1734567890.123456-abcxyz,front-door,1757590365.12,front-door/2026/09/11/18-32-45.mp4,7864320,3f2a…,UPLOADED,parents-home/clips/front-door/2026/09/11/18-32-45.mp4,1757590420.5
```

| Property | Value |
|---|---|
| Size | ~200 bytes per clip |
| Purpose | Readable without SQLite, diffable, greppable, and sufficient to know *what should exist* |
| Use case | The operator whose `state.db` will not open, at 2 a.m., who needs to know whether a specific clip is in the cloud |
| Restorability | Informational. `restore.sh` reads the `.db`; the manifest is for humans |

### 2.4 `clips-<ts>.tar` — optional, large

Not produced by default. `backup.sh --include-clips` creates it for the case where an
operator wants an offline copy without the cloud, or before decommissioning a site.
Documented with an explicit size warning: this is 66 GB at the reference capacity.

---

## 3. What is not backed up, and the honest consequences

| Not backed up | Consequence | Mitigation |
|---|---|---|
| `data/media/recordings/` | Recent continuous footage is lost in a total failure | Accepted and documented. The default keeps two days of it ([ADR-0022](../../adr/0022-continuous-recording-for-two-days.md)), and it is never uploaded: there is little to lose, and nothing is unbounded |
| Frigate's snapshots and review thumbnails | Frigate's UI history is lost; clips remain | The canonical clips and their sidecars carry the metadata that matters |
| Docker images | Re-pull | Digest-pinned, so the same version comes back |
| `data/uptime-kuma/` or the monitoring tool's state | Monitoring history is lost; monitors are regenerated | Acceptable; the tool is stateless from our perspective |
| The cloud objects themselves | — | The cloud *is* the backup |

**The design position:** this system is backed up by the cloud, not by tarballs. The
tarballs exist for the small, irreplaceable, non-video state. An operator who wants
their footage backed up twice should configure B2 replication, not a bigger tarball.

---

## 4. `backup.sh`

```text
./backup.sh                          config + state + manifest → ./backups/
./backup.sh --dest /mnt/usb/backups  write elsewhere
./backup.sh --include-clips          also archive data/clips (large)
./backup.sh --verify                 restore the newest backups into a temp dir and check
./backup.sh --upload                 push the bundles to B2 under <location>/backups/
./backup.sh --dry-run                report what would be produced and how large
```

### 4.1 Sequence

```text
1. Preflight: services running? DATA_DIR readable? destination writable? free space?
2. Verify state.db integrity BEFORE backing it up
     → if the source is corrupt, say so and stop; do not archive corruption silently
3. Produce state-<ts>.db via the online backup API
4. Re-open the output and run integrity_check
5. Produce state-<ts>.manifest.csv from the (read-only) database
6. Produce config-<ts>.tar.gz, redacting nothing (it needs the secrets to restore)
     → encrypt unless --unencrypted
7. chmod 600 all outputs
8. Write backups/MANIFEST with a checksum of each artefact
9. Report sizes, locations, and what each artefact is for
```

Step 8 matters. A backup directory with five similarly-named files and no index is a
guessing game during a restore.

### 4.2 Backing up to the cloud

`--upload` pushes the bundles to `<location>/backups/` in the same bucket.

| Consideration | Detail |
|---|---|
| Encrypted config bundle | Contains `.env`; uploaded only if encrypted, unless `--unencrypted` is explicit |
| Lifecycle | The site lifecycle rule is scoped to `<location>/clips/`, so backup objects are **not** auto-expired. A separate rule at `<location>/backups/` with a longer window is applied by the installer |
| Circularity | These bundles are small state, not long-term archive. Cloud backup of the cloud backup is about **host** loss, where the bucket is still reachable |

### 4.3 Scheduling

**Not scheduled by default.** Rationale: a cron job that runs unattended and silently
starts failing is the classic backup failure. Instead:

- The installer *offers* to install a systemd timer (or a documented cron entry for
  non-systemd hosts) after explaining what it will do and where output goes.
- Whatever the choice, the **freshness of the last successful backup is a health
  check**, surfaced in `/status` and alerted on if stale.

That combination gives the convenience of automation with the honesty of a monitored
result. A backup policy whose only evidence is a cron entry is not a backup policy.

---

## 5. Verification — a backup that has never been restored is a hypothesis

`backup.sh --verify`:

```text
1. Locate the newest state-<ts>.db and config-<ts>.tar.gz
2. Verify artefact checksums from backups/MANIFEST
3. Restore state.db into a temporary directory (never over the live one)
4. Run PRAGMA integrity_check and foreign_key_check
5. Assert expected tables exist and schema version matches the running binary
6. Extract the config bundle into a temporary directory
7. Assert cameras.yml parses and passes validation
8. Assert the camera list matches the live installation
9. Compare clip counts: backup vs live (within the expected drift since backup time)
10. Clean up; report
```

Step 9 is the interesting one: it detects a backup that is technically valid but
silently stale or truncated.

`--verify` is also run by `doctor.sh`, so an operator investigating any problem
incidentally learns whether their backups are real.

### 5.1 Restore drill

The runbooks include a quarterly drill: restore the newest backup onto a scratch
directory on the same host and confirm the clip list and camera list are correct. It
takes two minutes and is the only way to know the answer.

---

## 6. Restore scenarios

### 6.1 Scenario A — configuration lost, data intact

*Examples: someone deleted `cameras.yml`; a bad `app.yml` edit.*

```text
1. ./uninstall.sh                    stop services (data untouched)
2. Extract config-<ts>.tar.gz over config/ and .env
3. chmod 600 .env config/cameras.yml
4. ./install.sh --yes --regenerate
5. Verify: cameras match, health healthy
```

Data is untouched; only configuration is restored. Minutes.

### 6.2 Scenario B — new machine, full recovery

The PRD §15 scenario, and the reason the packaging-first design exists.

```text
1. Install Docker on the new machine
2. git clone <repository> cctv-stack && cd cctv-stack
3. Extract the config bundle → config/ and .env
4. ./install.sh --yes
      detects the host, generates config, validates, starts the stack
5. Stop the stack
      docker compose down
6. Restore state.db
      copy state-<ts>.db to data/state/state.db; chmod 600; remove -wal/-shm
7. docker compose up -d
8. Recover clips from B2 (if the local clips are also gone):
      ./restore.sh --clips-from-cloud
9. Verify
      ./doctor.sh
      /status must report every camera, the expected clip count, and a healthy pipeline
```

Step 8 is where the whole design pays off: because object keys are
`<location>/clips/` + the local relative path, recovery is a prefix download into
`data/clips/`. No mapping, no manifest resolution, no translation layer.

```text
restore.sh --clips-from-cloud
  1. List objects under <location>/clips/ (paginated, rate-limited)
  2. For each object:
       target = data/clips/<key minus "<location>/clips/">
       skip if present with a matching size
       else download atomically (.tmp → rename)
  3. Report: downloaded, skipped, failed
  4. Reconcile: rows in state.db with no local file → mark local_deleted_at
                files with no row → report as unexpected_local_file
```

Downloading 60 days of clips is an egress cost and takes hours. It is offered with the
volume and an estimate stated first.

### 6.3 Scenario C — `state.db` corrupted, clips intact

*Examples: power loss during a write, an accidental edit, a `cp` of a live database.*

```text
1. Stop services
2. Preserve the corrupt file as state.db.corrupt-<ts>   ← never delete evidence
3. Restore the newest backup
4. If no usable backup exists: reconstruct (below)
5. ./doctor.sh
```

**Reconstruction without a backup** (the fallback, and a good reason to have backups):

```text
1. Read state-<ts>.manifest.csv if present   → tells us what should exist
2. Otherwise enumerate data/clips/ and read each .mp4.json sidecar
3. For each clip, derive: camera, event_id, start_time, path, size, sha256
4. Ask the storage adapter whether the object exists, and its size
5. Insert rows:
      object present, local file present  → UPLOADED
      object present, local file absent   → UPLOADED + local_deleted_at
      object absent,  local file present  → PENDING  (safe: uploader will send it)
      object absent,  local file absent   → unrecoverable; report it explicitly
6. Report counts per category, and the list in the last category
```

Category 3 is safe by construction: an unknown clip is treated as *not yet uploaded*,
so the uploader re-uploads it, idempotently, to its deterministic key. This is
[INV-7](../invariants.md) doing the work it was designed for.

**Every sha256 is recomputed from the local files** rather than trusted from the
manifest, because the manifest is exactly the artefact whose accuracy is in question.

### 6.4 Scenario D — decommission

```text
1. ./backup.sh --include-clips --upload     final full copy
2. Verify with --verify and by listing the bucket prefix
3. ./uninstall.sh --purge                    requires typing the location name
4. Decide the bucket's fate explicitly:
     keep (paying for storage) → leave the lifecycle rule in place
     delete                    → ./uninstall.sh --remove-lifecycle, then delete the bucket
```

Step 4 is called out because silently leaving a bucket with lifecycle rules removed is
the worst outcome: objects that never expire and are never looked at again.

---

## 7. Health

| Check | HEALTHY | DEGRADED | UNHEALTHY |
|---|---|---|---|
| Backup freshness | < 48 h | 48 h – 7 d | > 7 d, or never |
| Backup artifact integrity | newest backup passes `--verify` | — | verification fails |
| Backup destination writable | yes | — | no |
| Cloud backup reachable (if `--upload` used) | yes | slow | unreachable |

"Never" is a distinct, reportable state. An installation that has never taken a backup
should say so rather than reporting "0 days since backup" or, worse, nothing at all.

---

## 8. Tests

| Test | Asserts |
|---|---|
| Online backup under load | A backup taken while clips are being written restores and passes integrity check |
| Live-copy rejection | The docs and doctor warn about `cp`; a test asserts `backup.sh` never uses it |
| Source integrity failure | A corrupt source database aborts the backup with an explicit message |
| Output integrity verification | A deliberately truncated `.db` fails verification and the backup is marked bad |
| Encryption required | An unencrypted config bundle is refused without `--unencrypted` |
| Secret handling | Every produced artefact is `chmod 600`; the config tarball is encrypted by default |
| Manifest accuracy | The manifest row count matches the database |
| **Scenario A** | Config restore returns a working installation with data untouched |
| **Scenario B** | Full recovery on a clean host yields correct cameras, clip count, and health |
| **Scenario B clips** | Cloud clip recovery restores every object to the path predicted by the key |
| **Scenario C** | Reconstruction without a backup classifies all four categories correctly |
| Reconstruction safety | Clips of unknown upload state become `PENDING`, never `UPLOADED` |
| Hash recomputation | Reconstruction never trusts a hash from the manifest |
| `--verify` staleness | A stale backup is detected by the clip-count comparison |
| Freshness health | An installation with no backup reports "never", not a number |
| Decommission | `--purge` requires the typed location name; lifecycle removal is explicit |
| Round-trip | backup → wipe → restore → `doctor.sh` green, over a corpus of synthetic installations |
