# Open-sourcing plan

The owner intends to release this publicly. That is a documentation and process
milestone, not a coding one — and it is cheap to do late and expensive to redo.

---

## 1. What open-sourcing requires

| Area | Artefacts | Milestone |
|---|---|---|
| Legal | `LICENSE`, `THIRD-PARTY-NOTICES`, trademark note | M8 |
| Contribution | `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue and PR templates | M8 |
| Security | `SECURITY.md`, secret scanning, private vulnerability reporting | M8 |
| Quality gates | CI: invariant tests, migrations, upgrade, rollback, secret scan, arm64 | M8 |
| Release | `CHANGELOG.md`, release-notes template, digest automation, tagging | M8 |
| Documentation | README, install runbook, hardware guidance, troubleshooting | M8 |
| Governance | Who decides, how ADRs are accepted, how conflicts resolve | M8 |

---

## 2. Licence

**Recommendation: Apache-2.0.** Full analysis in
[OQ-2](open-questions.md#oq-2--licence).

The deciding factor is the explicit patent grant. This project integrates with hardware
accelerators and detection models, where patent ambiguity is a real (if small) risk.
Apache-2.0 is as permissive as MIT for anyone using the software, and it also includes
an explicit contribution-licensing clause and a trademark clause.

Also required regardless of choice:

```text
THIRD-PARTY-NOTICES
  Frigate NVR — MIT — https://github.com/blakeheather/frigate
  Eclipse Mosquitto — EPL-2.0
  <monitoring tool> — <licence>
  Python dependencies — see services/requirements.lock
  YOLO models and any bundled weights — see the models section
```

| Item | Note |
|---|---|
| Trademark | The project name and logo are **not** covered by the code licence. State this explicitly |
| Bundled model weights | If any detection model is ever bundled, its licence governs it and may forbid commercial use. Currently we bundle none — Frigate provides them — but the notices file should say so |
| Contributor provenance | Consider DCO (`git commit -s`) rather than a CLA. Lowers the barrier and is sufficient for this project's scale |

---

## 3. CI gates

A release cannot be cut unless every one of these passes. They are listed in
[`lld/09`](../architecture/lld/09-upgrade-and-versioning.md#81-what-ci-must-prove-before-a-release);
the point here is that they are **requirements for being open source**, because they are
what lets a stranger trust the code.

| Gate | Verifies |
|---|---|
| Invariant tests | INV-1…INV-13, each by name and reported individually |
| Chaos tests | Kill -9 at each pipeline step: no loss, no duplication |
| Migration tests | Full chain over a snapshot of every released version |
| Upgrade tests | Previous MINOR and previous MAJOR, on a synthetic installation |
| Rollback test | `--rollback` produces a working installation |
| Determinism test | Regenerating an unchanged config is a no-op apart from the header |
| Contract gate | `api_contract_verified` == `frigate.version` |
| Digest verification | Every digest resolves and matches its tag |
| Secret scan | No credentials in the repository, images, or logs |
| Redaction test | The diagnostics bundle contains no credential values |
| arm64 build | Builds and passes a `pi-cpu` profile smoke test |
| Lint and types | `ruff` clean, `mypy --strict` on the shared library |

The invariant tests are the ones to lead with in the README. "Every safety property has
an executable test, and here is the list" is a stronger claim than any feature list, and
it is the claim that matters for a system holding someone's security footage.

---

## 4. Repository hygiene

### 4.1 Branching and review

| Rule | Reason |
|---|---|
| `main` is always releasable | Trunk-based; release tags come from `main` |
| No direct commits to `main` | Review is the mechanism that catches invariant violations |
| Every PR that changes behaviour updates docs in the same PR | A stale design doc is a wrong design doc |
| Every PR that makes a non-obvious decision adds or updates an ADR | The "why" is the most valuable thing to preserve |
| Conventional Commits | Enables automated changelogs and lets readers filter by intent |
| `git commit -s` | DCO provenance |

### 4.2 Issue templates

| Template | Required fields |
|---|---|
| Bug report | Version, host profile (from `doctor.sh`), what happened, expected, **attached diagnostics bundle** |
| Feature request | The problem, not the solution. "I cannot tell which camera is slow" beats "add a per-camera latency graph" |
| Hardware report | Works or does not, with the detection JSON. This is how the support matrix improves without us buying hardware |
| Documentation issue | Which page, what is wrong or missing |

The bug template requiring a diagnostics bundle is deliberate: it converts most reports
from a conversation into an answer. The bundle must be safe to share, which is why its
redaction is tested.

### 4.3 Labels

`bug` · `feature` · `docs` · `hardware` · `invariant` · `good first issue` · `needs
decision` · `blocked`

`invariant` marks anything touching INV-1…INV-13 and requires a second reviewer. These
are the changes that can lose somebody's footage.

---

## 5. Content that must never be in the repository

| Never | Why |
|---|---|
| Any real credential | [INV-2](../architecture/invariants.md) |
| A real camera URL, even redacted-looking | Hostnames and IPs are reconnaissance |
| Any screenshot containing a real camera view | This is somebody's home or business |
| A diagnostic bundle from a real installation | Might contain camera names, hostnames, paths, or B2 key IDs |
| Personal names, addresses, or location names in examples | Use `parents-home`, `front-door`, `192.168.1.50` |
| A `.env` in any form other than `.env.example` with empty values | Obvious, and still the most common leak |

The screenshot rule is worth emphasising. The most natural way to document a UI is to
screenshot it with real footage in it, and that is a privacy leak the author will not
notice because it looks like their own house.

---

## 6. README structure

The README is the whole first impression. Suggested order — chosen so that a reader
decides whether to care before being asked to read about architecture:

```text
1. One sentence: what it is and who it is for
2. A screenshot of the status page — with no real footage in it
3. "What you get" — the six guarantees, in plain language
4. Install, three lines
5. Hardware guidance — a short table linking to the full page
6. The safety claim — "every safety property has an executable test" + the invariant list
7. Architecture diagram, linking to the HLD
8. Cost expectation — clip backup is cheap: "roughly the cost of a coffee per month"
9. Documentation index
10. Contributing, licence, security policy
```

Item 8 is not filler. "Back up my CCTV to the cloud" sounds prohibitively expensive, and
the single most useful thing the README can do is correct that impression with a number.

---

## 7. Release process and cadence

| Aspect | Approach |
|---|---|
| Cadence | Released when ready. No schedule to keep, no calendar pressure to ship something unreviewed |
| Versioning | Semver as defined in [`lld/09`](../architecture/lld/09-upgrade-and-versioning.md) |
| Changelog | `CHANGELOG.md`, Keep a Changelog format, **mandatory breaking-changes section** (`None` if none) |
| Notes | What changed, who is affected, the migration step if any, and the rollback command |
| Tagging | `v<version>`, matching `versions.yml` |
| Digests | Automated: a scheduled job opens a PR refreshing digests for upstream patch releases. Without automation this will not happen, and stale digests mean undelivered security fixes |
| Security releases | A documented fast path: digests refreshed, patch version, notes saying it is a security release |

### 7.1 The security-patch tension

Digest pinning ([ADR-0014](../adr/0014-pinned-image-digests.md)) means an upstream CVE fix
reaches operators only through a release of ours. That is the sharpest cost of that
decision. Mitigations:

1. Automated digest-refresh PRs on a schedule, so it is a merge rather than a project
2. Patch releases made cheap — no feature content required
3. A documented manual path for an operator who must move faster than we do, with the
   exact command and a clear statement of what is untested

---

## 8. Governance

Deliberately lightweight, and stated so contributors know what to expect.

| Question | Answer |
|---|---|
| Who decides? | The maintainer, with reasoning recorded in an ADR |
| How are decisions contested? | Open an issue referencing the ADR. If it holds, say so in the ADR; if not, supersede it. The record is the point of disagreement, not the argument |
| How does someone become a maintainer? | Sustained, high-quality contribution and review. Not defined further, because defining it prematurely is worse than leaving it |
| What must never change? | The invariants. Changing one requires an ADR, an explicit statement of the data-loss risk, and a test that fails without the change |
| How is the roadmap decided? | The roadmap document, with items moving in and out as real use reveals what matters |

The invariants are the constitution. Everything else is negotiable, and saying so
explicitly prevents the most common open-source failure mode: contributors leaving
because they cannot tell what is fixed and what is open.

---

## 9. Documentation obligations

Open-sourcing means the docs are part of the product. Minimum bar for `1.0.0`:

| Document | Required |
|---|---|
| README | ✅ |
| Install runbook, verified by a second person | ✅ |
| Hardware guidance with honest limits | ✅ |
| Troubleshooting section covering the top ten failure modes | ✅ |
| `SECURITY.md`, with a stated threat model | ✅ |
| `CONTRIBUTING.md`, including how to run the tests and how to add a camera-related feature | ✅ |
| `CODE_OF_CONDUCT.md` | ✅ |
| Every design doc with no dead links and no unresolved `TODO`s | ✅ |

The threat model in `SECURITY.md` matters more than usual here. "We assume you trust
your LAN and that the host is not compromised" is a statement that sets expectations and
prevents security reports about things that are out of scope by design.

---

## 10. Sequencing

Open-sourcing is the last milestone because it is cheap to do late and expensive to redo.
But two things must happen **early**, because they cannot be added later without pain:

| Do early | Why |
|---|---|
| Choose the project name and image namespace | It appears in every command in every runbook, and in installed installations' `versions.yml` |
| Never commit a secret | Git history is permanent. This is the one mistake that cannot be undone by a later commit |

---

## 11. What would make this project worth using

A short, honest statement of what the project is competing on, for the README's own
sanity check.

| Not competing on | Competing on |
|---|---|
| Detection quality | **Not losing footage** |
| Number of features | **Being honest about whether it is working** |
| UI polish | **Being installable by a non-expert** |
| Hardware breadth | **Being understandable when it breaks** |

Every NVR claims to record video. The claim worth making is that this one will tell you
the truth about whether it is doing so, and will not quietly delete the only copy of
something you asked it to keep.
