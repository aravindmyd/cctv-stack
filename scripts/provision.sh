#!/usr/bin/env bash
# Configure the Backblaze B2 bucket: create it, set versioning, write the expiry rule, and
# prove the whole path with a probe object.
#
#   ./scripts/provision.sh              full sequence
#   ./scripts/provision.sh --status     report only, change nothing
#   ./scripts/provision.sh --dry-run    print the plan, change nothing
#   ./scripts/provision.sh --remove-lifecycle
#                                       remove our expiry rule, keeping anyone else's
#
# Safe to re-run: an existing bucket is left alone, an already-correct rule is not rewritten,
# and every other rule in the bucket is preserved. Unlike a plain PUT of a lifecycle
# configuration, nothing here replaces rules belonging to another tool.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  printf 'error: .env is missing; run ./install.sh first.\n' >&2
  exit 1
fi

if ! grep -q '^CCTV_STORAGE_BUCKET=..*' .env; then
  printf '%s\n' \
    'Cloud backup is not configured (CCTV_STORAGE_BUCKET is unset), so there is nothing to' \
    'provision. That is a supported state: the uploader runs and reports DISABLED.' \
    >&2
  exit 0
fi

exec docker compose --profile ops run --rm --no-deps provision \
  cctv-provision --cameras /config/cameras.yml "$@"
