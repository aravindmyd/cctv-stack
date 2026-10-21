#!/bin/sh
# Create/refresh the one SMB account, then run smbd in the foreground.
#
# The password comes from CCTV_SMB_PASSWORD on every start, so .env is the single
# source of truth: change it there and `docker compose up -d samba` applies it.
set -eu

USER_NAME="cctv"

if [ -z "${CCTV_SMB_PASSWORD:-}" ]; then
  echo "FATAL: CCTV_SMB_PASSWORD is empty. Set it in .env." >&2
  exit 1
fi

# Samba maps an SMB account onto a Unix account, so the name must resolve inside
# this container or authentication fails in a way the client cannot explain.
id "$USER_NAME" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"

# `-s` reads the password (and its confirmation) silently from stdin. Running
# this every start makes the password idempotent rather than set-once.
printf '%s\n%s\n' "$CCTV_SMB_PASSWORD" "$CCTV_SMB_PASSWORD" \
  | smbpasswd -s -a "$USER_NAME" >/dev/null
smbpasswd -e "$USER_NAME" >/dev/null

# Fail loudly and immediately on a bad smb.conf, instead of starting smbd that
# then refuses every connection.
testparm -s /etc/samba/smb.conf >/dev/null

echo "samba ready: share 'recordings' (read-only) for user '${USER_NAME}'"
exec smbd --foreground --no-process-group --debug-stdout
