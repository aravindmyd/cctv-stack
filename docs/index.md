# cctv-stack

cctv-stack runs alongside [Frigate](https://frigate.video) and turns each of its review
segments into one video file, backs those files up to Backblaze B2, and removes them
locally only after the backup has succeeded.

Frigate does the hard part: it talks to the cameras, runs object detection, records
continuously, and groups activity into reviews. cctv-stack adds the parts that matter the
day after the camera is installed:

- **One file per visit.** A clip lands at
  `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`, with a JSON sidecar recording what was
  requested, what was measured, and whether the result is trustworthy.
- **Optional cloud backup.** Clips are uploaded to B2 exactly once, driven by a state
  machine that survives restarts. With no bucket configured, the uploader reports
  `DISABLED` and uploads nothing.
- **Retention with a safety rule.** A clip is deleted locally only when its upload has
  finished successfully. If the upload is broken, the clip is held and reported, and the
  disk grows until you fix it.
- **One command to install.** `./install.sh` asks four questions — location, timezone,
  cameras, optional B2 — and does the rest: host detection, shared-memory sizing,
  validation, start-up, and waiting for Frigate.
- **Health you can query.** The uploader, cleanup and status services expose `/health`
  and `/metrics`, and `/status` on port 8084 answers *is my CCTV working?* in one page.
  Alerts go out through Gatus and ntfy.
- **Diagnosis, backup and upgrade.** `./scripts/doctor.sh` explains a broken installation
  without changing anything, `./backup.sh` writes an encrypted config bundle and a verified
  database backup, and `./update.sh` upgrades between a backup and a one-command rollback.

cctv-stack does not replace Frigate and does not talk to your cameras itself. It reads
Frigate's API and writes files next to it.

## Install

The stack runs as containers: an MQTT broker, a one-shot config generator, Frigate, the
clip worker, the uploader, cleanup, status, and the monitoring pair. You need Docker on the
host and RTSP URLs for your cameras.

From the repository, one command:

```bash
git clone https://github.com/aravindmyd/cctv-stack.git
cd cctv-stack
./install.sh
```

It asks for the location, timezone, your cameras and optional B2 credentials, validates
what it wrote, then starts everything and waits for Frigate. Nothing is uploaded anywhere
unless you asked for cloud backup.

Using the released image instead? Download `compose.yml` and `.env.example` (as `.env`),
then run the same interview against those files:

```bash
docker compose run --rm --no-deps init cctv-init
docker compose up -d
```

Full steps — including the by-hand path and the pip-only route — are in [Install](install.md).

## Documentation

| Page | What it covers |
|---|---|
| [Install](install.md) | Requirements, Docker Compose, pip, upgrades, removing |
| [Configure](configure.md) | `cameras.yml`, `.env`, adding and removing cameras |
| [Frigate UI login](frigate-ui.md) | Put the Frigate web interface behind a password |
| [Health](health.md) | The endpoints, the states, and what to check first |
| [Diagnose problems](doctor.md) | `./scripts/doctor.sh`: what it checks and how to read it |
| [Recover failed uploads](recover-uploads.md) | Why clips are not reaching B2, and what to do |
| [Rotate credentials](rotate-credentials.md) | B2 keys and camera passwords |
| [Disaster recovery](disaster-recovery.md) | What to back up, and how to rebuild |
| [Clip boundaries](clip-boundaries.md) | Clips that are short, gapped, or misaligned |
| [Glossary](glossary.md) | The vocabulary these pages use |

The design record (architecture, interface contracts, decision log) lives in the
[`internal/`](https://github.com/aravindmyd/cctv-stack/tree/master/internal) directory of
the repository. It is written for people changing the code and is not published here.
