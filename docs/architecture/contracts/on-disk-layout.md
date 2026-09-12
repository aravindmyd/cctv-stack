# Contract: on-disk layout

**Parties:** every component, `install.sh`, `backup.sh`, the operator

The filesystem is an interface. This document is its specification. Any component
that writes outside its declared paths, or assumes a path layout not listed here,
is in violation.

---

## 1. Principles

| # | Principle | Enforced by |
|---|---|---|
| P1 | Every persistent byte lives under `DATA_DIR`, never inside a container ([INV-4](../invariants.md)) | Compose mount audit + a `down && up` acceptance test |
| P2 | `DATA_DIR` is on **one filesystem**, so atomic rename and hardlink work | Installer preflight check across mount boundaries |
| P3 | A file only becomes "real" by atomic rename from a `.tmp` sibling | Writers use write→fsync→rename, never in-place |
| P4 | Names are portable: `[a-z0-9-_]`, no spaces, no uppercase, no unicode | Validation at config load and at path construction |
| P5 | Generated files are marked and never hand-edited | Header comment + drift check + `--regenerate-config` |
| P6 | Permissions are least-privilege and checked | Installer refuses to continue on a world-readable secret |

---

## 2. Host tree

```text
${DATA_DIR}/                          default /opt/cctv
│
├── config/                           GENERATED + one operator-edited file
│   ├── cameras.yml                   ← THE source of truth (0600 — may hold camera passwords)
│   ├── app.yml                        application defaults (0644)
│   ├── frigate/
│   │   ├── config.yml                GENERATED (0644)
│   │   └── mqtt.yml                  GENERATED, holds MQTT credentials (0600)
│   ├── uploader/config.yml           GENERATED (0644)
│   ├── cleanup/config.yml            GENERATED (0644)
│   └── monitoring/
│       ├── monitors.json             GENERATED (0600 — may contain tokens)
│       └── notifications.json        GENERATED (0600)
│
├── data/
│   ├── clips/                        OUR durable artefact — never touched by Frigate
│   │   └── <camera>/
│   │       └── YYYY/MM/DD/
│   │           ├── HH-MM-SS.mp4
│   │           └── HH-MM-SS.mp4.json
│   │
│   ├── media/                        Frigate's /media/frigate — Frigate owns this
│   │   ├── recordings/               YYYY-MM-DD/HH/<camera>/MM.SS.mp4 (UTC)
│   │   ├── clips/                    snapshots, review thumbnails
│   │   └── exports/
│   │
│   ├── frigate/                      Frigate's /config — Frigate owns this
│   │   ├── frigate.db                Frigate's DB (treated as a cache)
│   │   ├── model_cache/
│   │   └── go2rtc/
│   │
│   └── state/                        OUR state
│       ├── state.db                  SQLite, WAL mode (0600)
│       ├── state.db-wal
│       ├── state.db-shm
│       ├── tmp/                      staging area for atomic writes
│       └── db-backups/
│           └── state-YYYYMMDDTHHMMSSZ.db
│
├── logs/                             optional; containers log to stdout by default
│
└── backups/
    ├── cctv-config-YYYYMMDDTHHMMSSZ.tar.gz
    └── cctv-state-YYYYMMDDTHHMMSSZ.tar.gz
```

### 2.1 Why `data/clips/` and `data/media/` are separate subtrees

`data/clips/` is **ours**; `data/media/` is **Frigate's**. They are never mounted
into each other's containers.

| Reason | Detail |
|---|---|
| Ownership | Our services run as `CCTV_UID`; Frigate runs as root for device access. Separate subtrees avoid a shared-permission mess |
| Retention independence | Frigate can aggressively expire `recordings/` without any possibility of touching canonical clips |
| Destroy-and-rebuild | `rm -rf data/media/` and let Frigate relearn costs only recent recordings. Deleting the wrong tree would cost 90 days of clips |
| Mount safety | Frigate never has write access to the only copy of a clip |

---

## 3. Clip path contract

```text
data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
```

| Element | Rule |
|---|---|
| `<camera>` | Exactly the camera key from `cameras.yml`. `[a-z0-9-]{1,32}` |
| `YYYY/MM/DD` | Date components of the event **start** time, in the configured display timezone |
| `HH-MM-SS` | Time components of the event **start** time, same timezone, zero-padded, `-` separators (filesystem-safe on every OS we support) |
| Collision | If two events from the same camera start in the same second, append `-<eventid-short>` where `<eventid-short>` is the last 6 characters of the event id. Deterministic, never random |
| Extension | `.mp4` (H.264 copy of the source; see the remux note in [lld/02](../lld/02-clip-worker.md)) |
| Sidecar | `.mp4.json`, same basename, written **after** the clip is durable |

**Canonical definition:** this path is produced by exactly one function in the
shared library, used by clip-worker, uploader, cleanup, and backup
([INV-5](../invariants.md)). No component re-implements it.

**Timezone.** Default is the site timezone (`TZ` in `.env`), because humans browse
these directories and "the clip from 6pm yesterday" should be where they expect.
UTC is always recoverable from `state.db` and the `.json` sidecar. This is the
compromise recorded in [INV-12](../invariants.md).

**Alternative considered.** UTC-only paths would be simpler and immune to DST
ambiguity, but make the primary browsing interface hostile to the actual user. The
`-<eventid-short>` collision rule resolves the one-hour repeated window at a
DST fallback.

---

## 4. Mount map

Host UID/GID for our services: `${CCTV_UID:-1000}:${CCTV_GID:-1000}`.
Frigate runs as root (required for `/dev/dri` and USB device access).

| Service | Host path | Container path | Mode |
|---|---|---|---|
| frigate | `${DATA_DIR}/config/frigate/config.yml` | `/config/config.yml` | ro |
| frigate | `${DATA_DIR}/data/frigate` | `/config` | rw |
| frigate | `${DATA_DIR}/data/media` | `/media/frigate` | rw |
| clip-worker | `${DATA_DIR}/config/cameras.yml` | `/etc/cctv/cameras.yml` | ro |
| clip-worker | `${DATA_DIR}/config/app.yml` | `/etc/cctv/app.yml` | ro |
| clip-worker | `${DATA_DIR}/data/clips` | `/data/clips` | rw |
| clip-worker | `${DATA_DIR}/data/state` | `/data/state` | rw |
| uploader | `${DATA_DIR}/data/clips` | `/data/clips` | **ro** |
| uploader | `${DATA_DIR}/data/state` | `/data/state` | rw |
| cleanup | `${DATA_DIR}/data/clips` | `/data/clips` | rw (unlink only) |
| cleanup | `${DATA_DIR}/data/state` | `/data/state` | rw |
| status-collector | `${DATA_DIR}/data/state` | `/data/state` | **ro** |
| status-collector | `${DATA_DIR}/data/clips` | `/data/clips` | **ro** |
| uptime-kuma | `${DATA_DIR}/data/uptime-kuma` | `/app/data` | rw |
| mosquitto | `${DATA_DIR}/config/mosquitto/` | `/mosquitto/config` | ro |

**Least-privilege mounts are a design feature, not a detail.** The uploader mounts
clips read-only because it has no business modifying or deleting them — it uploads
and records state. The status-collector mounts everything read-only because it
observes. This makes whole classes of bug impossible rather than merely discouraged.

**Rejected path.** Mounting clips read-write into Frigate "so it can hardlink" was
considered and rejected: it would give Frigate write access to our only copy of a
clip, and hardlinks across a container boundary add failure modes for a saving that
matters only in the first place because we already download the stream.

---

## 5. Filesystem requirements

| Requirement | Reason | Checked by |
|---|---|---|
| Single filesystem for all of `DATA_DIR` | `rename()` is only atomic within a filesystem; the staging directory must be on the same device as its destination | Installer compares `st_dev` of each mount |
| POSIX-compliant, case-sensitive | See P4 | Installer warns on case-insensitive mounts (macOS default) |
| Supports atomic `rename()` | Every writer depends on it | Implied by the above |
| Supports file locking | SQLite WAL | SQLite open test at startup |
| Free space ≥ projected 90-day usage × 1.2 | §14 of the HLD | Installer projection |
| Not a network filesystem (NFS/SMB) | Locking and rename semantics are unreliable | Installer warns and requires explicit override |

**Explicitly unsupported:** exFAT, FAT32 (no POSIX permissions, no reliable atomic
rename beyond limits, 4 GB file cap), and network filesystems as the primary data
store. The installer detects and refuses these rather than letting an operator
discover the problem at 3 a.m.

---

## 6. Generated file header

Every generated file begins with a header that makes its provenance unambiguous:

```yaml
# ┌─────────────────────────────────────────────────────────────────────┐
# │ GENERATED FILE — DO NOT EDIT                                        │
# │ Source:     config/cameras.yml                                      │
# │ Generator:  cctv-stack 1.0.0                                        │
# │ Generated:  2026-09-12T10:14:22Z                                    │
# │ Regenerate: ./update.sh --regenerate-config                         │
# └─────────────────────────────────────────────────────────────────────┘
```

Files carrying this header are overwritten without warning. `install.sh` and
`update.sh` detect hand-edits (by comparing a stored content hash) and, if found,
refuse to overwrite: they save the operator's version as `*.local-backup` and tell
the operator exactly what happened. Silently destroying someone's manual fix is not
acceptable; silently keeping it while claiming to have regenerated is worse.

---

## 7. Atomic write protocol

Every writer of a persistent file follows this sequence. It is what makes
[INV-4](../invariants.md) and crash-safety real rather than aspirational.

```text
1. Create a temporary sibling:   <dir>/.<name>.tmp.<pid>-<rand>
2. Write the complete content
3. flush() + os.fsync(fd)
4. os.rename(tmp, final)          ← atomic within a filesystem
5. fsync the containing directory  ← makes the rename itself durable
6. Remove stale .tmp files older than 24 h on startup
```

A crash at any point leaves either the old file, the new file, or a stale `.tmp`
that is reaped on next startup. Never a partially written file at the final path.

The database uses SQLite's own transaction + WAL guarantees instead, and is
excluded from this protocol.

---

## 8. Cleanup and reaping rules

| Artefact | Who may delete | When |
|---|---|---|
| `data/clips/**/*.mp4` | cleanup only | Retention expiry **and** terminal-success upload state ([INV-1](../invariants.md)) |
| `data/clips/**/*.mp4.json` | cleanup only | With its clip, and only after the clip is gone |
| `data/clips/**/.*.tmp.*` | clip-worker | On startup, if older than 24 h |
| `data/state/tmp/*` | the owning service | On startup and on its own TTL |
| `data/state/db-backups/*` | cleanup | Keep the newest N (default 10) |
| `data/media/**` | **Frigate only** | Frigate's own retention |
| `data/frigate/**` | **Frigate only** | Frigate's own policy |
| Empty date directories under `clips/` | cleanup | After the last clip in them is removed |

No component other than cleanup may delete anything under `clips/`. No component
other than Frigate may delete anything under `media/` or `frigate/`.

---

## 9. Naming rules (validation-enforced)

| Thing | Rule | On violation |
|---|---|---|
| Camera key | `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` | Hard error at config validation |
| Camera key reserved words | `all`, `none`, `review`, `latest`, `_`, `tmp` rejected | Hard error |
| `LOCATION` | `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` | Hard error — it is part of the cloud key prefix |
| Clip basename | Derived only; never operator-supplied | n/a |
| Any path component | No `/`, `\`, `..`, control characters, leading `.` | Hard error |

Camera names are load-bearing: they appear in filesystem paths **and** cloud object
keys. A name that is valid on one platform and invalid on another is a portability
bug waiting for a restore onto different hardware. Hence the strict rule and the
absence of uppercase.
