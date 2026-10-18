# Health

The uploader and cleanup services each serve a JSON health report and Prometheus metrics on
a loopback port. Frigate has its own status interface; the clip worker has neither, because
it has no state of its own beyond the database.

| Service | Address | Report |
|---|---|---|
| `uploader` | `http://127.0.0.1:8082/health` | Queue, B2 reachability, credential validity, backlog age |
| `uploader` | `http://127.0.0.1:8082/metrics` | Prometheus text format |
| `cleanup` | `http://127.0.0.1:8083/health` | Last retention run, held clips, disk free |
| `cleanup` | `http://127.0.0.1:8083/metrics` | Prometheus text format |

Both ports are published on `127.0.0.1` only. To reach them from another machine, use an
SSH tunnel rather than binding to `0.0.0.0`.

```bash
curl -s http://127.0.0.1:8082/health | jq .
```

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
