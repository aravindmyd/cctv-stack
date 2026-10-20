-- Migration 0005 - `PURGED`, the state of a clip whose copies are both gone.
--
-- ADR-0023. Until J2 existed there was only ever one deletion order, so `DELETED_LOCAL` (local
-- removed; cloud copy exists) and `EXPIRED_CLOUD` (both gone) were enough. With cloud retention
-- live and `cloud_days` (60) shorter than `local_days` (90), the normal case is "cloud object
-- gone, local file still here for a month", and neither state describes it.
--
-- The two states now mean what they say - *this* copy is gone - and `PURGED` is the terminal
-- state reached when the second copy is removed:
--
--     UPLOADED --local--> DELETED_LOCAL --cloud--> PURGED
--        +-----cloud--> EXPIRED_CLOUD --local--> PURGED
--
-- The CHECK is closed, so it has to be widened before any service writes the new value, and
-- SQLite cannot alter a CHECK in place: rebuild, copy, swap - as in migrations 0002 and 0004.
-- The table definition below is 0001's, verbatim, with one value added to the CHECK.

CREATE TABLE clips_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity. The review id is Frigate's review item id and the dedup key
    -- (ADR-0016). event_ids preserves event-level searchability for a per-visit file.
    review_id           TEXT NOT NULL UNIQUE,
    event_ids           TEXT NOT NULL DEFAULT '[]',
    severity            TEXT NOT NULL CHECK (severity IN ('alert', 'detection')),
    camera              TEXT NOT NULL REFERENCES cameras (name) ON UPDATE CASCADE,
    label               TEXT,
    zones               TEXT NOT NULL DEFAULT '[]',

    -- What Frigate called the activity.
    start_time          REAL NOT NULL,
    end_time            REAL NOT NULL,

    -- What we actually got. These are the columns that make INV-14 auditable: a
    -- truncated clip, a clip with a hole, and a perfect clip are all valid MP4s that
    -- play fine and upload fine, and are otherwise indistinguishable.
    duration_s          REAL,
    requested_start_ts  REAL,
    requested_end_ts    REAL,
    requested_s         REAL,
    coverage_ratio      REAL,
    pre_roll_s          REAL,
    post_roll_s         REAL,
    max_gap_s           REAL,
    has_gaps            INTEGER NOT NULL DEFAULT 0 CHECK (has_gaps IN (0, 1)),
    has_audio           INTEGER NOT NULL DEFAULT 0 CHECK (has_audio IN (0, 1)),
    video_codec         TEXT,
    audio_codec         TEXT,
    fetch_attempts      INTEGER NOT NULL DEFAULT 0,
    degraded            INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0, 1)),
    long_clip           INTEGER NOT NULL DEFAULT 0 CHECK (long_clip IN (0, 1)),

    -- The artefact. `path` is relative to DATA_DIR, never absolute, so a restore to a
    -- different mount point still works.
    path                TEXT NOT NULL,
    sidecar_path        TEXT,
    size_bytes          INTEGER,
    sha256              TEXT,

    -- Upload state. See the contract §3 for the machine, and INV-1 for the one rule
    -- that matters most: PENDING, UPLOADING, RETRY_WAIT and FAILED_PERMANENT all hold
    -- the local file.
    upload_state        TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (upload_state IN (
                            'PENDING', 'UPLOADING', 'RETRY_WAIT', 'UPLOADED',
                            'FAILED_PERMANENT', 'SKIPPED_BY_POLICY',
                            'DELETED_LOCAL', 'EXPIRED_CLOUD', 'PURGED', 'FORCE_DELETED'
                        )),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     REAL,
    lease_owner         TEXT,
    lease_expires_at    REAL,
    last_error_class    TEXT CHECK (last_error_class IS NULL OR last_error_class IN (
                            'transient', 'auth', 'not_found', 'quota', 'checksum',
                            'unknown'
                        )),
    -- Truncated to 2000 characters on write. Never contains credentials (INV-2).
    last_error          TEXT,

    cloud_key           TEXT,
    cloud_etag          TEXT,
    cloud_size_bytes    INTEGER,
    uploaded_at         REAL,
    local_deleted_at    REAL,
    cloud_deleted_at    REAL,

    -- A pin protects Frigate's source footage until our copy is durable (INV-9).
    pinned              INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    pinned_at           REAL,

    materialised_at     REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,

    CHECK (last_error IS NULL OR length(last_error) <= 2000)
);

INSERT INTO clips_new (id, review_id, event_ids, severity, camera, label, zones, start_time, end_time, duration_s, requested_start_ts, requested_end_ts, requested_s, coverage_ratio, pre_roll_s, post_roll_s, max_gap_s, has_gaps, has_audio, video_codec, audio_codec, fetch_attempts, degraded, long_clip, path, sidecar_path, size_bytes, sha256, upload_state, attempts, next_attempt_at, lease_owner, lease_expires_at, last_error_class, last_error, cloud_key, cloud_etag, cloud_size_bytes, uploaded_at, local_deleted_at, cloud_deleted_at, pinned, pinned_at, materialised_at, created_at, updated_at)
SELECT id, review_id, event_ids, severity, camera, label, zones, start_time, end_time, duration_s, requested_start_ts, requested_end_ts, requested_s, coverage_ratio, pre_roll_s, post_roll_s, max_gap_s, has_gaps, has_audio, video_codec, audio_codec, fetch_attempts, degraded, long_clip, path, sidecar_path, size_bytes, sha256, upload_state, attempts, next_attempt_at, lease_owner, lease_expires_at, last_error_class, last_error, cloud_key, cloud_etag, cloud_size_bytes, uploaded_at, local_deleted_at, cloud_deleted_at, pinned, pinned_at, materialised_at, created_at, updated_at FROM clips;

DROP TABLE clips;

ALTER TABLE clips_new RENAME TO clips;

CREATE INDEX clips_claim_query ON clips (upload_state, next_attempt_at, id);
CREATE INDEX clips_local_retention ON clips (upload_state, start_time);
CREATE INDEX clips_cloud_retention ON clips (upload_state, uploaded_at);
CREATE INDEX clips_camera_start ON clips (camera, start_time DESC);
CREATE INDEX clips_pin_reaper ON clips (pinned, pinned_at);
CREATE INDEX clips_by_checksum ON clips (sha256);
