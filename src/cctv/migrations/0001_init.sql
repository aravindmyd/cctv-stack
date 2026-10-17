-- Migration 0001 — the initial schema.
--
-- Forward-only. There are no down migrations: a bad migration is corrected by a new one
-- (INV-8, docs/architecture/lld/03-state-store.md §4.1).
--
-- Two properties are enforced here rather than in application code, because a constraint
-- cannot be forgotten:
--
--   * `UNIQUE(review_id)` is the idempotency mechanism. Materialising the same review
--     twice cannot produce two rows, which is what makes INV-7 structural.
--   * The `upload_state` CHECK is closed. A typo in a state name fails at write time
--     instead of creating a row that no query will ever find again.
--
-- Timestamps are `REAL` epoch seconds — an instant, which carries no timezone (INV-12,
-- ADR-0017). No column stores a rendered local time.

CREATE TABLE cameras (
    name            TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    first_seen_at   REAL NOT NULL,
    retired_at      REAL,
    config_hash     TEXT,
    updated_at      REAL NOT NULL
);

CREATE TABLE clips (
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
                            'DELETED_LOCAL', 'EXPIRED_CLOUD', 'FORCE_DELETED'
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

-- Each index serves one named query. No speculative indexes: every one costs write
-- amplification on a table that is written on every clip.
CREATE INDEX clips_claim_query ON clips (upload_state, next_attempt_at, id);
CREATE INDEX clips_local_retention ON clips (upload_state, start_time);
CREATE INDEX clips_cloud_retention ON clips (upload_state, uploaded_at);
CREATE INDEX clips_camera_start ON clips (camera, start_time DESC);
CREATE INDEX clips_pin_reaper ON clips (pinned, pinned_at);
CREATE INDEX clips_by_checksum ON clips (sha256);

CREATE TABLE upload_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id         INTEGER NOT NULL REFERENCES clips (id) ON DELETE CASCADE,
    attempt_no      INTEGER NOT NULL,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    outcome         TEXT CHECK (outcome IS NULL OR outcome IN (
                        'success', 'transient_error', 'permanent_error', 'checksum_mismatch'
                    )),
    http_status     INTEGER,
    bytes_sent      INTEGER,
    error_class     TEXT,
    error_message   TEXT,
    duration_ms     INTEGER
);

CREATE TABLE sweep_state (
    name                    TEXT PRIMARY KEY,
    watermark               REAL,
    last_run_at             REAL,
    last_success_at         REAL,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    notes                   TEXT
);

CREATE TABLE pinned_events (
    event_id    TEXT PRIMARY KEY,
    camera      TEXT NOT NULL,
    pinned_at   REAL NOT NULL,
    unpinned_at REAL,
    reason      TEXT NOT NULL CHECK (reason IN ('materialisation', 'manual')),
    state       TEXT NOT NULL CHECK (state IN ('held', 'released', 'expired', 'failed'))
);

CREATE INDEX pinned_events_open ON pinned_events (state, pinned_at);

CREATE TABLE component_health (
    component   TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN (
                    'HEALTHY', 'DEGRADED', 'UNHEALTHY', 'UNKNOWN', 'DISABLED'
                )),
    detail      TEXT,
    version     TEXT,
    observed_at REAL NOT NULL
);

CREATE TABLE health_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT NOT NULL,
    state       TEXT NOT NULL,
    observed_at REAL NOT NULL
);

CREATE INDEX health_history_subject ON health_history (subject, observed_at DESC);

CREATE TABLE reconciliation_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      REAL NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN (
                    'orphan_object', 'dangling_row', 'checksum_mismatch',
                    'unexpected_local_file'
                )),
    subject     TEXT NOT NULL,
    detail      TEXT,
    resolved_at REAL
);

CREATE INDEX reconciliation_open ON reconciliation_findings (resolved_at, run_at);

CREATE TABLE system_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    component   TEXT NOT NULL,
    -- A stable machine API: alerting rules reference these, so changing one is a
    -- breaking change.
    code        TEXT NOT NULL,
    message     TEXT,
    detail      TEXT,
    dedupe_key  TEXT
);

CREATE INDEX system_events_recent ON system_events (at DESC);
CREATE INDEX system_events_dedupe ON system_events (dedupe_key, at DESC);
