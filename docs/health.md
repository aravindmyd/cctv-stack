# Health

The uploader and cleanup services each serve a JSON health report and Prometheus metrics on
a loopback port. Frigate has its own status interface; the clip worker has neither, because
it has no state of its own beyond the database.

A fourth service, `status`, aggregates everything into one answer to *is my CCTV working?*.

| Service | Address | Report |
|---|---|---|
| `status` | `http://<host>:8084/status` | The aggregate page: cameras, storage, backup, retention |
| `status` | `http://<host>:8084/health` | The collector's own report |
| `status` | `http://<host>:8084/metrics` | Prometheus text format |
| `uploader` | `http://127.0.0.1:8082/health` | Queue, B2 reachability, credential validity, backlog age |
| `uploader` | `http://127.0.0.1:8082/metrics` | Prometheus text format |
| `cleanup` | `http://127.0.0.1:8083/health` | Last retention run, held clips, disk free |
| `cleanup` | `http://127.0.0.1:8083/metrics` | Prometheus text format |

`8082` and `8083` are published on `127.0.0.1` only: use an SSH tunnel to reach them from
another machine. `8084` is published to the LAN on purpose — it is the page you open from a
phone to answer the question, it shows camera names and states, and it has no admin route
and no credentials. Set `STATUS_PORT` to change the port, or remove the `ports:` entry from
the `status` service to keep it loopback-only.

```bash
curl -s http://127.0.0.1:8082/health | jq .
curl -s http://127.0.0.1:8084/status  | jq '.overall, .answer'
```

## The aggregate status page

Open `http://<host>:8084/status` in a browser. The first line is generated from state, not
templated — "Recording is working. Cloud backup stalled 42 min ago." — and the details
below it come from the same object the JSON is rendered from, so the page and the API
cannot disagree.

It is read-only by design: no buttons, no forms, no JavaScript, so it keeps working when
the rest of the stack is having a bad day. It reports what it could not observe instead of
guessing: with Frigate down, the cameras read `UNKNOWN` and the headline says recording
cannot be verified.

A camera that is reachable but has stopped producing clips is called out as `QUIET` rather
than looking fine — that is the failure mode a simple camera-online check misses.

## Dashboard and notifications

Gatus runs alongside the stack on `http://<host>:3001` and owns two things this project
deliberately does not build: history, and alert delivery. Its configuration is generated from
`cameras.yml`, so a camera added to the config appears there without editing a second file.

Alerts go to **ntfy** on `http://<host>:8085`. Install the ntfy app, subscribe to the `cctv`
topic, and that is the whole setup. Every alert has a matching recovery notification, and the
generated policy fires only after three consecutive failures so a reconnect blip does not
wake anyone.

What is watched, beyond the four services:

| Monitor | Kind | Why |
|---|---|---|
| `camera-<name>` | heartbeat | One per camera, generated. A missing heartbeat alerts |
| `disk` | heartbeat | Free space and the projected days-to-full |
| `backup-freshness` | heartbeat | No successful upload for 30 minutes with a non-empty queue |
| `retention` | heartbeat | Cleanup has stopped completing passes |
| `mosquitto` | TCP | The broker is listening |
| `frigate`, the four services | HTTP (`?strict=1`) | A `DEGRADED` state alerts |

If the status collector itself dies, its heartbeats stop and Gatus alerts on the silence —
the tool does not depend on the thing it monitors to notice it is gone.

## Reading a report

```json
{
  "state": "DEGRADED",
  "summary": "1 check is degraded",
  "checks": [
    { "name": "state_db", "state": "HEALTHY", "detail": "schema 2" },
    { "name": "storage_reachable", "state": "HEALTHY" },
    { "name": "backlog_age", "state": "DEGRADED",
      "code": "UPLOAD_BACKLOG_AGE", "detail": "oldest pending clip is 52m old" }
  ]
}
```

The top-level `state` is the worst check. A service is never reported healthy because a
single check passed.

| State | Meaning |
|---|---|
| `HEALTHY` | Everything this service depends on is working |
| `DEGRADED` | It is still doing its job, but something needs attention |
| `UNHEALTHY` | It cannot do its job right now |
| `UNKNOWN` | No recent observation, or the service has not completed a first pass |
| `DISABLED` | Turned off by configuration, not broken |

`DISABLED` is the expected state for cloud storage on a local-only install. If you see it
and expected uploads, `CCTV_STORAGE_BUCKET` is empty.

Add `?strict=1` to get HTTP 503 when the state is `DEGRADED` or `UNKNOWN`, which is what a
load balancer or uptime monitor wants.

## What each check covers

Uploader:

| Check | Reports |
|---|---|
| `state_db` | Database reachable, schema version readable |
| `local_files_readable` | Clips that are due for upload can still be opened |
| `storage_reachable` | B2 endpoint answers |
| `storage_credentials` | B2 rejects or accepts the application key |
| `failed_permanent` | Clips that exhausted their retries |
| `upload_verification` | Whether uploads were verified by checksum, by size, or not at all |

Only an upload with no provider confirmation at all (`verified_by="none"`) is reported as
`DEGRADED`. Above the multipart threshold the provider returns a composite digest, so the
uploader compares size plus the local SHA-256 instead; that is the documented, expected
result and it keeps the component healthy.
| `backlog_age` | Age of the oldest pending clip |
| `upload_staleness` | Time since the last successful upload |

Cleanup:

| Check | Reports |
|---|---|
| `state_db` | Database reachable |
| `clips_dir` | The clips directory exists and is readable |
| `last_run` | Age of the last completed retention pass |
| `clips_overdue_retention` | Clips held past their retention date because they are not uploaded |
| `disk_free` | Free space, compared with the warn and critical thresholds |

Cleanup also reports `storage_reachable` as `DISABLED`: cloud retention is not implemented
yet, so it has nothing to check.

## When something looks wrong

Start with the containers, then the services:

```bash
docker compose ps
docker compose logs --tail=100 frigate
docker compose logs --tail=100 clip-worker
curl -s http://127.0.0.1:8082/health | jq '.state, [.checks[] | select(.state != "HEALTHY")]'
curl -s http://127.0.0.1:8083/health | jq '.state, [.checks[] | select(.state != "HEALTHY")]'
```

Camera state lives in Frigate, not here. Frigate's image has no curl, but it does have
`python3`:

```bash
docker compose exec -T frigate python3 -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('http://127.0.0.1:5000/api/stats'))
print({name: cam['camera_fps'] for name, cam in d['cameras'].items()})
"
```

A camera with `camera_fps` of zero is not delivering frames, whatever the clip count says.

## Admin endpoints

The uploader exposes two endpoints that are not mounted at all unless
`INTERNAL_API_TOKEN` is set. They are for diagnosis, not automation.

```bash
TOKEN=$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)

# Backlog summary
curl -s http://127.0.0.1:8082/admin/queue \
  -H "Authorization: Bearer $TOKEN" | jq .

# Requeue failed clips. A reason and a filter are both required.
curl -s -X POST http://127.0.0.1:8082/admin/retry \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"camera":"front-door","reason":"rotated the B2 key after 403 errors"}'
```

`/admin/queue` returns counts per upload state, `oldest_pending_age_s`, `uploaded_24h`, and
the time of the last successful upload. Retrying without a reason, or without naming a
clip, a camera, or a `since` timestamp, is refused with a 400.

See [Recover failed uploads](recover-uploads.md) for what to do with the numbers.

## Exit codes

Every service exits `78` when its configuration is invalid; Compose will show an exited
container and the reason is in its log. The most common cause is a half-configured bucket:
`CCTV_STORAGE_BUCKET` set, but `CCTV_STORAGE_KEY_ID` or
`CCTV_STORAGE_APPLICATION_KEY` missing.

An uploader that cannot reach B2 does not exit. It starts, reports `UNHEALTHY`, and keeps
the clips on disk.

## What is not here yet

There is no aggregate status page, no alerting, and no notification integration. Each
service reports on itself, and the operator checks those reports. If you run an uptime
monitor, point it at `/health?strict=1` on both ports.

## Reconciliation findings

Once a day, `cleanup` compares the bucket listing against the database and writes what it finds
to `reconciliation_findings`. It never fixes anything: every repair is a guess about which side is
right, and deleting an "orphan" object can destroy the only surviving copy of a clip whose row was
lost to a restore.

| Finding | Means | Severity |
|---|---|---|
| `dangling_row` | the row says an object exists; it does not | critical |
| `checksum_mismatch` | the stored object is not the bytes we recorded | critical |
| `key_mismatch` | `cloud_key` is not the key this version would build from the path | critical |
| `orphan_object` | an object no row references | warning — it is billed |
| `unexpected_local_file` | a file under `clips/` with no row | warning |
| `missing_local_file` | a row whose file is gone | warning |

The `reconciliation` health check reports `RECONCILE_FINDINGS` when any critical finding is open,
and `RECONCILE_STALE` when the daily pass has not completed for twice its interval. A finding is
recorded once per subject while it stays unresolved, so a problem you have not fixed yet is one
row, not one row per day.
