# Open questions

Decisions not yet made. Each has a **recommendation** so that not deciding does not
block progress — but each is genuinely open, and the owner's answer may differ.

**How to use this file.** When a question is answered, move the answer into the
relevant design document and delete the entry here, or convert it into an ADR if it was
significant. Do not leave answered questions in this file; a stale open-questions list
stops being read.

| # | Question | Recommendation | Blocks |
|---|---|---|---|
| OQ-1 | Repository hosting and project name | Decide before M1 | Nothing technically; everything socially |
| OQ-2 | Licence | Apache-2.0 | M8 |
| OQ-3 | Monitoring tool: Gatus or Uptime Kuma | Gatus | M5 |
| OQ-4 | Config escape hatch for raw Frigate options | Ship in `1.1`, design now | Nothing |
| OQ-5 | Sidecar JSON on by default | Yes | M2 |
| OQ-6 | Remux on by default | Yes | M2 |
| OQ-7 | Install a backup timer by default | Offer, do not impose | M7 |
| OQ-8 | Back up snapshots alongside clips | Not in v1 | M3 |
| OQ-9 | Run the uploader in local-only deployments | Yes, idle | M6 |
| OQ-10 | Frigate recording retention defaults | As specified, with a warning | M1 |
| OQ-11 | Clip filename timezone: local or UTC | Local | M2 |
| OQ-12 | Official support for Windows | Best-effort, documented | M6 |
| OQ-13 | Protect the status page with a password | No by default | M5 |
| OQ-14 | Per-event notifications ("person at the front door") | Not in v1 | M5 |
| OQ-15 | Reference hardware recommendation | Publish one | M8 |
| OQ-16 | Default pre-roll and post-roll values | 5 s / 10 s | M1 |

---

## OQ-1 — Repository hosting and project name

**Question.** Where does this live publicly, and what is it called?

**Why it matters.** The name appears in the image path (`ghcr.io/<owner>/cctv-stack`),
the `versions.yml` digest references, the docs, and every command in every runbook.
Renaming later means rewriting all of them and invalidating any installation that
references the old image.

**Recommendation.** Choose before M1. If GitHub, prefer an organisation over a personal
account so the project can outlive one person's account. Verify the name is not already
used by another CCTV project before committing to it.

**Notes for the eventual decision.**
- `cctv-stack` is descriptive and probably unclaimed
- Something more distinctive is easier to search for and to own
- The name should not imply a cloud service, since this is self-hosted

**Decide by:** before M1 begins.

---

## OQ-2 — Licence

**Question.** MIT or Apache-2.0 (or something else)?

**Recommendation: Apache-2.0.**

| | MIT | Apache-2.0 |
|---|---|---|
| Simplicity | Simplest possible | Longer |
| Patent grant | None | **Explicit** |
| Trademark clause | None | Yes |
| Contribution clarity | Implicit | Explicit (section 5) |
| Frigate's licence | — | MIT |

The patent grant is the deciding factor. This project integrates with hardware
accelerators and detection models, and a contributor's patent could otherwise create
ambiguity. Apache-2.0 costs nothing in practice — it is as permissive as MIT for anyone
using the software — and removes a class of risk.

**Consideration against:** MIT is what Frigate uses, and matching the ecosystem reduces
friction for contributors who do not read licences. This is a real argument and a
reasonable answer if simplicity is preferred.

**Also needed regardless:** a `THIRD-PARTY-NOTICES` file acknowledging Frigate and
other dependencies, and a note that the project name and logo are not covered by the
code licence.

**Decide by:** M8. Safe to defer, but cheap to settle now.

---

<a id="oq-3-monitoring-tool"></a>

## OQ-3 — Monitoring tool: Gatus or Uptime Kuma?

**Question.** Which external tool provides the dashboard, status page, and alert
delivery?

**Context.** The owner's guidance was to reuse a tool like Uptime Kuma rather than
building a dashboard. During design, one requirement turned out to be structural:

> **The tool must be configurable from a file we generate.**

Because everything else in this project is generated from a single source of truth
([ADR-0003](../adr/0003-cameras-yml-single-source-of-truth.md)), a monitoring tool that
can only be configured through a UI breaks that model and creates a component whose
state lives only in a UI and a container volume — which also undermines
[INV-4](../architecture/invariants.md) unless its volume is mounted carefully.

| Requirement | Gatus | Uptime Kuma |
|---|---|---|
| Configurable from a generated file | ✅ native — the product *is* a YAML file | ❌ no official REST API; socket.io only |
| ntfy + Telegram | ✅ | ✅ |
| Status page | ✅ | ✅ (nicer, more customisable) |
| Push heartbeats | ✅ | ✅ |
| Provisioned by the installer | ✅ trivial | ⚠️ community socket.io client, or a manual import step |
| Notification breadth | Good | Excellent (~90 providers) |
| UI polish | Functional | Better |

**Recommendation: Gatus**, for the first row. Everything else is comparable, and the
first row determines whether `install.sh` can configure the tool at all.

**If Uptime Kuma is preferred anyway** (its UI is genuinely nicer, and it is more
widely known): the cost is one documented manual step — the installer generates a
`kuma-import.json` and prints the exact import instructions. That is acceptable, and
the `MonitoringAdapter` interface means it is a contained decision.

**Note.** This question nearly caused a change to [ADR-0010](../adr/0010-external-monitoring-tool.md).
The ADR was written to be tool-agnostic and to keep the choice open, which is why the
adapter exists.

**Decide by:** M5.

---

## OQ-4 — Config escape hatch for raw Frigate options

**Question.** `cameras.yml` covers the common cases. What should an advanced operator do
when they need a Frigate option we do not expose — custom `ffmpeg` arguments, a
stationary-object threshold, a zone polygon?

**Options**

| Option | Pros | Cons |
|---|---|---|
| Pass-through block: `frigate_overrides:` merged verbatim into the generated config | Simple, flexible, obvious | Escapes validation; a bad merge produces a broken config; the generated file stops being fully explainable |
| Explicit support for each requested option | Validated, documented, safe | Slow; every request is a release |
| Tell operators to edit the generated file | No work | Defeated by the drift protection — we refuse to overwrite hand-edits, so they would have to re-apply on every regeneration. Worse than useless |
| No escape hatch | Simplest, most predictable | Advanced users cannot proceed, and will fork |

**Recommendation.** Ship the pass-through block in `1.1`, and design its validation
rules now so M1 does not paint us into a corner. Requirements if adopted:

- Merged **last**, after all generated values, so it can override anything
- Validated by asking Frigate to validate the result before applying
- A visible warning whenever it is non-empty: "this configuration contains overrides
  that cctv-stack does not validate"
- Documented as "you are on your own" for anything inside it, without being hostile
  about it
- Never needed for cameras, RTSP, detection resolution, retention, or notifications —
  those are first-class

**Decide by:** `1.1`, but design the merge point now.

---

## OQ-5 — Sidecar JSON files on by default?

**Recommendation: yes, on by default**, with `json_sidecar: false` to disable.

Each clip gains a `<name>.mp4.json` beside it. The PRD specifies only `.mp4`, so this is
strictly an addition.

| Argument for | Argument against |
|---|---|
| ~1 KB per clip — negligible against a 7 MB video | Doubles the file count, which some operators find untidy |
| Enables recovery without a database ([`lld/08`](../architecture/lld/08-backup-and-restore.md)) | The database is authoritative anyway |
| Preserves the full Frigate event payload, making "we ignored field X" safe | Contains metadata some operators may consider sensitive |
| Enables re-processing later (new fields, new analysis) without re-fetching from Frigate — which may be impossible once recordings expire | One more thing to upload, delete, and keep in sync |
| Makes debugging trivial: read the file next to the clip | |

The fourth argument for is the strongest: after Frigate's recordings expire, the event
payload exists **nowhere else**. Not storing it discards information we can never
recover.

**Decide by:** M2.

---

## OQ-6 — Remux on by default?

**Recommendation: yes, on by default.**

Frigate returns a fragmented MP4 (`frag_keyframe+empty_moov`). Our default is
`ffmpeg -c copy -movflags +faststart`: no re-encoding, just a container metadata
rewrite, roughly 50–200 ms per clip.

| Argument for | Argument against |
|---|---|
| Plays in every client, including browsers | ~100 ms CPU per clip on the host |
| Moov atom at the front enables progressive playback | One more failure mode (handled: a remux failure stores the original) |
| Better for any future browser-based clip browser | Slightly different bytes from what Frigate served |
| Cost is I/O-bound, not CPU-bound | |

**Decide by:** M2, after M1 measures the actual cost.

---

## OQ-7 — Install a backup timer by default?

**Recommendation: offer it, do not impose it.**

The question is whether `install.sh` should install a systemd timer (or cron entry) for
`backup.sh`.

| Argument for | Argument against |
|---|---|
| Unattended backups are the only ones that happen | A silently failing cron job is the classic backup failure |
| A systemd timer can be monitored properly | It is a change to the host outside the compose boundary, which PRD §1 discourages |
| The freshness health check makes failures visible | Backup destinations need decisions (where, encryption key, retention) that belong to the operator |

**Recommended shape:** ask during install, explain exactly what would be installed and
where output goes, and default to "yes" for the state backup and "ask me later" for
anything requiring an external destination. Whatever is chosen, backup freshness is a
health check either way — so an operator who declines still sees "never backed up"
rather than nothing.

**Decide by:** M7.

---

## OQ-8 — Back up snapshots alongside clips?

**Question.** Frigate captures a JPEG snapshot per event. Should those go to B2 too?

**Recommendation: not in v1.**

| For | Against |
|---|---|
| A still image is far cheaper to store and faster to browse than a video | ~100 KB × events/day is small but not nothing |
| Useful for a quick "what happened" without downloading video | Adds an upload path, a state machine branch, and deletion logic |
| Useful for notifications (attaching an image) | Frigate already stores snapshots locally, and `osq-14` (notifications) is out of v1 |
| | Doubles the object count for modest benefit |

Natural time to revisit: when notifications or a clip browser are added, since both
would want a still image.

**Decide by:** `1.1`.

---

## OQ-9 — Run the uploader in local-only deployments?

**Question.** If an operator answers "no" to cloud backup during install, should the
uploader container run at all?

**Recommendation: run it, idle, reporting `SKIPPED_BY_POLICY`.**

| Alternative | Consequence |
|---|---|
| Run it idle (**recommended**) | One idle container, but the pipeline's shape is identical in every deployment, so there is one topology to test and one set of failure modes to understand. Enabling cloud backup later is a config change |
| Omit it via a compose profile | Saves ~40 MB of RAM, but creates two topologies to test, and makes enabling backup later a compose change rather than a config change |
| Run it and let it fail | No — a failing container looks like a fault |

The `SKIPPED_BY_POLICY` state exists precisely so that a deliberate local-only
deployment is distinguishable from a broken one, in health and on the status page.

**Decide by:** M6.

---

## OQ-10 — Frigate recording retention defaults

**Recommendation: keep as specified, with a clear warning.**

| Setting | Proposed default |
|---|---|
| `record.continuous.days` | `0` (disabled) |
| `record.motion.days` | `3` |
| `record.alerts.retain.days` | `30` |
| `record.detections.retain.days` | `14` |

The `3`-day motion window is not an arbitrary default: it is the clip-worker's recovery
budget if it is down ([INV-9](../architecture/invariants.md)). Changing it changes how long
an outage can last before clips are permanently lost.

**The question for the owner:** is 3 days the right recovery budget, or is it worth
more disk to have a longer one? At 2 Mbps and 20 % motion duty, 7 days costs roughly an
extra 30 GB per camera.

**Also relevant:** the PRD's "90-day local retention" describes **clips**, not
continuous recordings. These are different things at very different costs (66 GB vs
1.9 TB at the reference capacity), and the docs must be explicit about that or an
operator will reasonably expect 90 days of continuous video.

**Decide by:** M1.

---

## OQ-11 — Clip filename timezone: local or UTC?

**Recommendation: local time, with UTC always stored in the database.**

Frigate speaks UTC everywhere. The operator browses directories in local time. These
disagree for most of the world.

| | Local filenames | UTC filenames |
|---|---|---|
| Human browsing | Natural: "the clip from 6pm" is where you expect | Confusing: 6pm appears as 08:00 |
| DST ambiguity | Collisions at fallback, handled by a deterministic suffix | None |
| Alignment with Frigate | Requires conversion | None |
| Debugging | Requires knowing the configured timezone | Unambiguous |

**Recommendation: local**, because the directory tree is the primary browsing interface
for a human, and the DST collision is handled deterministically. UTC is always
recoverable from the database and the sidecar.

This is recorded in [ADR-0006](../adr/0006-canonical-clip-layout.md); confirming it here
because the DST edge case is the kind of thing worth an explicit "yes, I accept that".

**Decide by:** M2.

---

## OQ-12 — Official Windows support?

**Recommendation: best-effort, documented, not a deployment target.**

The owner has a Windows machine. Windows support means Docker Desktop with the WSL2
backend, plus significant caveats:

| Caveat | Detail |
|---|---|
| USB passthrough | Coral USB via WSL2 is unreliable and should be documented as unsupported |
| Filesystem | `DATA_DIR` must be in the Linux filesystem, **not** under `/mnt/c` — the semantics and performance are both unacceptable |
| Docker Desktop required | Not plain Docker; a licensed desktop product |
| `systemd` | Not available, so timers and service integration differ |
| Testing burden | Every release would need a Windows verification pass |

**Recommended position:** support WSL2 as a documented, best-effort path with the
caveats stated plainly, and keep Windows out of the CI matrix. Developers on Windows
should use WSL2 with an Ubuntu distribution, which is effectively a Linux deployment.

**Decide by:** M6.

---

## OQ-13 — Password-protect the status page?

**Recommendation: no by default; document how to add one.**

| For | Against |
|---|---|
| The page shows camera names, disk usage, and failure reasons — useful reconnaissance | It is published only on the LAN, not the internet |
| | A password on a diagnostics page is a support burden: "I can't get in to see what's wrong" |
| | It would need its own auth, session handling, and password reset — exactly the kind of UI work we are avoiding |
| | Anyone on the LAN can already reach Frigate's UI |

**Recommended shape:** unauthenticated on the LAN by default, with a documented
`STATUS_AUTH_TOKEN` option and a README note recommending the whole stack sit behind a
VPN or reverse proxy if the LAN is not trusted. The page exposes no footage and no
credentials.

**Decide by:** M5.

---

## OQ-14 — Per-event notifications?

**Question.** Should the system send a notification when a person is detected at the
front door, or only when something is unhealthy?

**Recommendation: not in v1.**

| For | Against |
|---|---|
| It is the most requested feature in every NVR project | Frigate already supports notifications via Home Assistant, MQTT, and its own integrations — we would be duplicating a solved problem |
| It is genuinely useful for a business | Notification design is a deep rabbit hole: cooldowns, zones, per-camera routing, quiet hours, escalation |
| | It would pollute the alerting channel that exists for system health. An operator who ignores alerts because of pet notifications will miss a disk-full warning |

**Recommended position:** health alerts only. Document in the README how to wire Frigate's
own notifications (Home Assistant, or an `frigate/events` MQTT subscriber) for the
people-detection case. If we build it later, use a **separate notification channel** from
system health, so the two can never be confused.

**Decide by:** M5, and revisit in `1.1`.

---

## OQ-15 — Reference hardware recommendation

**Question.** Should the project publish a "buy this" recommendation?

**Recommendation: yes, in M8.**

Operators repeatedly ask "what should I run this on?" A short, opinionated page removes
the most common pre-installation friction and reduces the number of Raspberry Pi
deployments that disappoint their owners.

**Suggested content**

| Tier | Hardware | Camera guidance |
|---|---|---|
| Minimum | Raspberry Pi 5, 8 GB | 2 cameras at 640×360, CPU detection only, no continuous recording |
| **Recommended** | x86 mini-PC (N100 or similar), 16 GB, SSD, Intel iGPU | 6–8 cameras, OpenVINO detection, hardware decode |
| Best | Same plus a Coral Edge TPU or Hailo-8L | 12+ cameras, low CPU |
| Storage | SSD for `DATA_DIR`; avoid SD cards for continuous writes | — |

Each row should state what it can and cannot do, and the Pi row should be honest that
CPU detection is slow rather than implying it works fine.

**Decide by:** M8.

---

## OQ-16 — Default pre-roll and post-roll values

**Question.** How much footage should sit outside the event window by default?

**Recommendation: `pre_roll_s: 5`, `post_roll_s: 10`.** Both configurable globally and
per camera.

This came directly from the owner's prior experience that clip generation "had a lot of
issues". Pre-roll and post-roll fix one of the four causes of bad clips — the other three
are the segment-flush race, missing segments, and `-c copy` cut precision, all addressed
in [ADR-0015](../adr/0015-clip-boundaries-and-padding.md).

### Why post-roll should exceed pre-roll

`event.end_time` is when Frigate **stopped tracking**, which is usually well before the
subject has left the frame — a person walks out of the detection zone but is still
visible, a car is tracked until it stops moving but the driver then gets out. Post-roll
is therefore the more valuable of the two.

`event.start_time` is when tracking **began**, and Frigate's own detection already has
some lead-in before it confirms an object. So a smaller pre-roll is usually enough.

### The tension with pre-roll

> **Pre-roll reaches outside what the event pin protects.**

The pin ([INV-9](../architecture/invariants.md)) protects the *event*. Recording segments
*before* `event.start_time` may fall outside what Frigate considers retained, so a large
pre-roll can ask for footage that is already gone — producing a shorter clip and lowering
coverage for a reason no retry can fix. Hence the modest 5 s default and a validation
warning above 30 s.

### Cost of the defaults

| | Per clip | 100 events/day | 90 days |
|---|---|---|---|
| +10 s at 2 Mbps | ~2.5 MB | ~250 MB/day | **~22 GB** |

That is the honest price of not missing the beginning and end of every event. It is
roughly a third of the clip storage itself, which is why the values are a question rather
than a hardcoded constant.

### What to consider

| If your cameras are… | Consider |
|---|---|
| A doorway or hallway (fast transit) | Defaults are fine |
| A driveway or car park (slow manoeuvring) | `post_roll_s: 20` per camera |
| A large yard with slow approach | `pre_roll_s: 10` — but accept it is less protected |
| Storage-constrained (Pi, small SSD) | `post_roll_s: 5`, and review `coverage_ratio` before trimming further |
| Producing clips you fast-forward through | Reduce both; the defaults are not a target |

**M1 will produce real data before this needs answering.** The milestone measures the
actual flush delay and cut precision on your hardware, and you can then check a few real
clips to see whether the defaults are right for your camera placements. Answer it after
M1 rather than in the abstract.

**Decide by:** M1 exit, using the measured flush delay and a sample of real clips.
