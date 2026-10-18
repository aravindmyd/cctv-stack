# Disaster recovery

The stack keeps its state in three places: the configuration files, the SQLite database, and
the clips on disk. Knowing which of them you can rebuild — and which you cannot — decides
how much work a failure costs.

There is no backup script yet. This page gives the commands to run yourself.

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
rows to expire. There is no reconstruction tool yet, so keep this file.

### 4. Restore clips that never uploaded

If you copied `data/clips/`, put it back:

```bash
rsync -a /path/to/clip-copy/ data/clips/
```

Clips that did upload can be downloaded from the bucket instead:

```bash
BUCKET=$(grep '^CCTV_STORAGE_BUCKET=' .env | cut -d= -f2)
LOCATION=$(grep -E '^\s*location:' config/cameras.yml | awk '{print $2}')

aws s3 cp --recursive "s3://$BUCKET/$LOCATION/clips/" data/clips/ \
  --endpoint-url "https://s3.$(grep '^CCTV_STORAGE_REGION=' .env | cut -d= -f2).backblazeb2.com"
```

Egress is billed by Backblaze, so check the volume before starting a large restore. The
object key mirrors the local path, so a prefix download needs no manifest.

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

## If the database is gone

The stack will run, but it starts with no history:

- Frigate keeps recording and publishing reviews, and the worker materialises new clips from
  them. Those work normally.
- Existing files under `data/clips/` are ignored. Nothing uploads them, nothing expires
  them, and nothing in the health report mentions them.
- Uploads that already happened are unknown to the system, so their local copies are not
  eligible for deletion.

Keep the database in your backup set. Recreating it means losing the upload and retention
state, which is the part that protects your footage.

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
