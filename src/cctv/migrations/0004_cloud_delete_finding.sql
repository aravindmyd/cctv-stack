-- Migration 0004 — allow the cloud-retention finding kind.
--
-- J2 (`lld/05` §4) records `cloud_delete_failed` when the provider accepted a DELETE but the
-- object is still there, or when the call failed outright. The row stays `UPLOADED` and is
-- retried next run, so this finding is the only durable evidence that retention is not working.
--
-- Same reasoning as 0002: the table holds only findings, nothing references it, and the CHECK is
-- what stops a typo creating a finding no query will ever find.

CREATE TABLE reconciliation_findings_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      REAL NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN (
                    'orphan_object', 'dangling_row', 'checksum_mismatch',
                    'unexpected_local_file', 'missing_local_file', 'key_mismatch',
                    'cloud_delete_failed'
                )),
    subject     TEXT NOT NULL,
    detail      TEXT,
    resolved_at REAL
);

INSERT INTO reconciliation_findings_new (id, run_at, kind, subject, detail, resolved_at)
SELECT id, run_at, kind, subject, detail, resolved_at FROM reconciliation_findings;

DROP TABLE reconciliation_findings;

ALTER TABLE reconciliation_findings_new RENAME TO reconciliation_findings;

CREATE INDEX reconciliation_open ON reconciliation_findings (resolved_at, run_at);
