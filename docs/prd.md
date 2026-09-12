# CCTV Stack — Packaging-First Architecture

## Objective

Build a complete CCTV solution that can be deployed on a new machine with a **single, repeatable installation process**.

The system should not depend on manually installing packages, creating cron jobs, copying scripts, or remembering machine-specific configuration.

The desired experience is:

```text
New machine
    │
    ├── Docker
    ├── Repository
    └── Configuration
          │
          ▼
    ./install.sh
          │
          ▼
    Complete CCTV Stack
```

The same package should work at:

```text
Primary Home
Parents Home
Business
Future locations
```

Only location-specific configuration should change.

---

# 1. Package Everything as One Application

The entire project should be treated as a single deployable application:

```text
cctv-stack/
│
├── compose.yml
├── .env.example
├── install.sh
├── update.sh
├── uninstall.sh
├── backup.sh
│
├── config/
│   ├── frigate/
│   │   └── config.yml
│   │
│   ├── uploader/
│   │   └── config.yml
│   │
│   ├── cleanup/
│   │   └── config.yml
│   │
│   └── monitoring/
│
├── services/
│   ├── clip-worker/
│   ├── uploader/
│   ├── cleanup/
│   └── monitoring/
│
├── data/
│   ├── clips/
│   ├── recordings/
│   ├── database/
│   └── state/
│
└── README.md
```

The important principle is:

**The repository contains the application.**

The host machine should contain as little custom configuration as possible.

---

# 2. Separate Application From Machine Configuration

There should be two categories of configuration.

### Application configuration

Things that are common everywhere:

```text
90-day local retention
60-day cloud retention
MP4 format
Event processing
Upload retry policy
Cleanup behavior
Health checks
```

These should have sensible defaults.

### Location configuration

Things that differ between installations:

```text
Location name
Camera names
RTSP URLs
Camera credentials
Storage location
B2 bucket
B2 credentials
Detection settings
Hardware acceleration
```

These should live in `.env` or separate configuration files.

For example:

```env
LOCATION=parents-home

CAMERA_1_NAME=front-door
CAMERA_1_RTSP=rtsp://...

CAMERA_2_NAME=garage
CAMERA_2_RTSP=rtsp://...

LOCAL_RETENTION_DAYS=90
CLOUD_RETENTION_DAYS=60

B2_BUCKET=cctv-parents-home
```

The application itself shouldn't need to be modified.

---

# 3. Installation Should Be Extremely Simple

The target installation experience should be something like:

```bash
git clone <repository>
cd cctv-stack

cp .env.example .env
nano .env

./install.sh
```

The installer should:

1. Validate prerequisites.
2. Create required directories.
3. Validate configuration.
4. Pull required Docker images.
5. Start the containers.
6. Verify that all services are healthy.
7. Display the URLs for the dashboard and Frigate.

Ideally:

```text
========================================
 CCTV Stack
========================================

Location: parents-home

✓ Docker detected
✓ Configuration valid
✓ Storage available
✓ Camera configuration valid
✓ Containers started
✓ Frigate healthy
✓ Uploader healthy
✓ Cleanup healthy
✓ Monitoring healthy

Frigate:
http://<host>:5000

Dashboard:
http://<host>:xxxx

Installation complete.
```

---

# 4. Docker Compose Is the Deployment Boundary

Docker Compose should be the main boundary of the application.

Conceptually:

```text
                 CCTV STACK
                     │
               Docker Compose
                     │
      ┌──────────────┼───────────────┐
      │              │               │
   Frigate        Uploader        Cleanup
      │              │               │
      │              │               │
      └──────────────┼───────────────┘
                     │
                 Monitoring
```

Running:

```bash
docker compose up -d
```

should bring up the entire application.

Running:

```bash
docker compose down
```

should stop it cleanly.

---

# 5. Persistent Data Must Live Outside Containers

Containers should be disposable.

The important data should live in predictable host directories:

```text
/opt/cctv/
│
├── config/
├── data/
│   ├── clips/
│   ├── recordings/
│   ├── database/
│   └── state/
└── logs/
```

If the containers are destroyed:

```bash
docker compose down
docker compose up -d
```

the data remains.

If the machine itself dies, the `/opt/cctv` directory can be restored onto another machine.

---

# 6. Camera Configuration Should Be Data, Not Code

We shouldn't have code like:

```python
if camera == "front_camera":
    ...
elif camera == "garage":
    ...
```

Instead, cameras should be defined through configuration.

For example:

```yaml
cameras:

  front-door:
    rtsp:
      main: ...
      sub: ...
    detect:
      enabled: true
      objects:
        - person

  garage:
    rtsp:
      main: ...
      sub: ...
    detect:
      enabled: true
      objects:
        - person

  backyard:
    rtsp:
      main: ...
      sub: ...
    detect:
      enabled: false
```

This makes adding a camera a configuration change rather than a programming change.

---

# 7. Standardize the Event Output

Regardless of which camera generated the event, the output must always follow the same structure:

```text
clips/
└── <camera-name>/
    └── YYYY/
        └── MM/
            └── DD/
                └── HH-MM-SS.mp4
```

For example:

```text
clips/
├── front-door/
│   └── 2026/
│       └── 09/
│           └── 11/
│               ├── 18-32-45.mp4
│               └── 18-47-12.mp4
│
└── parking/
    └── 2026/
        └── 09/
            └── 11/
                └── 19-03-51.mp4
```

This convention should be guaranteed by the application.

---

# 8. Cloud Storage Should Be an Adapter

The application should not be tightly coupled to Backblaze B2.

The initial implementation can use:

```text
Cloud provider:
Backblaze B2
```

but internally we should think in terms of:

```text
Local Clip
    │
    ▼
Cloud Storage Adapter
    │
    └── B2
```

Later we can support:

```text
B2
S3
Wasabi
MinIO
etc.
```

without rewriting the CCTV event system.

---

# 9. Uploading Must Be Reliable

The uploader should not simply do:

```text
file appears → upload → delete
```

Instead:

```text
              MP4 created
                   │
                   ▼
             Upload queue
                   │
                   ▼
                Upload
              /         \
          success       failure
             │             │
             ▼             ▼
          Complete       Retry
                           │
                       exponential
                          backoff
```

The system should maintain upload state.

For example:

```text
PENDING
UPLOADING
UPLOADED
FAILED
```

This means a machine restart doesn't lose track of what has already been uploaded.

---

# 10. Cleanup Should Be Its Own Service

Retention should not be implemented as random shell commands or cron jobs.

There should be a dedicated cleanup component.

Local:

```text
Age > 90 days
       │
       ▼
     DELETE
```

Cloud:

```text
Age > 60 days
       │
       ▼
     DELETE
```

Where possible, cloud retention should also use the cloud provider's native lifecycle mechanism.

That gives us:

```text
Application cleanup
+
Cloud lifecycle policy
```

rather than relying on one mechanism.

---

# 11. Health Should Be a First-Class Feature

Every component should expose a health state.

For example:

```text
Frigate        HEALTHY
Clip Worker    HEALTHY
Uploader       HEALTHY
Cleanup        HEALTHY
Storage        HEALTHY
Camera 1       ONLINE
Camera 2       ONLINE
Camera 3       OFFLINE
```

The monitoring system should consume these states.

This allows the dashboard to answer:

> **"Is my CCTV system actually working?"**

rather than merely showing that Docker containers are running.

---

# 12. Secrets Must Never Be Part of the Repository

The repository should contain:

```text
.env.example
```

but never:

```text
.env
```

with real credentials.

For example:

```env
B2_KEY_ID=
B2_APPLICATION_KEY=
```

The real values are supplied during deployment.

Similarly, camera passwords should never be committed.

---

# 13. Versioning and Updates

The CCTV package should be versioned.

For example:

```text
v1.0.0
v1.1.0
v1.2.0
```

Updating a machine should ideally be:

```bash
./update.sh
```

The updater should:

1. Pull the new application version.
2. Validate configuration.
3. Pull updated images.
4. Recreate required containers.
5. Verify health.
6. Report success/failure.

The goal is to avoid manually changing containers on each machine.

---

# 14. Backup and Restore

The package should have a clear distinction between:

### Configuration

Easy to back up:

```text
compose.yml
config/
.env
```

### Persistent data

Potentially huge:

```text
clips/
recordings/
```

### Application state

Potentially important:

```text
database/
state/
```

A backup mechanism should at least be able to preserve:

* Configuration
* Database/state
* Camera definitions
* Upload state

The actual 90-day video archive does not necessarily need to be included in a configuration backup because it already exists on the local storage and cloud.

---

# 15. The Ideal End State

The final user experience should be:

```text
I have a new machine.
        │
        ▼
Install Docker
        │
        ▼
Clone CCTV repository
        │
        ▼
Create .env
        │
        ▼
./install.sh
        │
        ▼
┌──────────────────────────────────┐
│          CCTV SYSTEM              │
│                                  │
│ Frigate          ✓               │
│ Cameras          ✓               │
│ Event clips      ✓               │
│ Local retention  ✓               │
│ Cloud upload     ✓               │
│ Cloud retention  ✓               │
│ Cleanup          ✓               │
│ Monitoring       ✓               │
│ Dashboard        ✓               │
│ Alerting         ✓               │
└──────────────────────────────────┘
```

The important principle is:

> **A CCTV installation should be a configuration exercise, not a software-engineering exercise.**

If we get the packaging right, deploying the exact same system at the parents' home, business, or a future location should require changing configuration—not rebuilding the application.
