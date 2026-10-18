# Contract: service contracts

**Parties:** clip-worker, uploader, cleanup, status-collector → each other, the
monitoring stack, and the operator

Every service in this stack behaves identically with respect to health, logging,
configuration, and lifecycle. This document is that shared behaviour. It exists so
that adding a fifth service is copying a pattern rather than inventing one.

---

## 1. Health endpoint

### 1.1 Endpoint

```http
GET /health          →  application/json
GET /health?strict=1 →  same body, stricter status code
```

### 1.2 Response schema

```jsonc
{
  "component": "uploader",              // stable identifier
  "version": "1.0.0",                   // cctv-stack version
  "state": "DEGRADED",                  // HEALTHY | DEGRADED | UNHEALTHY | UNKNOWN | DISABLED
  "since": 1757673600.0,                // when this state began (UTC epoch)
  "observed_at": 1757677200.0,          // when this response was produced
  "uptime_s": 3600,
  "checks": [
    {
      "name": "state_db",               // stable check name
      "state": "HEALTHY",
      "code": null,                     // stable machine code when not HEALTHY
      "detail": "read/write ok, 412 clips",
      "latency_ms": 2,
      "observed_at": 1757677200.0
    },
    {
      "name": "storage",
      "state": "DEGRADED",
      "code": "STORAGE_STALE",
      "detail": "last successful upload 42m ago (threshold 30m); 3 transient errors in window",
      "latency_ms": 1201,
      "observed_at": 1757677200.0
    }
  ],
  "summary": "3 clips pending, oldest 42m; storage last OK 42m ago"
}
```

### 1.3 Derivation rule

`state` is **derived**, never assigned ([INV-6](../invariants.md)):

```text
DISABLED    if every check is DISABLED (component deliberately off)
UNHEALTHY   if any check is UNHEALTHY
DEGRADED    if any check is DEGRADED
HEALTHY     if every check is HEALTHY or DISABLED
UNKNOWN     if no check has been evaluated yet
```

Two refinements the list above only implies, both learned from implementing it:

* **A `DISABLED` check is neutral, not disqualifying.** A component with a deliberately-off
dependency — cloud backup not configured, for instance — is neither all-`HEALTHY` nor
all-`DISABLED`, and a literal reading would fall through to `UNKNOWN`, which strict mode
renders as `503`. Docker would then mark a perfectly intentional deployment permanently
unhealthy.
* **`UNKNOWN` is a `503` in both modes.** The status-code table below does not list it, which
is an omission rather than a licence: "we cannot tell" must never read as `200`, or a
component that has never evaluated a single check looks fine to every monitor.

### 1.4 HTTP status codes

| Condition | Default | `?strict=1` |
|---|---|---|
| `HEALTHY` | `200` | `200` |
| `DEGRADED` | `200` | `503` |
| `UNHEALTHY` | `503` | `503` |
| `DISABLED` | `200` | `200` |
| `UNKNOWN` | `503` | `503` |

`DEGRADED` returning `200` by default is deliberate: a backlog is worth surfacing
but is not a reason for Docker to consider the container dead. Monitoring uses
`?strict=1` when it wants a binary signal.

### 1.5 Mandatory checks per component

| Component | Checks |
|---|---|
| clip-worker | `state_db`, `frigate_api`, `mqtt`, `clips_dir_writable`, `retention_headroom`, `sweep_lag`, `clip_quality`, `clip_gaps`, `flush_pressure` |
| uploader | `state_db`, `storage_reachable`, `storage_credentials`, `local_files_readable`, `upload_staleness`, `backlog_age`, `failed_permanent`, `upload_verification` |
| cleanup | `state_db`, `clips_dir`, `storage_reachable`, `last_run`, `clips_overdue_retention` |
| status-collector | `state_db`, `frigate_api`, `mqtt`, `collector_freshness` |

> **Implementation note (v0.4.0, J1 only).** The `cleanup` service implements `state_db`,
> `clips_dir`, `last_run` and `clips_overdue_retention`, plus a `disk_free` check from `lld/05`
> §9. It reports `storage_reachable` as **`DISABLED`** — cloud retention (J2) does not exist
> yet, and an unimplemented check must never read as a check that passed. `DISABLED` is neutral
> in `derive_state`, so the gap is stated without making the component look broken. The
> `POST /admin/force-delete` route below is likewise not built; M4.13 and the single-instance
> lock (M4.14) are deliberately deferred by
> [ADR-0021](../../adr/0021-defer-force-delete-and-advisory-lock.md).

### 1.6 Mandatory machine codes

Codes are a **stable API**: alerting rules reference them. Changing or removing one
is a breaking change requiring a migration note.

| Code | Meaning |
|---|---|
| `DB_UNAVAILABLE` | Cannot open or query `state.db` |
| `DB_INTEGRITY_FAILED` | `PRAGMA integrity_check` failed |
| `FRIGATE_UNREACHABLE` | HTTP connection failed |
| `FRIGATE_AUTH_FAILED` | `401`/`403` from Frigate |
| `FRIGATE_VERSION_MISMATCH` | Running version ≠ `versions.yml` |
| `MQTT_DISCONNECTED` | Broker connection lost |
| `SWEEP_LAG_HIGH` | Event sweep watermark behind by more than the threshold |
| `RETENTION_HEADROOM_LOW` | Unmaterialised events close to Frigate's recording expiry |
| `STORAGE_UNREACHABLE` | Cannot reach the storage endpoint |
| `STORAGE_AUTH_FAILED` | Storage rejected our credentials |
| `STORAGE_STALE` | No successful upload within the window |
| `BACKLOG_AGING` | Oldest pending clip exceeds the threshold |
| `UPLOAD_FAILED_PERMANENT` | At least one clip will not retry |
| `UPLOAD_LOCAL_FILE_MISSING` | A clip in a held state has no file on disk — the database and the disk disagree |
| `UPLOAD_UNVERIFIED` | An upload completed but no provider checksum could be compared against the local file. An unverified backup is not a backup |
| `DISK_PRESSURE` | Free space below the warning threshold |
| `DISK_CRITICAL` | Free space below the critical threshold, or projected to fill within N days |
| `CLIPS_OVERDUE_RETENTION` | Clips held past retention because uploads have not succeeded |
| `CLIP_COVERAGE_LOW` | Clips materialised below the coverage tolerance — they are shorter than requested ([INV-14](../invariants.md)) |
| `CLIP_HAS_GAPS` | Clips containing a hole from a missing recording segment |
| `CLIP_FLUSH_PRESSURE` | Mean `fetch_attempts` above tolerance: the flush grace period is too short for this host |
| `CLEANUP_STALE` | Cleanup has not completed a run within its interval |
| `CAMERA_OFFLINE` | A camera's role status is `offline` beyond the grace period |
| `CAMERA_QUIET` | A camera is online but has stopped producing clips, compared with its own recent history |
| `COLLECTOR_STALE` | The status collector has not completed a collection within its own cadence |
| `DETECTION_FALLING_BEHIND` | Frigate's inference speed exceeds the detection interval |

### 1.7 Staleness windows

A component that has not reported within its window is `UNKNOWN`, regardless of its
last claim.

| Component | Window | Rationale |
|---|---|---|
| Frigate | 60 s | Heartbeats frequently |
| clip-worker | 120 s | |
| uploader | 300 s | Legitimately idle when the queue is empty |
| cleanup | 1 h | Runs on a slow cadence |
| status-collector | 120 s | |

> **Idle is not unhealthy.** A component with nothing to do reports `HEALTHY` with
> checks that say "no work pending". An uploader with zero pending clips and a
> recent successful upload is healthy. `HEALTHY` must never depend on there being
> work.

---

## 2. Aggregate status endpoint

Served by `status-collector` only, at `GET /status`.

> **Implementation note (v0.5.0, slice 1).** The collector, the JSON payload, the HTML page
> and `/metrics` are built and tested. It is published on `0.0.0.0:8084` on purpose — the page
> is meant to be opened from a phone on the LAN, it exposes camera names and states but no
> credentials, and it mounts no admin route. **Not built:** the `MonitoringAdapter`, the
> generated Gatus/Kuma configuration, push heartbeats, and alert delivery (§6 of
> [`lld/06`](../lld/06-health-and-monitoring.md)). `components[].observed` is an addition to
> the example below: `false` means the collector could not reach that source, which is how
> partial observation is stated rather than hidden.

### 2.1 JSON

```jsonc
{
  "site": "parents-home",
  "version": "1.0.0",
  "generated_at": 1757677200.0,
  "overall": "DEGRADED",
  "overall_code": "STORAGE_STALE",
  "answer": "Recording is working. Cloud backup is 42 minutes behind.",
  "components": [
    { "name": "frigate",        "state": "HEALTHY",  "summary": "running 3d 4h" },
    { "name": "clip-worker",    "state": "HEALTHY",  "summary": "last clip 6m ago" },
    { "name": "uploader",       "state": "DEGRADED", "summary": "3 pending, oldest 42m" },
    { "name": "cleanup",        "state": "HEALTHY",  "summary": "last run 12m ago" },
    { "name": "storage",        "state": "DEGRADED", "summary": "last success 42m ago" },
    { "name": "mosquitto",      "state": "HEALTHY",  "summary": "connected" }
  ],
  "cameras": [
    { "name": "front-door", "state": "ONLINE",  "last_clip_at": 1757676840.0, "detection_fps": 5.1 },
    { "name": "garage",     "state": "ONLINE",  "last_clip_at": 1757670000.0, "detection_fps": 4.8 },
    { "name": "backyard",   "state": "OFFLINE", "last_clip_at": 1757640000.0, "detection_fps": 0.0,
      "offline_since": 1757660000.0 }
  ],
  "storage": {
    "data_dir": "/opt/cctv",
    "total_bytes": 500107862016,
    "free_bytes": 318342512640,
    "clips_bytes": 71082172416,
    "projected_days_to_full": 412,
    "oldest_clip_at": 1755000000.0
  },
  "backlog": {
    "pending": 3,
    "uploading": 1,
    "retry_wait": 0,
    "failed_permanent": 0,
    "skipped_by_policy": 12,
    "uploaded_24h": 214,
    "oldest_pending_age_s": 2520,
    "clips_overdue_retention": 0
  },
  "retention": {
    "local_days": 90,
    "cloud_days": 60,
    "next_cleanup_at": 1757680800.0
  }
}
```

### 2.2 HTML

`GET /status` negotiates content type. A browser gets a one-screen HTML page
answering the PRD's question in the first line, with the details below it. It is
**read-only** and has no controls — it is a status page, not a control panel.

Layout requirement, in order:

```text
┌──────────────────────────────────────────────────────────────┐
│  CCTV — parents-home                    ● DEGRADED           │
│                                                              │
│  Recording is working. Cloud backup is 42 minutes behind.    │
│                                                              │
│  Cameras    front-door ●  garage ●  backyard ○              │
│  Storage    296 GB free · 66 GB clips · 412 days projected   │
│  Backup     3 pending, oldest 42m · last success 42m ago     │
│  Retention  90d local / 60d cloud · next run in 14m          │
│                                                              │
│  Frigate UI  →  https://host:8971                            │
│  Monitoring  →  http://host:3001                             │
└──────────────────────────────────────────────────────────────┘
```

The `answer` field is the headline. It must be generated from state, not templated:
"Recording is working" requires that every camera is `ONLINE` and Frigate is
`HEALTHY`. A status page that says "OK" while a camera is dead is the exact failure
this project exists to prevent.

### 2.3 Overall state derivation

```text
UNHEALTHY  if any camera is offline beyond grace, or any component is UNHEALTHY,
           or backup has not succeeded within the critical window
DEGRADED   if any component is DEGRADED, or any camera is offline within grace,
           or a camera that normally produces clips has gone quiet
UNKNOWN    if the collector cannot observe something it needs
HEALTHY    otherwise
```

---

## 3. Metrics

`GET /metrics` on every service, Prometheus text exposition format. Optional in v1
(nothing scrapes it by default) but implemented from the start so that adding
Prometheus later is a config change.

| Metric | Type | Labels |
|---|---|---|
| `cctv_component_state` | gauge | `component`, `state` (1 = current) |
| `cctv_clips_total` | counter | `camera`, `label` |
| `cctv_clips_pending` | gauge | `camera` |
| `cctv_clip_materialise_seconds` | histogram | `camera`, `outcome` |
| `cctv_upload_seconds` | histogram | `outcome` |
| `cctv_upload_bytes_total` | counter | `outcome` |
| `cctv_upload_failures_total` | counter | `error_class` |
| `cctv_disk_free_bytes` | gauge | — |
| `cctv_disk_projected_days_to_full` | gauge | — |
| `cctv_oldest_pending_age_seconds` | gauge | — |
| `cctv_sweep_lag_seconds` | gauge | — |
| `cctv_retention_headroom_seconds` | gauge | — |
| `cctv_camera_online` | gauge | `camera` |

Cardinality rule: labels are **bounded sets only**. Never a label per event id or
per clip path.

---

## 4. Admin endpoints

Internal-network-only, and additionally protected by a shared token
(`INTERNAL_API_TOKEN` in `.env`, generated at install). Bound to the internal
compose network, never published.

| Service | Endpoint | Purpose |
|---|---|---|
| uploader | `GET /admin/queue` | Dump the queue summary and the oldest N pending/failed clips |
| uploader | `POST /admin/retry` | Reset `FAILED_PERMANENT` → `PENDING` for a filter (`clip_id`, `camera` or `since`, plus a mandatory `reason`) |
| uploader | `POST /admin/verify` | Stream-verify one uploaded clip against its `sha256`. **Not implemented** — it is the only operation here that costs egress. `aws s3api head-object` answers the same question from the object's own metadata |
| cleanup | `POST /admin/force-delete` | Delete a specific un-uploaded clip. Body: `{clip_id, reason}`. Audited |
| clip-worker | `POST /admin/rematerialise` | Re-fetch one event by id (used after a transient Frigate failure) |
| clip-worker | `POST /admin/unpin-all` | Release all held pins (emergency disk relief in Frigate) |

**Every admin endpoint requires an explicit reason string** for destructive actions.
The reason is written to `system_events`. "Because I clicked the button" is not a
reason; `recover-failed-uploads.md` tells the operator what to write.

No admin endpoint is exposed on a published port. There is no web UI for them.

---

## 5. Logging

### 5.1 Format

One JSON object per line to stdout. Docker captures it; nothing writes log files by
default.

```json
{"ts":"2026-09-12T10:14:22.481Z","level":"INFO","component":"uploader","code":"UPLOAD_OK","msg":"uploaded clip","camera":"front-door","clip_id":8123,"event_id":"1734567890.123456-abcxyz","bytes":7864320,"duration_ms":1841}
```

| Field | Required | Notes |
|---|---|---|
| `ts` | ✅ | RFC 3339 UTC, millisecond precision |
| `level` | ✅ | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `component` | ✅ | |
| `msg` | ✅ | Human sentence, no structured data smuggled into it |
| `code` | — | Stable machine code, matching `system_events.code` where applicable |
| context | — | `camera`, `event_id`, `clip_id`, `attempt`, `duration_ms` |

### 5.2 Prohibited in logs

Never logged at any level: credentials, tokens, full RTSP URLs (they contain
credentials — log `rtsp://host:554/path` with userinfo stripped), `.env` contents,
full cloud object listings, or health payloads containing tokens.

A redaction layer strips anything matching configured secret values from log
records before emission. Tests assert that the fixture credentials never appear in
captured log output.

### 5.3 Levels

| Level | Use |
|---|---|
| `DEBUG` | Per-event detail; off unless `LOG_LEVEL=DEBUG` |
| `INFO` | State transitions, one line per clip materialised and per clip uploaded |
| `WARNING` | Recoverable, unexpected: a dropped MQTT message, an unknown camera |
| `ERROR` | An operation failed but the service continues |
| `CRITICAL` | The service cannot do its job, or an invariant is at risk |

Routine success is not `WARNING`. An operator filtering for `WARNING` must see only
things worth looking at.

---

## 6. Configuration loading

Every service, at startup, in this order:

```text
1. Read /etc/cctv/app.yml                    → application defaults
2. Apply environment overrides               → .env / compose environment
3. Read /etc/cctv/cameras.yml                 → camera definitions
4. Validate                                  → abort on error (exit 78)
5. Log the effective configuration           → with secrets redacted
6. Open the database, run migrations         → abort on failure (exit 75)
7. Begin serving /health immediately          → UNHEALTHY with a reason
8. Reconcile, then start work                 → become HEALTHY only when actually ready
```

**Step 7 before step 8 matters.** A service that is coming up must be *visible* as
unhealthy, with a reason, rather than absent. Otherwise an operator sees "container
not started" and has nothing to go on.

### 6.1 Environment variables honoured by all services

| Variable | Purpose | Default |
|---|---|---|
| `LOG_LEVEL` | Log verbosity | `INFO` |
| `DATA_DIR` | Host data root, for path resolution | `/data` in-container |
| `TZ` | Presentation timezone; storage stays UTC | from `site.timezone` |
| `STATE_DB_PATH` | Database location | `/data/state/state.db` |
| `INTERNAL_API_TOKEN` | Admin endpoint auth | none (admin endpoints refuse to start without it) |
| `HEALTH_STALENESS_S` | Override the staleness window | per component |
| `DRY_RUN` | Log intended actions without performing them | `false` |

`DRY_RUN=1` must be honoured by every writer, including cleanup and the uploader.
It exists so an operator can answer "what would this delete?" without finding out.

---

## 7. Lifecycle, signals, and exit codes

| Signal | Behaviour |
|---|---|
| `SIGTERM` | Stop accepting new work; finish the current unit of work (one clip upload / one materialisation); release leases cleanly; exit `0`. Grace period 30 s |
| `SIGINT` | Same as `SIGTERM` |
| `SIGHUP` | Reload `cameras.yml`. Validate first; on failure keep the old config and log `ERROR` |

Docker `stop_grace_period: 30s`. A service killed mid-upload must leave no lease
held: leases expire naturally, but a clean release means the retry is immediate
rather than delayed.

| Exit code | Meaning | Restart policy behaviour |
|---|---|---|
| `0` | Clean shutdown | Do not restart |
| `1` | Unhandled internal error | `on-failure` restarts, bounded |
| `75` (`EX_TEMPFAIL`) | Critical dependency unavailable at startup; retrying may help | `on-failure` restarts, bounded |
| `78` (`EX_CONFIG`) | Invalid configuration; retrying will not help | `on-failure` restarts, bounded, and the log says exactly what to fix |

**Do not loop forever on a configuration error.** `restart: on-failure:5` means a
misconfiguration produces five attempts and then a visible `Exited` container with a
specific error, rather than a permanent restart storm that hides the cause.

---

## 8. Version reporting

Every service reports `version` in `/health`, sourced from a single build-time value.
`status-collector` compares all reported versions and reports `DEGRADED` with
`FRIGATE_VERSION_MISMATCH` (or an equivalent) if they disagree — a mixed-version
stack is a state we support during an upgrade and must therefore make visible.

---

## 9. What services must not do

| Prohibited | Why |
|---|---|
| Call another cctv-stack service's HTTP API for coordination | Coordination is the database. HTTP coupling turns one slow service into a cascade |
| Mount the Docker socket | Privilege escalation for no operational need ([HLD §13.2](../hld.md)) |
| Write anywhere outside the mounts listed in [`on-disk-layout.md`](on-disk-layout.md) | Containers are disposable ([INV-4](../invariants.md)) |
| Hold a database write transaction across network I/O | Turns a slow network into a locked database |
| Delete anything under `data/media/` or `data/frigate/` | Belongs to Frigate |
| Delete anything under `data/clips/` other than via the cleanup retention path | [INV-1](../invariants.md) |
| Return `healthy` as a literal | [INV-6](../invariants.md) |
| Construct a clip path itself | One implementation, in the shared library ([INV-5](../invariants.md)) |
