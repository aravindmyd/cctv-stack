#!/usr/bin/env bash
# Diagnose an installation. Read-only: nothing is started, stopped, migrated or deleted.
#
#   ./scripts/doctor.sh            human-readable report
#   ./scripts/doctor.sh --json     machine-readable, for a bug report
#
# Also writes data/logs/doctor-<timestamp>.json, so a report survives the terminal scrollback.
# The report is redacted: no camera password and no B2 key appears in it.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  printf 'error: .env is missing; run ./install.sh first.\n' >&2
  exit 1
fi

# `--save` puts the JSON next to the other logs; the human report still goes to stdout.
exec docker compose --profile ops run --rm --no-deps doctor \
  cctv-doctor --save /data/logs "$@"
