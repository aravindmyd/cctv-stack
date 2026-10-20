# Current problems

Living issues, known gaps, and things that are broken right now.

---

## 1. Port-binding tests fail in sandbox

**Impact:** the HTTP tests in `tests/test_frigate.py` and `tests/test_health.py` cannot bind a
listener under the macOS seatbelt sandbox (`ThreadingHTTPServer` is blocked).

**Root cause:** the sandbox does not permit the Python `http.server` module to bind to
ephemeral ports.

**Fix:** allow `sandbox.network.allowLocalBinding: true` in the sandboxing harness so it can
start listener sockets.

**Status:** Not a code bug, and not observed on the normal path — `uv run pytest` runs the
whole suite locally (748 passed, 18 skipped) — and CI has no sandbox.

---

## 2. Architecture docs awaiting owner review

**Impact:** the design record is written but never signed off, so code and docs are kept in
step by review rather than by an approved baseline.

**What needs review** (39 documents)
- `architecture/hld.md` — system architecture
- `architecture/invariants.md` — the hard rules
- 6 contracts in `architecture/contracts/`
- 10 LLDs in `architecture/lld/` (`00`–`09`)
- 21 ADRs in `adr/` — 0001–0019 and 0021 Accepted, 0020 Proposed

The runbooks are no longer internal documents: they are published pages under `docs/`.

**Status:** Written, waiting for the project owner to review.

---

## 3. Deferred milestones

**Impact:** The product is not yet a complete solution — several capabilities are still on the roadmap.

| Milestone | What's missing | Target |
|---|---|---|
| M4 (retention) | J1 local retention, J2 cloud retention, J3 reconciliation and J4 housekeeping are all built and run as `cctv-cleanup`; J5's full report is the remaining piece. `force-delete` (M4.13) and the lock (M4.14) are deliberately deferred — [ADR-0021](adr/0021-defer-force-delete-and-advisory-lock.md) | J5 |
| M5 (status page) | Aggregate status page and alerting | After M4 |
| M6 (installer) | One-command client install, signed bundles, detection | After M5 |
| M7 (backup) | Backup, restore, upgrade | After M6 |

---

## Open questions

Only one question is still live for the next milestone:

- OQ-3: monitoring tool — Gatus is recommended for M5, for the reason recorded in
  [ADR-0010](adr/0010-external-monitoring-tool.md).

The rest should be removed from `.private/plans/open-questions.md`: OQ-1 (the name is
`cctv-stack`, published at `github.com/aravindmyd/cctv-stack`), OQ-2 (Apache-2.0 in
`LICENSE`), OQ-10 and OQ-16 (answered by measurement and recorded in
[ADR-0015](adr/0015-clip-boundaries-and-padding.md)).
