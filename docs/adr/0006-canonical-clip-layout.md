# ADR-0006: The canonical clip layout is deterministic and mirrors the cloud key

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-5](../architecture/invariants.md), [INV-12](../architecture/invariants.md), PRD §7

---

## Context

PRD §7 specifies the output convention precisely and asks that the application
guarantee it:

```text
clips/
└── <camera-name>/
    └── YYYY/
        └── MM/
            └── DD/
                └── HH-MM-SS.mp4
```

Several things are unstated by the PRD that must be decided for this to be
implementable:

1. **Where the time comes from.** Frigate speaks epoch seconds — an instant, which has
   no timezone — and the operator browses directories in local time. These are different
   kinds of value, and the rendering has to happen somewhere. See
   [ADR-0017](0017-time-handling.md).
2. **What happens on collision.** Two events from one camera can start in the same
   second.
3. **Whether the cloud key matches.** If the local path and the object key differ in
   structure, restore requires a mapping layer that can drift.
4. **Whether the path is derivable.** If the path is generated at write time rather
   than derived from metadata, then any component needing it must ask the writer, and
   reconstruction from a bucket listing becomes a reverse-engineering exercise.

## Decision

**Clip paths are a pure function of camera name, review start time, and the configured
display timezone, and the cloud object key is `<location>/clips/` concatenated with
the local path relative to `data/clips/`.**

```text
local:   data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
cloud:   <location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
```

Supporting decisions:

| Concern | Decision |
|---|---|
| Basis time | **Review** start time (`review.start_time`), per [ADR-0016](0016-clip-per-review-item.md). Not the event's |
| Timezone | `site.display_timezone`, defaulting to `site.timezone` — `Asia/Kolkata` for the first deployment. The stored value is an instant, which has no timezone ([ADR-0017](0017-time-handling.md)) |
| Collision | Append `-<last 6 chars of review id>`. Deterministic, never random, never a counter |
| Ownership | Exactly **one** implementation, in the shared library, used by clip-worker, uploader, cleanup, and restore. No component re-implements it |
| Sidecar | `<name>.mp4.json`, written after the clip |
| Separators | `HH-MM-SS` rather than `HH:MM:SS` — colons are invalid in Windows paths and hostile in URLs |
| Cameras | Names restricted to `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` because they appear in paths across platforms and in object keys |

## Consequences

### Positive

- **Materialisation is idempotent.** Re-running it produces the same path, so a retry
  cannot create a duplicate file ([INV-7](../architecture/invariants.md))
- **Any component can compute a clip's path from metadata alone**, with no query and no
  coordination. Cleanup, backup, verification, and restore all rely on this
- **Cloud restore is a prefix download.** Because the key mirrors the path, recovery is
  "strip the prefix, write the file" — no mapping table, no manifest resolution
- **Human-browsable.** The operator finds "the front door clip from 6pm yesterday"
  where they expect it, in their own timezone
- **Collisions are impossible-but-handled**, deterministically, including the
  repeated hour at a DST fallback
- **Portable.** The path is valid on Linux, macOS, and Windows, and in an object key

### Negative / accepted costs

- **Local-time filenames introduce a DST edge case.** During a fallback, the same
  `HH-MM-SS` occurs twice in one day, so collisions are guaranteed rather than rare.
  Handled by the deterministic suffix rule, and tested explicitly ([INV-12](../architecture/invariants.md))
- **Deviates slightly from a strict reading of PRD §7.** The PRD does not mention a
  collision suffix. This is an addition, documented here and in
  [`on-disk-layout.md`](../architecture/contracts/on-disk-layout.md), and it only
  appears in the rare colliding case
- **Two representations in play** (an unrendered instant in the database, a rendered
  local time in the path). This must be stated wherever paths are handled, because
  confusing them is the classic bug. Mitigated by a single path function that takes an
  explicit timezone, and by a test that walks a DST boundary
- **The basis time is now the review's, not the event's.** A review can start before its
  first event is confirmed and can contain several events, so a path no longer
  corresponds to any single event. `event_ids` in the sidecar preserves event-level
  searchability
- **Collision suffix is opaque.** `18-32-45-a1b2c3.mp4` means nothing to a human.
  Acceptable: the alternative is losing a clip or overwriting one
- **Clock skew on the host** propagates into filenames. Detection is via NTP checks in
  `doctor.sh`, and the database's UTC value remains authoritative

### Neutral

- A `.json` sidecar sits beside each clip. The PRD does not mention it. It is small,
  optional in principle, and it is what makes recovery and future re-processing
  possible
- Camera renames mean a new directory. Existing clips stay under the old camera name,
  because a rename is a new logical camera ([INV-13](../architecture/invariants.md))

## Alternatives considered

| Alternative | Why not |
|---|---|
| **UTC-only paths** | Simplest and immune to DST ambiguity, and matches Frigate exactly. Rejected because the directory tree is the primary browsing interface for a human, and a clip recorded at 6pm local appearing under `08-00-00` is a daily papercut. The database keeps UTC, so nothing is lost |
| `YYYY-MM-DD/HH-MM-SS.mp4` (date as one directory) | Fewer directories, but the PRD specifies nested `YYYY/MM/DD`, and nested directories keep any single directory small on a busy camera |
| Include the event id in every filename | Unique without a collision rule, but unreadable and contradicts PRD §7's stated layout |
| Include the label or a sequence number | The label is not known at path-derivation time in all cases, and a sequence number is stateful, non-deterministic, and therefore not derivable |
| Symlinks or hardlinks from Frigate's tree instead of materialising | Cheaper, but Frigate does not store clips as files, so there is nothing to link to. Also couples us to Frigate's internal layout |
| Generate the path at write time and store it | Works, but then nothing else can predict it. Cleanup could not derive what to look for, restore could not match objects to files, and the guarantee in PRD §7 would be a convention rather than a property |

## Revisit if

- Frigate begins storing clips as files, at which point the download step could become
  a link or a move rather than a copy
- DST collision handling proves confusing in practice and a UTC-only tree with a
  local-time UI becomes preferable
- A requirement appears for a different object-key layout (for example, date-major for
  a lifecycle rule strategy), which would require a migration plan for existing objects
