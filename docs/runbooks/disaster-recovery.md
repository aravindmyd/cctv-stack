# Runbook: Disaster recovery

> **Status:** 🚧 Planned.
> **Time:** 1 hour if you have backups. Days if you do not.
> **Prerequisite:** a backup you have actually restored before.

---

## Read this before you need it

**Rehearse this once, while nothing is wrong.** A backup that has never been restored is
a hypothesis. The rehearsal takes ten minutes and it is the only way to know the answer
to the question you will be asking under pressure.

```bash
./backup.sh --verify
```

Do that now, and again after any configuration change.

---

## What survives what

| Lost | Recoverable? | From where |
|---|---|---|
| Containers, images | ✅ | Re-pull and recreate |
| `config/` and `.env` | ✅ | Config backup (**must be encrypted**) |
| `cameras.yml` | ✅ | Config backup |
| `state.db` (upload state) | ✅ from backup / ⚠️ partially by reconstruction | Backup |
| `data/clips/` | ✅ | Backblaze B2 |
| `data/media/recordings/` | ❌ **(by design)** | Nowhere |
| Frigate's own DB | ❌ | Rebuilt automatically |
| The B2 bucket | ⚠️ | Provider-side; enable bucket replication if it matters |

**The honest summary:** you get your configuration and your event clips back. You do not
get continuous recordings from before the disaster, and that is an accepted trade
(PRD §14). If continuous footage matters that much to you, back up `data/media/`
separately.

---

## Scenario A — Configuration only

*Someone deleted `cameras.yml`. A bad `app.yml` edit. A failed update.*

Data is intact, so this is easy.

```bash
cd /path/to/cctv-stack
./uninstall.sh                                   # stop services; data is untouched

tar -xzf backups/config-<ts>.tar.gz -C ./         # restores config/ and .env
chmod 600 .env config/cameras.yml

./install.sh --yes --regenerate
./scripts/doctor.sh
```

**Time:** ~10 minutes.

---

## Scenario B — Full recovery onto new hardware

*The machine died. You have a replacement.*

### B.1 Provision

```bash
# Install Docker, then:
git clone <repository> cctv-stack
cd cctv-stack
```

### B.2 Restore configuration

```bash
tar -xzf /path/to/backups/config-<ts>.tar.gz -C ./
chmod 600 .env config/cameras.yml

./install.sh --yes
```

This detects the new hardware, regenerates the Frigate config, and starts the stack
against an empty database.

> **Watch the host profile.** If the new machine has different hardware, the installer
> detects that and proposes a new profile. Review it — a config restored from an Intel
> iGPU machine will not automatically work on a Raspberry Pi.

### B.3 Restore state

```bash
docker compose down

cp /path/to/backups/state-<ts>.db "$DATA_DIR/data/state/state.db"
chmod 600 "$DATA_DIR/data/state/state.db"
rm -f "$DATA_DIR/data/state/state.db"-wal "$DATA_DIR/data/state/state.db"-shm

docker compose up -d
```

Removing the `-wal` and `-shm` files is **not optional**. A stale write-ahead log beside
a restored database causes inconsistency.

Verify the restore took:

```bash
docker compose exec uploader curlie -s localhost:8082/health | jq .state
docker compose logs uploader | grep -i "integrity\|schema"
```

### B.4 Recover clips from the cloud

```bash
./restore.sh --clips-from-cloud --dry-run     # shows volume and estimated time
./restore.sh --clips-from-cloud
```

This downloads every object under `<location>/clips/` into `data/clips/`. Because the
object key mirrors the local path, recovery is a prefix download — no manifest, no
mapping.

| | |
|---|---|
| Volume | 60 days of clips. At the reference rate, ~22–44 GB |
| Time | Hours on a home connection |
| Cost | **Egress is billed.** Check the estimate before starting |
| Safety | Downloads are atomic (`.tmp` → rename); a failure leaves a re-runnable state |

If the disk cannot hold everything at once, recover the most recent 30 days first:

```bash
./restore.sh --clips-from-cloud --since 2026-08-13
```

### B.5 Verify

```bash
./scripts/doctor.sh
curl -s http://<host>:8080/status | jq '{answer, cameras: [.cameras[]|{name,state}]}'
```

Expected:

- all cameras `ONLINE`
- clip count matching the backup
- uploads resuming (oldest pending draining)
- `clips_overdue_retention: 0`

**Time:** ~1 hour, plus download time. Compare that to rebuilding without backups.

---

## Scenario C — `state.db` is corrupted

*Power loss mid-write. An accidental edit. Someone copied a live database with `cp`.*

### C.1 Preserve the evidence

```bash
docker compose down
cp "$DATA_DIR/data/state/state.db" "$DATA_DIR/data/state/state.db.corrupt-$(date +%s)"
```

**Do not delete the corrupt file.** It may still contain recoverable rows, and it tells
you what happened.

### C.2 Restore from backup

```bash
cp backups/state-<ts>.db "$DATA_DIR/data/state/state.db"
chmod 600 "$DATA_DIR/data/state/state.db"
rm -f "$DATA_DIR/data/state/state.db"-wal "$DATA_DIR/data/state/state.db"-shm
docker compose up -d
./scripts/doctor.sh
```

### C.3 If there is no usable backup — reconstruct

```bash
./restore.sh --reconstruct-from-files --dry-run
./restore.sh --reconstruct-from-files
```

This reads every sidecar `.mp4.json` under `data/clips/`, recomputes each file's hash,
asks B2 whether each object exists, and classifies every clip:

| Object in cloud | Local file | Resulting state | Safe? |
|---|---|---|---|
| ✅ | ✅ | `UPLOADED` | ✅ |
| ✅ | ❌ | `UPLOADED` + `local_deleted_at` | ✅ |
| ❌ | ✅ | `PENDING` | ✅ — the uploader re-uploads it |
| ❌ | ❌ | **unrecoverable** | Reported explicitly |

The third row is the important one, and it is safe by construction: an unknown clip is
treated as *not yet uploaded*, and the deterministic key plus `UNIQUE(review_id)` make the
re-upload idempotent. Nothing is duplicated, and nothing is skipped.

Hashes are **recomputed from the files**, never trusted from a manifest — the manifest is
exactly the artefact whose accuracy is in question.

### C.4 Accept what is genuinely gone

Anything in the fourth row is lost. `restore.sh` lists those event ids explicitly so you
know the extent, rather than discovering it later.

**Time:** 20 minutes, plus reconstruction.

---

## Scenario D — Decommissioning a site

```bash
./backup.sh --include-clips --upload      # final complete copy
./backup.sh --verify

# Confirm the cloud copy independently
aws s3 ls --recursive s3://<bucket>/<location>/clips/ | wc -l

./uninstall.sh --purge                    # asks you to type the location name
```

Then decide the bucket's fate **explicitly**:

| Choice | Action |
|---|---|
| Keep the footage | Leave the lifecycle rule in place. You continue to pay, and objects continue to expire on schedule |
| Delete everything | `./uninstall.sh --remove-lifecycle`, then delete the bucket in the console |

**Never leave a bucket with no lifecycle rule and no owner.** Objects that expire
according to no policy and are never looked at again is the worst outcome available.

---

## Scenario E — Site is intact, cloud bucket is lost

*The B2 bucket was deleted or is inaccessible. Local clips are fine.*

```bash
# 1. Suspend uploads so nothing accumulates failures
#    .env:  B2_ENABLED=false   (or CAMERA retention.upload: false)
docker compose up -d uploader

# 2. Create a new bucket, then point at it
$EDITOR .env               # B2_BUCKET=cctv-<location>-new
docker compose up -d uploader

# 3. Re-upload everything still on disk
docker compose exec uploader curlie -X POST localhost:8082/admin/retry \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
  -d '{"reason":"re-uploading after B2 bucket loss"}'
```

Clips already deleted locally are gone from the cloud too, and there is no way to recover
them. Clips still on disk — including everything held past retention because uploads had
failed — re-upload cleanly.

---

## Post-recovery checklist

| # | Check | Command |
|---|---|---|
| 1 | All components healthy | `./scripts/doctor.sh` |
| 2 | Database integrity | `docker compose exec uploader curlie -s localhost:8082/health \| jq '.checks[] \| select(.name=="state_db")'` |
| 3 | All cameras online | `/status` |
| 4 | Clips being produced | `docker compose logs --tail=50 clip-worker \| grep CLIP_MATERIALISED` |
| 5 | Uploads succeeding | `docker compose logs --tail=50 uploader \| grep UPLOAD_OK` |
| 6 | Backups working again | `./backup.sh --verify` |
| 7 | Nothing held past retention | `/status` → `clips_overdue_retention: 0` |
| 8 | Cloud key rotation considered | If credentials were exposed, [`rotate-credentials.md`](rotate-credentials.md) |

---

## What to do differently next time

Write down, in your own words, what was missing. Then fix it:

| If… | Then |
|---|---|
| You had no backup | Turn on the backup timer. See OQ-7 (tracked in `.private/plans/open-questions.md`, not in docs) |
| The backup was stale | Check `/status` → backup freshness. It should be a health check you can see |
| You did not know your B2 key | Store it in a password manager, not only in `.env` |
| You did not know which cameras existed | `cameras.yml` should be in the config backup. Verify it is |
| The restore took longer than expected | Rehearse it. It is always faster the second time |
| You lost continuous recordings you needed | Reconsider `record.mode` and whether `data/media/` deserves its own backup |

---

## Emergency contacts and resources

| Need | Where |
|---|---|
| Provider status | Backblaze status page |
| Frigate documentation | `https://docs.frigate.video` |
| This project's issues | Repository issues |
| Report a bug | Attach `./scripts/doctor.sh --bundle` output |

**Diagnostics bundle:**

```bash
./scripts/doctor.sh --bundle
# logs/doctor-<timestamp>.tar.gz
```

Credentials are redacted **by value** and the redaction is tested. It still contains
camera names and host details — skim it before posting publicly.
