#!/usr/bin/env bash
#
# Run the live integration suite against a real S3-compatible server.
#
#   ./tests/integration/run.sh                     # everything that needs an endpoint
#   ./tests/integration/run.sh -k outage            # a subset
#   CCTV_TEST_SLOW=1 ./tests/integration/run.sh     # including the 8 x 500 soak
#
# Starts RustFS, waits for it to answer, runs the tests, and tears the server down again.
# See README.md for why RustFS replaced MinIO, and how to run against something else instead.
#
# **First run pulls an image**, which is a multi-minute job on a slow connection.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PORT="${CCTV_TEST_STORAGE_PORT:-19000}"
READY_URL="http://127.0.0.1:${PORT}/health"

cd "$REPO"

cleanup() {
  docker compose -f "$HERE/compose.yml" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "starting the S3 test double on 127.0.0.1:${PORT} …"
docker compose -f "$HERE/compose.yml" up -d

echo "waiting for it to become ready …"
for _ in $(seq 1 60); do
  curl -fsS "$READY_URL" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "$READY_URL" >/dev/null 2>&1; then
  echo "the test double never became ready. Recent logs:" >&2
  docker compose -f "$HERE/compose.yml" logs --tail 40 >&2
  exit 1
fi
echo "ready."

export CCTV_TEST_STORAGE_ENDPOINT_URL="${CCTV_TEST_STORAGE_ENDPOINT_URL:-http://127.0.0.1:${PORT}}"
export CCTV_TEST_STORAGE_KEY_ID="${CCTV_TEST_STORAGE_KEY_ID:-cctv-integration}"
export CCTV_TEST_STORAGE_APPLICATION_KEY="${CCTV_TEST_STORAGE_APPLICATION_KEY:-cctv-integration-secret}"
export CCTV_TEST_STORAGE_REGION="${CCTV_TEST_STORAGE_REGION:-us-east-1}"

# `-s` so the measured provider behaviour (which verification a real single-part PUT allows)
# is visible in the run log; it is evidence that gets written into the contract.
#
# Not `exec`: replacing this shell would skip the EXIT trap and leave the server running.
uv run --extra storage pytest "$HERE" -m integration -s -ra "$@"
