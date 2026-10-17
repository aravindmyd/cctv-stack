#!/usr/bin/env bash
# Generate the fixture the publisher loops.
#
# MPEG-TS rather than MP4 on purpose: TS is what RTSP carries natively, so
# `-c copy` into an RTSP stream works without a bitstream filter to unwrap MP4's
# length-prefixed NAL units. That removes a whole class of "it plays locally but
# Frigate sees nothing" failures.
#
# Runs in the Frigate image rather than requiring ffmpeg on the host — the host does not
# have it, and a fixture whose codec depends on the developer's ffmpeg build is a fixture
# that behaves differently on different machines.
#
# Usage:
#   ./fixture-generate.sh              # 10 minutes, 1280x720
#   ./fixture-generate.sh 3600         # an hour, for long-review tests
#
# 1280x720 matches fixture-from-video.sh, which matters because `detect.width/height` is
# a property of the camera, not of the fixture. Two fixtures at different sizes would
# force one of them to be upscaled or downscaled by Frigate before detection — and the
# upscaled case is the one Frigate warns about.
set -euo pipefail

cd "$(dirname "$0")"
DURATION="${1:-600}"
OUT="fixture/pattern.ts"

mkdir -p fixture
if [[ -f "$OUT" && "${FORCE:-0}" != "1" ]]; then
  echo "$OUT already exists (${DURATION}s requested). Set FORCE=1 to regenerate." >&2
  exit 0
fi

echo "Generating ${DURATION}s of pattern into $OUT …"

# Frigate vendors several ffmpeg builds and keeps them off PATH, at
# `/usr/lib/ffmpeg/<version>/bin/ffmpeg`. Resolving the newest at runtime rather than
# hardcoding a version keeps this working across Frigate upgrades.
# Same architecture choice as reviews.sh: the standard image is published per architecture.
if [[ -z "${FRIGATE_IMAGE:-}" ]]; then
  case "$(uname -m)" in
    arm64 | aarch64) FRIGATE_IMAGE="ghcr.io/blakeblackshear/frigate:stable-standard-arm64" ;;
    *) FRIGATE_IMAGE="ghcr.io/blakeblackshear/frigate:stable-standard-amd64" ;;
  esac
fi
IMAGE="$FRIGATE_IMAGE"
FFMPEG="$(docker run --rm --entrypoint sh "$IMAGE" -c \
  'ls -1 /usr/lib/ffmpeg/*/bin/ffmpeg | sort -V | tail -1')"
echo "Using $FFMPEG"

docker run --rm -v "$PWD/fixture:/out" \
  --entrypoint "$FFMPEG" \
  "$IMAGE" \
  -hide_banner -loglevel warning \
  -f lavfi -i "testsrc2=size=1280x720:rate=15:duration=${DURATION}" \
  -c:v libx264 -preset veryfast -g 15 -pix_fmt yuv420p \
  -f mpegts "/out/$(basename "$OUT")"

echo "Done: $(du -h "$OUT" | cut -f1)"
cat <<'EOF'

Note: this pattern has no person in it, so it produces recordings but no detections.
That is fine — the review-lifecycle experiments drive activity through the manual-event
API, which needs no detection at all.

A *colour-coded* fixture (one distinct colour per second) is still needed for M2.13,
"the first and last decoded frames match start - pre_roll and end + post_roll". That
test has to be able to name the second it is looking at, which a test pattern cannot.
EOF
