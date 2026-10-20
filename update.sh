#!/usr/bin/env bash
# Upgrade this installation to a newer image, or roll back to the previous one.
#
#   ./update.sh --to ghcr.io/jothikaraja/cctv-stack:0.6.0
#   ./update.sh                 use CCTV_IMAGE_NEXT from .env
#   ./update.sh --dry-run       print every step, change nothing
#   ./update.sh --rollback      previous image + the pre-upgrade database backup
#
# The sequence is lld/09 §4.1, with the git steps left out because a site is deployed as a
# pinned image, not a checkout (ADR-0020):
#
#   preflight → back up → point .env at the new image → pull → stop the workers →
#   migrate with the new code → start → verify
#
# Frigate and mosquitto keep running throughout, so recording continuity is never interrupted.
# Nothing is migrated until a backup of the database has been written and checked.
set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN=0
ROLLBACK=0
TO_IMAGE=""

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --to) ;;
    --rollback) ROLLBACK=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      if [[ "${PREV_ARG:-}" == "--to" ]]; then
        TO_IMAGE="$arg"
      else
        printf 'error: unknown option %s\n\n' "$arg" >&2
        usage >&2
        exit 64
      fi
      ;;
  esac
  PREV_ARG="$arg"
done

run() {
  if ((DRY_RUN)); then
    printf '  would run: %s\n' "$*"
  else
    "$@"
  fi
}

step() { printf '\n[%s] %s\n' "$1" "$2"; }
ok() { printf '  ✓ %s\n' "$*"; }
fail() {
  printf '\n  ✗ %s\n' "$*" >&2
  exit 1
}

env_value() { sed -n "s/^$1=//p" .env | tail -1; }

set_env_value() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    # `|` as the separator so image refs and digests survive untouched.
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$value" >>.env
  fi
}

# --- preflight --------------------------------------------------------------------------

if [[ ! -f .env ]]; then
  fail ".env is missing; this does not look like an installed site. Nothing has been changed."
fi
if ! command -v docker >/dev/null 2>&1; then
  fail "docker is not on PATH. Nothing has been changed."
fi

DATA_DIR="$(env_value CCTV_DATA_DIR)"
DATA_DIR="${DATA_DIR:-./data}"
FROM_IMAGE="$(env_value CCTV_IMAGE)"
FROM_IMAGE="${FROM_IMAGE:-cctv-stack:local}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if ((ROLLBACK)); then
  TO_IMAGE="$(env_value CCTV_IMAGE_PREVIOUS)"
  [[ -n "$TO_IMAGE" ]] || fail "no CCTV_IMAGE_PREVIOUS in .env: there is nothing to roll back to."
else
  TO_IMAGE="${TO_IMAGE:-$(env_value CCTV_IMAGE_NEXT)}"
  [[ -n "$TO_IMAGE" ]] || fail "no target image. Pass --to <image> or set CCTV_IMAGE_NEXT in .env."
  [[ "$TO_IMAGE" != "$FROM_IMAGE" ]] || fail "already running $TO_IMAGE; nothing to do."
fi

printf '\n════════════════════════════════════════════════════════════════════\n'
if ((ROLLBACK)); then
  printf ' CCTV Stack — rollback %s → %s\n' "$FROM_IMAGE" "$TO_IMAGE"
else
  printf ' CCTV Stack — upgrade %s → %s\n' "$FROM_IMAGE" "$TO_IMAGE"
fi
printf '════════════════════════════════════════════════════════════════════\n'

# --- 1. back up -------------------------------------------------------------------------

step "1/6" "Pre-upgrade backup"
if ((DRY_RUN)); then
  printf '  would run: docker compose --profile ops run --rm --no-deps backup cctv-backup create\n'
else
  docker compose --profile ops run --rm --no-deps backup cctv-backup create ||
    fail "the pre-upgrade backup failed. Nothing has been changed."
  newest="$(ls -1t "$DATA_DIR"/backups/state-*.db 2>/dev/null | head -1 || true)"
  [[ -n "$newest" ]] || fail "the backup produced no state database. Nothing has been changed."
  mkdir -p "$DATA_DIR/db-backups"
  cp "$newest" "$DATA_DIR/db-backups/pre-upgrade-${STAMP}.db"
  ok "$DATA_DIR/db-backups/pre-upgrade-${STAMP}.db"
fi

# --- 2. point the deployment at the target ----------------------------------------------

step "2/6" "Recording the previous version"
if ((DRY_RUN)); then
  printf '  would set CCTV_IMAGE=%s and CCTV_IMAGE_PREVIOUS=%s in .env\n' "$TO_IMAGE" "$FROM_IMAGE"
  printf '  would copy .env to .env.pre-upgrade-%s\n' "$STAMP"
else
  cp .env ".env.pre-upgrade-$STAMP"
  set_env_value CCTV_IMAGE_PREVIOUS "$FROM_IMAGE"
  set_env_value CCTV_IMAGE "$TO_IMAGE"
  ok ".env now pins $TO_IMAGE (previous kept as CCTV_IMAGE_PREVIOUS)"
fi

# --- 3. pull -----------------------------------------------------------------------------

step "3/6" "Pulling $TO_IMAGE"
run docker pull "$TO_IMAGE" || fail "could not pull $TO_IMAGE. .env has been pointed back by hand: set CCTV_IMAGE=$FROM_IMAGE"

# --- 4. quiesce and migrate ---------------------------------------------------------------

if ((ROLLBACK)); then
  step "4/6" "Stopping workers and restoring the database"
  run docker compose stop clip-worker uploader cleanup
  latest="$(ls -1t "$DATA_DIR"/db-backups/pre-upgrade-*.db 2>/dev/null | head -1 || true)"
  [[ -n "$latest" ]] || fail "no pre-upgrade backup under $DATA_DIR/db-backups."
  ok "restoring $(basename "$latest")"
  run docker compose --profile ops run --rm --no-deps backup \
    cctv-backup restore-state "/data/db-backups/$(basename "$latest")" --force
else
  step "4/6" "Stopping workers, then migrating with the new code"
  run docker compose stop clip-worker uploader cleanup
  run docker compose --profile ops run --rm --no-deps migrate cctv-migrate ||
    fail "the migration failed. Frigate is still recording; run ./update.sh --rollback"
fi

# --- 5. start -----------------------------------------------------------------------------

step "5/6" "Starting the stack"
run docker compose up -d --remove-orphans

# --- 6. verify -----------------------------------------------------------------------------

step "6/6" "Verifying"
run docker compose --profile ops run --rm --no-deps doctor cctv-doctor ||
  fail "the stack started but doctor found failures. Roll back with ./update.sh --rollback"

if ((ROLLBACK)); then
  printf '\nRolled back to %s. Run ./backup.sh --verify when you are satisfied.\n' "$TO_IMAGE"
else
  printf '\nUpgrade complete. Running %s.\n' "$TO_IMAGE"
  printf 'Rollback: ./update.sh --rollback\n'
fi
