# Runbook: Install

> **Status:** 🚧 Planned. Written for an operator, not a developer.
> **Time:** 15–30 minutes, plus event time.

---

## Before you start

| Requirement | Detail |
|---|---|
| A machine that stays on | This is a server. A mini-PC, a small NAS, or a Raspberry Pi 5 |
| Docker | Engine 24+ and Compose v2. Docker Desktop on Windows/macOS |
| Storage | An SSD, sized for 90 days of clips. **Not an SD card** |
| Cameras | RTSP URLs, including credentials. ONVIF discovery is not used |
| A Backblaze B2 bucket | Optional. Without it the system runs in local-only mode |
| Network | Outbound internet for B2 and notifications. No inbound access needed |

**Rough sizing.** Clip backup is far cheaper than continuous recording:

| | Per camera |
|---|---|
| Clips (100 events/day, 30 s, 2 Mbps) | ~750 MB/day → **~66 GB / 90 days** |
| Continuous recording, if enabled | ~21 GB/day → **~1.9 TB / 90 days** |

Continuous recording is **off by default**, including in the installer's projection. If
you turn it on, expect roughly 30× the storage.

---

## Install

```bash
git clone <repository> cctv-stack
cd cctv-stack
./install.sh
```

That is the whole procedure. The installer will:

1. Check Docker and its version
2. **Detect your hardware** and print what it found, including anything worrying
3. Ask where to store data (default `/opt/cctv`)
4. Ask for your location name, timezone, and cameras
5. Ask about cloud backup and notifications
6. Validate everything **before** starting anything
7. Pull images, start the stack, and verify each component

### What the installer will tell you about your hardware

Read this section rather than skimming it. It sets your expectations.

```text
  ⚠  No Coral Edge TPU or Hailo accelerator was found.
     Object detection will run on the CPU.
     With 4 cameras this will likely be too slow to be useful.
```

If you see warnings like this, the correct response is to reduce camera count or
detection resolution, or to fit an accelerator. The system will work either way; it will
just be limited. See the hardware guidance page.

### Answering the camera questions

Have your RTSP URLs ready. A typical Hikvision URL looks like:

```text
rtsp://user:password@192.168.1.50:554/Streaming/Channels/101    # main stream
rtsp://user:password@192.168.1.50:554/Streaming/Channels/102    # sub stream
```

The **sub stream is important.** Detection runs on it, so a low-resolution sub stream
cuts CPU dramatically at no cost to detection quality. If your camera has one, give it.

The installer validates each camera as you enter it, so mistakes surface immediately.

Camera names must be lowercase with hyphens: `front-door`, not `FrontDoor`. These names
become directory names and cloud object keys, which is why the rule exists.

### Interactive or not

```bash
./install.sh                          # guided
./install.sh --yes                    # accept the recommended profile and existing config
./install.sh --config my-cameras.yml  # pre-written camera config
```

---

## After the install

The installer prints URLs. Bookmark all three:

| URL | What it is |
|---|---|
| `http://<host>:8971` | **Frigate** — live view, playback, review, exports |
| `http://<host>:3001` | **Monitoring** — history, and where notifications are configured |
| `http://<host>:8080/status` | **Status** — the one-screen answer to "is it working?" |

### Generate a Frigate login

The installer creates a service account for the pipeline. You need your own:

```bash
docker compose exec frigate python3 -m frigate --create-user
```

### Configure notifications

ntfy and Telegram were configured during install and each received a test message. If
you skipped that step, configure them in the monitoring tool's UI and send a test. **Do
not skip the test** — a notification channel you have never seen work is not a
notification channel.

### Recommended next steps

1. **Enable remote access properly.** Do not port-forward. Use Tailscale or WireGuard.
   See the security section below.
2. **Enable bucket encryption.** In the Backblaze console, enable server-side encryption
   on the bucket.
3. **Let it run for a day**, then check `/status`. You are looking for:
   - every camera `ONLINE`
   - clips counting up
   - `uploaded_24h` non-zero
   - projected days to full in the hundreds, not the tens
4. **Watch one clip end to end.** Play it and check that it starts *before* the person
   appears and ends *after* they leave. If it starts late or cuts off early, see
   [`clip-boundaries.md`](clip-boundaries.md) — it is a configuration problem, not a
   limitation you have to live with
4. **Take a backup and verify it**:
   ```bash
   ./backup.sh --verify
   ```
5. **Rehearse a restore once**, while you do not need it. See
   [`disaster-recovery.md`](disaster-recovery.md).

---

## Verifying the install

```bash
./scripts/doctor.sh
```

A full diagnostic. Run it whenever anything seems wrong, and attach its output to any
bug report.

A healthy install reports:

```text
✓ Host          Raspberry Pi 5, arm64, profile pi-cpu
✓ Docker        27.1.1, compose v2.29.1
✓ Storage       412 GB free, single filesystem, projected 118 days to full
✓ Config        valid, no drift
✓ Frigate       reachable, version 0.16.1 (matches versions.yml)
✓ MQTT          connected
✓ Database      integrity ok, schema 7
✓ clip-worker   healthy, last clip 4m ago
✓ uploader      healthy, oldest pending 12s, 214 uploaded in 24h
✓ cleanup       healthy, last run 22m ago
✓ Cameras       3 configured, 3 online
```

---

## Troubleshooting

### A camera shows as offline

1. Test the URL outside the stack:
   ```bash
   docker compose exec frigate ffprobe -v error -rtsp_transport tcp "<your rtsp url>"
   ```
2. Check the sub stream URL separately from the main — they often differ by one digit
3. Confirm the camera allows more than one concurrent connection. Many cameras have a
   low limit, and Frigate takes two connections per camera
4. Check that the camera is not already at its stream limit from another client

Then fix `config/cameras.yml` and apply:

```bash
./update.sh --regenerate-config
```

### Clips are the wrong length

A clip that starts after the action began, cuts off early, or jumps in the middle. This
is the most common complaint about Frigate-based systems, and it has four separate
causes — only one of which is about configuration.

```bash
curl -s http://<host>:8080/status | jq '.clip_quality'
docker compose exec clip-worker sqlite3 -readonly /data/state/state.db \
  "SELECT round(avg(coverage_ratio),3), round(avg(fetch_attempts),2), count(*)
     FROM clips WHERE start_time > strftime('%s','now') - 86400;"
```

| Reading | Meaning |
|---|---|
| `coverage` near 1.000, `fetch_attempts` near 1 | Healthy |
| `coverage` below 0.95 | Clips are being truncated — see below |
| `fetch_attempts` above 1.2 | The flush grace is too short for this machine |

**The single most likely fix**, if clips are ending early:

```yaml
# config/app.yml
clip:
  segment_flush_grace_s: 15     # Frigate flushes recording segments after writing them
```

```bash
./update.sh --regenerate-config && docker compose restart clip-worker
```

If the clip starts too late or you want more of the aftermath on screen, adjust
`clip.pre_roll_s` and `clip.post_roll_s` — per camera, not just globally.

Full diagnosis, including holes from missing segments and how to audit a whole period
with one query: [`clip-boundaries.md`](clip-boundaries.md).

### The status page is `DEGRADED` and says backup is behind

Normal during a network outage. It clears itself. Investigate only if it persists
beyond an hour — and read the backlog age in the message: 42 minutes is a hiccup, 42
hours is a problem.

### The status page says `CLIPS_OVERDUE_RETENTION`

**Read this one carefully.** It means clips are being **held past their retention date**
because they have not been uploaded. The system is deliberately not deleting them
([why](../adr/0009-cleanup-safety-invariant.md)). Your disk will grow until you fix the
upload.

See [`recover-failed-uploads.md`](recover-failed-uploads.md).

### Disk is filling up

```bash
./scripts/doctor.sh          # projected days to full
docker compose exec uploader curlie localhost:8082/admin/queue
```

Most likely causes, in order:
1. Continuous recording was enabled — the default is off for this reason
2. An upload backlog — see above
3. Retention values set higher than you intended in `cameras.yml`

### Frigate's UI shows nothing

Frigate needs a restart to pick up config changes:

```bash
docker compose restart frigate
```

### I want to see what retention *would* do

```bash
docker compose run --rm -e DRY_RUN=1 cleanup
```

Nothing is deleted. Every intended deletion is logged.

---

## Security

The install publishes three ports, all on your LAN:

| Port | Service |
|---|---|
| `8971` | Frigate UI |
| `3001` | Monitoring |
| `8080` | Status page |

**None of these should be exposed to the internet.**

| Do | Don't |
|---|---|
| Use **Tailscale** or WireGuard for remote access — no open ports, device-level auth | Port-forward |
| Put a TLS reverse proxy with auth in front if you must expose something | Publish Frigate's port 5000 — it is **unauthenticated** |
| Keep the host patched | Reuse a camera password for anything else |
| Enable bucket encryption | Email yourself the `.env` file |

**Check your `.env` permissions:**

```bash
ls -l .env        # must be -rw------- (600)
```

The installer sets this. If it is ever wrong, fix it immediately — it contains your B2
key and your camera passwords.

---

## Uninstall

```bash
./uninstall.sh                  # stop and remove containers. KEEPS all data and config
./uninstall.sh --images         # also remove images
./uninstall.sh --purge          # also remove config and data — will ask you to type the location name
```

The default removes nothing you would miss and leaves your footage in place.

If you are decommissioning the site entirely, decide the bucket's fate **explicitly** —
either leave the lifecycle rule in place and keep paying, or remove it and delete the
bucket. Leaving a bucket with no lifecycle rule means objects that never expire and are
never looked at again.

---

## Getting help

```bash
./scripts/doctor.sh --bundle
```

Produces `logs/doctor-<timestamp>.tar.gz`. It contains detection output, redacted
configuration, container statuses, and logs. **Credentials are redacted by value**, and
the redaction is tested — but skim it before posting anywhere public, because it
contains your camera names and host details.
