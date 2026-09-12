# LLD 01 — Configuration model

**Implements:** [INV-3](../invariants.md), [INV-11](../invariants.md), [INV-12](../invariants.md)
**Specified by:** [`contracts/cameras-config-schema.md`](../contracts/cameras-config-schema.md)

---

## 1. Responsibilities

| # | Responsibility |
|---|---|
| R1 | Load `app.yml`, `.env`, `cameras.yml` and merge them with defined precedence |
| R2 | Validate everything, with messages that name the file, line, value, and fix |
| R3 | Generate `config/frigate/config.yml` and the service configs deterministically |
| R4 | Detect drift between the generated output and what is on disk |
| R5 | Resolve host-profile-dependent values (hwaccel, detector) |
| R6 | Support dry-run: show what would change without changing it |

---

## 2. Three layers, one precedence order

```text
Priority 1 (lowest)  config/app.yml        application defaults, tracked in git
Priority 2           .env                   site identity + secrets, chmod 600
Priority 3           config/cameras.yml     cameras and per-camera policy
Priority 4 (highest) environment            explicit container env vars (tests, DRY_RUN)
```

### 2.1 Ownership rule

A value belongs to exactly one layer. Validation rejects cross-layer definitions:

| Violation | Error |
|---|---|
| `B2_APPLICATION_KEY` in `cameras.yml` | `secrets belong in .env, not cameras.yml` |
| `LOCATION` differing between `.env` and `cameras.yml` | `LOCATION mismatch: .env says "parent-home", cameras.yml says "parents-home"` |
| A camera name in `.env` | `camera definitions belong in cameras.yml` |
| `retention.local_days` in `.env` | `retention is camera policy — set it in cameras.yml or accept the default` |

This rule exists because the alternative — "it works if you put it in the right
place, and silently does the wrong thing otherwise" — is how configuration systems
rot.

### 2.2 `app.yml` — the application layer

Same everywhere. Contains retention defaults, retry policy, health thresholds, and
every knob from [`00-overview.md`](00-overview.md#4-configuration-knobs). Every key
carries a comment stating its effect and its accepted range.

---

## 3. Load sequence

```text
1. Locate
     APP_CONFIG     default /etc/cctv/app.yml
     .env           default <repo>/.env
     CAMERAS_CONFIG default /etc/cctv/cameras.yml
2. Parse each as YAML / dotenv. A parse error aborts with the file and line.
3. Expand ${VAR} references in cameras.yml from the environment
     → unset variable is a hard error naming the line
4. Merge: app → cameras defaults → per camera
5. Validate (full matrix, §5)
6. Resolve the host profile, fill hwaccel/detector values
7. Produce an immutable Config object
8. Emit the effective config at INFO with secrets redacted
```

### 3.1 Redaction

The `Config` object holds a nested structure of `str` subclasses registered as
secret (`CameraPassword`, `B2Key`, `ApiToken`, `MqttPassword`). Their `__repr__`,
`__str__`, and `__format__` return `"***"`.

This matters more than it looks: the most common way a secret leaks is
`logger.debug("config: %r", config)` during development. With redacting types, that
line is safe by construction.

---

## 4. Generation

### 4.1 Determinism

Same inputs must produce byte-identical output. Achieved by:

- Sorting camera keys alphabetically in the generated output, regardless of the
  order in `cameras.yml`. Reordering the source must not produce a spurious diff.
- Serialising YAML with fixed options (`sort_keys=True`, `default_flow_style=False`,
  2-space indent, no line wrapping).
- Excluding timestamps from the body; the generation timestamp lives in the header
  and is the **only** thing that changes between identical regenerations.

A CI test asserts `generate(generate(x)) == generate(x)` and that regenerating an
unchanged config produces a diff consisting solely of the header timestamp.

### 4.2 Header

Per [`on-disk-layout.md`](../contracts/on-disk-layout.md#6-generated-file-header).

### 4.3 Hand-edit protection

```text
On generation:
  1. If the target exists and does NOT carry the GENERATED header
       → refuse; back up to <name>.local-backup; tell the operator exactly what happened
  2. If the target exists, carries the header, but its body hash ≠ the hash recorded
    at last generation
       → refuse; back up; report the drift with a diff
  3. Otherwise → write
```

Refusing to overwrite is the right default twice over: silently destroying an
operator's manual fix loses their work, and silently keeping it while reporting
success produces a config that does not match its source.

Drift is also surfaced at runtime: `status-collector` compares the expected hash
against the file and reports `DEGRADED / FRIGATE_VERSION_MISMATCH`-style drift if
they disagree, so a hand-edited config becomes visible rather than mysterious.

### 4.4 What is generated

| Output | Content |
|---|---|
| `config/frigate/config.yml` | Full Frigate config, per the mapping table in the cameras contract |
| `config/uploader/config.yml` | Retention policy, storage settings, thresholds |
| `config/cleanup/config.yml` | Retention windows per camera, reconciliation schedule |
| `config/monitoring/monitors.json` | Uptime Kuma monitors, one per component and per camera |

---

## 5. Validation

Full rule table: [`cameras-config-schema.md` §5](../contracts/cameras-config-schema.md#5-validation-rules).

### 5.1 Error message contract

Every validation error is a structured object:

```python
{
  "file": "config/cameras.yml",
  "line": 31,
  "path": "cameras.front-door.rtsp.main",
  "code": "E_RTSP_SCHEME",
  "message": "unsupported scheme \"http\"",
  "fix": "use rtsp:// or rtsps://",
}
```

Rendered for humans:

```text
✗ config/cameras.yml:31  cameras.front-door.rtsp.main
  unsupported scheme "http"
  → fix: use rtsp:// or rtsps://
```

| Requirement | Rationale |
|---|---|
| File and line | "Invalid configuration" without a location is a support ticket |
| The offending value | So the operator does not have to guess which of five URLs is wrong |
| A `fix` string | Every error we can describe, we can suggest a fix for |
| A stable `code` | Lets tests assert precisely and lets the doctor script group failures |
| No secrets in the message | The value is redacted if the path is under a secret-typed key |

### 5.2 Validation phases

| Phase | Checks | When |
|---|---|---|
| Syntax | YAML parses, no duplicate keys, unknown keys rejected | Always |
| Semantic | Types, ranges, enums, cross-field consistency | Always |
| Referential | `${VAR}` resolve, `notify.labels ⊆ detect.objects` | Always |
| Environment | `LOCATION` matches, timezone exists, `DATA_DIR` writable and single-fs | Install/update |
| Capacity | Projected storage vs free space | Install/update |
| Generation | The produced Frigate config passes Frigate's own validation | Install/update |

The last check is the decisive one: after generating, we ask Frigate to validate the
config before restarting it. Frigate has a config validation entrypoint; using it
means we catch our own generator bugs rather than restarting into a broken camera
setup.

> **VERIFY:** the exact mechanism for asking the pinned Frigate version to validate a
> config without applying it. Candidates: a CLI validation mode inside the container,
> or a dry-run field on the config-update API. If neither exists, validate by
> comparing against Frigate's published schema and note the limitation here.

---

## 6. Reload

`SIGHUP` triggers a reload:

```text
1. Re-run phases Syntax → Referential
2. On failure: keep the running configuration, log ERROR, raise health to DEGRADED.
   Never apply a partially valid config
3. On success: swap the Config object atomically (single reference assignment)
4. Log what changed, field by field
5. If cameras changed, reconcile state.db (add/retire) per the cameras contract §6
```

**What a reload can and cannot change:**

| Change | Effect |
|---|---|
| Camera added / removed / disabled | Applied to our services immediately; Frigate needs its config regenerated and reloaded or restarted |
| Retention values | Applied on the next cleanup run |
| Detection settings | Frigate only; requires regeneration |
| RTSP URLs | Frigate only; requires regeneration |

Our services reload cheaply. Frigate's config is only re-read on a Frigate restart,
so the generator plus a `restart` is the safe path for camera changes. `update.sh`
handles the ordering: regenerate → validate with Frigate → restart Frigate → wait
healthy → reload our services.

---

## 7. Interaction with the host profile

Host-profile values are **not** in `cameras.yml`; they come from
[`lld/07`](07-installer-and-host-profiles.md) and are stored in `config/host.yml`
(generated).

```text
config/host.yml
├── arch: amd64
├── profile: intel-igpu
├── hwaccel_args: preset-vaapi
├── detector: {type: openvino, device: GPU}
└── shm_size: 256mb
```

The generator merges them into Frigate's config. If `host.yml` is missing, the
generator falls back to a **conservative** profile (CPU detection, no hwaccel) and
warns — a slow working system beats a fast broken one, and an explicit warning beats
silent degradation.

---

## 8. Tests

| Test | Asserts |
|---|---|
| Determinism | `generate(generate(x)) == generate(x)`; camera reordering produces no diff |
| Merge semantics | Lists replace rather than concatenate; deep merge on mappings; `null` inherits |
| Redaction | No secret value appears in `repr(config)`, `str(config)`, or serialised config |
| Interpolation | Unset `${VAR}` is a hard error naming the line |
| Validation corpus | Each invalid fixture produces its expected `code` and a `fix` string, and exits 78 |
| Hand-edit protection | A modified generated file is backed up, not overwritten |
| Drift detection | A hand-edited generated file is reported by `status-collector` |
| Cross-layer rejection | A secret in `cameras.yml` and a camera in `.env` are both rejected |
| Host fallback | A missing `host.yml` yields the conservative profile and a warning |
