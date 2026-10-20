#!/usr/bin/env bash
# Back up the configuration, the state database and a human-readable manifest.
#
#   ./backup.sh                  create a backup under data/backups/
#   ./backup.sh --verify         check the newest backup end to end
#
# The configuration bundle is encrypted by default because it contains .env and cameras.yml.
# Set CCTV_BACKUP_PASSPHRASE in .env; without it this refuses to write an unencrypted bundle.
#
# Nothing here touches footage: the cloud is the backup for clips.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  printf 'error: .env is missing; run ./install.sh first.\n' >&2
  exit 1
fi

if ! grep -q '^CCTV_BACKUP_PASSPHRASE=..*' .env; then
  printf '%s\n' \
    'error: CCTV_BACKUP_PASSPHRASE is not set in .env.' \
    '       The config bundle holds your RTSP password and B2 key, so it is encrypted.' \
    '       Generate one with:  openssl rand -base64 32' \
    >&2
  exit 1
fi

exec docker compose --profile ops run --rm --no-deps backup \
  cctv-backup "$@"
