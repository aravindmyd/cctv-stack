#!/usr/bin/env bash
#
# End-to-end: a synthetic camera -> Frigate review -> clip-worker -> canonical clip.
#
#   ./tests/e2e/run.sh
#
# This is the only test that exercises the real services rather than a unit double:
# it starts the dev-synthetic stack, runs the *built image* as a real clip-worker on
# that compose network, drives one manual event through Frigate, and asserts that a
# file appeared at the canonical `clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4` path with a
# sidecar that describes it.
#
# What it deliberately does not cover: MQTT delivery (the worker runs sweep-only here,
# which is the guarantee rather than the latency optimisation) and the cloud upload
# (the live integration suite covers the adapter). Those are separate jobs on purpose,
# so a failure names one layer instead of "e2e broke".
#
# **First run pulls the Frigate image**, which is a multi-minute job on a slow link.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

IMAGE="${CCTV_E2E_IMAGE:-cctv-stack:e2e}"
WORKER="cctv-e2e-worker"
TMP="$(mktemp -d)"

step() { printf '\n==> %s\n' "$*"; }

cleanup() {
  docker rm -f "$WORKER" >/dev/null 2>&1 || true
  ./dev-synthetic/reviews.sh down >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

step "Building the CCTV image"
docker build -t "$IMAGE" .

step "Starting the synthetic stack"
./dev-synthetic/reviews.sh up

# Give the recorder time to write a segment *before* the event. The clip starts at
# `event_start - pre_roll_s`, so an event created seconds after Frigate starts has no
# footage for its pre-roll; the sidecar is then correctly marked `degraded`, which would
# fail this test for a reason that has nothing to do with the code.
sleep 20

# The compose file names the project, so the network is deterministic; asserting it
# exists catches a compose file whose project name changed without this script being
# updated.
NETWORK="$(docker network ls --filter name=dev-synthetic_default --format '{{.Name}}' | head -1)"
if [[ -z "$NETWORK" ]]; then
  echo "the dev-synthetic compose network was not created" >&2
  docker network ls >&2
  exit 1
fi

step "Preparing a camera config and a data directory"
mkdir -p "$TMP/config" "$TMP/data"
# The image runs as uid 10001, while `mktemp` creates a directory owned by the runner.
# Without this the worker starts and immediately fails to open the database — the kind of
# permissions failure that looks like a code bug. The installer does the equivalent chown
# on a real host.
chmod 0777 "$TMP/data"
cat >"$TMP/config/cameras.yml" <<'YAML'
site:
  location: e2e
  timezone: UTC

defaults:
  clip:
    pre_roll_s: 5
    post_roll_s: 10
    severities: [alert, detection]
    objects: [person]

cameras:
  synthetic:
    display_name: Synthetic
    enabled: true
    # The worker never opens the camera; it cuts clips from Frigate's API. This value
    # exists because the schema requires a main stream for an enabled camera.
    rtsp:
      main: rtsp://mediamtx:8554/synthetic
YAML

step "Starting the clip-worker on the synthetic network"
# No CCTV_MQTT_HOST on purpose: sweep-only is the code path every deployment relies on
# when the broker is down, and it is the one that guarantees a clip rather than merely
# delivering it quickly.
docker run -d --name "$WORKER" --network "$NETWORK" \
  -e CCTV_CONFIG_DIR=/config \
  -e CCTV_DATA_DIR=/data \
  -e CCTV_DB=/data/cctv.db \
  -e CCTV_FRIGATE_URL=http://frigate:5000 \
  -e CCTV_SWEEP_INTERVAL_S=5 \
  -e CCTV_LOG_LEVEL=DEBUG \
  -v "$TMP/config:/config:ro" \
  -v "$TMP/data:/data" \
  "$IMAGE" cctv-clip-worker

step "Waiting for the worker to log its first sweep"
ready=0
for _ in $(seq 1 30); do
  if docker logs "$WORKER" 2>&1 | grep -q "sweep"; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "the clip-worker never completed a sweep. Its log:" >&2
  docker logs "$WORKER" >&2 || true
  exit 1
fi

step "Driving one visit through Frigate"
# 5s of activity, no gap. The review closes about 40s after the last activity (the alert
# cutoff), and this command already waits 60s for that; the worker's 5s sweep then picks
# it up.
./dev-synthetic/reviews.sh sequence 0 5

step "Waiting for a canonical clip and its sidecar"
CLIP=""
for _ in $(seq 1 60); do
  CLIP="$(find "$TMP/data/clips" -type f -name '*.mp4' 2>/dev/null | head -1)"
  # The clip is renamed into place before the sidecar is written, so a find that stops at
  # the first `.mp4` can race the sidecar by a few milliseconds.
  [[ -n "$CLIP" && -f "${CLIP}.json" ]] && break
  sleep 2
done
if [[ -n "$CLIP" && ! -f "${CLIP}.json" ]]; then
  echo "clip appeared but its sidecar did not: $CLIP" >&2
  exit 1
fi

if [[ -z "$CLIP" ]]; then
  echo "no clip was materialised. Worker log:" >&2
  docker logs "$WORKER" >&2 || true
  echo "Frigate review state:" >&2
  ./dev-synthetic/reviews.sh report >&2 || true
  exit 1
fi

step "Asserting the clip and its sidecar"
printf 'clip: %s\n' "$CLIP"
python3 - "$CLIP" <<'PY'
import json
import pathlib
import re
import sys

clip = pathlib.Path(sys.argv[1])

# The path is the contract (INV-5): clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4.
assert re.fullmatch(r"\d{4}", clip.parent.parent.parent.name), clip
assert re.fullmatch(r"\d{2}", clip.parent.parent.name), clip
assert re.fullmatch(r"\d{2}", clip.parent.name), clip
assert re.fullmatch(r"\d{2}-\d{2}-\d{2}\.mp4", clip.name), clip
assert clip.parent.parent.parent.parent.name == "synthetic", clip

sidecar_path = clip.with_suffix(clip.suffix + ".json")
assert sidecar_path.is_file(), f"sidecar missing: {sidecar_path}"
sidecar = json.loads(sidecar_path.read_text())

assert sidecar["camera"] == "synthetic", sidecar
assert sidecar["degraded"] is False, sidecar
assert sidecar["coverage_ratio"] is not None, sidecar
assert sidecar["size_bytes"] == clip.stat().st_size, sidecar
assert sidecar["sha256"], sidecar

print(
    "sidecar:",
    json.dumps(
        {
            key: sidecar[key]
            for key in (
                "camera",
                "requested_start_ts",
                "requested_end_ts",
                "coverage_ratio",
                "degraded",
                "size_bytes",
            )
        }
    ),
)
PY

echo
echo "e2e passed: one review -> one canonical clip with a valid sidecar."
