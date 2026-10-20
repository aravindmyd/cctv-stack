#!/usr/bin/env bash
# Restore from a backup produced by ./backup.sh.
#
#   ./restore.sh            list what is in data/backups/
#   ./restore.sh state      restore the newest state database (asks before replacing)
#   ./restore.sh config     restore the newest configuration bundle (asks before replacing)
#
# Restoring is the one destructive operator command here, so it is deliberately explicit:
# the default action lists, and replacing an existing file needs --force.
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//'
}

if [[ ! -f .env ]]; then
  printf 'error: .env is missing.\n' >&2
  exit 1
fi

case "${1:-}" in
  state|config)
    kind=$1
    shift
    if [[ "$kind" == "state" ]]; then
      source_path=$(ls -1t data/backups/state-*.db 2>/dev/null | head -n 1 || true)
    else
      source_path=$(ls -1t data/backups/config-*.tar.gz* 2>/dev/null | head -n 1 || true)
    fi
    if [[ -z "$source_path" ]]; then
      printf 'error: no %s backup found in data/backups/. Run ./backup.sh first.\n' "$kind" >&2
      exit 1
    fi
    printf 'Restoring %s from %s\n' "$kind" "$source_path"
    printf 'The current file is kept alongside as a .pre-restore copy.\n'
    read -r -p 'Continue? [y/N] ' answer
    if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
      printf 'Nothing done.\n'
      exit 0
    fi
    if [[ "$kind" == "state" ]]; then
      exec docker compose --profile ops run --rm --no-deps backup \
        cctv-backup restore-state "/data/backups/$(basename "$source_path")" --force
    fi
    exec docker compose --profile ops run --rm --no-deps backup \
      cctv-backup restore-config "/data/backups/$(basename "$source_path")" --force
    ;;
  ""|list|--help|-h)
    if [[ -n "${1:-}" ]]; then usage; exit 0; fi
    printf 'Backups in data/backups/:\n'
    ls -1 data/backups 2>/dev/null || printf '  (none)\n'
    printf '\nRestore with: ./restore.sh state|config\n'
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
