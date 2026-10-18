# Rotate credentials

Three credentials matter: the B2 application key, camera passwords, and (if you turned it
on) the Frigate UI login.

The rule for all of them: create the new credential, verify it works, then revoke the old
one. Revoking first turns a rotation into an outage.

## Backblaze B2 application key

Rotate this yearly, after any suspected exposure, or when someone with access to the console
leaves.

### 1. Create the new key

In the Backblaze console, open **Application Keys** and add a new key:

| Field | Value |
|---|---|
| Name | `cctv-<location>-<YYYY-MM>`, so you know what to delete later |
| Bucket access | This site's bucket only. Never all buckets |
| Type | Read and Write |
| File prefix | `<location>/`, which narrows what the key can reach |

Copy the `applicationKey` immediately. Backblaze shows it once.

### 2. Update `.env`

```bash
cp .env .env.bak-$(date +%Y%m%d)
$EDITOR .env        # CCTV_STORAGE_KEY_ID and CCTV_STORAGE_APPLICATION_KEY
chmod 600 .env
```

The backup is a rollback path. Delete it once the new key works; a file with a valid
credential in it is a credential.

### 3. Restart the uploader

```bash
docker compose up -d uploader
```

Compose recreates the container because the environment changed.

### 4. Verify

```bash
curl -s http://127.0.0.1:8082/health \
  | jq '.checks[] | select(.name | startswith("storage"))'
```

Both checks should be `HEALTHY`. The credentials check reads only, so confirm a write
actually happens:

```bash
TOKEN=$(grep '^INTERNAL_API_TOKEN=' .env | cut -d= -f2)
curl -s http://127.0.0.1:8082/admin/queue -H "Authorization: Bearer $TOKEN" \
  | jq '{uploaded_24h, last_successful_upload_at}'
```

`last_successful_upload_at` should move within a few minutes on a site with regular
activity. If the queue is empty because nothing is happening, wait for the next clip.

### 5. Revoke the old key

Delete it in the console. If you delete it too early, uploads fail with
`STORAGE_AUTH_FAILED`; nothing is lost, and restoring the previous values from
`.env.bak-*` brings uploads back.

### 6. Clean up

```bash
shred -u .env.bak-* 2>/dev/null || rm -f .env.bak-*
```

## Camera passwords

### 1. Change the password on the camera

Use the camera's own web interface. From this moment the camera expects the new password and
the stack still uses the old one, so that camera's stream is down until step 3.

### 2. Update `.env`

```bash
$EDITOR .env        # e.g. CAMERA_GARAGE_RTSP and CAMERA_GARAGE_SUB
```

The generated Frigate config carries the substituted URL, so it needs regenerating.

### 3. Regenerate and restart

```bash
docker compose run --rm --no-deps config \
  cctv-config validate --cameras /config/cameras.yml
docker compose up -d
```

`up -d` re-runs the generator and recreates Frigate if the file changed.

### 4. Verify

```bash
docker compose exec -T frigate ffprobe -v error -rtsp_transport tcp \
  "rtsp://user:new-password@192.168.1.51:554/Streaming/Channels/102"
```

Then check the camera's frame rate in Frigate, because a stream can connect and still stall:

```bash
docker compose exec -T frigate python3 -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('http://127.0.0.1:5000/api/stats'))
print({n: c['camera_fps'] for n, c in d['cameras'].items()})
"
```

If several cameras share a password, rotate them one at a time and verify each. Rotating all
of them together means finding several mistakes at once.

## Frigate UI login

Enabling, resetting, and disabling the web login are covered in
[Frigate UI login](frigate-ui.md). The reset uses Frigate's own
`auth.reset_admin_password` flag, applied to the generated config for one restart.

## MQTT

The Compose broker accepts anonymous connections and is not published outside the Compose
network, so there is no credential to rotate. If you put an authenticated broker in front of
it, set `CCTV_MQTT_USERNAME` and `CCTV_MQTT_PASSWORD` for the clip worker and configure the
same credentials in Mosquitto. Both sides must change together; a mismatch means Frigate
publishes reviews that nobody receives, and the worker falls back to its polling interval.

## After any rotation

```bash
curl -s http://127.0.0.1:8082/health | jq '.state, [.checks[] | select(.state != "HEALTHY")]'
curl -s http://127.0.0.1:8083/health | jq '.state, [.checks[] | select(.state != "HEALTHY")]'
docker compose ps
```

Confirm reality as well as health. Health can pass while nothing is being written:

```bash
ls -lt data/clips/*/*/*/*/ | head
docker compose logs --tail=100 clip-worker | grep -c ''   # recent activity
```

## If a rotation goes wrong

None of these failure modes deletes data.

| Symptom | Cause | Fix |
|---|---|---|
| `STORAGE_AUTH_FAILED` | Wrong or revoked B2 key | Restore `.env.bak-*`, or create another key |
| Camera missing from Frigate, or frame rate at zero | Password changed on one side only | Put the old password back on the camera, then repeat the camera section |
| No new clips, cameras online | Frigate is publishing; the worker is not receiving | Check the broker, then the worker's log |
| Uploads stall after a key change | The uploader still has the old environment | `docker compose up -d uploader` |

Emergency rollback:

```bash
cp .env.bak-<timestamp> .env
docker compose up -d
```
