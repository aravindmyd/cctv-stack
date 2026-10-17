# dev-synthetic — a camera that does not exist

A self-contained stack that produces Frigate recordings, events, and **review items** with
no camera and no person in front of one. It exists because the system's riskiest
assumption cannot be tested by hand on demand, and because M2 requires a pipeline that
runs in CI.

```text
  ffmpeg (loops a pattern)  ──RTSP──►  mediamtx  ──RTSP──►  frigate
                                                              │
        manual events via the admin API ──────────────────────┘  → review items
```

---

## Why this exists, in one paragraph

Everything about our clip design now rests on how Frigate groups activity into **review
items**: the trigger, the dedup key, the path basis, and the visit threshold
([ADR-0016](../../docs/adr/0016-clip-per-review-item.md)). That behaviour needs a subject
who moves on demand. A person cannot always be produced, and even when they can, they
cannot be produced *identically twice* — which makes them useless for a regression test.
So the subject is replaced: Frigate does not care whether the activity it sees is live or
synthesised.

This is not a spike. M2 lists a `dev-synthetic` profile "[so CI can exercise the pipeline
without cameras]", and this is that profile. It was pulled forward because it also happens
to unblock the deferred Phase 0 measurement.

---

## The part that makes it work: manual events

The obvious approach — a video of a person walking — needs a person, in a video, at the
right moments. Frigate provides something better:

```http
POST /api/events/synthetic/person/create   {"include_recording": true, "duration": null}
PUT  /api/events/{event_id}/end            {}
```

A manual event goes through **the same review maintainer** as a real detection. With
`duration: null` the event stays open until we end it, and ending it sets the review's
`last_alert_time` to that moment. The review then closes when

```python
frame_time > last_alert_time + review.alerts.cutoff_time
```

— which is precisely the timer the Phase 0 experiment is about, exercised with timing we
control to the second rather than a person's guess.

Two documented quirks of the API, both relied on here:

- "Creating a manual event does not trigger an update to `/events` MQTT topic." Fine: we
  read `/api/review`, and a real deployment gets its trigger from MQTT.
- "The review item is an alert unless the label is listed in the camera's
  `review -> detections -> labels` config." So `person` gives an alert, and the 40 s alert
  cutoff applies by default — the value being measured.

---

## What it can prove

| Question | Covered? |
|---|---|
| Q1 Return inside the cutoff stays one review | ✅ via `sequence 30` |
| Q2 Return after the cutoff gives two reviews | ✅ via `sequence 75` |
| Q3 Is `type: end` published, and is a review ever reopened | ✅ via `watch` |
| Q5 Does `review.alerts.cutoff_time` move the boundary | ✅ edit `frigate/config.yml`, re-run |
| Q6 Does a restart mid-visit leave an unclosed review | ✅ via `restart-midvisit` |
| Review contiguity — the next starts at the previous `end_time` | ✅ |
| Long-review behaviour (`fetch_timeout_s`, `long_clip_warn_s`) | ✅ generate a longer fixture |
| Clip fetch, pre/post-roll boundaries, flush race | ✅ footage always exists, timeline known |

## What it cannot prove, and it is worth being clear about

| | Why |
|---|---|
| **Q4 — how long a stationary subject keeps a review open** | Needs live per-frame detection. Manual events update `last_alert_time` once, at completion; a detection updates it every frame. The *timer* is the same code, but the input it is fed is not |
| That a live camera produces sensible groupings | A pattern is not a scene. One walk-through of a real doorway is still worth doing, eventually — as a `0.1.0` acceptance note rather than a gate |
| Whether the trigger fires from MQTT in production | The scripts use the HTTP API. `watch` shows the MQTT messages, but nothing here consumes them yet |
| Cut precision at different keyframe intervals | The fixture is one encoder setting. `-c copy`'s outward error depends on the GOP length, which a real camera chooses |

---

## Running it

```sh
cd dev-synthetic
./reviews.sh up              # generates the fixture if needed, starts, waits, reports status
```

In a second terminal, watch the thing under test:

```sh
./reviews.sh watch           # frigate/reviews on MQTT
```

Then run an experiment:

```sh
./reviews.sh sequence 30     # two 15s visits 30s apart -> expect ONE review
./reviews.sh sequence 75     # two 15s visits 75s apart -> expect TWO reviews
./reviews.sh list            # what Frigate stored
./reviews.sh restart-midvisit
```

`./reviews.sh down` stops it. `./reviews.sh` with no argument prints usage.

### Requirements

Docker and a `python3` on the host for the output formatting. **ffmpeg is not required on
the host** — the fixture is generated inside the Frigate image, which is deliberate: a
fixture whose codec depends on the developer's ffmpeg build is a fixture that behaves
differently on different machines.

### Two deliberate contract deviations

Both are visible in `compose.yml` rather than buried:

1. **Port 5000 is not published.** The integration contract §2 says production never
   publishes it, because Frigate resolves requests on 5000 as anonymous-admin. Exposing it
   "just for dev" is exactly how such a rule erodes, so the scripts reach the API through a
   container on the compose network instead. Nothing is reachable from outside.
2. **`auth.enabled: false`.** Only safe because of point 1, and it is what lets the
   admin-only manual-event API be called without a login.

---

## Recording the results

Put answers in [`spike/RESULTS.md`](../../spike/RESULTS.md) as a `## Review lifecycle`
section, then mark **M1.13 – M1.16** in
[`docs/plans/milestones.md`](../../docs/plans/milestones.md). The Phase 0 run sheet and
this directory describe the same six questions; whichever answers them first is the one
worth keeping, and `spike/` is still scheduled for deletion.

---

## What is still missing

- **A colour-coded fixture** — one distinct colour per second, so a test can name the
  second it is looking at. Required by M2.13, "the first and last decoded frames match
  `start − pre_roll` and `end + post_roll`". A test pattern cannot express that.
- **A CI job** that runs the pipeline against this stack. M2 requires it; nothing here
  assumes a human.
- **Digest pinning** for the three external images. The tags are pinned to versions, which
  is enough for dev tooling, but `versions.yml` discipline applies to anything that ships.
