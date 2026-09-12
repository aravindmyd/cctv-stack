# Runbook: Rotate credentials

> **Status:** 🚧 Planned.
> **Time:** 10 minutes per credential.
> **Risk:** Low if followed in order. The dangerous step is always "revoke the old one".

---

## The universal rule

> **Add the new credential, verify it works, then revoke the old one.**

Never revoke first. Every rotation below follows the same four steps:

```text
1. Create the new credential alongside the old
2. Update .env
3. Restart and VERIFY it works
4. Only then revoke the old credential
```

---

## 1. Backblaze B2 application key

**When:** annually, on suspicion of exposure, or when an employee with access leaves.

### 1.1 Create the new key

Backblaze console → **Application Keys** → **Add a New Application Key**

| Field | Value |
|---|---|
| Name | `cctv-<location>-<YYYY-MM>` — dated so you know what to clean up |
| Allow access to Bucket(s) | **Only this site's bucket.** Never "All" |
| Type of Access | Read and Write |
| File name prefix | `<location>/` — narrows the blast radius further |
| Duration | Leave blank, or 1 year if you want a rotation reminder |

**Copy the key immediately.** Backblaze shows `applicationKey` once; it cannot be
retrieved later.

### 1.2 Update `.env`

```bash
cd /path/to/cctv-stack
cp .env .env.bak-$(date +%Y%m%d)     # a rollback path costs nothing
$EDITOR .env                          # update B2_KEY_ID and B2_APPLICATION_KEY
```

### 1.3 Restart and verify

```bash
docker compose up -d uploader
sleep 10
docker compose exec uploader curlie -s localhost:8082/health \
  | jq '.checks[] | select(.name | startswith("storage"))'
```

Expected:

```json
{ "name": "storage_reachable",   "state": "HEALTHY" }
{ "name": "storage_credentials", "state": "HEALTHY" }
```

Confirm an upload actually happens — health checks can pass while writes fail:

```bash
docker compose logs -f uploader | grep UPLOAD_OK
```

### 1.4 Revoke the old key

Only now. Backblaze console → Application Keys → find the old key → **Delete**.

If you delete it too early, uploads fail with `STORAGE_AUTH_FAILED`, nothing is lost, and
you can restore `B2_KEY_ID`/`B2_APPLICATION_KEY` from `.env.bak-*`.

### 1.5 Clean up

```bash
shred -u .env.bak-* 2>/dev/null || rm -f .env.bak-*
```

Do not leave old keys in `.env.bak`. A backup file with a valid credential in it is a
credential.

---

## 2. Camera passwords

**When:** on a camera change, on suspicion of exposure, or when rotating a shared
credential across a fleet.

### 2.1 Change it on the camera first

Camera web UI → user management → change the password.

The camera is now using the new password and the stack is using the old one, so
**detection is broken until step 2 completes.** Do this when a few minutes without that
camera does not matter.

### 2.2 Update `.env` and apply

```bash
$EDITOR .env          # update GARAGE_PASSWORD
./update.sh --regenerate-config
```

The generated Frigate config interpolates from the environment, so it does not need
editing.

### 2.3 Verify

```bash
./scripts/doctor.sh | grep -A1 garage
docker compose exec frigate ffprobe -v error -rtsp_transport tcp \
  "rtsp://${GARAGE_USER}:${GARAGE_PASSWORD}@192.168.1.51:554/Streaming/Channels/102"
```

### 2.4 Notes

| Situation | Handling |
|---|---|
| Several cameras share a password | Rotate them **one at a time**, verifying each. Rotating all at once means finding five typos simultaneously |
| The camera enforces a new password on first login | Log in through its UI first, then follow this |
| The camera locks out after failed attempts | Pause, and check for a typo in `.env` before retrying. Some cameras lock for 30 minutes |

---

## 3. Frigate API credentials

**When:** the service account is compromised, or during a Frigate major upgrade.

The pipeline authenticates to Frigate as an admin-role user, because `retain` and
`unretain` require it.

### 3.1 Create a replacement

```bash
docker compose exec frigate python3 -m frigate --create-user
# name: cctv-service-2026-09
# role: admin
```

### 3.2 Log in and capture the token

The exact mechanism depends on the pinned Frigate version — see the `VERIFY` note in
[`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md#2-authentication).

```bash
curl -s -X POST http://localhost:5000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"user":"cctv-service-2026-09","password":"<new password>"}'
```

### 3.3 Update and verify

```bash
$EDITOR .env          # FRIGATE_USER, FRIGATE_PASSWORD (and FRIGATE_API_TOKEN if used)
docker compose up -d clip-worker status-collector
docker compose exec clip-worker curlie -s localhost:8081/health \
  | jq '.checks[] | select(.name=="frigate_api")'
```

### 3.4 Disable the old service account

Frigate UI → Settings → Users → disable the old service user.

### 3.5 Do not simply switch auth off

Publishing Frigate's unauthenticated port `5000` to make the pipeline work is a security
regression on a machine holding your camera feeds. The config validator will refuse it.

---

## 4. MQTT credentials

**When:** on exposure, or as part of a general rotation.

MQTT is internal-only and not published, so this is lower priority than the others.

```bash
# 1. Generate
NEW_MQTT_PASSWORD=$(openssl rand -base64 24)

# 2. Update both sides — they must match, or Frigate stops publishing
#    .env:            FRIGATE_MQTT_PASSWORD / MQTT_PASSWORD
#    mosquitto.conf:  password_file entry (or ADD, do not REMOVE yet)

# 3. Restart the broker first, then the producers
docker compose restart mosquitto
sleep 5
docker compose restart frigate clip-worker status-collector

# 4. Verify
docker compose exec status-collector curlie -s localhost:8084/health \
  | jq '.checks[] | select(.name=="mqtt")'
docker compose logs --tail=50 frigate | grep -i mqtt
```

Both sides must change together. A mismatch means Frigate cannot publish events, which
means **no new clips** — the status page will show cameras online with `clip-worker`
degraded and `SWEEP_LAG_HIGH`.

---

## 5. Notification tokens

### 5.1 Telegram bot token

1. Talk to `@BotFather` → `/revoke` → select the bot → get a new token
2. Update the token in the monitoring tool's UI
3. Send a test notification. Do not skip this
4. Confirm a real alert arrives by triggering one:

```bash
docker compose stop clip-worker
# wait for the alert threshold (3 consecutive failed checks)
docker compose start clip-worker
# confirm both the alert AND the recovery notification arrived
```

### 5.2 ntfy

If using a self-hosted ntfy with auth, rotate the access token in its UI, update the
monitoring tool, and send a test.

---

## 6. Verifying after any rotation

```bash
./scripts/doctor.sh
curl -s http://<host>:8080/status | jq '.answer, .overall'
```

A full health sweep after every rotation. Specifically confirm:

| Check | Must not show |
|---|---|
| `storage_credentials` | `STORAGE_AUTH_FAILED` |
| `frigate_api` | `FRIGATE_AUTH_FAILED` |
| `mqtt` | `MQTT_DISCONNECTED` |
| Cameras | `OFFLINE` |
| `upload_staleness` | rising |

Then confirm reality, not just health:

```bash
docker compose logs --tail=100 clip-worker | grep CLIP_MATERIALISED   # clips still being made
docker compose logs --tail=100 uploader     | grep UPLOAD_OK          # uploads still succeeding
```

---

## 7. If it goes wrong

**Nothing is lost in any credential rotation failure.** The failure modes are all
"stops working", never "deletes data".

| Symptom | Cause | Fix |
|---|---|---|
| `STORAGE_AUTH_FAILED` | Wrong or revoked B2 key | Restore from `.env.bak-*`, or create another key |
| `FRIGATE_AUTH_FAILED` | Wrong Frigate credentials | Restore the previous `.env`, or reset the password in the Frigate UI |
| `MQTT_DISCONNECTED` | Broker and client credentials differ | Make them match; restart the broker first, then clients |
| Camera `OFFLINE` | Password changed on one side only | Put the old password back in the camera UI, then redo §2 |
| No new clips, cameras online | Usually MQTT | Check `mqtt` health, then `sweep_lag` |

**Emergency rollback:**

```bash
cp .env.bak-<timestamp> .env
docker compose up -d
```
