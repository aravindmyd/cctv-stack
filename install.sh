#!/usr/bin/env bash
# Install the stack on this machine.
#
#   ./install.sh
#
# Safe to run repeatedly. Every step reconciles rather than destroys, and nothing here
# deletes state — an installer that can lose footage is worse than one that refuses to run.
#
# The script is deliberately chatty about *why* it stops. The two places it exits early are
# both "this machine needs a value only you know", and silently continuing with a
# placeholder RTSP URL would produce a camera that looks configured and never connects.
set -euo pipefail

cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# ---------------------------------------------------------------------------------------
step "Checking prerequisites"

command -v docker >/dev/null 2>&1 || die "docker is not installed. See https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not available (a bare 'docker-compose' binary is not enough)."
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable. Start Docker and run this again."

# ---------------------------------------------------------------------------------------
step "Checking location configuration"

if [[ ! -f .env ]]; then
  cp .env.example .env
  cat <<'EOF'

Created .env from .env.example.

Next: open .env and set your camera RTSP URLs. They are the only values there this machine
cannot work out for itself. Then run ./install.sh again.
EOF
  exit 0
fi

if grep -q 'user:password@' .env; then
  die ".env still has the placeholder RTSP URL from .env.example. Set your real camera URLs."
fi

# ---------------------------------------------------------------------------------------
step "Checking camera configuration"

if [[ ! -f config/cameras.yml ]]; then
  cp config/cameras.example.yml config/cameras.yml
  cat <<'EOF'

Created config/cameras.yml from the example.

Next: open it and check that each camera's `rtsp:` entries reference the variable names you
set in .env (for example `${CAMERA_FRONT_DOOR_RTSP}`). Then run ./install.sh again.
EOF
  exit 0
fi

# ---------------------------------------------------------------------------------------
step "Building the image"

docker compose build

# ---------------------------------------------------------------------------------------
# Validation runs in the same image that generates the config, so "valid" cannot mean two
# different things here and in the `config` service.
step "Validating the configuration"

docker compose run --rm --no-deps config \
  cctv-config validate --cameras /config/cameras.yml

# ---------------------------------------------------------------------------------------
step "Starting"

docker compose up -d

# ---------------------------------------------------------------------------------------
# Readiness is checked against `/api/stats`, never `/api/version`: Frigate answers the
# latter with an empty body, so a probe against it can never pass (integration contract §9).
step "Waiting for Frigate"

ready=0
for _ in $(seq 1 45); do
  if docker compose exec -T clip-worker python -c \
    "import urllib.request; urllib.request.urlopen('http://frigate:5000/api/stats', timeout=5)" \
    >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

if [[ "$ready" -ne 1 ]]; then
  say
  say "Frigate did not become reachable within 90 seconds. Check its log:"
  say "  docker compose logs --tail=100 frigate"
  exit 1
fi

say "Frigate is up."

# ---------------------------------------------------------------------------------------
step "Status"

docker compose ps

cat <<'EOF'

Running.

  Frigate UI     https://127.0.0.1:8971         (loopback only; see FRIGATE_UI_PORT)
  Clips          ./data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
  Worker log     docker compose logs -f clip-worker
  Frigate log    docker compose logs -f frigate
  Cloud backup   curl http://127.0.0.1:8082/health
                 Reports DISABLED until CCTV_STORAGE_BUCKET and a B2 key are set in .env.
  Uploader log   docker compose logs -f uploader
  Retention      curl http://127.0.0.1:8083/health
                 Local cleanup runs, but it only deletes a clip once its upload is
                 terminal-success. With cloud backup DISABLED nothing is ever deleted.
  Cleanup log    docker compose logs -f cleanup
  Stop           docker compose down            (state is kept)

The UI is HTTPS, with a certificate Frigate generates for itself on first start, so your
browser will warn once. That is expected: the port is bound to loopback, so nothing leaves
this machine. Plain http:// on that port is answered by nginx with a 400, not the UI.

A clip appears once the visit has ended, not while it is still happening. Frigate keeps
counting activity until the person has been gone for the alert cutoff (40s by default), so
the file lands a little after they leave. There is no partial clip by design: cutting an open
review would capture half a visit with no way to extend it afterwards.

First run takes a few minutes: Frigate downloads its detection model and has to record some
footage before there is anything for a clip to be cut from.

What is live in this release (v0.3+): clips are materialised at the canonical path and
uploaded to B2 exactly once when storage is configured; local retention deletes a clip
only after its upload is terminal-success and it is past `retention.local_days`. With
cloud backup DISABLED, nothing is ever deleted and clips accumulate — watch the disk.
EOF
