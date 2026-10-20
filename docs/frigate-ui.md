# Frigate UI login

Frigate's web interface shows live view, playback, reviews and exports on port `8971`. By
default it has no login. This page turns one on, resets it when you are locked out, and
explains what it does and does not protect.

The stack's own services are unaffected either way. They talk to Frigate's internal port
`5000`, which is exempt from authentication and is never published outside the Compose
network. Turning the UI login on cannot break clip creation or uploads.

## What the login covers

| | Login off (default) | Login on |
|---|---|---|
| Who can open the UI | Any process that can reach `127.0.0.1:8971` | Only someone with the password |
| Network exposure | None; the port is bound to loopback | None; same binding, with a login on top |
| The clip worker, uploader, cleanup | Work | Work, unchanged |
| **System page** (CPU, RAM, GPU, storage, UI logs) and the config editor | **`Access Denied`** | Open |
| If it is misconfigured | There is no password to lose | A forgotten password locks humans out of the UI, not out of recordings |

The login is access control for the person at the machine. It is not a network boundary:
`8971` listens on loopback and speaks HTTPS only.

!!! warning "Login off does not mean "logged in as admin""
    In Frigate 0.18 an unauthenticated request gets the **`viewer`** role, not `admin`
    (`frigate/api/auth.py`: `/api/auth` answers `202` with the headers `remote-user: viewer` and
    `remote-role: viewer`). The UI's route table puts `/system` and `/config` behind
    `requiredRoles: ["admin"]`, so clicking **CPU** on the System page (or opening the config
    editor or the log viewer) lands on `/unauthorized` — *"Access Denied — You don't have
    permission to view this page."* Nothing is broken; those pages simply require a login.

    Verify the role without a browser: `curl -sk -i https://127.0.0.1:8971/api/auth | head -12`
    prints `202` with `remote-role: viewer` while the login is off, and `401` once it is on.
    Live view, review, explore, export and the Settings page work either way, and the stack's own
    services keep using port `5000`, so nothing else in this file is affected.

## Turn it on

### 1. Set the switch

```yaml
# config/cameras.yml
frigate:
  auth:
    enabled: true
```

### 2. Regenerate and restart

```bash
docker compose up -d
```

This re-runs the one-shot `config` service, which rewrites `config/frigate/config.yml`, and
recreates Frigate because the file changed.

`docker compose restart frigate` does not regenerate the config. Restarting alone is only
correct for the password reset below, which edits the generated file directly.

### 3. Read the generated password

On the first start with authentication enabled and no users in its database, Frigate creates
an `admin` user and prints the password once:

```bash
docker compose logs frigate 2>&1 | grep -B1 -A3 -iE "Created a default user|Password:"
```

```text
***    Auth is enabled, but no users exist.
***    Created a default user:
***    User: admin
***    Password: 3f9c...
```

Copy it now. The config does not contain it, and it cannot be read back later.

### 4. Log in and change it

Open <https://127.0.0.1:8971>. Use `https`, not `http`; a plain request on that port is
answered with `400 The plain HTTP request was sent to HTTPS port`. Accept the certificate
warning once; Frigate generated that certificate on first start.

Log in as `admin`, then set your own password under **Settings → Users**. Frigate requires
at least twelve characters.

### 5. Verify

```bash
curl -k -s -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8971/api/profile
# 401

docker compose ps
curl -s http://127.0.0.1:8082/health | jq .state
curl -s http://127.0.0.1:8083/health | jq .state
```

The 401 is the login working. The services should report as they did before.

## The JWT secret

| Setting | Default | Note |
|---|---|---|
| Session length | 24 hours | Refreshed while you use the UI |
| JWT secret | generated on first start | Stored as `.jwt_secret` in `config/frigate/` on the host |

`.jwt_secret` is a credential. Do not commit it or paste it into an issue. Deleting it
invalidates every session and is not needed unless you suspect it leaked.

## Locked out

Frigate has a one-shot reset flag. It is not part of `cameras.yml`, because leaving it there
would reset the password on every start.

```bash
$EDITOR config/frigate/config.yml    # set reset_admin_password: true under auth:
docker compose restart frigate
docker compose logs frigate 2>&1 | grep -A3 -iE "Reset admin password"
```

Log in with the new password, change it in **Settings → Users**, then remove the flag and
restart Frigate again.

If the UI is unusable, nothing else is lost. Recordings, clips, and uploads all go through
port `5000` and keep working. Reset the login when it is convenient.

## Turn it off

```yaml
frigate:
  auth:
    enabled: false
```

```bash
docker compose up -d
```

Frigate keeps the user in its database; the login simply stops being enforced. Turning it
back on reuses that user, with no new password printed.

## Behind a reverse proxy

Frigate does not implement OIDC, SAML or LDAP. If a proxy in front of it already
authenticates users, the usual pattern is to turn Frigate's login off and let the proxy pass
user headers, with a proxy secret so nothing else can forge them. Those settings live in
Frigate's own `proxy:` block, which this generator does not emit. Hand-edit them only if you
accept that the next regeneration will overwrite the file.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `400 The plain HTTP request was sent to HTTPS port` | Used `http://` | Use `https://127.0.0.1:8971` |
| Browser warns about the certificate | Self-signed certificate generated by Frigate | Expected on a loopback port; accept it once |
| No password in the log | A user already exists | Reset it, or log in with the existing password |
| Login drops after a day | The 24-hour session expired | Log in again |
| The login turned itself off after `docker compose up -d` | The switch was set only in the generated file | Set `frigate.auth.enabled: true` in `cameras.yml` |
| Another machine cannot reach the UI | `8971` is bound to loopback on purpose | Use an SSH tunnel or a reverse proxy; do not publish it |
