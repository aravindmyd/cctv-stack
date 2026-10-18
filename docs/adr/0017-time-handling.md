# ADR-0017: Time is stored as an instant and rendered in the site timezone

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-12](../architecture/invariants.md), [ADR-0006](0006-canonical-clip-layout.md), [`cameras-config-schema.md`](../architecture/contracts/cameras-config-schema.md)

---

## Context

The owner asked for timestamps to be "IST, not UTC". Read literally that is not a thing
that can be done, and the misunderstanding is worth removing at the root, because it is
the most common source of time bugs in a system like this.

Three different things get called "the time":

| Thing | Example | Timezone |
|---|---|---|
| An **instant** | `1718987129.308396` | none — the same instant everywhere on earth |
| A **rendered** time | `2024-06-21 18:45:29` | whatever zone you render it in |
| A **path or key** | `clips/front-door/2024/06/21/18-45-29.mp4` | whatever zone its parts were rendered in |

Frigate speaks in instants (epoch seconds). So does the database. An instant has no
timezone, so storing it "as UTC" and storing it "as IST" are the same operation. The only
real question is what zone the **rendered** parts use.

The project convention said "timestamps are stored in UTC everywhere; local time is a
presentation concern only", with clip filenames rendered in `site.display_timezone`
([ADR-0006](0006-canonical-clip-layout.md)). The owner's intent — a directory tree that
reads naturally in Indian Standard Time — is already what that design produces. It was
described in a way that reads as "everything is UTC", which is why it looked wrong.

Two further facts shape the decision:

- **IST is UTC+05:30 with no DST.** India has never observed DST, so this deployment has
  no ambiguous hour and no missing hour. ADR-0006's fallback-collision case cannot arise
  here.
- **This project intends to be open-sourced, and its own invariant is that one deployment
  differs from another only by configuration.** Hard-coding `Asia/Kolkata` into path
  rendering would hand a site in London IST folder names.

## Decision

**Instants are stored as epoch seconds and are never rendered in storage. The site's
configured timezone is applied only at the edges, by exactly one function.**

| Layer | Representation | Timezone |
|---|---|---|
| `state.db` (`REAL` epoch), comparisons with Frigate, leases, watermarks | epoch seconds | none — an instant |
| Cloud object metadata (`start-time`, `end-time`) | epoch seconds, as a string | none |
| **Clip path components** (`YYYY/MM/DD/HH-MM-SS`) | rendered | `site.display_timezone`, defaulting to `site.timezone` |
| Logs, `/status` JSON and HTML, notifications | RFC 3339 with offset | `site.timezone` |
| Frigate's own recordings tree | Frigate's business | **UTC** — measured, and independent of the container's `TZ`. See below |

Supporting decisions:

1. **`site.timezone` is mandatory and never guessed.** The installer requires an explicit
   IANA name and does not infer one from the host. The first deployment is
   `Asia/Kolkata`. This was already the design; it is restated because it is now
   load-bearing for a decision the owner explicitly made.
2. **One rendering function.** `path_parts(instant, tz)` is the only place a path
   component is produced, and it takes the timezone explicitly. No component
   re-implements it.
3. **The timezone is resolved once, at config load** — not per call, and not from the
   process environment.
4. **`datetime.now()` with no timezone, and `time.localtime`, are forbidden in service
   code.** A lint rule enforces it.
5. **IST is not special-cased.** Nothing branches on the offset, and `+05:30` needs no
   handling beyond correct `zoneinfo` use.

## Consequences

### Positive

- **The tree reads in local time**, which is what the operator asked for: the door clip
  from 6:45 pm is at `18-45-29.mp4`, not `13-15-29.mp4`.
- **Nothing is lost by storing instants.** Any zone, including UTC, stays derivable at
  any point — rendering is a pure function of a stored value.
- **Comparisons with Frigate are exact.** Frigate sends instants; we compare instants.
  There is no conversion step that could be applied twice or in the wrong direction.
- **Still config-only per site.** `Asia/Kolkata` here, any IANA zone at the next site,
  with no code change — the property that makes the project publishable.
- **No DST hazard for this deployment**, because IST has none.

### Negative / accepted costs

- **Two representations are in play** — instants internally, rendered strings at the
  edges — so anyone touching paths has to understand the conversion. Mitigated by there
  being exactly one function that performs it.
- **Sites in DST zones inherit the fallback collision**, handled by ADR-0006's
  deterministic suffix. Accepted, and stated in the runbook.
- **Frigate's recordings tree is UTC — measured, so our original documentation was
  right.** The concern was that `segment_path_time()` derives the directory from Frigate's
  own segment naming and might therefore follow the container's `TZ`. Measured on
  2026-09-13 with `TZ=Asia/Kolkata` and a local clock reading 00:24 IST: segments landed
  under `recordings/2026-09-12/18/`, which is UTC. The tree is UTC whatever the container
  is set to, so [`disaster-recovery.md`](../runbooks/disaster-recovery.md) may rely on
  that, and no code change follows. The measurement came free with the first run of
  [`dev-synthetic/`](../../dev-synthetic/README.md) (in the repo root)
- **Displayed time can disagree with Frigate's own UI** if the two are configured with
  different zones. IST's `:30` offset makes that more visible than a whole-hour offset
  would. Added as a `doctor.sh` check.

### Neutral

- The cloud object key inherits the rendered path, so bucket listings read in local time
  too. Consistent with ADR-0006.
- Changing a site's timezone later re-renders only **new** paths. Existing clips keep
  their names, which remain derivable because the instant is stored.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Store rendered local-time strings instead of instants** | The literal reading of the request. Rejected: it turns every comparison with Frigate into a parse-and-convert, makes a DST repeat silently overwrite, makes ordering lexicographic rather than numeric, and loses the ability to re-render in another zone. Nothing is gained |
| **Hard-code `Asia/Kolkata`** | Satisfies this deployment exactly and is the simplest possible code. Rejected: it breaks the packaging-first premise and the open-source goal — a second site would need a code change |
| **Store UTC and also a rendered string** | Denormalised duplication that can drift, with no benefit over rendering on demand from the instant |
| **Render paths in UTC and localise only the UI** | Simplest and DST-immune, and was ADR-0006's rejected alternative. Rejected again for the same reason: the directory tree is the browsing interface, and `13-15-29` for a 6:45 pm event is a daily papercut |
| **Let the process `TZ` environment variable decide** | Implicit, easy to get wrong across six containers, and invisible in the config. Rejected in favour of one explicit `site.timezone` |

## Revisit if

- A site is deployed in a DST zone and the collision handling proves confusing in
  practice, at which point a UTC tree with a local-time UI may become preferable
- A second deployment in another zone exposes a place where the rendering function was
  bypassed
