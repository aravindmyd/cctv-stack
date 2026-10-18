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

**What needs review** (38 documents)
- `architecture/hld.md` — system architecture
- `architecture/invariants.md` — the hard rules
- 6 contracts in `architecture/contracts/`
- 10 LLDs in `architecture/lld/` (`00`–`09`)
- 20 ADRs in `adr/` — 0001–0019 Accepted, 0020 Proposed

The runbooks are no longer internal documents: they are published pages under `docs/`.

**Status:** Written, waiting for the project owner to review.

---

## 3. Deferred milestones

**Impact:** The product is not yet a complete solution — several capabilities are still on the roadmap.

| Milestone | What's missing | Target |
|---|---|---|
| M4 (retention) | J1 local retention is built and runs as `cctv-cleanup`; missing: `force-delete` (M4.13), the single-instance lock (M4.14), then cloud retention (J2), reconciliation (J3) and housekeeping (J4) | M4.13/14 open |
| M5 (status page) | Aggregate status page and alerting | After M4 |
| M6 (installer) | One-command client install, signed bundles, detection | After M5 |
| M7 (backup) | Backup, restore, upgrade | After M6 |

---

## 4. No 0.3.1 yet

Two defects are fixed in the tree but unreleased, so the published site works around them:

- `cctv-config validate` exits **1** on an invalid config, while M6.5 and [INV-11](architecture/invariants.md)
  require **78**.
- The published `0.3.0` image predates the top-level `frigate:` key in `cameras.yml` and
  rejects the current `config/cameras.example.yml`, so `docs/install.md` pins `:edge` rather
  than a version.

Cutting `0.3.1` closes both.

---

## Open questions

Only one question is still live for the next milestone:

- OQ-3: monitoring tool — Gatus is recommended for M5, for the reason recorded in
  [ADR-0010](adr/0010-external-monitoring-tool.md).

The rest should be removed from `.private/plans/open-questions.md`: OQ-1 (the name is
`cctv-stack`, published at `github.com/aravindmyd/cctv-stack`), OQ-2 (Apache-2.0 in
`LICENSE`), OQ-10 and OQ-16 (answered by measurement and recorded in
[ADR-0015](adr/0015-clip-boundaries-and-padding.md)).
