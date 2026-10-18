# ADR-0007: Event-driven clip detection via MQTT plus an API sweep

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-9](../architecture/invariants.md), [`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md), [`lld/02`](../architecture/lld/02-clip-worker.md)

---

## Context

The clip-worker must learn about completed Frigate events promptly, because Frigate
does not store event clips as files: it cuts them on demand from recording segments,
and its own retention eventually expires those segments. Once that happens, the clip
is unrecoverable by any later action.

So the worker needs a source of "what happened" that is (a) low-latency, (b) complete,
and (c) available after downtime. Candidate mechanisms:

| Mechanism | Latency | Completeness | After downtime |
|---|---|---|---|
| MQTT `frigate/events` | milliseconds | at-most-once from our perspective | missed messages are gone |
| Poll `GET /api/events` | up to the poll interval | complete for the queried window | fully recoverable |
| Watch `/media/frigate/` with inotify | instant | only sees what lands on disk | and clips do not land on disk at all |
| Read Frigate's `frigate.db` | instant | complete | couples us to a private schema |

The critical realisation is that **no single mechanism is both fast and complete**.
MQTT is fast and lossy; polling is slow and complete.

## Decision

**Use both: MQTT for latency, and a periodic HTTP sweep for correctness. The sweep is
the mechanism that guarantees no clip is lost; MQTT is an optimisation on top of it.**

```text
frigate/events (MQTT)  → immediate enqueue                       [fast path]
        +
GET /api/events?has_clip=1&in_progress=0&after=<watermark>
                       → periodic enqueue of anything missed     [correct path]
```

Supporting decisions:

- The sweep watermark is **persisted** in `state.db` and advances only after every
  event in a batch is durably recorded, so a crash causes rework, never a gap
- The sweep queries from `watermark − overlap_window` to tolerate clock skew and events
  whose `end_time` is well after their `start_time`
- Every enqueue is idempotent, keyed on `event_id`, so overlapping paths are free
- Sweep lag and retention headroom are **health signals**, not just logs
- The worker calls `POST /api/events/{id}/retain` before downloading, protecting source
  footage while the copy is made ([INV-9](../architecture/invariants.md))
- We never read Frigate's filesystem or its database

## Consequences

### Positive

- **No clip is lost to a dropped message.** A broker restart, a network blip, or
  several hours of clip-worker downtime are all recovered by the sweep
- **Low latency in the normal case**, so the pin is held briefly and Frigate disk
  pressure is minimal
- **The two mechanisms cross-check each other.** If MQTT stops delivering entirely but
  the sweep keeps finding work, sweep lag stays zero and the MQTT check reports
  `DEGRADED` — a precise, actionable signal
- **Decoupled from Frigate's internals.** We depend on documented API and MQTT
  surfaces, not on its database or file layout, both of which have changed across
  versions
- **Recoverable after long downtime**, bounded only by Frigate's recording retention —
  and that window is itself a health metric

### Negative / accepted costs

- **Two code paths to build and test.** The sweep is not optional, so it must be as
  well-tested as the fast path
- **An extra container: the MQTT broker.** It must be configured, authenticated,
  health-checked, and kept internal
- **Sweep cost.** A periodic paginated API query. Negligible at our volumes, but it is
  a recurring load on Frigate
- **The pin is an extra API call per event**, doubling the request count and adding a
  failure mode (pin fails → we deliberately produce no clip, because downloading
  without protection risks an unrecoverable loss)
- **The deadlock of the retention clock.** If the worker is down for longer than
  Frigate's recording retention, clips are lost regardless of the sweep. Mitigated by
  making headroom a first-class `UNHEALTHY` signal, and by the generated default
  keeping a 3-day Frigate recording window — but not eliminated
- Events are counted as `EXPIRED_BEFORE_FETCH` when they cannot be retrieved, which
  means the system must be honest about a loss it cannot prevent

### Neutral

- MQTT QoS and retained messages are Frigate's choices, not ours; we configure the
  broker but do not change what Frigate publishes
- The broker holds no state we must back up

## Alternatives considered

| Alternative | Why not |
|---|---|
| **MQTT only** | Fast and simple, and it would work most of the time. Rejected because "most of the time" means occasionally losing a clip with no way to detect it. Unacceptable for the system's core promise |
| **Polling only** | Simple and complete, and it removes the broker container entirely. Rejected because the poll interval directly sets how long pins are held and how much Frigate disk we withhold. A 30-second poll on a busy site means dozens of events pinned simultaneously; a 5-minute poll makes it hundreds. Latency has a direct cost here |
| **inotify on Frigate's media directory** | Fails outright: event clips are not files. Watching the recordings directory would require reimplementing the segment-to-event association that Frigate already does |
| **Read Frigate's SQLite database directly** | Would be complete and fast, and avoids both MQTT and polling. Rejected: it is a private cache with no compatibility promise, it would breach the integration contract, and a schema change would break us silently |
| **Read the `/clips` directory** | The direct consequence of an outdated understanding of Frigate. `frigate/events/cleanup.py` states plainly that mp4 clips are no longer stored there |
| **Webhooks from Frigate** | Frigate publishes to MQTT for this purpose; a webhook implementation would be a parallel mechanism with the same lossy properties as MQTT, plus a listener to run |

## Revisit if

- Frigate adds a durable, ordered event stream with delivery guarantees (for example
  an at-least-once feed), at which point the sweep becomes a much cheaper safety net or
  becomes unnecessary
- Frigate begins storing clips as files, which would change the whole timing pressure
  that motivates this design
- The broker's operational cost proves disproportionate — for example on a minimal
  Raspberry Pi deployment where a poll-only mode with a short interval would suffice.
  A `ingest_mode: mqtt_plus_sweep | sweep_only` setting would be the natural shape
