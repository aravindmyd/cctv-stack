# ADR-0015: Clip boundaries err outward — configurable pre-roll, post-roll, and a flush-aware fetch schedule

- **Status:** Accepted — **confirmed by measurement (M1)**
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [INV-9](../architecture/invariants.md), [INV-14](../architecture/invariants.md), [`lld/02`](../architecture/lld/02-clip-worker.md), [`contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md)

---

## Confirmed by measurement

Probed against Frigate **0.18.0-77a66e7** on 2026-09-12. Full evidence in
spike/RESULTS.md (in the repo root — removed from docs).

| Claim in this ADR | Measured | Verdict |
|---|---|---|
| An immediate fetch returns a truncated clip | **5 of 6 sampled lags returned `200` with a short clip**; full length only at lag **10 s** | ✅ **Confirmed, and worse than assumed** |
| `-c copy` errs outward | mean **+1.88 s** per 10 s clip, **5/5 samples outward** | ✅ Confirmed |
| An unavailable range is `400`, not `404` | **`400 "No recordings found"`** | ✅ Confirmed |
| Audio survives the cut and remux | preserved as **AAC**, browser-playable | ✅ Confirmed |

**The flush race was worse than the design assumed.** `segment_flush_grace_s` defaulted
to 5 s; the measured floor is **10 s**. With the original default, essentially every clip
fetched promptly after an event would have ended early — silently, on a green dashboard.
The default is now **15 s**.

This is the clearest vindication of doing M1 before M2: the defect would have shipped,
and nothing in the design would have detected it.

### A correction this ADR owes

The gap detector as specified — packet-timestamp deltas above `max_gap_s` — **does not
work**. A healthy 30 s clip showed a **3.55 s** packet gap against a **2.55 s** keyframe
interval. The likely cause is that `-c copy` concatenation of recording segments produces
timestamp discontinuities at segment joins, so a gap in PTS is not evidence of missing
footage.

Left as designed, `max_gap_s: 2.0` would have flagged nearly every clip as holed — and a
signal that always fires carries no information. The criterion must change before it
ships: detect gaps against Frigate's `recordings` table (a missing segment is a known
fact, not an inference), or compare clip duration against the summed durations of the
overlapping segments. Until then `max_gap_s` is **advisory only** and no health signal
may depend on it. See [`lld/02`](../architecture/lld/02-clip-worker.md#9-configuration).

---

## Context

The project owner reported, from prior experience with Frigate: *"I remember I got a lot
of issues when properly generating the event clips."*

That is a specific and well-founded complaint, and investigating it turned up **four
independent causes**, of which only one is about padding. Treating it as a single
"add pre-roll" problem would fix one cause and leave three.

### Cause 1 — Boundaries are the event's boundaries (static, by design)

`event.start_time` is when Frigate first tracked the object, and `event.end_time` is when
it stopped tracking it. A clip cut to exactly that range therefore:

- **starts after the action began.** The person is already halfway up the driveway at
  `start_time`; the approach is missing.
- **ends before the action is over.** Frigate stops tracking when the object is no longer
  detected — often while the subject is still in frame or still walking away.

This is the cause pre/post-roll addresses.

### Cause 2 — The fetch races Frigate's segment flush (timing)

Frigate does not store event clips as files. It cuts them on demand from **recording
segments**. Per Frigate's own recording documentation, new segments are written to cache
and only moved to disk when they match the retention policy.

So when `frigate/events` reports an event as ended, **the final recording segment
containing the end of that event may not be on disk yet.** Fetching immediately produces
a clip that is silently truncated — it ends early, with no error, no exception, and no
indication that anything is missing. The clip looks fine. It is just short.

This is the most damaging of the four causes precisely because it is invisible. It is
almost certainly a major contributor to the owner's experience, and **pre-roll and
post-roll alone do not fix it** — in fact a naive fix makes it worse, because you still
fetch immediately, just with a wider requested range that the missing segment cannot
satisfy.

### Cause 3 — Missing segments produce silent holes

If the camera dropped, or a segment was already expired by Frigate's own retention
before we fetched, `-c copy` concatenation simply joins what remains. The result is a
clip with a **jump cut**, unflagged. The footage lawfully recorded on either side is
present; the middle is gone, and nothing says so.

### Cause 4 — `-c copy` cannot cut frame-exactly

Frigate's cut is a stream copy (`-c copy`) over a concat of segments with
`inpoint`/`outpoint` directives. Stream copy can only cut at keyframe boundaries, so the
achieved range will not match the requested range exactly. Depending on alignment, the
result can over- or under-run by up to one GOP.

**Overshoot is harmless. Undershoot is data loss.** Any design that tries to make the
boundaries exact by trimming will, by the same mechanism, be unable to trim to the exact
requested point and may cut *into* the footage the operator wanted.

## Decision

**Clip boundaries err outward, always. We request a generous range, we never fetch before
Frigate has flushed the segments, we measure what we actually got, and we retry rather
than accept a short clip.**

### The requested range

```text
requested_start = event.start_time - clip.pre_roll_s     (default 5 s)
requested_end   = event.end_time   + clip.post_roll_s    (default 5 s)
```

Both are configurable globally in `app.yml` and per camera in `cameras.yml`.

### The endpoint: the range endpoint, not `?padding=`

```http
GET /api/{camera_name}/start/{requested_start}/end/{requested_end}/clip.mp4
```

This **replaces** the earlier decision to use
`GET /api/events/{event_id}/clip.mp4?padding=N`. That reversal is deliberate and recorded
here rather than changed quietly.

| | Range endpoint | Event endpoint with `padding=N` |
|---|---|---|
| Asymmetric pre/post-roll | ✅ exact | ❌ **symmetric only** — Frigate applies `start − N` and `end + N` |
| Cost of asymmetric defaults | none | wasteful, see below |
| `has_clip` check | ✗ | ✅ |
| Error when no footage exists | `400` + `"No recordings found for the specified time range"` — **more informative** | `404`, ambiguous |

**Why the `padding` waste matters.** Frigate's padding is symmetric, so a
`pre_roll = 5, post_roll = 20` request requires `padding = 20`, yielding footage from
`start − 20` to `end + 20`. That is 15 unnecessary seconds per clip, on the front, of
footage the operator did not ask for:

```text
15 s × 2 Mbps / 8 = 3.75 MB per clip
× 100 events/day   = 375 MB/day
× 90 days          = ~34 GB of unwanted footage, growing weekly
```

That is not a rounding error for a home deployment. The range endpoint avoids it
entirely and supports genuinely asymmetric rolls, which is what a real deployment wants
(usually more post-roll than pre-roll, because Frigate stops tracking before the subject
has left).

**Losing the `has_clip` check costs us nothing**: we already hold the event payload from
MQTT or the sweep, and it carries `has_clip`.

### The fetch schedule: never before the flush

```text
fetch_not_before = event.end_time + post_roll_s + segment_flush_grace_s   (default 5 s)
```

Enqueued items carry a `not_before` timestamp. A worker does not touch an item before it
is due. This directly addresses Cause 2: the requested range now ends *after* the
recording segment containing `event.end_time` has had time to be flushed to disk.

It also means the pin is held a little longer, which is exactly right — the pin is what
protects those segments from Frigate's retention while we wait ([INV-9](../architecture/invariants.md)).

### Verification: measure, then decide

After downloading, measure what was actually obtained and act on it:

```text
requested_s = requested_end − requested_start
actual_s    = ffprobe format.duration
coverage    = actual_s / requested_s
```

| Coverage | Action |
|---|---|
| ≥ `coverage_ok` (0.95) | Accept |
| `coverage_retry` (0.80) – 0.95 | Log `WARNING`, **keep it**, mark `degraded`, and retry once later in case a later fetch does better |
| < 0.80 | **Do not accept.** Wait `flush_retry_delay_s` and re-fetch, up to `max_flush_retries` (default 3) |
| Still short after retries | **Accept the best result obtained**, mark `degraded`, record `CLIP_COVERAGE_LOW`, alert |

The last row is the important one and the point of the whole design: **a short clip is
always kept.** Discarding it on the theory that a better one might arrive is how you turn
a degraded clip into no clip.

### Never truncate

There is no trimming step by default. If the achieved range over-runs the request, we keep
the extra footage.

`clip.trim_to_range` exists as an opt-in for operators who want exact boundaries and accept
the consequences: a `-c copy` trim can only cut at keyframes, so it cannot hit the exact
requested point, and it can only ever remove footage — never restore it. It is off by
default and the documentation says plainly why.

### Preserve every stream

`-c copy` throughout. The remux maps all streams and never passes `-an`. If the camera
provides audio, the clip keeps it. The stream layout (`has_audio`, video codec, audio
codec) is recorded in the sidecar and the database, because "the clip has no sound" is
otherwise indistinguishable from "the camera has no microphone".

### Detect and record gaps

`ffprobe -show_packets` yields inter-packet deltas. A delta above `max_gap_s`
(default 2.0) is a hole from a missing segment.

A clip with gaps is **recorded and flagged**, never discarded and never repaired. The
largest gap is stored, `has_gaps` is set, and the clip appears in the status page's
count. Repair would be re-encoding or re-fetching, and neither can restore footage that
Frigate no longer holds.

### Long events are not truncated

Multi-hour events (a parked car tracked all night) are stored in full. Truncating to a
cap would be data loss. Above `long_clip_warn_s` (default 1800) we log `WARNING`, mark
the clip, and surface the count — because it is a *storage* problem worth knowing about,
not a *clipping* problem worth "fixing".

## Consequences

### Positive

- **Clips start before the action and end after it**, which is what makes them usable as
  evidence.
- **The invisible truncation failure is designed out**, not merely mitigated: the fetch
  cannot run before the segment is on disk, and if it still comes back short we retry
  and then keep what we have.
- **Asymmetric rolls are free**, so operators can favour post-roll (the common need)
  without paying for symmetric padding. Roughly 34 GB saved at reference settings.
- **Boundary quality is measurable.** `coverage_ratio`, `max_gap_s`, `has_audio` and the
  requested/actual range are all persisted, so "are the clips right?" becomes a query
  rather than an opinion — and the answer can be asserted in CI.
- **Overshoot is accepted rather than engineered away**, which removes an entire class of
  bug: nothing in the pipeline ever removes footage to make a number match.
- Fits the existing architecture: `not_before` is one column, the fetch schedule is one
  comparison, and the pin already provides the protection the longer wait needs.

### Negative / accepted costs

- **Clips are longer, so storage grows.** +10 s per clip at defaults (~2.5 MB) → ~225 MB
  per 100 events per day. Deliberate: storage is cheap, missing footage is not.
- **Latency from event end to clip on disk increases** by `post_roll + flush_grace`
  (~10 s at defaults) plus any retry delay. Acceptable — nothing user-facing depends on
  a clip existing within seconds.
- **The pin is held longer**, consuming more of `max_pinned_events` and Frigate's disk.
  The pin reaper and the budget check already bound this; the defaults remain far from
  the limit at typical event rates.
- **Retention headroom shrinks slightly.** The effective deadline is now
  `event.end_time + post_roll + grace` rather than `event.end_time`, so the recovery
  window is a few seconds shorter. The headroom health check must account for it
  (it does).
- **Pre-roll is less protected than event footage.** The pin covers the *event*; segments
  *before* `start_time` may fall outside what Frigate considers retained. A large
  pre-roll can therefore reach into footage that is not protected. Hence a modest default
  (5 s) and a validation warning above `pre_roll_warn_s` (30 s).
- **`coverage` cannot distinguish "the pipeline truncated it" from "Frigate never had
  it".** Both look like a short clip. That is acceptable because the response is the same
  either way — retry, then keep and flag — and the alternative would be to guess.
- **Gap detection costs an extra `ffprobe` pass** over packet timestamps. Bounded by
  `gap_check_max_s` (default 300): above that duration we sample rather than scan, and
  record that we sampled.

### Neutral

- The range endpoint's `400 "No recordings found"` must be classified as **retryable**,
  not as a bad request. It usually means the flush has not happened yet. Getting this
  wrong would turn every flush lag into a permanent failure.
- The event endpoint with `padding = max(pre_roll, post_roll)` becomes the documented
  fallback if the range endpoint is unavailable in the pinned version.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep the event endpoint with symmetric `padding`** | Works, one call, keeps `has_clip`. Rejected because asymmetric rolls cost real storage (~34 GB at reference settings) and the operator's honest need — more post-roll than pre-roll — is exactly the asymmetric case |
| **Symmetric padding, then trim the excess** | Would make padding exact in effect. Rejected because trimming is a `-c copy` cut that can only land on keyframes, so it cannot hit the requested point exactly, and it only ever removes footage. It adds a failure mode to save storage we can afford |
| **Trim with a re-encode for exactness** | Frame-exact. Rejected outright: re-encoding degrades evidence quality, costs real CPU on the host, and exists only to satisfy an aesthetic preference. This system stores what the camera recorded |
| **Fetch immediately, accept whatever Frigate gives** | The status quo that produced the reported problem. Rejected |
| **Fetch immediately, retry until duration looks right** | Better, but it fights the flush by hammering instead of waiting for it. The `not_before` schedule is strictly better: it avoids most retries rather than repeating them |
| **Increase Frigate's recording retention instead** | Does not help. A longer retention window changes when footage *expires*, not when it *flushes*. The segment is not on disk yet |
| **Store continuous recordings and cut from them ourselves** | Would give exact control over boundaries. Rejected: it means reimplementing Frigate's segmenter, conflict with [ADR-0001](0001-frigate-as-detection-engine.md), and storage 30× larger than clip-only retention |
| **Discard clips below a coverage threshold** | Would make every clip "correct" by refusing to store the imperfect ones. Rejected as the worst option available: it converts a degraded clip into no evidence at all |
| **Default pre-roll of 30 s to be safe** | Tempting. Rejected because pre-roll reaches outside what the pin protects, so it is the least reliable footage in the clip, and because it inflates every clip for a case that is better handled by Frigate's own detection lead-in |

## Revisit if

- Frigate gains a configuration key for event clip padding, at which point the generator
  can set it globally and the per-request range becomes a refinement rather than the
  mechanism
- Frigate exposes a signal that a recording segment has been flushed, which would replace
  the `segment_flush_grace_s` estimate with a fact
- Measurement in M1/M2 shows `coverage` is consistently high with a shorter grace period,
  in which case the default can tighten
- Measurement shows truncation persists beyond the grace period, in which case the
  defaults are wrong and the flush model needs revisiting
