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
- **Health you can query.** The uploader and cleanup services expose `/health` and
  `/metrics`, so "is the CCTV working?" has an answer that is not a guess.

cctv-stack does not replace Frigate and does not talk to your cameras itself. It reads
Frigate's API and writes files next to it.

## Install

The stack runs as six containers: MQTT broker, a one-shot config generator, Frigate, the
clip worker, the uploader, and cleanup. You need Docker on the host and RTSP URLs for your
cameras.

```bash
mkdir cctv-stack && cd cctv-stack
curl -fsSLO https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/compose.yml
curl -fsSL https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/.env.example -o .env
mkdir -p config
curl -fsSL https://raw.githubusercontent.com/aravindmyd/cctv-stack/master/config/cameras.example.yml -o config/cameras.yml
```

Edit `.env` (image tag, camera URLs, timezone) and `config/cameras.yml` (camera names,
padding, retention), then start it:

```bash
docker compose pull
docker compose up -d
```

Full steps, including validation and the pip-only path, are in [Install](install.md).

## Documentation

| Page | What it covers |
|---|---|
| [Install](install.md) | Requirements, Docker Compose, pip, upgrades |
| [Configure](configure.md) | `cameras.yml`, `.env`, adding and removing cameras |
| [Frigate UI login](frigate-ui.md) | Put the Frigate web interface behind a password |
| [Health](health.md) | The endpoints, the states, and what to check first |
| [Recover failed uploads](recover-uploads.md) | Why clips are not reaching B2, and what to do |
| [Rotate credentials](rotate-credentials.md) | B2 keys and camera passwords |
| [Disaster recovery](disaster-recovery.md) | What to back up, and how to rebuild |
| [Clip boundaries](clip-boundaries.md) | Clips that are short, gapped, or misaligned |
| [Glossary](glossary.md) | The vocabulary these pages use |

The design record (architecture, interface contracts, decision log) lives in the
[`internal/`](https://github.com/aravindmyd/cctv-stack/tree/master/internal) directory of
the repository. It is written for people changing the code and is not published here.
