# Current problems

Living issues, known gaps, and things that are broken right now.

---

## 1. Port-binding tests fail in sandbox

**Impact:** 47 tests refuse to run locally when using the macOS seatbelt sandbox (ThreadingHTTPServer cannot bind).

**What fails**
- `tests/test_frigate.py` — all 36 tests
- `tests/test_health.py` — all 11 tests

**Root cause:** The sandbox does not permit the Python `http.server` module to bind to ephemeral ports.

**Fix:** Allow `sandbox.network.allowLocalBinding: true` in the Claude Code settings so the harness can start listener sockets.

**Status:** Not a code bug — tests pass in CI (no sandbox).

---

## 2. Architecture docs awaiting owner review

**Impact:** All 42 design documents are in `📐` (draft) state. No one has sign-off yet.

**What needs review**
- `architecture/hld.md` — system architecture
- `architecture/invariants.md` — 12 hard rules
- 6 contract documents
- 9 LLDs
- 7 runbooks

**Status:** Written, waiting for the project owner to review.

---

## 3. Deferred milestones

**Impact:** The product is not yet a complete solution — several capabilities are still on the roadmap.

| Milestone | What's missing | Target |
|---|---|---|
| M4 (cloud retention) | Cloud retention, reconciliation, housekeeping, force-delete, single-instance lock | After J1 certified |
| M5 (status page) | Aggregate status page and alerting | After M4 |
| M6 (installer) | One-command client install, signed bundles, detection | After M5 |
| M7 (backup) | Backup, restore, upgrade | After M6 |

---

## 4. Working tree has uncommitted changes

The following files are modified but not committed:

- `src/cctv/config.py`
- `src/cctv/config_cli.py`
- `tests/test_config.py`
- `tests/test_config_cli.py`
- `config/cameras.example.yml`
- `docs/architecture/contracts/cameras-config-schema.md`
- `docs/architecture/contracts/frigate-integration.md`
- `docs/index.md`
- `docs/runbooks/install.md`
- `docs/runbooks/rotate-credentials.md`
- `docs/runbooks/frigate-authentication.md` (new, untracked)

These are active working changes, not release-ready.

---

## Open questions

Some items from `plans/open-questions.md` remain:

- OQ-1: Project name for the Python package (already `cctv-stack` in pyproject.toml)
- OQ-2: Licence (Apache-2.0 in LICENSE)
- OQ-3: Monitoring tool (external — Uptime Kuma per ADR-0010)
- OQ-10: Recording retention budget (answered by running the probe)
- OQ-16: Pre/post-roll defaults (answered by running the probe)
