#!/usr/bin/env bash
# Build the *detection* fixture from real footage.
#
# `pattern.ts` (see fixture-generate.sh) is a test pattern: Frigate records it happily,
# but nothing in it is a detectable object, so review items can only be produced through
# the manual-event API. That path is precise and reproducible, and it has a hard ceiling.
# It cannot answer two questions at all:
#
#   1. **What does a real `frigate/reviews` message actually look like?** The parser is
#      currently written against payloads we assumed. Real detections publish real ones.
#   2. **Does a stationary subject keep a review open?** A manual event sets
#      `last_alert_time` *once*, when it is ended. A real detection updates it every
#      frame. The question is unanswerable by construction on the manual-event path.
#
# So this builds a second fixture containing actual people, by encoding a real clip
# between two long blank segments.
#
# Why the blank segments
# ---------------------
# A review closes `cutoff_time` seconds after the *last* activity — 40 s for alerts,
# 30 s for detections, Frigate's defaults, which the config deliberately does not
# override. Looping footage in which somebody is always in frame therefore produces one
# single review that never ends, and no clip boundary is ever observable. The gaps are
# what let a review close. `GAP_S` defaults to 50 s so it is unambiguously past both
# cutoffs rather than deliberately in the ambiguous band.
#
# One gap per clip, ordered gap, clip, gap, clip. Because the publisher loops the file,
# the gap *before* the first clip is also the gap *after* the last one, so every clip is
# followed by a full gap without needing a trailing segment.
#
# Why this re-encodes when fixture-generate.sh stream-copies
# ---------------------------------------------------------
# fixture-generate.sh can `-c copy` because it produces one continuous pattern from one
# source. Here, segments from several sources have to be joined, and stream-copying
# MPEG-TS across sources leaves the timestamps discontinuous at every seam. The publisher
# then loops the result forever, so a seam that confuses the decoder is a failure that
# surfaces minutes later, in a clip, somewhere unrelated to this script. One encode into
# one continuous stream removes the whole class.
#
# Audio is dropped (`-an`, which is also the default here since no audio stream is
# mapped). Testing that audio survives a clip cut needs Frigate configured to record
# audio at all, which is a separate change to the camera config. Deferring it keeps this
# fixture about detection.
#
# Usage:
#   ./fixture-from-video.sh                  # every .mp4 in fixture/, in filename order
#   ./fixture-from-video.sh "clip 1.mp4"     # just one
#   GAP_S=20 ./fixture-from-video.sh         # shorter gaps — faster, but a review may
#                                            # not have closed before the next clip
#   WIDTH=1920 HEIGHT=1080 ./fixture-from-video.sh
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="ghcr.io/blakeblackshear/frigate:stable-standard-arm64"
OUT="detect.ts"
GAP_S="${GAP_S:-50}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-15}"

# bash 3.2 ships with macOS and errors on `${#arr[@]}` for an empty array under `set -u`,
# so the count is tracked separately rather than derived from the array. Same reason the
# expansion below is guarded rather than plain.
CLIPS=()
N=0
add_clip() {
  CLIPS[$N]="$1"
  N=$((N + 1))
}

if (($# > 0)); then
  for arg in "$@"; do
    # Accept either `clip 1.mp4` or `fixture/clip 1.mp4`, since both are natural to type.
    add_clip "${arg#fixture/}"
  done
else
  while IFS= read -r found; do
    [ -n "$found" ] && add_clip "$found"
  done < <(find fixture -maxdepth 1 -name '*.mp4' -exec basename {} \; | sort)
fi

if ((N == 0)); then
  {
    echo "No source clips found."
    echo
    echo "Put one or more .mp4 files in fixture/ and re-run. They are gitignored, so real"
    echo "footage never reaches the repository — which is the point, since a video of an"
    echo "identifiable person has no business in a repository being open-sourced."
  } >&2
  exit 1
fi

for ((i = 0; i < N; i++)); do
  if [[ ! -f "fixture/${CLIPS[$i]}" ]]; then
    echo "Missing: fixture/${CLIPS[$i]}" >&2
    exit 1
  fi
done

mkdir -p fixture

FFMPEG="$(docker run --rm --entrypoint sh "$IMAGE" -c \
  'ls -1 /usr/lib/ffmpeg/*/bin/ffmpeg | sort -V | tail -1')"
echo "Using $FFMPEG"

# Build the input list and the filter graph together, because the pad names have to
# agree between them.
ARGS=()
FILTER=""
SEGMENTS=""

for ((i = 0; i < N; i++)); do
  ARGS+=(-i "/f/${CLIPS[$i]}")
  # Timebase and sample aspect ratio are set explicitly on *every* branch. The concat
  # filter refuses inputs that disagree on either, and a lavfi colour source does not
  # agree with a decoded H.264 stream on either by default. Omitting these produces
  # "Input link parameters do not match" naming pads rather than the property, which is
  # a slow thing to diagnose.
  FILTER+="[${i}:v]scale=${WIDTH}:${HEIGHT},fps=${FPS},format=yuv420p,setsar=1,"
  FILTER+="settb=AVTB,setpts=PTS-STARTPTS[c${i}];"
  SEGMENTS+="[g${i}][c${i}]"
done

# The blank source. `gray` rather than black so a black frame is never mistaken for an
# absent signal, and so a detection fired on it is obviously a false positive.
ARGS+=(-f lavfi -i "color=c=gray:size=${WIDTH}x${HEIGHT}:rate=${FPS}:duration=${GAP_S}")
FILTER+="[${N}:v]fps=${FPS},format=yuv420p,setsar=1,settb=AVTB,setpts=PTS-STARTPTS[graw];"

# A filter pad can be consumed exactly once, so one `split` yields the N gap copies.
SPLIT_OUTS=""
for ((i = 0; i < N; i++)); do
  SPLIT_OUTS+="[g${i}]"
done
FILTER+="[graw]split=${N}${SPLIT_OUTS};"
FILTER+="${SEGMENTS}concat=n=$((N * 2)):v=1:a=0[outv]"

echo "Encoding ${N} clip(s) with ${GAP_S}s gaps at ${WIDTH}x${HEIGHT}@${FPS} (this takes a moment)…"

docker run --rm -v "$PWD/fixture:/f" --entrypoint "$FFMPEG" "$IMAGE" \
  -hide_banner -loglevel warning \
  "${ARGS[@]}" \
  -filter_complex "$FILTER" \
  -map "[outv]" \
  -c:v libx264 -preset veryfast -crf 23 -g $((FPS * 2)) -pix_fmt yuv420p \
  -f mpegts -y "/f/${OUT}"

FFPROBE="$(docker run --rm --entrypoint sh "$IMAGE" -c \
  'ls -1 /usr/lib/ffmpeg/*/bin/ffprobe | sort -V | tail -1')"
DURATION="$(docker run --rm -v "$PWD/fixture:/f:ro" --entrypoint "$FFPROBE" "$IMAGE" \
  -hide_banner -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \
  "/f/${OUT}")"

echo
echo "Built fixture/${OUT}"
echo "  size     $(du -h "fixture/${OUT}" | cut -f1)"
echo "  duration ${DURATION}s"
echo
echo "The publisher loops this forever. One full cycle is ${DURATION}s and contains"
echo "${N} activity window(s), each preceded by ${GAP_S}s of nothing."
echo
echo "Start it with:  FIXTURE=${OUT} ./reviews.sh up"
