# Browse the recordings over SMB

Frigate keeps its footage on the host at `${CCTV_DATA_DIR}/media` (`recordings/`,
`clips/`, `exports/`). This page adds a small Samba container that publishes that tree as
one **read-only** network share, so you can open it in Finder, Explorer, or any SMB client
instead of clicking through Frigate's UI.

It is opt-in. The default install does not open port 445.

## Why not just use the Frigate UI

The UI is built for reviewing events. It is not built for pulling a file out, checking what
a folder holds, or handing a clip to someone. A share is.

## What you get

| | |
|---|---|
| Share name | `recordings` |
| Path served | `${CCTV_DATA_DIR}/media` |
| Contents | `recordings/`, `clips/` (snapshots and clips), `exports/` |
| Access | read-only, one account, no guest access |
| Protocol | SMB2.1 minimum, SMB3 maximum (SMB1 refused) |

Read-only is enforced twice: `read only = yes` in `smb.conf` refuses the write at the SMB
layer, and the container mounts the tree `:ro`, so even a Samba bug could not modify or
delete footage. Frigate owns retention; this share exists to look.

## Enable it

The build context is `docker/samba/` in this repository. Add a service to
`compose.override.yml` next to the deployment:

```yaml
services:
  samba:
    build: ./docker/samba
    image: cctv-stack-samba:local
    restart: unless-stopped
    environment:
      CCTV_SMB_PASSWORD: ${CCTV_SMB_PASSWORD:?set CCTV_SMB_PASSWORD in .env}
    ports:
      - "445:445"
    volumes:
      - ${CCTV_DATA_DIR:-./data}/media:/srv/media:ro
    healthcheck:
      test: ["CMD-SHELL", "nc -z 127.0.0.1 445 || exit 1"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 20s
```

Then set the password and start it:

```bash
openssl rand -hex 12          # or choose your own
echo 'CCTV_SMB_PASSWORD=...' >> .env
docker compose build samba
docker compose up -d --no-deps samba
```

The username is always `cctv`. It is fixed in `smb.conf` so that file stays a plain file
you can read, rather than something generated at start-up.

## Connect from a Mac

Finder → **Go → Connect to Server** (⌘K) → `smb://<vm-ip>/recordings`, then sign in as
`cctv` with the password from `.env`.

To mount it from the command line:

```bash
mount_smbfs //cctv@192.168.0.152/recordings ~/mnt/cctv
```

## Verify

From the container, which tests the server without involving the network:

```bash
docker exec -it cctv-stack-samba-1 \
  smbclient //127.0.0.1/recordings -U 'cctv%'"$CCTV_SMB_PASSWORD" -c ls
```

You should see `recordings`, `clips` and `exports`. A write must be refused:

```bash
docker exec -it cctv-stack-samba-1 \
  smbclient //127.0.0.1/recordings -U 'cctv%'"$CCTV_SMB_PASSWORD" -c 'mkdir nope'
# NT_STATUS_ACCESS_DENIED making remote directory \nope
```

From the client side, `smbutil view //cctv@<vm-ip>` lists the share, and
`nc -z <vm-ip> 445` confirms the port is reachable.

## If it fails

| Symptom | Check |
|---|---|
| Finder cannot connect at all | Is 445 published? `docker compose ps samba`, then `nc -z <vm-ip> 445` from the client |
| "The operation can't be completed" / wrong password | `docker logs cctv-stack-samba-1` and the per-client log at `/var/log/samba/log.<client-ip>` — `log level = 2` records failed logons, the Samba default records nothing |
| Share mounts but the folder is empty | The mount is `${CCTV_DATA_DIR}/media`. Confirm `CCTV_DATA_DIR` in `.env` points at the directory that actually holds `recordings/` |
| Containers refuse to start | `CCTV_SMB_PASSWORD` is unset. The entrypoint exits with a message rather than serving an empty password |

## Security notes

- Port **445 is published to the LAN**. This is the one service in the stack that is not
  loopback-only, so it is protected by an account and a password, not by obscurity. Remove
  the `ports:` entry to keep the share container-internal.
- `map to guest = never` and `restrict anonymous = 2`: no anonymous access, no share
  enumeration without credentials.
- `force user = root` is deliberate. Frigate writes its date directories as root inside its
  own container, so serving them as root avoids loosening permissions across tens of
  gigabytes of footage. The `:ro` mount is what bounds the risk.
- The password lives in `.env`. Rotate it by editing `.env` and running
  `docker compose up -d --no-deps samba`; the entrypoint re-applies it on every start.
