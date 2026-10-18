# cctv-stack

A self-hosted CCTV stack built on **[Frigate](https://github.com/blakeblackshear/frigate)**.

Frigate handles ingest, detection, recording and review segmentation. `cctv-stack` adds the
four things it leaves to the operator:

1. **Canonical clips** — one deterministic file per review item, at
   `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4`.
2. **Durable backup** — clips are uploaded to Backblaze B2, exactly once.
3. **Independent retention** — local cleanup never deletes a clip that is not safely
   uploaded; the cloud copy follows its own lifecycle.
4. **Health surfaces** — every service answers `/health` and `/metrics`.

It is not a replacement for Frigate. It is the layer around it.

---

## Start here

| If you want to… | Read |
|---|---|
| Install the stack on a machine | [Install](runbooks/install.md) |
| Add or change a camera | [Add a camera](runbooks/add-a-camera.md) |
| Put the Frigate UI behind a login | [Frigate UI authentication](runbooks/frigate-authentication.md) |
| Understand the vocabulary | [Glossary](glossary.md) |

## Install

```bash
git clone https://github.com/aravindmyd/cctv-stack.git
cd cctv-stack
./install.sh
```

The first run creates `.env` and `config/cameras.yml` and exits. Fill in your RTSP URLs and
B2 keys, then run it again. It validates the configuration, starts the stack and waits for
Frigate to come up.

Requirements: Docker and Docker Compose on Linux (`x86_64` or `arm64`); Python 3.12+ only
if you install the package from PyPI instead of using Compose.

## What it produces

```text
data/
├── clips/
│   └── <camera>/YYYY/MM/DD/HH-MM-SS.mp4        # the canonical clip
│       <camera>/YYYY/MM/DD/HH-MM-SS.mp4.json   # its sidecar: review ids, boundaries,
│                                               # coverage, codecs, size, SHA-256
└── cctv.db                                     # upload and retention state
```

Clip filenames are rendered in the **site timezone**. The cloud object key is
`<location>/clips/<path-inside-clips>`, so the bucket mirrors the local tree.

A clip appears **after** the visit ends — Frigate keeps a review open until its alert
cutoff passes. There are no partial clips by design, and a clip that came back short is
retried rather than truncated.

## Health

| Service | Address | What it tells you |
|---|---|---|
| `cctv-uploader` | `http://127.0.0.1:8082/health`, `/metrics` | Upload queue, B2 reachability, `DISABLED` when no bucket is configured |
| `cctv-cleanup` | `http://127.0.0.1:8083/health`, `/metrics` | Retention runs, disk free, clips held past expiry |
| Frigate UI | `https://127.0.0.1:8971` | Live view, playback, review browsing (HTTPS, loopback only) |

Bind the health endpoints to loopback. Nothing in the stack needs an inbound port from
the LAN.

## Operational guides

- [Recover failed uploads](runbooks/recover-failed-uploads.md) — diagnose and drain a stuck queue
- [Rotate credentials](runbooks/rotate-credentials.md) — B2 keys, camera passwords
- [Disaster recovery](runbooks/disaster-recovery.md) — rebuild on new hardware
- [Clip boundaries](runbooks/clip-boundaries.md) — why a clip can look fine and still be wrong

---

## About these docs

This site covers installing, configuring and operating the stack.

The design record — architecture, invariants, interface contracts, low-level design and
the decision log — lives in the [`internal/`](https://github.com/aravindmyd/cctv-stack/tree/master/internal)
directory of the repository. It is written for people changing the code, and is
deliberately not published here.

Contributing, the development workflow and the CI/release process are in
[`CONTRIBUTING.md`](https://github.com/aravindmyd/cctv-stack/blob/master/CONTRIBUTING.md).
