# Contract: Frigate integration

**Parties:** `clip-worker`, `status-collector`, `cleanup` (read-only), the config
generator → **frigate**

Frigate is treated as a third-party subsystem. This contract is the only place
where knowledge of its API, MQTT topics, and storage layout is allowed to live.
No other document or code file may assume Frigate behaviour.

---

## 1. Version compatibility

| | |
|---|---|
| Pinned version | Exactly one Frigate version per release of cctv-stack, pinned by image **digest** ([ADR-0014](../../adr/0014-pinned-image-digests.md)) |
| Declared in | `compose.yml` + `versions.yml` (generated) |
| Supported upgrade path | Only forward, only between versions listed in [`lld/09`](../lld/09-upgrade-and-versioning.md) |
| Rationale | Frigate's API and MQTT payloads evolve. Floating tags would let a `pull` change our input contract without a test run |

**Rule:** any use of a Frigate API endpoint or MQTT field that is not listed below
is a bug. Add it here first, with the version it was verified against.

> **VERIFY AT IMPLEMENTATION.** The exact call signature, payload shape, and
> auth requirement of every endpoint below must be re-verified against the pinned
> Frigate version and the results recorded in this file (see §9). Frigate's API is
> documented at `https://docs.frigate.video/integrations/api/`.

---

## 2. Authentication

**Resolved during M1 research.** Frigate exposes two ports with deliberately different
security postures:

| Port | Posture | Upstream's stated intent |
|---|---|---|
| `8971` | Authenticated, **HTTPS only** — JWT cookie or `Authorization: Bearer` | The UI, and reverse proxies |
| `5000` | **Unauthenticated, and resolved as `anonymous` with role `admin`** | *"Intended to be used within the docker network for services that integrate with Frigate and do not support authentication."* |

> **Measured 2026-09-30 against `0.18.0-77a66e7`.** Frigate serves `8971` over TLS using a
> self-signed certificate it generates on first start (`tls.enabled` defaults to `true`).
> A plain-HTTP request to that port is answered by nginx with
> `400 The plain HTTP request was sent to HTTPS port` — it never reaches Frigate, so it is
> **not** an auth failure and must not be diagnosed as one. Operator-facing text says
> `https://`.

Frigate's auth middleware resolves a request arriving on the internal port to
`remote-user: anonymous`, `remote-role: admin`, keyed off an `x-server-port` header that
its nginx sets and that cannot be spoofed from outside the container. The port is
deliberately fixed at container start rather than read from live config, so an in-memory
config change cannot move which port is trusted.

### Decision: our services use the internal port, not credentials

| | Internal port `5000` **(chosen)** | Authenticated `8971` + JWT |
|---|---|---|
| Credentials to manage | **None** | Service user, password, rotation, JWT secret |
| Token lifecycle | None | `session_length` default 24 h; `refresh_time` default 30 min |
| Failure modes | Network only | Expiry, rotation, login rate limiting, restart invalidating tokens |
| Blast radius | Any container on the compose network has Frigate admin | Only a credential holder does |
| Upstream intent | Documented explicitly for this purpose | For humans and reverse proxies |

**We choose the internal port.** Blast radius is the only argument against it, and it
does not bite: every service in this stack already has read access to `data/clips/` and
`state.db`, so a compromised container could read the footage directly. An extra
credential would not create a real boundary, while token lifecycle management would be a
recurring operational cost and a new class of silent failure.

### The invariant this creates

> **Port `5000` is never published to the host.** It is reachable only on the internal
> compose network.

This replaces the earlier rule that our services must not use port 5000. The real
constraint was never *"do not use the internal port"* — it is **"never expose the
internal port"**, which is exactly what upstream means by *"access to this port should
be limited"*.

- The installer fails validation if the compose file publishes `5000`
- Only `8971`, authenticated, is published — for humans
- A test asserts that no host port maps to container `5000`

### Consequences

| Concern | Detail |
|---|---|
| Human access | `8971` is published on loopback only, HTTPS only. Frigate's own login is **opt-in**: `frigate.auth.enabled` in `cameras.yml` is projected to `auth.enabled`. Default `false` |
| Installer | Does not create a service user at all. Simpler, and one fewer secret ([INV-2](../invariants.md)) |
| Operator password | With the login enabled, Frigate generates the admin password on first start and prints it to the log; change it in **Settings → Users**. `auth.reset_admin_password: true` regenerates a forgotten one (a transient hand edit to the generated file; [`docs/frigate-ui.md`](../../../docs/frigate-ui.md)) |
| Auth posture | With the login **off**, the only thing protecting the UI is that it is bound to `127.0.0.1` and that `5000` is never published. With it **on**, `8971` requires a JWT. The internal port is exempt either way, so our services are unaffected |
| Hardened deployments | `FRIGATE_API_BASE` is configurable, so an operator wanting defence in depth can point at an authenticated endpoint instead. One config value, no code change |
| Failure mode | `401`/`403` from the internal port means the base URL is misconfigured, not that a token expired — a clearer signal |
| Never silently degrade | An auth error must still surface the exact upstream message, stop retrying, and alert. **Never degrade to "no clips"** |

> **Posture note (0.3.1).** The generator used to hardcode `auth.enabled: false`, which
> contradicted the "published UI must not be open" line above. It is now projected from
> `cameras.yml` (`frigate.auth.enabled`) and can be enabled without hand-editing a
> generated file. It stays **off by default**: turning it on changes first-start behaviour
> everywhere, and the only place the generated password appears is Frigate's log. A site
> that wants the login opts in; [`docs/frigate-ui.md`](../../../docs/frigate-ui.md)
> is the procedure.

> **RESOLVED (M1).** Verified against Frigate `0.18.0-77a66e7`: `/config`, `/stats` and
> `/events` all return `200` with **no credentials**, and `POST /api/auth` with
> `x-server-port: 5000` returns `202`. Services therefore need no credentials.
>
> **One correction to the reasoning above.** `/api/auth` is the wrong probe: it reports
> *login state* and correctly returns `401` to an anonymous caller, which is not an
> authorisation failure. The meaningful test is whether the endpoints the pipeline uses
> work, and they do. The `x-server-port` trust mechanism exists and is confirmed.
>
> On this host Frigate's nginx does **not** appear to set `x-server-port` on requests to
> `localhost:5000` from inside the container, yet the pipeline endpoints still return
> `200`. The effect we depend on holds; the exact mechanism is less certain than the
> source suggested. Re-verify if an upgrade changes any endpoint to `401`/`403`.

---

## 3. MQTT

Broker: mosquitto on the internal network, authenticated, anonymous disabled.
Topic prefix: `frigate` (Frigate's default; we do not change it).

### 3.1 Topics we consume

| Topic | QoS | Consumer | Purpose |
|---|---|---|---|
| `frigate/reviews` | 1 | clip-worker | **Primary trigger.** A canonical clip is one review item, not one event ([ADR-0016](../../adr/0016-clip-per-review-item.md)) |
| `frigate/events` | 1 | clip-worker | Metadata only — the event ids inside a review, stored in the clip's sidecar |
| `frigate/available` | 1 (retained) | status-collector | Frigate liveness |
| `frigate/stats` | 0 | status-collector | Detection FPS, inference speed, camera state |
| `frigate/<camera>/status/<role>` | 1 | status-collector | Per-camera per-role `online` / `offline` / `disabled` |

### 3.2 `frigate/events` payload — fields we depend on

```jsonc
{
  "type": "new" | "update" | "end",
  "before": { /* previous state */ },
  "after": {
    "id": "1734567890.123456-abcxyz",   // event id; stable across new/update/end
    "camera": "front-door",             // must match a name in cameras.yml
    "label": "person",
    "sub_label": null,
    "zones": ["driveway"],
    "start_time": 1734567890.123456,
    "end_time": 1734567925.654321,      // null while in progress
    "has_clip": true,                   // authoritative: is a clip retrievable
    "has_snapshot": true,
    "top_score": 0.91,
    "false_positive": false,
    "retain_indefinitely": false
  }
}
```

**Trigger condition.** The clip-worker enqueues materialisation when:

```text
after.end_time != null
AND after.has_clip == true
AND after.false_positive != true
AND after.camera is known in cameras.yml
AND event.id not already in state.db
```

`type == "end"` is the expected carrier, but the condition above is evaluated on
**every** message regardless of `type`. This is deliberate: Frigate publishes
`update` messages when it finds a better snapshot or a zone change, and an event
can be ended by a final message that we should not depend on receiving.

**Fields we deliberately ignore:** `snapshot`, `box`, `area`, `region`, `ratio`,
`current_zones`, `entered_zones`, `thumbnail`, `motionless_count`,
`position_changes`, `current_estimated_speed`, `average_estimated_speed`,
`velocity_angle`, `recognized_license_plate`, `attributes`. They are stored
verbatim in the clip's JSON sidecar (§5.3) so nothing is lost, but no logic
branches on them.

**Unknown fields must not break parsing.** The parser is tolerant: it reads known
keys, ignores everything else, and never validates exhaustively. A Frigate release
adding a field must not take down our pipeline.

**Unknown cameras must not break parsing.** An event for a camera not in
`cameras.yml` is logged at `WARNING` with the camera name and dropped. (This is
normal when a camera was just removed — see [INV-13](../invariants.md).)

**Message ordering is not guaranteed.** Processing is keyed by event id and is
idempotent, so duplicate and out-of-order `update` messages are harmless.

### 3.3 MQTT is an optimisation, not the source of truth

Delivery is at-most-once from our perspective: a broker restart, a network blip,
or clip-worker downtime drops messages. Losing a message must never lose a clip.
The reconciliation sweep (§4.3) is the correctness mechanism.

**Ordering note.** We still consume `frigate/events`, but only for metadata: the event
ids listed in a review's `data.detections` are written into the clip's sidecar, which
preserves 1:1 event searchability even though the file is per-visit. Events are **not**
a trigger — see §3.4.

---

### 3.4 Review items are the primary trigger

**A canonical clip is one Frigate review item, not one event** ([ADR-0016](../../adr/0016-clip-per-review-item.md)).
Frigate already groups events into activity periods; we consume that grouping rather than
implementing our own visit-window logic.

A single visitor produces several tracked objects — approaching, waiting, leaving — and
therefore several events. Those are one *visit*: the thing a human wants to watch.

```jsonc
{
  "type": "new" | "update" | "end",
  "before": { /* previous state */ },
  "after": {
    "id": "1718987129.308396-fqk5ka",     // review id — the dedup key
    "camera": "front-door",
    "start_time": 1718987129.308396,
    "end_time": 1718987174.123456,          // null while the review is still open
    "severity": "alert",                    // or "detection"
    "thumb_path": "/media/frigate/clips/review/thumb-front-door-….webp",
    "data": {
      "detections": ["1718987128.947436-g92ztx"],   // the events inside this review
      "objects": ["person", "car"],
      "sub_labels": [],
      "zones": [],
      "audio": []
    }
  }
}
```

**Trigger condition** — evaluated on **every** message regardless of `type`, for the same
reason as §3.2: a broker restart can eat the final `end`.

```python
should_materialise = (
    after.end_time is not None  # review is final
    and after.camera in known_cameras
    and after.severity in camera.clip.severities  # default: [alert, detection]
    and any(o in camera.clip.objects for o in after.data.objects)
    and not db.exists(review_id=after.id)  # dedup on the review id
)
```

| Field | Use |
|---|---|
| `id` | Dedup key. `UNIQUE(review_id)` on `clips` |
| `start_time` | **Basis for the clip path** ([ADR-0006](../../adr/0006-canonical-clip-layout.md)), and the range start |
| `end_time` | Range end. Null means the review is still open — do not act |
| `severity` | `alert` \| `detection`. Per-camera filter |
| `data.detections` | Event ids, stored in the sidecar |
| `data.objects` | Per-camera label filter |

**Unknown fields and unknown cameras must not break parsing**, exactly as in §3.2. A
review for a camera not in `cameras.yml` is logged at `WARNING` and dropped.

**Why the range endpoint and not `/api/review/{id}/clip.mp4`:** the review endpoint takes
a symmetric `padding`, so asymmetric pre-roll and post-roll would inflate every clip. We
read `start_time`/`end_time` from the review and request the exact range ourselves — the
same reasoning as §4.2.

### 3.5 The review lifecycle — `cutoff_time` is the visit threshold

**Frigate, not us, decides where one visit ends and the next begins.** The rule is in
`frigate/review/maintainer.py`, and it is a quiet-period timer:

```python
# an alert review closes when
frame_time > segment.last_alert_time + config.review.alerts.cutoff_time
# a detection review closes when
segment.severity == detection and frame_time > segment.last_detection_time
    + config.review.detections.cutoff_time
```

| Setting | Default | Meaning |
|---|---|---|
| `review.alerts.cutoff_time` | **40 s** | Quiet period before an **alert** review is closed |
| `review.detections.cutoff_time` | **30 s** | Quiet period before a **detection** review is closed |

Consequences we depend on:

1. **"Leaves and comes back within the threshold" is one clip.** A person who walks out
   of frame and returns inside `cutoff_time` never closes the review, so the return is
   part of the same visit and therefore the same clip. **We implement no merging logic of
   our own** — this is the whole reason we consume reviews instead of raw events.
2. **Reviews for one camera cannot overlap, and are contiguous.** When an alert review
   ends while detections are still active, Frigate starts the next segment at exactly the
   previous one's `end_time`. Two consecutive clips therefore share a boundary timestamp:
   no gap, no double-count.
3. **The threshold is a latency cost, not only a boundary.** We cannot materialise a clip
   until the review closes (`end_time` non-null), so `cutoff_time` puts a floor of
   `cutoff_time + post_roll_s + segment_flush_grace_s` — about 65 s by default — on clip
   latency. Raising `cutoff_time` so a longer return-window stays "one clip" delays every
   clip by the same amount.
4. **An open review is cleared on Frigate restart.** The maintainer sends
   `CLEAR_ONGOING_REVIEW_SEGMENTS` at start-up, so a review that was open when Frigate
   restarted may never receive a `type: end`. This is why §3.4 evaluates the trigger on
   **every** message and why §6.2 of the HLD keeps a sweep.
5. **Frigate pads its own export by ±4 s.** The UI calls the export endpoint with
   `REVIEW_PADDING = 4` on both edges. We deliberately do not reuse it — our pre-roll and
   post-roll are asymmetric and operator-configured
   ([ADR-0015](../../adr/0015-clip-boundaries-and-padding.md)).
6. **A review can have no events.** An audio-only review has `data.detections: []` and
   populates `data.audio` instead. Nothing can be pinned for it with `retain_indefinitely`,
   and the default `clip.objects` filter drops it.
7. **`end_time` is the last *activity* time, not the time the review closed.** When a
   segment ends, `get_data(ended=True)` sets `end_time` to `last_alert_time` — not
   `frame_time`, and not `last_alert_time + cutoff_time`:

   ```python
   if ended:
       end_time = self.last_alert_time if severity is alert else self.last_detection_time
   ```

   A 30 s visit with a 40 s alert cutoff therefore *closes* 70 s after it began but
   reports an `end_time` 30 s after it began. Two things follow, and both are load-bearing:

   - `end_time - start_time` is the **activity span**, which is what a clip boundary ought
     to be. Cutting to `end_time + post_roll_s` captures the visit exactly, so
     [ADR-0015](../../adr/0015-clip-boundaries-and-padding.md) is correct by construction
     rather than by luck.
   - The `end_time` we act on is already ~`cutoff_time` in the **past** by the time we
     learn it. The latency floor in item 3 is unchanged, but the footage we then fetch
     ends up to 40 s behind the moment we start fetching.

Because `cutoff_time` defines the clip boundary, it is **projected into the generated
Frigate config** from `cameras.yml` rather than filtered at our layer — unlike
`clip.severities` and `clip.objects`, which are ours alone because changing them does not
change Frigate's grouping. See [`cameras-config-schema.md`](cameras-config-schema.md) §4.

> **MEASURED 2026-09-13** — Frigate 0.18.0-77a66e7, `TZ=Asia/Kolkata`, default cutoffs,
> driven through the manual-event API so the review maintainer ran its normal path. Method
> and limitations are in the repo-level dev-synthetic/ directory (removed from docs).
>
> | Question | Result |
> |---|---|
> | Returning **inside** the cutoff stays one review | **Confirmed.** Two visits 25 s apart merged into one review of 97 s containing **three** events |
> | Returning **after** the cutoff gives two reviews | **Confirmed.** Two visits 75 s apart produced two reviews, 15 s each |
> | Reviews are contiguous and never overlap | **Confirmed** across five consecutive reviews |
> | The alert cutoff is **40 s** | **Bracketed, and it fits.** Merged at 25 s and 35 s; split at 45 s and 75 s. So the cutoff lies in (35, 45], which is consistent with the source's `default=40` |
>
> **A Frigate restart silently truncates a visit, and announces nothing.** This is the
> most consequential result here. Two runs, both restarting Frigate mid-visit:
>
> | Run | Event | Review | `frigate/reviews` |
> |---|---|---|---|
> | 1 | `00:59:12 → 00:59:42` (30 s) | `00:59:17 → 00:59:25` (8 s) | — |
> | 2 | `01:00:15 → 01:00:44` (29 s) | `01:00:20 → **01:00:20**` (0 s) | one `update`, **no `new`, no `end`** |
>
> In both, the review closed while its event was still live, so the visit lost a third to
> a half of its footage. In run 2 the review has **equal start and end times**, and the
> database records an `end_time` while **no `end` was ever published on MQTT** — the
> subscriber was connected to mosquitto, which never restarted, so it would have seen
> anything Frigate sent.
>
> The mechanism matches the source: the maintainer sends `CLEAR_ONGOING_REVIEW_SEGMENTS`
> at start-up, which writes the segment closed *without* going through
> `_publish_segment_end`. That is why §3.4 evaluates the trigger on **every** message
> rather than only on `type: end`, and why the periodic sweep in HLD §6.2 is a correctness
> mechanism rather than a nicety. A deployment relying on `end` alone would lose that clip
> entirely, and the loss would be silent.
>
> **One caveat on the message types — since resolved.** Run 2 showed an `update` but no
> `new`. That is expected for the manual-event path, which assigns
> `active_review_segments[camera]` directly instead of calling `check_if_new_segment`, and
> only that path publishes `new`. **Live detections do publish it** — confirmed in the
> second measurement below. The missing `new` was an artefact of the method, not a
> property of reviews.
>
> **One design-relevant observation.** In three of three samples, `review.start_time`
> landed **~5 s after** the triggering event's own `start_time` (event 00:37:33 → review
> 00:37:38; event 00:41:46 → review 00:41:51). Since the clip path is named from
> `review.start_time` and the fetch begins at `review.start_time − pre_roll_s`, a 5 s lag
> is covered by the 5 s pre-roll **only just**. A camera configured with `pre_roll_s: 0`
> would begin every clip about 5 s *after* the subject first appeared. Still not measured
> against live detections.

#### Second measurement — live detections, 2026-09-13

The first measurement drove everything through the manual-event API, which cannot answer
the stationary-subject question and could not confirm the message types. The second run
replaces the test pattern with **real footage containing real people**
(fixture-from-video.sh is in the repo-level dev-synthetic/ directory, removed from docs), so
Frigate runs its own path end to end: ffmpeg → detector → events → review maintainer. No
API is involved at any point.

| Question | Result |
|---|---|
| Real detections produce review items | **Confirmed.** 13 events on `frigate/events`, all `label: person`, producing 5 review items, all `severity: alert` |
| `frigate/reviews` publishes `new` | **Confirmed.** `{"type": "new", "before": …, "after": {… "end_time": null}}` |
| `frigate/reviews` publishes `end` | **Confirmed** — and `end` is the **only** message type that carries a non-null `end_time`. `new` and `update` both send it as `null` |
| Reviews close in a quiet gap | **Confirmed.** A 50 s quiet segment closes every review. Consecutive reviews were separated by 55–57 s end-to-start and never merged |
| `end_time` equals the close time | **Falsified.** See item 7 — `end_time` is the last activity time |
| A `person` review is an *alert*, not a detection | **Confirmed.** `review.alerts.labels` defaults to `["person", "car"]` (`DEFAULT_ALERT_OBJECTS` in `frigate/config/camera/review.py`), so no zone configuration is needed for a person to become an alert |

Observed review durations were 28.8 s, 4.6 s, 25.4 s and 4.6 s — alternating, because the
fixture alternates a 30 s indoor clip with a 12 s outdoor one and the people are only
inside the frame for part of the shorter clip. Those are **activity spans**, per item 7,
not time-to-close.

> **How review data actually reaches MQTT.** Worth naming, because it is not obvious and
> it is easy to conclude the opposite. `maintainer.py` sends the review JSON to the
> internal ZMQ requestor with `send_data("reviews", …)`, and `Dispatcher._receive` has a
> **fall-through** for topics it has no handler for:
>
> ```python
> elif topic in topic_handlers:
>     return topic_handlers[topic]()
> else:
>     self.publish(topic, payload, retain=False)   # → MQTT frigate/reviews
> ```
>
> `Dispatcher.publish` fans out to every communicator, so the unrecognised `"reviews"`
> topic lands on MQTT with `topic_prefix` prepended. The neighbouring
> `handle_upsert_review_segment` — the function that actually persists the review — does
> **not** publish anything, so reading that function alone suggests reviews never reach
> MQTT at all. They do. This cost a wrong conclusion once; the full-topic capture is what
> caught it.

**The payload shape, checked against our parser.** Every field `reviews.parse_review`
reads was present with the expected type, so §3.4's parser is verified rather than assumed:

```jsonc
{
  "type": "new",                 // or "update", or "end"
  "before": { …segment as it was… },
  "after": {
    "id": "1789304550.318037-z09b2n",
    "camera": "synthetic",
    "start_time": 1789304550.318037,
    "end_time": null,           // non-null only on "end"
    "severity": "alert",
    "thumb_path": "/media/frigate/clips/review/thumb-synthetic-….webp",
    "data": {
      "detections": ["1789304550.124755-91x3l8", "…"],   // event ids
      "objects": ["person"],
      "verified_objects": [],
      "sub_labels": [],
      "zones": [],
      "audio": [],
      "thumb_time": 1789304550.605706,
      "metadata": null
    }
  }
}
```

`verified_objects`, `thumb_time` and `metadata` are fields our parser ignores — the
unknown-key tolerance in §3.4 earning its place rather than being hypothetical.

> **Still unmeasured:**
>
> - **How long a stationary subject keeps a review open.** Still the one open question, and
>   it matters because it sets the maximum length of a single clip. Note that
>   `detect.stationary.interval` (default **50** frames) throttles re-detection of a
>   stationary object, so it is a lever on the answer, not merely a setting.
> - Whether a review is ever reopened after `end`.
> - The ~5 s `start_time` lag above, against live detections.
> - `POST`/`DELETE /api/events/{id}/retain` — both exist and are admin-scoped in the
>   source, but neither was exercised.
>
> **Bonus, from the same run:** Frigate's database is at **`/config/frigate.db`**, and it
> has a `recordings` table. That is where [ADR-0015](../../adr/0015-clip-boundaries-and-padding.md)'s
> preferred replacement for the broken gap detector should start.

---

## 4. HTTP endpoints we call

### 4.1 Pin and unpin an event

```http
POST   /api/events/{event_id}/retain     → {"success": true, "message": "..."}
DELETE /api/events/{event_id}/retain     → {"success": true, "message": "..."}
```

| | |
|---|---|
| Purpose | Protect an event's source footage from Frigate's own retention until we hold our own copy ([INV-9](../invariants.md)) |
| Auth | Internal port (§2) — Frigate resolves it as `admin`, which this endpoint requires |
| When | Pin: immediately on enqueue. Unpin: after the canonical clip is durably written |
| Idempotency | Must be treated as idempotent by us; a repeated pin is not an error |
| Failure to pin | **Fatal for that event.** Do not download without a successful pin. Log, record `PIN_FAILED`, alert — because the clip may be lost |
| Failure to unpin | Non-fatal. Log, and let the retention sweep unpin later. A stuck pin wastes Frigate disk, it does not lose data |
| Safety valve | A `pinned_at` timeout reaper unpins events pinned longer than N hours, so a dead clip-worker cannot fill the disk with pinned events |

> **VERIFY:** that this endpoint is available and admin-scoped in the pinned
> version. It exists in the current `dev` branch. If a newer mechanism supersedes
> it (the events API also exposes a `favorites` / `retain_indefinitely` concept),
> use that and record the change here.

### 4.2 Download a clip for a time range

```http
GET /api/{camera_name}/start/{start_ts}/end/{end_ts}/clip.mp4
```

**This is the primary clip-retrieval endpoint, and it is deliberately not the
event-scoped one.** The operator needs footage before `event.start_time` and after
`event.end_time`, asymmetrically — and only this endpoint can express that
([ADR-0015](../../adr/0015-clip-boundaries-and-padding.md)).

The range we request is:

```text
start_ts = review.start_time − clip.pre_roll_s
end_ts   = review.end_time   + clip.post_roll_s
```

| | |
|---|---|
| Semantics | **Not a file read.** Frigate runs ffmpeg over the recording segments overlapping the range, using a concat playlist with `inpoint`/`outpoint` and `-c copy -movflags frag_keyframe+empty_moov`, and streams a **fragmented MP4** |
| Response | `200` streaming `video/mp4` |
| No footage | **`400`** with `{"success": false, "message": "No recordings found for the specified time range"}`. Must be classified as **retryable** — see below |
| Cut precision | Stream copy can only cut at keyframes, so the achieved range will not match the request exactly. We **err outward** and never trim ([INV-14](../invariants.md)) |
| Audio | Preserved by `-c copy`. We never pass `-an`. Some cameras emit codecs browsers cannot play (G.711); we record the codec rather than transcode |
| Cost | CPU and disk I/O on the Frigate container for the duration of the cut. A stream copy, not a re-encode, but not free |
| Concurrency | Bounded by `clip-worker.fetch_concurrency` (default 2) so Frigate's detection is not starved |
| Timeout | Generous but bounded (`fetch_timeout_s`, default 120). Timeout → retry, not immediate failure |
| Partial content | A truncated download is detected by comparing bytes received against `Content-Length` when present. A short read is a retryable failure |

#### Why not the event endpoint with `?padding=`

`GET /api/events/{event_id}/clip.mp4?padding=N` also exists, and its padding is
**symmetric**: Frigate requests `start_time − N` through `end_time + N`. There is no way
to ask for a different pre-roll and post-roll.

A deployment wanting 5 s of pre-roll and 20 s of post-roll would therefore have to pass
`padding=20` and accept 15 s of unwanted lead-in on **every** clip — roughly **34 GB**
over 90 days at 100 events/day and 2 Mbps. The range endpoint gives the exact range, so
pre-roll and post-roll are independent and neither is inflated.

The event endpoint remains the **documented fallback**, used with
`padding = max(pre_roll_s, post_roll_s)`. It has one advantage we give up: it checks
`has_clip` and returns `404` when a clip is unavailable. We do not need that check — the
event payload we already hold from MQTT or the sweep carries `has_clip`.

#### Never fetch before the segment is flushed

> ⚠️ **Frigate writes new recording segments to cache and moves them to disk shortly
afterwards.** When `frigate/events` reports an event as ended, the final segment
containing the end of that event may not be on disk yet. A clip fetched immediately is
therefore **silently truncated** — it ends early, with no error and no indication that
anything is missing.

The clip-worker schedules every fetch at:

```text
fetch_not_before = review.end_time + post_roll_s + segment_flush_grace_s
```

and does not touch the item before it is due. A `400 "No recordings found"` is
classified as **retryable** for the same reason — it usually means the flush has not
happened yet, not that the request was malformed. Treating it as a permanent error would
turn every flush lag into a lost clip.
| Cost | CPU and disk I/O on the Frigate container for the duration of the cut. It is a stream copy, not a re-encode, but it is not free |
| Concurrency | Bounded by `clip-worker.fetch_concurrency` (default 2) to avoid starving Frigate's detection work. This limit exists for a reason — raising it can degrade detection latency |
| Timeout | Generous but bounded (`fetch_timeout_s`, default 120). Timeout → retry, not immediate failure |
| Partial content | A truncated download is detected by comparing bytes received against `Content-Length` when present. A short read is a retryable failure |

**Consequence for `clip-worker`:** because the response is a fragment-keyframe MP4
stream and has no meaningful `Content-Length` guarantee, we always write to a
`.tmp` file, then remux, then verify with `ffprobe`, then atomically rename. See
[`lld/02-clip-worker.md`](../lld/02-clip-worker.md).

**Not used:** the HLS/VOD endpoints and `review/{id}/clip.mp4`. They overlap in function
but are not scoped to a time range we control.

Note the iOS caveat Frigate's docs attach to this endpoint — Safari does not reliably
play progressive MP4, so Frigate suggests HLS for iOS *playback*. That concerns live
playback, not retrieval: our remux to a faststart MP4 happens immediately after download,
and the stored file plays in every client we care about.

### 4.3 Reconciliation sweep

```http
GET /api/events?has_clip=1&in_progress=0&after={watermark}&limit={n}&sort=date_asc
```

| | |
|---|---|
| Purpose | **Correctness backstop.** Catch every completed event with a clip that MQTT did not deliver |
| Frequency | `sweep_interval_s`, default 300 |
| Watermark | Highest `start_time` for which **every** returned event has been durably recorded in `state.db`. Persisted, so restarts resume |
| Overlap | The sweep always starts at `watermark - overlap_window` (default 600 s) to tolerate clock skew and events that end after their start time |
| Pagination | Loop until fewer than `limit` results are returned |
| Failure | On any error, do **not** advance the watermark. Retry next tick |
| Backlog bound | If the watermark lags more than `max_backlog_s` (default 6 h), raise health to `UNHEALTHY` — this means events are at risk of expiring in Frigate |
| Staleness detection | Frigate's own `has_clip` may flip to `false` once recordings expire. Such events are recorded as `EXPIRED_BEFORE_FETCH` and counted as **data loss**, not silently skipped |

### 4.4 Health and statistics

```http
GET /api/stats      → object with cameras, detectors, service uptimes, storage
GET /api/version    → running version, for the compatibility check
```

Used by `status-collector` to populate health. Also used at startup for a
compatibility assertion: if the running Frigate version does not match
`versions.yml`, the stack reports `DEGRADED` with an explicit message rather than
running against an untested contract.

> **VERIFY:** the exact shape of `/api/stats` in the pinned version. Only the fields
> listed in [`lld/06`](../lld/06-health-and-monitoring.md) may be read; no logic may
> depend on undocumented keys.

### 4.5 Endpoints we deliberately do not use

| Endpoint | Why not |
|---|---|
| `DELETE /api/events/{id}` | We never delete Frigate's data. Frigate owns its retention |
| `POST /api/events/{id}/plus` | Frigate+ is the operator's choice, not ours |
| `/{camera}/{label}/clip.mp4` | Latest-event semantics, not event-scoped |
| `/api/events/{id}/snapshot.jpg` | Not needed for v1; may be added as a sidecar later |
| `/api/restart`, `/api/media/sync` | Frigate lifecycle is not ours to drive |

---

## 5. Storage layout we read

### 5.1 Frigate's paths

| Path | Contents | Retention |
|---|---|---|
| `/media/frigate/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4` | Recording segments, **UTC** | Frigate's `record:` config |
| `/media/frigate/clips/` | Event snapshots, review thumbnails, faces | Frigate's `snapshots:` config |
| `/config/frigate.db` | Frigate's database (events, recordings, reviews) | n/a — treated as a cache |

**We do not read any of these paths directly.** All access is via the HTTP API.
This is deliberate: the on-disk layout is an internal detail of Frigate and has
changed across versions (most notably when clips stopped being stored as files).
Reading files would couple us to that instability.

### 5.2 The retention clock

The clip-worker's recovery window is bounded by Frigate's recording retention:

```text
headroom_s = frigate_record_retention_s − age_of_oldest_unmaterialised_event
```

Frigate's retention is known to us because **we generate its config**. The
generated default must keep this window comfortably large:

| Setting | Default | Rationale |
|---|---|---|
| `record.continuous.days` | `2` | Bounded 24/7 footage so an incident with no motion still has video ([ADR-0022](../../adr/0022-continuous-recording-for-two-days.md)). Local only: it is never uploaded |
| `record.motion.days` | `7` | Keeps footage attached to activity five days longer than the 24/7 churn, and gives the clip-worker a week's recovery window |
| `record.alerts.retain.days` | `30` | Keeps alert footage available to Frigate's UI |
| `record.detections.retain.days` | `14` | Bounded |

**Validation rule:** the shortest non-zero window in the generated block must be at least
`min_headroom_days` (default 2), because the clip-worker's backlog could otherwise outlive
its source. With the default two-day continuous window that is satisfied *exactly* — raising
`min_headroom_days` without raising `continuous.days` is refused at generation time. See §6
of this document and [INV-9](../invariants.md).

### 5.3 Clip JSON sidecar

Alongside each canonical clip we write `<name>.mp4.json` containing the original
event payload verbatim plus our own metadata. This is the escape hatch that makes
"we ignored field X" safe: nothing Frigate told us is discarded.

---

## 6. Error handling matrix

| Upstream condition | Our behaviour | Retry? | Health effect |
|---|---|---|---|
| `401` / `403` | Stop, surface the message, alert | No | `UNHEALTHY` |
| `404` on `retain` | Event ended and was pruned already | No | `WARNING`, count as `EXPIRED_BEFORE_FETCH` |
| **`400` + `"No recordings found"`** | Segments not flushed yet, or expired | **Yes**, with a delay — this is the flush race, not a bad request | `DEGRADED` if it persists past the retries |
| `404` on the clip range | Range endpoint unavailable; fall back to the event endpoint once | Yes, then fallback | `WARNING` |
| Connection refused | Frigate down | Yes, backoff | `UNHEALTHY` |
| Timeout mid-download | Truncated stream | Yes, discard `.tmp`, backoff | `DEGRADED` |
| `5xx` | Upstream fault | Yes, backoff | `DEGRADED` |
| Malformed MQTT JSON | Parse failure | No (message dropped) | `WARNING` counter |
| Unknown camera | Drop with a warning | No | none |
| Frigate version mismatch | Continue, flag it | n/a | `DEGRADED` |
| Frigate unreachable at startup | Wait and retry forever | Yes | `UNHEALTHY` until reachable |

---

## 7. Resource limits we impose on ourselves

| Limit | Default | Why |
|---|---|---|
| `fetch_concurrency` | 2 | Protect Frigate's detection pipeline — we are a guest |
| `sweep_interval_s` | 300 | Bound API load |
| `sweep_page_size` | 100 | Bound memory and response size |
| Max pinned events | 500 | Prevent a stuck worker from filling Frigate's disk |
| Pin timeout | 6 h | Safety valve on the above |
| MQTT client id | Stable per service | Avoids reconnect storms |

---

## 8. Testing without real cameras

`dev-synthetic` profile ([ADR-0013](../../adr/0013-compose-profiles.md)) substitutes a
synthetic RTSP source for real cameras, letting CI exercise the full pipeline:

```text
synthetic RTSP source → frigate → frigate/events → clip-worker → canonical clip
                                                        ↓
                                                  uploader → RustFS (B2-compatible test double)
```

Requirements this creates:

- The storage adapter must be testable against an S3-compatible endpoint
  (`STORAGE_ENDPOINT_URL` override), not only real B2.
- Object keys must not encode anything B2-specific.
- The synthetic source must be able to produce deterministically-timed events so
  that clip path derivation can be asserted.

---

## 9. Verification checklist (must be completed before implementation is approved)

Every item below must be checked against the pinned Frigate version and the answer
recorded here, because a wrong assumption here invalidates the LLD.

- [ ] `POST` / `DELETE /api/events/{id}/retain` exist, are admin-scoped, and behave idempotently
- [ ] `GET /api/events/{id}/clip.mp4` returns a usable stream for a completed event
- [x] **`GET /api/{camera}/start/{start}/end/{end}/clip.mp4` accepts arbitrary asymmetric ranges** and returns the expected footage
- [x] The range endpoint returns `400 "No recordings found"` (not `404`) when there is no footage — confirming the retryable classification
- [x] Segment flush delay measured → **10 s**; truncation **reproduced** at 5 of 6 shorter lags
- [x] Cut precision measured → errs **outward**, mean **+1.88 s** per 10 s
- [x] Audio survives the cut and the remux → **AAC**, browser-playable
- [x] Keyframe/GOP interval → **2.55 s**. **But packet gaps are not a usable hole signal** — see the `max_gap_s` correction in [`lld/02`](../lld/02-clip-worker.md#9-configuration)
- [ ] The downloaded stream is playable by the clients we care about **after** our remux step
- [ ] `GET /api/events?has_clip=1&in_progress=0&after=…&sort=date_asc` supports the parameters we rely on
- [ ] The `sort` value for ascending-by-start-time is correct
- [ ] `frigate/events` payload matches §3.2 for `new`, `update`, and `end`
- [ ] `frigate/<camera>/status/<role>` values match `online` / `offline` / `disabled`
- [x] `/api/stats` contains the fields §4.4 requires
- [x] **The pipeline's endpoints return `200` with no credentials** — §2 confirmed (see the correction there)
- [x] Publishing port `5000` is genuinely unnecessary for our services to work
- [ ] Port `5000` really is unauthenticated (confirming why it must never be published)
- [ ] Frigate's recording retention defaults are configurable as §5.2 assumes
- [ ] The image digest for the host profile's variant is available

**Still outstanding, and why:** the items above that need a completed *event* were skipped
because the probe config disables detection for speed and determinism. Re-run the probe
with detection enabled (see spike/README.md in the repo root — removed from docs) — or simply walk
past a camera — to cover `retain`/`unretain`, the event-scoped endpoint, and the
`frigate/events` payload.
