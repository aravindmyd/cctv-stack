# LLD 03 — state store

**Implements:** [INV-4](../invariants.md), [INV-7](../invariants.md), [INV-8](../invariants.md), [INV-10](../invariants.md)
**Schema specified by:** [`contracts/database-schema.md`](../contracts/database-schema.md)

The schema lives in the contract. This document covers the *implementation*: how the
database is opened, migrated, accessed concurrently, backed up, and verified.

---

## 1. Responsibilities

| # | Responsibility |
|---|---|
| R1 | Provide one correctly-configured connection per service |
| R2 | Run migrations before serving traffic; refuse to run against a newer schema |
| R3 | Offer typed accessors — no service writes raw SQL outside this module |
| R4 | Implement the atomic claim query |
| R5 | Verify integrity at startup |
| R6 | Take pre-migration backups |
| R7 | Provide online backups for `backup.sh` |

**Non-responsibility:** business logic. This module must not know what a clip is
beyond its schema.

---

## 2. Connection management

```python
def open_db(path: Path, *, readonly: bool = False) -> Connection:
    conn = sqlite3.connect(
        f"file:{path}?mode={'ro' if readonly else 'rwc'}",
        uri=True,
        isolation_level=None,       # explicit transaction control
        timeout=5.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    return conn
```

| Pragma | Choice | Why |
|---|---|---|
| `journal_mode=WAL` | always | Concurrent readers with one writer — exactly our pattern |
| `synchronous=FULL` | always | We are a low-rate workload where durability dominates throughput. Losing a committed upload record to a power cut is unacceptable; losing 2 ms of throughput is not |
| `foreign_keys=ON` | always | Off by default in SQLite, which surprises people |
| `busy_timeout=5000` | 5 s | Wait rather than fail on a brief lock. Anything longer indicates a bug (a transaction held across I/O) |
| `wal_autocheckpoint=1000` | 1000 pages | Keeps the WAL from growing unboundedly on a read-heavy workload |

### 2.1 Read-only connections

`status-collector` and `backup.sh` open read-only (`mode=ro`). A read-only connection
cannot accidentally write, which is a structural guarantee that the observer never
mutates observed state.

### 2.2 Thread safety

`sqlite3` connections are not thread-safe across threads with `check_same_thread=False`
unless serialised. Rules:

- One connection **per thread**, held in a `threading.local`.
- `asyncio` tasks that touch the DB go through `asyncio.to_thread`, so each lands on
  a thread with its own connection.
- Never share a connection between the event loop and a worker thread.

The alternative — a connection pool — adds complexity for a workload of a few writes
per minute. One connection per thread is simpler and sufficient.

---

## 3. Transactions

```python
@contextmanager
def write_txn(conn) -> Iterator[Connection]:
    conn.execute("BEGIN IMMEDIATE")     # acquire the write lock now, not later
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
```

| Rule | Why |
|---|---|
| `BEGIN IMMEDIATE`, never deferred | A deferred transaction that starts reading and later writes can hit `SQLITE_BUSY` on upgrade and cannot be retried safely mid-transaction |
| **No network I/O inside a transaction** | A slow Frigate response would hold the write lock and stall every writer. This is the single most important rule in this document |
| Short transactions | Milliseconds, not seconds |
| One writer at a time per table | Achieved by short transactions, not by coordination |

The canonical violation to avoid:

```python
# WRONG — holds the write lock across an upload
with write_txn(conn):
    clip = claim_next(conn)
    storage.put_object(clip)          # could take 30 seconds
    mark_uploaded(conn, clip)

# RIGHT — claim, then work, then complete
clip = claim_next(conn)               # own transaction, commits
storage.put_object(clip)              # no transaction held
mark_uploaded(conn, clip)             # own transaction
```

A lint rule and a code-review checklist item exist for this, because it is easy to
write the wrong version and hard to notice until a network hiccup freezes the system.

---

## 4. Migrations

```
services/cctv/db/migrations/
├── 0001_init.sql
├── 0002_add_cloud_etag.sql
└── …
```

### 4.1 Runner

```text
1. Ensure schema_migrations exists
2. Read applied versions
3. Read the newest available version N from the migrations directory
4. If applied_max > N:
       refuse to start — exit 78 — "database schema is newer than this build"
5. For each migration in (applied_max, N]:
       a. verify its checksum against the recorded one (if recorded)
       b. take a pre-migration backup
       c. BEGIN; execute; INSERT INTO schema_migrations; COMMIT
6. On any failure: log the failing migration and the SQL error; exit 78
```

| Property | Detail |
|---|---|
| Forward-only | No down migrations. A bad migration is corrected by a new one |
| Transactional | Each migration is atomic; a partial migration cannot exist |
| Checksummed | A changed migration file is a hard error — it means someone edited history |
| Backed up | A copy goes to `db-backups/` before every migration ([INV-8](../invariants.md)) |
| Idempotent chain | Running the full chain on an empty DB produces the canonical schema |

### 4.2 Compatibility window

A stack being upgraded runs briefly with a new schema and old binaries. Rules that
make that safe:

- **Additive first.** A migration that adds a column must give it a default, so old
  code can still insert without naming it.
- **Never rename in one release.** Add the new column, write to both, migrate, drop
  the old one in a later release.
- **Never drop in the same release** that stops using a column.

`update.sh` sequences the upgrade so migrations complete before new containers start,
and old containers have already stopped (see [`lld/09`](09-upgrade-and-versioning.md)).

### 4.3 Migration testing

CI applies the full chain to a database snapshot from **every released version** and
asserts:

- the migration chain completes without error
- `PRAGMA integrity_check` and `foreign_key_check` pass
- row counts per table are unchanged except where the migration intends
- the distribution of `upload_state` is unchanged

The last two are what catch the silent data-loss migration.

---

## 5. Access layer

Services do not write SQL. Each service has a repository module exposing intent-named
functions:

```python
# clip_worker
clips.insert_materialised(conn, clip: ClipRecord) -> int
clips.exists_event(conn, event_id: str) -> bool
pins.hold(conn, event_id, camera, reason) -> None
pins.release(conn, event_id) -> None
pins.expired(conn, older_than: float) -> list[PinRecord]

# uploader
clips.claim_batch(conn, worker: str, batch: int, lease_s: int) -> list[ClipRecord]
clips.mark_uploaded(conn, clip_id, etag, size) -> None
clips.mark_retry(conn, clip_id, error_class, error, next_at) -> None
clips.mark_permanent(conn, clip_id, error_class, error) -> None
clips.promote_retries(conn, now: float) -> int
clips.release_leases(conn, worker: str) -> int

# cleanup
clips.expired_local(conn, before: float) -> Iterator[ClipRecord]
clips.expired_cloud(conn, before: float) -> Iterator[ClipRecord]
clips.mark_local_deleted(conn, clip_id, at) -> None
clips.mark_cloud_deleted(conn, clip_id, at) -> None

# status
status.summary(conn) -> StatusSummary
```

Rules:

- Every function is **intent-named**, not mechanism-named. `claim_batch`, not
  `update_clips_where_state_pending`.
- Every mutating function takes `conn` explicitly. No module-level connection.
- State transitions are validated in Python against the allowed-transition table
  before the SQL runs. An illegal transition raises `IllegalStateTransition` and is
  logged `CRITICAL` — it means a logic bug or external tampering.
- No `SELECT *`. Columns are listed, so a schema addition cannot change behaviour.

---

## 6. The claim query

The most correctness-critical piece of SQL in the system. Reproduced from the DB
contract with implementation notes:

```python
def claim_batch(conn, *, worker: str, batch: int, lease_s: float) -> list[ClipRecord]:
    now = utcnow()
    with write_txn(conn):
        rows = conn.execute("""
            UPDATE clips
               SET upload_state     = 'UPLOADING',
                   lease_owner      = ?,
                   lease_expires_at = ?,
                   attempts         = attempts + 1,
                   updated_at       = ?
             WHERE id IN (
                   SELECT id FROM clips
                    WHERE upload_state = 'PENDING'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY start_time ASC
                    LIMIT ?
             )
            RETURNING id, camera, path, sidecar_path, sha256, size_bytes,
                      attempts, event_id, start_time
        """, (worker, now + lease_s, now, now, batch)).fetchall()
    return [ClipRecord(**row) for row in rows]
```

| Property | Mechanism |
|---|---|
| No double-claim | `UPDATE` inside a write transaction; SQLite serialises writers |
| Crash recovery | `lease_expires_at` in the past makes a row reclaimable |
| Bounded backlog age | `ORDER BY start_time ASC` — oldest first |
| Attempt counting | Incremented at claim, not at failure, so a kill still counts as an attempt |
| Expired-lease reclaim | Runs as part of the same claim via a union, or as a separate periodic statement |

Expired-lease reclaim (separate, cheap, every 60 s):

```sql
UPDATE clips SET upload_state = 'PENDING', lease_owner = NULL, lease_expires_at = NULL
 WHERE upload_state = 'UPLOADING' AND lease_expires_at < :now;
```

No attempt is refunded. An attempt that was killed mid-flight may still have uploaded
an object, so counting it is the conservative choice.

---

## 7. Integrity and startup checks

Run at startup, before serving traffic:

| Check | Failure action |
|---|---|
| File exists and is readable | exit 75 (`EX_TEMPFAIL`) — a fresh install may create it |
| `PRAGMA integrity_check` == `ok` | exit 78, `DB_INTEGRITY_FAILED`, `UNHEALTHY` |
| `PRAGMA foreign_key_check` empty | log `CRITICAL`; continue (do not block ingest on a referential oddity) |
| `schema_migrations` applied ≤ available | exit 78 if newer |
| No row in `UPLOADING` with a `lease_expires_at` more than 24 h in the past | log `WARNING`, reclaim them |
| Every `clips.path` resolves within `DATA_DIR` | log `CRITICAL` for any that do not — a path traversal or a corrupted row |

That last check is a security control as well as a correctness one: a `path` value
from a corrupted or tampered database must never be used to unlink a file outside
`DATA_DIR`.

### 7.1 Path safety

Every consumer of `clips.path` resolves it as:

```python
def resolve_clip_path(data_dir: Path, rel: str) -> Path:
    p = (data_dir / "data" / "clips" / rel).resolve()
    if not p.is_relative_to((data_dir / "data" / "clips").resolve()):
        raise PathEscapeError(rel)
    return p
```

Applied before every read and every unlink. The cleanup service depends on this: it
is a deletion path driven by database content, so it must not trust that content.

---

## 8. Backup and restore

### 8.1 Online backup

```bash
sqlite3 "$STATE_DB" ".backup '$BACKUP_PATH'"
```

The online backup API takes a consistent snapshot while writers continue. It is
**not** `cp` — copying a live WAL database produces a corrupt file, and this is the
single most likely operator mistake, which is why `backup.sh` exists rather than
documentation telling people to copy a file.

### 8.2 Restore

```text
1. Stop all services
2. Preserve the current state.db as state.db.corrupt-<ts> (never delete evidence)
3. Remove state.db, state.db-wal, state.db-shm
4. Copy the backup into place
5. chmod 600
6. Start services; each runs integrity_check
7. Verify: clip count, pending count, oldest pending, camera list
```

Steps 2 and 7 are the ones people skip and regret.

### 8.3 Backup rotation

Keep the newest 10 in `db-backups/`. Retention is enforced by cleanup, not by the
backup script, so a failed backup never silently prunes the only good copy.

---

## 9. Testing

| Test | Asserts |
|---|---|
| Claim atomicity | 8 concurrent workers × 100 clips → each clip claimed exactly once |
| Lease expiry | A killed worker's lease is reclaimed within 2 × the reclaim interval |
| Illegal transition | `UPLOADED → UPLOADING` raises and logs CRITICAL |
| Migration chain | Full chain on empty DB == canonical schema (compared by schema dump) |
| Migration on snapshots | Every released snapshot migrates with row counts and state distribution preserved |
| Newer schema refused | A DB with a future schema version exits 78 |
| Checksum mismatch | An edited migration file is a hard error |
| Pre-migration backup | A backup file is written before every migration |
| Integrity check | A deliberately corrupted DB is detected and refused |
| Path escape | A `../` path in `clips.path` raises rather than unlinking outside `DATA_DIR` |
| Online backup | A backup taken during active writes restores and passes integrity check |
| `cp` rejection | Documentation and the doctor script warn about copying a live DB |
| No I/O in transactions | Static analysis / lint catches network calls inside `write_txn` |
| Read-only enforcement | A `mode=ro` connection cannot write, even when asked |
