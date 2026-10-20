#!/usr/bin/env bash
# Restore or recover an installation.
#
#   ./restore.sh                     what is in data/backups/, and the recovery verbs
#   ./restore.sh state               restore the newest state database (asks first)
#   ./restore.sh config              restore the newest configuration bundle (asks first)
#   ./restore.sh clips [--dry-run]   download every clip from B2 into data/clips/
#   ./restore.sh rebuild [--into-existing]  reconstruct the clips table from disk and B2
#
# Restoring is the one destructive family of operator commands here, so it is deliberately
# explicit: the default action lists, and replacing an existing file needs --force.
#
# `clips` and `rebuild` are the "the house burned down" verbs. They recover footage from the
# bucket and the bookkeeping from the footage, and neither deletes anything.
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  # Print the comment block under the shebang, without the `# `. An `awk` rather than a `sed`
  # range because a range re-enters after every blank line, which turns the whole file into
  # help text.
  awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"
}

require_env() {
  if [[ ! -f .env ]]; then
    printf 'error: .env is missing.\n' >&2
    exit 1
  fi
}

confirm() {
  local answer
  read -r -p "$1 [y/N] " answer
  if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
    printf 'Nothing done.\n'
    exit 0
  fi
}

case "${1:-}" in
  state|config)
    require_env
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
    confirm 'Continue?'
    if [[ "$kind" == "state" ]]; then
      exec docker compose --profile ops run --rm --no-deps backup \
        cctv-backup restore-state "/data/backups/$(basename "$source_path")" --force
    fi
    exec docker compose --profile ops run --rm --no-deps backup \
      cctv-backup restore-config "/data/backups/$(basename "$source_path")" --force
    ;;
  clips)
    require_env
    shift
    recover=(docker compose --profile ops run --rm --no-deps backup cctv-recover clips)
    if [[ "${1:-}" == "--dry-run" ]]; then
      exec "${recover[@]}" --dry-run
    fi
    # The estimate comes first because 60 days of footage is an egress bill and a long night.
    printf 'Listing the bucket first; nothing is written yet.\n\n'
    "${recover[@]}" --dry-run
    printf '\nRe-run ./restore.sh clips --dry-run at any time to see what is still missing.\n'
    confirm 'Download the clips listed above?'
    exec "${recover[@]}"
    ;;
  rebuild)
    require_env
    shift
    printf 'This reconstructs the clips table from data/clips/ and the bucket.\n'
    printf 'It refuses a database that already has rows unless --into-existing is given,\n'
    printf 'and a clip whose upload history is unknown is recorded as PENDING, never UPLOADED.\n'
    printf 'Stop the stack first if you want the counts to be stable: docker compose stop\n'
    confirm 'Continue?'
    exec docker compose --profile ops run --rm --no-deps backup cctv-recover rebuild "$@"
    ;;
  ""|list|--help|-h)
    if [[ -n "${1:-}" ]]; then usage; exit 0; fi
    printf 'Backups in data/backups/:\n'
    ls -1 data/backups 2>/dev/null || printf '  (none)\n'
    printf '\nRestore with: ./restore.sh state|config\n'
    printf 'Recover with: ./restore.sh clips [--dry-run] | rebuild [--into-existing]\n'
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
