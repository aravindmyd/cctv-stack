# Diagnose a broken installation

When something is wrong and you do not yet know what, run this:

```bash
./scripts/doctor.sh
```

It checks the host, the storage, the configuration, the database, the network paths the
services use, and the clip pipeline — and it changes **nothing**. No migration, no restart, no
deletion. It is safe to run on a working system, and safe to run while someone is watching
footage.

The report ends with a line that tells you what to do next:

| Verdict | Meaning |
|---|---|
| `Everything doctor can check is fine.` | The stack is healthy as far as anything can tell. If it still feels broken, the problem is beyond what a check can see — send the report with a bug. |
| `Nothing is broken; the warnings are worth reading.` | Working, with something worth fixing. A camera that has been quiet for two days, a `.env` that is world-readable, a disk that will fill. |
| `Something is broken. The FAIL lines above are where to start.` | At least one check failed. The `FAIL` lines name the thing. |

```bash
./scripts/doctor.sh --json    # same report, machine-readable
```

Every run also writes `data/logs/doctor-<timestamp>.json`. That file is the one to attach to a
bug report: it is written from the same objects as the terminal output, so the two cannot
disagree, and credentials are redacted from it — a camera password never reaches it.

## Which checks exist

| Section | What it establishes |
|---|---|
| **host** | That this machine is on the support matrix, which profile it matched, and any warning the detector raised for it |
| **storage** | That the data directory is writable and has room; below 5 GiB free is a warning, because a full disk stops recording |
| **config** | That `cameras.yml` validates, how many cameras are enabled, that `.env` exists and is not readable by other users, and that Frigate's config has been generated |
| **database** | That `PRAGMA integrity_check` passes, that the schema version is the one this build expects, the clip counts per state, and the WAL size |
| **connectivity** | That `uploader`, `cleanup` and `status` answer `/health`, that Frigate answers `/api/stats`, and that MQTT accepts a connection |
| **pipeline** | The age of the oldest pending upload, any permanently failed uploads, and the last clip from each camera |
| **versions** | The `cctv` and Python versions running doctor |

A check that could not run is reported as `skip` with the reason. That is deliberate: a doctor
that passed a check it never performed would hide the one fact the operator came for.

## One section at a time

The report is ordered, and reading it in order answers most questions:

1. **host** and **storage** fail first on a host that cannot run this stack at all.
2. **config** fails next: a bad `cameras.yml` explains almost every downstream symptom, including
   cameras that never produce clips.
3. **database** failing means the state store is damaged or behind; everything else reads
   through it, so stop here.
4. **connectivity** and **pipeline** are where a stack that *installed* fine but does not *work*
   fine shows up.

## When doctor is not enough

- A stuck upload queue has its own page: [Recover failed uploads](recover-uploads.md).
- Clips that are short or misaligned: [Clip boundaries](clip-boundaries.md).
- Rebuilding after a disaster: [Disaster recovery](disaster-recovery.md).
