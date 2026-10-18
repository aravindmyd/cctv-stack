# Runbook: Frigate UI authentication

> **Status:** ✅ Available now — the switch is `frigate.auth.enabled`.
> **Time:** 5 minutes.

Frigate's Web UI — live view, playback, review browsing, exports — is served on
`8971`. This runbook turns its login on, recovers it, and says what it does and
does not protect.

The stack's own services are **not** affected either way: they talk to Frigate's
internal port `5000`, which is exempt from auth by design and is never published
([`frigate-integration.md` §2](../architecture/contracts/frigate-integration.md#2-authentication)).
Enabling the login cannot break clip creation or uploads.

---

## What the login does and does not protect

| | Login **off** (default) | Login **on** |
|---|---|---|
| Who can reach the UI | Any local process that can reach `127.0.0.1:8971` | Only someone with the password |
| Network exposure | None — compose binds `127.0.0.1` only | None — same bind, now with a login on top |
| Our services | Work (port `5000`) | Work (port `5000`, unchanged) |
| Failure if misconfigured | None — there is no password to get wrong | A forgotten password locks humans out of the UI, never out of recordings |

The login is a **human-access control for the local operator**. It is not a network
boundary: `8971` is loopback-only and HTTPS-only. Do not publish `5000` to gain
anything — that port is a Frigate admin bypass by design.

---

## Enable the login

### 1. Set the switch in `cameras.yml`

```yaml
frigate:
  auth:
    enabled: true
```

`config/cameras.yml` is the source of truth ([INV-3](../architecture/invariants.md));
`config/frigate/config.yml` is generated from it and any hand edit there is lost.

### 2. Regenerate and restart

```bash
docker compose up -d
```

That re-runs the one-shot `config` service (which rewrites
`config/frigate/config.yml`) and recreates `frigate`.

> `docker compose restart frigate` does **not** regenerate the config. Use it only
> when you changed the generated file directly, as in the reset procedure below.

### 3. Read the generated admin password

On the first start with auth enabled and no users in Frigate's database, Frigate
creates `admin` and prints the password once:

```bash
docker compose logs frigate 2>&1 | grep -B1 -A3 -iE "Created a default user|Password:"
```

```text
********************************************************
***    Auth is enabled, but no users exist.          ***
***    Created a default user:                       ***
***    User: admin                                   ***
***    Password: 3f9c…                               ***
********************************************************
```

Copy it now. It is not recoverable afterwards from the config — only by resetting
it (below).

### 4. Log in and change the password

1. Open **<https://127.0.0.1:8971>** — `https`, not `http`. The port is HTTPS-only;
   a plain-HTTP request is answered with `400 The plain HTTP request was sent to
   HTTPS port` by nginx and never reaches Frigate. Accept the one-time
   self-signed-certificate warning; Frigate generates that certificate on first
   start.
2. Log in as `admin` with the password from the log.
3. **Settings → Users → admin** — set a new password. Frigate requires at least
   12 characters.

### 5. Verify

```bash
# Unauthenticated API access to the published port must now be refused.
curl -k -s -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8971/api/profile
# 401

# The pipeline must be untouched.
docker compose ps
curl -s http://127.0.0.1:8082/health | head -c 200   # uploader (if enabled)
curl -s http://127.0.0.1:8083/health | head -c 200   # cleanup
```

In the UI, the account menu should show your username and role. On Frigate's
internal port the same UI would show `anonymous` / `admin` — that is the exemption
working, not a hole, as long as `5000` stays unpublished.

---

## Sessions and the JWT secret

| Setting | Default | Note |
|---|---|---|
| Session length | 86400 s (24 h) | Refreshed while you use the UI; not projected by our generator |
| JWT secret | generated on first start | Stored as `.jwt_secret` in Frigate's `/config`, i.e. `config/frigate/.jwt_secret` on the host |

`.jwt_secret` is a credential: never commit it or paste it into an issue
([INV-2](../architecture/invariants.md)). Deleting it invalidates every session
and is not needed unless you suspect it leaked.

---

## Locked out: reset the admin password

`auth.reset_admin_password` is a **one-shot emergency flag**, so it is deliberately
not part of `cameras.yml` — leaving it in the source of truth would rotate the
password on every restart.

```bash
# 1. Add it to the generated config, next to `enabled:`
#    (this edit is temporary; do not rely on it surviving `docker compose up`)
$EDITOR config/frigate/config.yml
```

```yaml
auth:
  enabled: true
  reset_admin_password: true
```

```bash
# 2. Restart WITHOUT regenerating, then read the new password
docker compose restart frigate
docker compose logs frigate 2>&1 | grep -A3 -iE "Reset admin password"

# 3. Log in, change the password in Settings → Users,
#    then remove `reset_admin_password` and restart again
docker compose restart frigate
```

If the UI is unreachable entirely, nothing is lost: recordings, clips and uploads
all run through port `5000` and keep working. Reset the login when convenient.

---

## Turn the login off again

```yaml
frigate:
  auth:
    enabled: false
```

```bash
docker compose up -d
```

Frigate keeps the user in its database; the login is simply not enforced. Turning
it back on reuses that user (no new password is generated).

---

## Behind a reverse proxy

Frigate does not implement OIDC, SAML or LDAP. If an upstream proxy already
authenticates users, the intended pattern is:

1. Turn Frigate's login **off** (`frigate.auth.enabled: false`).
2. Configure the proxy to pass `remote-user` / `remote-role` headers, and set a
   proxy secret so nothing else can forge them.
3. Set `trusted_proxies` in Frigate's config so login rate limiting sees the real
   client address instead of the proxy's.

Those three options live in Frigate's own `proxy:` block, which this generator does
not emit yet. Treat them as a hand-managed extension and expect the file to change
when the generator grows that support.

---

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `400 The plain HTTP request was sent to HTTPS port` | Used `http://` | Use `https://127.0.0.1:8971` |
| Browser warns about the certificate | Frigate's self-signed cert | Expected on a loopback port; accept once |
| No password in the logs | A user already exists (auth was enabled before) | Reset it (above), or log in with the existing password |
| Login works, then drops after a day | Session length elapsed (default 24 h) | Log in again |
| `docker compose up -d` silently turned the login back off | The switch was set only in the generated file | Set `frigate.auth.enabled: true` in `config/cameras.yml` |
| Other devices on the LAN cannot reach the UI | `8971` is bound to `127.0.0.1` by design | Do not bind it to `0.0.0.0`; use an SSH tunnel or a reverse proxy |

---

## Related

- [`install.md`](install.md) — what the install verifies
- [`rotate-credentials.md`](rotate-credentials.md) — camera and B2 credentials
- [`../architecture/contracts/frigate-integration.md`](../architecture/contracts/frigate-integration.md#2-authentication) — why the internal port needs no credentials
- [`../architecture/contracts/cameras-config-schema.md`](../architecture/contracts/cameras-config-schema.md) — the `frigate.auth` field
