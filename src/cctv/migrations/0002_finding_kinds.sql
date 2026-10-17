-- Migration 0002 — widen reconciliation_findings.kind.
--
-- `lld/05` §5.1 documents six finding kinds. Migration 0001's CHECK accepted four: it was
-- missing `missing_local_file`, which J1 records when a row points at a file that is already
-- gone, and `key_mismatch`, J3's check that `cloud_key` still equals the derived key. J1
-- cannot be implemented faithfully without the first, and the second is the same omission.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. That is safe here because
-- the table holds only findings: nothing references it, and it has no foreign keys. The
-- alternative — loosening the constraint, or dropping it — would trade a schema guarantee for
-- convenience: the CHECK is what stops a typo creating a finding that no query will ever find
-- again.
--
-- Forward-only, per INV-8. There is no down migration; a mistake here is corrected by 0003.

CREATE TABLE reconciliation_findings_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      REAL NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN (
                    'orphan_object', 'dangling_row', 'checksum_mismatch',
                    'unexpected_local_file', 'missing_local_file', 'key_mismatch'
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
