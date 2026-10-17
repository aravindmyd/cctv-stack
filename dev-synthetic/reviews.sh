#!/usr/bin/env bash
# Drive and observe review-item behaviour without a camera and without a person.
#
# Frigate's manual-event API creates activity that goes through the *same* review
# maintainer as a real detection:
#
#   POST /api/events/{camera}/{label}/create   {include_recording, duration}
#   PUT  /api/events/{event_id}/end            {}
#
# With `duration: null` the event stays open until we end it, and ending it sets the
# review's `last_alert_time` to that moment. The review then closes when
# `frame_time > last_alert_time + review.alerts.cutoff_time` — the exact timer this
# experiment is about. So the cutoff behaviour is genuinely under test, with timing we
# control to the second instead of a person guessing.
#
# All API calls run *inside the Frigate container*, using its own python3 and urllib.
# Three earlier attempts got this wrong:
#
#   1. A separate `curlimages/curl` container had to be pulled first, and the pull
#      stalling made the whole script hang with no output — which is indistinguishable
#      from a broken stack.
#   2. Publishing port 5000 would have been the easy fix and would have broken the
#      contract's rule that it is never published (contract §2).
#   3. Polling `/api/version` for readiness can never pass: Frigate answers it with an
#      empty body. The spike measured that. It is `/api/stats`.
#
# Usage:
#   ./reviews.sh up                 start the stack and wait for it to be usable
#   ./reviews.sh watch              tail frigate/reviews — run in another terminal
#   ./reviews.sh list               show the review items Frigate has stored
#   ./reviews.sh status             is the camera actually recording?
#   ./reviews.sh sequence 30        two 15s visits 30s apart → expect ONE review
#   ./reviews.sh sequence 75        two 15s visits 75s apart → expect TWO reviews
#   ./reviews.sh restart-midvisit   start a visit, restart Frigate, end the visit
#   ./reviews.sh report             dump reviews and events, for recording results
#   ./reviews.sh down
set -euo pipefail

cd "$(dirname "$0")"

# `stable` is multi-arch. An earlier version picked `stable-standard-<arch>`, but the
# amd64 variant does not exist in the registry (only the arm64 one did), so the harness
# failed on CI with `manifest unknown`. Override with FRIGATE_IMAGE for a different build.
if [[ -z "${FRIGATE_IMAGE:-}" ]]; then
  FRIGATE_IMAGE="ghcr.io/blakeblackshear/frigate:stable"
  export FRIGATE_IMAGE
fi

COMPOSE=(docker compose -f compose.yml)
CAMERA="synthetic"
LABEL="person"

# Runs inside the Frigate container. `localhost:5000` there is the internal port the
# services use, which is why nothing needs publishing.
API_PY='
import json, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
req = urllib.request.Request(
    "http://localhost:5000" + path,
    data=None if body is None else body.encode(),
    headers={"Content-Type": "application/json"},
    method=method,
)
try:
    with urllib.request.urlopen(req, timeout=60) as response:
        sys.stdout.write(response.read().decode())
except urllib.error.HTTPError as exc:
    sys.stdout.write(exc.read().decode())
    sys.exit(1)
except Exception as exc:
    sys.stderr.write("request failed: {}\n".format(exc))
    sys.exit(1)
'

API_DB_PY='
import datetime, json, sqlite3, sys

con = sqlite3.connect("file:/config/frigate.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

def clock(value):
    if value is None:
        return "OPEN"
    return datetime.datetime.fromtimestamp(value).strftime("%H:%M:%S")

reviews = list(con.execute(
    "SELECT id, start_time, end_time, severity, data FROM reviewsegment ORDER BY start_time"))
print("review items: {}".format(len(reviews)))
previous_end = None
for review in reviews:
    end_time = review["end_time"]
    duration = "" if end_time is None else "  dur={:.0f}s".format(end_time - review["start_time"])
    gap = "" if previous_end is None else "  gap_from_prev={:.0f}s".format(review["start_time"] - previous_end)
    print("  {:<9} {} -> {}{}{}".format(
        review["severity"], clock(review["start_time"]), clock(end_time), duration, gap))
    detections = (json.loads(review["data"]) or {}).get("detections") or []
    print("      events: {}".format(len(detections)))
    previous_end = end_time

events = list(con.execute("SELECT start_time, end_time, label FROM event ORDER BY start_time"))
print("events: {}".format(len(events)))
for event in events:
    duration = "" if event["end_time"] is None else "  dur={:.0f}s".format(event["end_time"] - event["start_time"])
    print("  {:<8} {} -> {}{}".format(
        event["label"], clock(event["start_time"]), clock(event["end_time"]), duration))
'

run_api() { "${COMPOSE[@]}" exec -T frigate python3 -c "$API_PY" "$@"; }
api_get() { run_api GET "$1"; }
api_post() { run_api POST "$1" "${2:-}"; }
api_put() { run_api PUT "$1" "${2:-}"; }

wait_for_api() {
  echo -n "Waiting for Frigate's API"
  # Three minutes, not two: a cold CI runner has to start Frigate *and* let it load its
  # detection model, which is slower than a warm laptop. A bound that is too tight turns
  # a slow start into a false failure.
  for _ in $(seq 1 90); do
    # `/api/stats`, never `/api/version` — see the header comment.
    if api_get /api/stats >/dev/null 2>&1; then
      echo " — up."
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo
  echo "Frigate did not become reachable in 3 minutes. Check: ${COMPOSE[*]} logs frigate" >&2
  return 1
}

# Starts a visit and echoes the new event id. `duration: null` means it stays open until
# ended, which is how the gap between visits is controlled precisely.
start_visit() {
  local duration="${1:-null}" response
  response=$(api_post "/api/events/${CAMERA}/${LABEL}/create" \
    "{\"include_recording\": true, \"duration\": ${duration}}")
  printf '%s' "$response" | sed -n 's/.*"event_id":"\([^"]*\)".*/\1/p'
}

end_visit() { api_put "/api/events/$1/end" '{}' >/dev/null; }

cmd_up() {
  mkdir -p fixture data
  if [[ ! -f fixture/pattern.ts ]]; then
    echo "No fixture yet — generating one first."
    ./fixture-generate.sh
  fi
  "${COMPOSE[@]}" up -d
  wait_for_api
  echo
  cmd_status
}

cmd_down() { "${COMPOSE[@]}" down; }

cmd_status() {
  local tmp
  tmp=$(mktemp)
  api_get /api/stats > "$tmp"
  python3 - "$tmp" <<'PY'
import json
import sys

stats = json.load(open(sys.argv[1]))
print("  frigate", (stats.get("service") or {}).get("version"))
for name, cam in (stats.get("cameras") or {}).items():
    recording = cam.get("recording") or {}
    print("  camera {}: detection={} recording={} fps={}".format(
        name, cam.get("detection_enabled"), recording.get("enabled"),
        (cam.get("detection_fps") or 0)))
for name, detector in (stats.get("detectors") or {}).items():
    print("  detector {}: inference_speed={}ms".format(name, detector.get("inference_speed")))
PY
  rm -f "$tmp"
}

cmd_list() { "${COMPOSE[@]}" exec -T frigate python3 -c "$API_DB_PY"; }

report() {
  echo "=== Frigate's stored review items and events ==="
  cmd_list
}

cmd_watch() {
  echo "Tailing frigate/reviews — leave this running while driving the experiment."
  "${COMPOSE[@]}" exec mosquitto mosquitto_sub -h localhost -t 'frigate/reviews' -v
}

cmd_sequence() {
  local gap="${1:?usage: reviews.sh sequence <gap-seconds> [visit-seconds]}"
  local visit="${2:-15}"

  echo "Visit 1: ${visit}s of activity."
  local first
  first=$(start_visit null)
  echo "  event ${first}"
  sleep "$visit"
  end_visit "$first"
  echo "  ended at $(date +%H:%M:%S)"

  echo "Gap: ${gap}s since the last activity."
  sleep "$gap"

  echo "Visit 2: ${visit}s of activity."
  local second
  second=$(start_visit null)
  echo "  event ${second}"
  sleep "$visit"
  end_visit "$second"
  echo "  ended at $(date +%H:%M:%S)"

  echo
  echo "Waiting 60s for the review to close (cutoff plus slack)…"
  sleep 60
  echo
  report
  echo
  if (( gap < 40 )); then
    echo "Expected: ONE review item, because ${gap}s is under the 40s alert cutoff."
  else
    echo "Expected: TWO review items, because ${gap}s is over the 40s alert cutoff."
  fi
}

cmd_restart_midvisit() {
  echo "Starting a visit…"
  local event_id
  event_id=$(start_visit null)
  echo "  event ${event_id}"

  echo "Restarting Frigate mid-visit…"
  "${COMPOSE[@]}" restart frigate >/dev/null
  wait_for_api

  echo "Ending the visit after the restart…"
  sleep 15
  end_visit "$event_id" || true

  echo
  echo "Watch for a 'type: end' on frigate/reviews. The source says the maintainer sends"
  echo "CLEAR_ONGOING_REVIEW_SEGMENTS at start-up, so a review spanning the restart should"
  echo "be discarded rather than closed — no 'end' arrives, and only the sweep can rescue"
  echo "the clip."
  sleep 20
  echo
  report
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  watch) cmd_watch ;;
  list) cmd_list ;;
  report) report ;;
  status) cmd_status ;;
  sequence) shift; cmd_sequence "$@" ;;
  restart-midvisit) cmd_restart_midvisit ;;
  *)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
