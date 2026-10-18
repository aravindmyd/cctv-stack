# ADR-0010: Reuse an external monitoring tool for the dashboard and alerting

- **Status:** Accepted (tool selection still open — see plans/open-questions.md (moved to `.private/plans/`))
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-6](../architecture/invariants.md), PRD §11, [`lld/06`](../architecture/lld/06-health-and-monitoring.md)

---

## Context

PRD §11 asks that health be first-class, that every component expose a health state,
and that monitoring consume those states so the dashboard answers *"Is my CCTV system
actually working?"*

That requirement decomposes into two quite different pieces of work:

| Piece | Nature | Difficulty |
|---|---|---|
| **Deriving** a correct, aggregate, honest health judgement from many sources | Domain logic: cameras, backlog age, disk projection, retention headroom, staleness | Genuinely hard, and specific to this system |
| **Presenting** it: a dashboard, history, notifications to ntfy and Telegram, a status page | Generic infrastructure | Solved well by several existing tools |

The owner's guidance was explicit: *"try to utilize some libraries like Uptime Kuma as
much as possible so that we can ignore that headache… But if it's not possible to
custom build everything, feel free to use anything."* And on the dashboard: *"That's
just a monitoring dashboard… We will expose the Frigate WebUI from where we can see
the playbacks."*

So: building a UI is out of scope, and the only thing we must build ourselves is the
domain judgement.

## Decision

**We build `status-collector` — the component that derives honest, aggregate health —
and we reuse an external tool for presentation, history, and alert delivery. We do not
build a dashboard, and we do not build notification delivery.**

```text
each service /health  ─┐
Frigate /api/stats    ─┤
Frigate MQTT status   ─┼──► status-collector ──► /status (JSON + HTML)
state.db metrics      ─┤            │                └── the human answer
host disk             ─┘            └──► push heartbeats ──► external tool
                                                                    │
                                                                    └──► ntfy
                                                                         Telegram
```

Supporting decisions:

1. **Each service owns its own `/health`** and must derive `HEALTHY` rather than
   assert it ([INV-6](../architecture/invariants.md))
2. **`status-collector` owns aggregation, staleness, camera state, disk projection,
   and the generated answer sentence.** These are the parts no off-the-shelf tool can
   do, because they require knowing what a clip backlog is
3. **The external tool owns the dashboard, the status page, history, and
   notifications.** Notification delivery is configured through it, not written by us
4. **Monitoring is behind a `MonitoringAdapter` interface** so the tool is a
   configuration choice rather than an architectural one
5. **The collector pushes heartbeats** rather than being only polled, so the external
   tool can detect the collector's own death
6. **Alert policy is deliberately small** (11 alerts, defined in
   [`lld/06`](../architecture/lld/06-health-and-monitoring.md#65-alert-policy)) with a
   three-consecutive-failure threshold and mandatory recovery notifications, because
   noisy alerting produces ignored alerting

### Tool selection — recommendation, not yet final

| Requirement | Gatus | Uptime Kuma |
|---|---|---|
| Configurable from a **file we generate** | ✅ native — the product *is* a YAML file | ❌ no official REST API; socket.io only |
| HTTP + JSON/keyword conditions | ✅ | ✅ |
| Push heartbeats for dynamic monitors | ✅ | ✅ |
| ntfy + Telegram | ✅ both | ✅ both (and ~90 more) |
| Status page | ✅ | ✅ (nicer) |
| arm64 | ✅ | ✅ |
| Provisionable from an installer | ✅ trivial | ⚠️ needs a community socket.io client, or a manual import step |

**Recommendation: Gatus**, because the "configurable from a file we generate"
requirement is a structural fit with everything else in this project
([ADR-0003](0003-cameras-yml-single-source-of-truth.md)). A tool configured
imperatively through a UI creates a component whose state lives only in a UI and a
container volume — violating the packaging premise, and making threat #4 below real.

Uptime Kuma remains fully supported, at the cost of one documented manual import step.
The final choice is tracked in plans/open-questions.md (moved to `.private/plans/`).

## Consequences

### Positive

- **Weeks of UI and notification work avoided**, and the result is better than what we
  would have written: the external tools have history, uptime percentages, and
  notification retry handling already
- **Notification providers are free.** ntfy and Telegram support arrive as
  configuration, and adding Discord or email later is a config change
- **A status page for the family or staff** requires no design or development work
- **We concentrate effort where the value is**: the health derivation, which no tool
  can do for us
- **The tool is replaceable.** The adapter interface means the choice can be reversed
  without touching the health model
- **Alerting is testable in isolation** — the collector's judgement can be verified
  without involving the tool

### Negative / accepted costs

- **Another container**, with its own volume, image, version, and upgrade path
- **A hard dependency on a tool we do not control**, including its API, its image
  availability, and its maintenance. Mitigated by the adapter, but a deprecated tool
  still needs replacing
- **Provisioning is the weak link.** If the chosen tool has no official API
  (Uptime Kuma), installing it means either a socket.io client that may break between
  versions, or asking the operator to click through an import. This is the strongest
  argument for Gatus, and it is the main open question
- **Limited presentation flexibility.** An operator wanting an upload-backlog timeline
  in the dashboard will not get one. Mitigated by `/status` carrying the data and by
  `/metrics` being available for a future Grafana
- **The tool's own state is not backed up** (PRD §14 excludes it). Acceptable because
  monitors are regenerated by the installer, so losing the tool's history is a
  cosmetic loss
- **A monitoring tool can be the thing that declares the system healthy when it is
  not.** Mitigated by making `/status` — not the tool — the authoritative aggregate

### Neutral

- The external tool is not the source of truth for health; `status-collector` is. The
  tool displays and notifies
- A second monitoring tool can be added later without displacing the first

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Build our own dashboard and alerting** | Explicitly out of scope per the owner, and PRD §15 lists "Dashboard" as a deliverable rather than something to build from scratch. It is also the least differentiated part of the system |
| **Prometheus + Grafana** | Excellent for metrics and long-term trends, and far more capable. Rejected for v1 as disproportionate: two more containers, a query language, and dashboard-as-code, to answer a question that needs one screen of text. `/metrics` is implemented from the start so this can be added without rework |
| **Home Assistant as the front end** | Many operators already run it, and it integrates with Frigate natively. Rejected as a *requirement*: it would make a third-party smart-home platform mandatory for a CCTV system. Worth mentioning in the docs as an option operators may already have |
| **Glances / Netdata** | System metrics rather than service health. They would say the CPU is fine and say nothing about a clip backlog |
| **Healthchecks.io or a hosted service** | Introduces a third-party dependency and sends operational metadata off-site, contradicting the local-first posture |
| **Rely on Frigate's own UI for health** | Frigate knows about Frigate. It cannot know about upload backlog, disk projection, or retention headroom — which is most of what PRD §11 is asking for |
| **Alert by email only, using a small script** | Every provider integration becomes our code. The external tools already support ntfy and Telegram, both of which the owner selected |

## Revisit if

- The chosen tool becomes unmaintained or its image is discontinued
- Provisioning friction proves unacceptable for operators (the strongest argument for
  Gatus over Uptime Kuma)
- Presentation requirements grow beyond a status page — for example an operator wanting
  per-camera event-rate charts, which would point at Grafana reading `/metrics`
- A second config surface proves to be a real burden, which would argue for folding
  simple alerting into `status-collector` and dropping the external tool
