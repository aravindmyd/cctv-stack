-- 0003: disk-growth samples for the status collector's storage projection.
--
-- lld/06 §4 projects "days until full" from the trailing growth rate rather than from free
-- space alone, because 300 GB free is not a decision and "412 days projected" is. That needs
-- history, and there was nowhere to keep it: `component_health` stores states, not sizes.
--
-- One row per sample, keyed by the sample time so a retried write cannot duplicate history.
-- The collector prunes old rows itself; the retention here is "whatever answers a 7-day
-- window", and keeping a few weeks is cheap (three integers a minute is ~10 MB a year).

CREATE TABLE disk_samples (
    at          REAL NOT NULL PRIMARY KEY,
    free_bytes  INTEGER NOT NULL,
    clips_bytes INTEGER NOT NULL,
    media_bytes INTEGER NOT NULL
);
