#!/usr/bin/env bash
# Stop and remove the stack. Data and configuration survive by default.
#
#   ./uninstall.sh                 stop and remove containers, keep data and config
#   ./uninstall.sh --images        also remove the images this stack used
#   ./uninstall.sh --purge         also remove config/ and the data directory (typed confirmation)
#   ./uninstall.sh --remove-lifecycle
#                                  remove the B2 lifecycle rule, if this build created one
#   ./uninstall.sh --dry-run       print what would happen, change nothing
#
# The default destroys nothing. Footage, the state database and the generated config all
# survive a plain uninstall, because reinstalling should not be the only way to fix a stack
# and "I ran uninstall to restart it" must never cost a recording (LLD 07 §11).
#
# `--purge` asks for the location name to be typed. A `--yes` flag is too easy to paste from a
# forum answer; typing `parents-home` is not. It also refuses to delete anything that is not a
# plausible data directory, so a stray `CCTV_DATA_DIR=/` in `.env` cannot become a disaster.
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN=0
REMOVE_IMAGES=0
PURGE=0
REMOVE_LIFECYCLE=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --images) REMOVE_IMAGES=1 ;;
    --purge) PURGE=1 ;;
    --remove-lifecycle) REMOVE_LIFECYCLE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option %s\n\n' "$arg" >&2
      usage >&2
      exit 64
      ;;
  esac
done

run() {
  if ((DRY_RUN)); then
    printf 'would run: %s\n' "$*"
  else
    "$@"
  fi
}

note() { printf '  %s\n' "$*"; }
title() { printf '\n%s\n' "$*"; }

# --- refuse to guess ------------------------------------------------------------------

if [[ ! -f .env ]]; then
  printf 'error: .env is missing, so this does not look like an installed site.\n' >&2
  printf '       Nothing has been changed.\n' >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'error: docker is not on PATH; nothing has been changed.\n' >&2
  exit 1
fi

# --- what this installation calls itself ----------------------------------------------

SITE="$(sed -n 's/^CCTV_SITE=//p' .env | tail -1)"
SITE="${SITE:-$(sed -n 's/^ *location: *//p' config/cameras.yml 2>/dev/null | head -1)}"
SITE="${SITE:-unknown}"

DATA_DIR="$(sed -n 's/^CCTV_DATA_DIR=//p' .env | tail -1 | tr -d '"')"
DATA_DIR="${DATA_DIR:-./data}"

if ((PURGE)); then
  # A data directory that resolves to something enormous is almost always a bad variable
  # rather than an intention.
  resolved="$(cd "$DATA_DIR" 2>/dev/null && pwd || printf '%s' "$DATA_DIR")"
  case "$resolved" in
    / | /Users | "$HOME" | /home | /data | /var | /etc | /usr | /System)
      printf 'error: refusing to purge %s: that is not a data directory.\n' "$resolved" >&2
      printf '       Check CCTV_DATA_DIR in .env. Nothing has been changed.\n' >&2
      exit 1
      ;;
  esac
fi

title "Uninstall site: ${SITE}"
note "compose project: $(pwd)"

# --- stop the stack -------------------------------------------------------------------

title "Stopping and removing containers"
run docker compose --profile "*" down --remove-orphans

if ((REMOVE_IMAGES)); then
  title "Removing images"
  while read -r image; do
    [[ -n "$image" ]] || continue
    run docker rmi "$image" || true
  done < <(docker compose --profile "*" config --images 2>/dev/null | sort -u)
fi

if ((REMOVE_LIFECYCLE)); then
  title "Backblaze B2 lifecycle rule"
  if grep -q '^CCTV_STORAGE_BUCKET=..*' .env; then
    run docker compose --profile ops run --rm --no-deps provision \
      cctv-provision --cameras /config/cameras.yml --remove-lifecycle \
      || note "could not remove the rule; remove it in the B2 console."
  else
    note "No bucket is configured, so there is no rule of ours to remove."
  fi
fi

# --- optionally remove state ----------------------------------------------------------

if ((PURGE)); then
  title "Purging configuration and data"
  note "This deletes config/ and ${DATA_DIR}, including every local clip and the database."
  note "Clips already uploaded to Backblaze B2 are NOT deleted: the bucket is untouched."
  printf 'Type the location name (%s) to continue: ' "$SITE"
  read -r typed || true
  if [[ "$typed" != "$SITE" ]]; then
    printf '\nNot confirmed. Containers were removed; configuration and data are untouched.\n'
    exit 1
  fi
  run docker compose --profile "*" down --volumes --remove-orphans
  run rm -rf config
  run rm -rf "$DATA_DIR"
  printf '\nPurged. Cloud footage remains in the bucket.\n'
  exit 0
fi

# --- what is left behind --------------------------------------------------------------

title "Kept"
note "config/          — cameras.yml, generated Frigate config, host.yml"
note "${DATA_DIR}     — clips, media, the state database, logs"
note "Backblaze B2     — every clip that finished uploading"
printf '\nNothing was deleted. Reinstall with ./install.sh, or remove data by hand once you are sure.\n'
