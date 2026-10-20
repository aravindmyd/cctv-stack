# Disaster recovery

The stack keeps its state in three places: the configuration files, the SQLite database, and
the clips on disk. Knowing which of them you can rebuild — and which you cannot — decides
how much work a failure costs.

There is no single command that undoes a disaster, because there is no single disaster. What
there is: `./restore.sh` for what you backed up, and `./restore.sh clips` / `./restore.sh
rebuild` for what you did not. Between them the clip footage in B2 and the bookkeeping in
`state.db` are both recoverable — one from the bucket, the other from the bucket and the
files on disk.

## Backing up

Set a passphrase once in `.env` (generate it with `openssl rand -base64 32`), then:

```bash
./backup.sh            # writes data/backups/config-*.tar.gz.enc, state-*.db, *.manifest.csv
./backup.sh --verify   # re-reads the checksums, reopens the database, compares row counts
```

The configuration bundle is **encrypted by default**, because it contains `.env` and
`cameras.yml` — your RTSP password and B2 key. Every artefact is created `0600`.

`--verify` is the part that matters: it proves the backup opens, its checksum still matches,
and the clip count has not drifted from the live database. A backup that has silently stopped
being taken is reported as stale rather than passing.

Restoring what you backed up:

```bash
./restore.sh            # list what is available
./restore.sh state      # newest state database; asks first
./restore.sh config     # newest configuration bundle; asks first
```

The file being replaced is kept alongside as `*.pre-restore`, and an existing database is
never overwritten without confirmation.

Recovering what you maybe did not back up:

```bash
./restore.sh clips --dry-run   # how many objects, how much egress — writes nothing
./restore.sh clips             # download every object from B2 into data/clips/
./restore.sh rebuild           # reconstruct the clips table from disk + B2
```

`clips` is the footage. `rebuild` is the bookkeeping. Both are covered below.

## What survives what

| Lost | Recoverable | From where |
|---|---|---|
| Containers, images | Yes | `docker compose pull && docker compose up -d` |
| `compose.yml` | Yes | Re-download it, or keep a copy |
| `.env` and `config/cameras.yml` | Only if you backed them up | Your copy |
| `config/frigate/config.yml` | Yes | Generated from `cameras.yml` |
| `data/cctv.db` | Only if you backed it up | Your copy |
| `data/clips/` | Yes, for clips that uploaded | The B2 bucket |
| `data/clips/` for clips that never uploaded | No | Nowhere |
| `data/media/` (Frigate recordings) | No | Nowhere |
| The B2 bucket itself | Provider-side | Backblaze support, or bucket replication if you enabled it |

Configuration and clips are recoverable. Continuous recordings are not: the stack backs up
its own clips, not Frigate's recording tree. If you need those, back up `data/media/`
separately and accept that it is large.

## What to back up

```bash
mkdir -p backups

# Configuration. Small, changes rarely.
tar czf "backups/config-$(date +%F).tar.gz" .env compose.yml config/cameras.yml
chmod 600 "backups/config-$(date +%F).tar.gz"
```

The database needs care. It runs in WAL mode, so copying the file while services are
writing can produce a copy that does not open. Use SQLite's own backup:

```bash
docker compose exec -T uploader python - <<'PY'
import sqlite3
source = sqlite3.connect("/data/cctv.db")
dest = sqlite3.connect("/data/cctv.db.backup")
with dest:
    source.backup(dest)
dest.close()
source.close()
print("database copied")
PY

cp data/cctv.db.backup "backups/cctv-$(date +%F).db"
rm -f data/cctv.db.backup
chmod 600 "backups/cctv-$(date +%F).db"
```

Clips are already in B2 if the uploader is healthy. You can also copy `data/clips/`, which
is the only copy for clips whose upload has not succeeded.

A restore you have never tested is a guess. Do one now, on a spare machine or a spare
directory, while nothing is wrong.

## Rebuild on new hardware

### 1. Install the stack without starting it

Download the three files as described in [Install](install.md), but stop before
`docker compose up -d`.

### 2. Restore configuration

```bash
tar xzf backups/config-<date>.tar.gz
chmod 600 .env
```

If the new host stores data somewhere other than `./data`, set `CCTV_DATA_DIR` in `.env`
before continuing.

### 3. Restore the database

```bash
mkdir -p data
cp backups/cctv-<date>.db data/cctv.db
chmod 600 data/cctv.db
```

Do not copy a `-wal` or `-shm` file from an old machine. A database copied with SQLite's
backup API is self-contained; stale write-ahead logs beside it are not.

The database records upload state. Without it, the stack starts empty: clips already on disk
are not adopted, clips already in the bucket are not linked to anything, and cleanup has no
rows to expire. Keep this file — and if you have lost it, see [Rebuild the database from
what is left](#rebuild-the-database-from-what-is-left).

### 4. Restore clips that never uploaded

If you copied `data/clips/`, put it back:

```bash
rsync -a /path/to/clip-copy/ data/clips/
```

Clips that did upload can be downloaded from the bucket instead:

```bash
./restore.sh clips --dry-run   # lists the objects and estimates the egress; no writes
./restore.sh clips             # downloads them, skipping anything already the right size
```

Egress is billed by Backblaze, so the dry run exists: it tells you the volume before you
commit to it. The object key mirrors the local path, so a prefix download needs no manifest,
and re-running it after an interruption costs a listing rather than a second 66 GB.

Each file is written to a scratch name and renamed into place, so an interrupted run never
leaves a half-clip at a path that looks complete. A download that fails is reported by key
and does not stop the others: the point of a recovery is to get back what it can and tell
you precisely what it could not.

### 5. Start and verify

```bash
docker compose pull
docker compose up -d

docker compose ps
curl -s http://127.0.0.1:8082/health | jq '.state'
curl -s http://127.0.0.1:8083/health | jq '.state'
```

Then confirm the pieces that health cannot see: cameras online in Frigate, new clips
appearing under `data/clips/`, and the queue's `uploaded_24h` moving.

## Rebuild the database from what is left

With no usable backup, `state.db` is reconstructed from the two things that survive: the
files under `data/clips/` (each with an `.mp4.json` sidecar written beside it) and the objects
in the bucket, whose user metadata carries camera, location, start and end times and the
review id.

```bash
docker compose stop      # so the counts are stable
./restore.sh rebuild
```

Every clip lands in one of three categories, and the report gives the count of each:

| Situation | Recorded as | Why |
|---|---|---|
| Bucket object and local file both present | `UPLOADED` | Both copies are accounted for |
| Bucket object only | `UPLOADED` + `local_deleted_at` | The object is the last copy, and saying so is what stops cleanup looking for a file to delete |
| Local file only | `PENDING` | The safe guess: the uploader sends it to its deterministic key, and the provider deduplicates a repeat |

That last row is the whole design in one line. A clip whose history is unknown is *never*
recorded as already backed up. Guessing `PENDING` costs one duplicate upload; guessing
`UPLOADED` would mark a clip as safe in the cloud when it is not, which is the failure the
local-retention rule exists to prevent.

A few details worth knowing, because they are choices rather than accidents:

- **Hashes of local files are recomputed.** The sidecar's hash is not trusted, because the
  sidecar is one of the things whose accuracy is in question.
- **`uploaded_at` comes from the object, not from now.** Cloud retention expires on that
  column, so stamping the rebuild time would restart every object's 60 days.
- **A clip with no recorded start time is read from its path**, in the site's timezone, and
  the report counts how many rows that affected.
- **A clip whose camera is no longer in `cameras.yml` is left alone** and reported. The camera
  table is generated from that file, so nothing invents a camera to hold the row.
- **A file that is not a canonical clip path is reported, not adopted.** A row whose path
  cannot be read as a time cannot drive retention honestly.

It refuses a database that already has rows, because a damaged database is `./restore.sh
state`'s problem. `./restore.sh rebuild --into-existing` adds only the clips it does not
already have, and any run can be repeated safely: it skips a path that is already recorded.

## If nothing at all can be rebuilt

No backup, no bucket, no local tree — the stack will still run:

- Frigate keeps recording and publishing reviews, and the worker materialises new clips from
  them. Those work normally.
- Existing files under `data/clips/` are ignored. Nothing uploads them, nothing expires them,
  and nothing in the health report mentions them.
- Uploads that already happened are unknown to the system, so their local copies are not
  eligible for deletion.

Keep the database in your backup set anyway. It is small, it changes constantly, and it is
the part that protects your footage.

## Site decommissioned

```bash
docker compose down
```

That removes containers and leaves `data/`, `config/`, and `.env`. Before deleting them,
decide what happens to the bucket:

| Choice | Action |
|---|---|
| Keep the footage | Leave a lifecycle rule in place so objects expire on their own |
| Delete it | Delete the objects, then the bucket, in the Backblaze console |

A bucket that belongs to no running site still bills, and nobody will notice it going stale.

## If the bucket is lost but the site is fine

Local clips are unaffected. Point the uploader at a new bucket:

```bash
$EDITOR .env        # CCTV_STORAGE_BUCKET and a key scoped to it
docker compose up -d uploader
```

Clips that were already deleted locally are gone from the cloud as well; there is no copy of
those. Everything still on disk uploads to the new bucket on the next pass, including clips
that were being held because uploads had failed.

## Getting back to a known-good state

| Check | Command |
|---|---|
| Containers running | `docker compose ps` |
| Services healthy | `curl -s http://127.0.0.1:8082/health`, `:8083/health` |
| Cameras delivering frames | `/api/stats` through the Frigate container (see [Health](health.md)) |
| Clips being produced | `ls -lt data/clips/*/*/*/*/ \| head` |
| Uploads succeeding | `/admin/queue` → `uploaded_24h` |
| Nothing held past retention | cleanup `/health` → `clips_overdue_retention` |
| Configuration backed up | `tar tzf backups/config-<date>.tar.gz` |
