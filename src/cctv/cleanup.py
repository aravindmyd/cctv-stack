"""Local clip retention — cleanup job J1
([LLD 05](../../../internal/architecture/lld/05-cleanup.md)).

This is the only module in the system that deletes a clip, and therefore the one that has to
be the most conservative. [INV-1](../../../internal/architecture/invariants.md) is the reason: a
clip is never deleted locally until its upload state is terminal-success, or the operator
explicitly forces it.

The implementation is shaped so that the unsafe thing is **impossible** rather than unlikely
(LLD 05 §2):

1. `db.local_expiry_batch` enumerates the states that permit deletion, so a state added later
   is safe by default rather than deletable by default.
2. Each clip's row is re-read and re-checked immediately before its file is unlinked, which
   catches a state change between the page query and the unlink.
3. An `UPLOADED` clip must also carry a `cloud_key` **and** an `uploaded_at`, so a row marked
   uploaded without a recorded upload is never treated as backed up.
4. The path is resolved and must stay inside `clips/`, so a corrupted or tampered `path`
   cannot reach anything else.
5. `DRY_RUN` logs every intended deletion and performs none.

What this module deliberately does **not** do (LLD 05 §2.3): walk the filesystem to decide
what to delete; delete differently for a retired camera; touch `data/media/` or
`data/frigate/`; list the bucket to decide anything; re-upload; or auto-resolve a finding.

## Scope

**J1 and J2.** Cloud retention deletes an object only after the provider confirms it is gone,
and only for a clip whose upload succeeded. Reconciliation (J3), housekeeping (J4) and the full
retention report (J5) are not implemented yet; the deferred work is recorded in
[`.private/plans/milestones.md`](../../../.private/plans/milestones.md).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import sqlite3
import sys
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from . import atomic, db, health, paths, storage, timeutil
from .config import CamerasConfig, ConfigError, load_cameras_config
from .logging import configure_logging
from .reconcile import CRITICAL_KINDS, Reconciler, ReconcileRun
from .timeutil import Instant

logger = logging.getLogger(__name__)

__all__ = [
    "COMPONENT",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DELETE_CONCURRENCY",
    "DEFAULT_HEALTH_PORT",
    "DEFAULT_LOCAL_DAYS",
    "DEFAULT_RETIRE_GRACE_DAYS",
    "DEFAULT_RUN_INTERVAL_S",
    "CleanupHealth",
    "CleanupPathError",
    "CleanupService",
    "CleanupSettings",
    "CloudRetention",
    "CloudRun",
    "HousekeepRun",
    "Housekeeper",
    "LocalRetention",
    "RetentionRun",
    "main",
    "resolve_within_clips_dir",
]

#: The component name used in logs, health reports and `system_events`.
COMPONENT = "cleanup"

#: The directory, under the data directory, that local retention owns.
CLIPS_DIR_NAME = "clips"

SECONDS_PER_DAY = 86400.0

DEFAULT_RUN_INTERVAL_S = 3600.0
DEFAULT_BATCH_SIZE = 200
DEFAULT_SCHEDULER_TICK_S = 1.0

#: Cloud deletion is network I/O, so four at a time (lld/05 §12). Higher multiplies provider
#: rate limits without making a run measurably shorter.
DEFAULT_DELETE_CONCURRENCY = 4

#: How many consecutive failed cloud deletions make the health check unhealthy (lld/05 §9).
DEFAULT_CLOUD_DELETE_FAILURE_CRITICAL = 5

#: Reconciliation is daily (`lld/05` §5). It walks every object, so a shorter cadence would cost
#: more than the drift it finds.
DEFAULT_RECONCILE_INTERVAL_S = 86400.0

#: Objects one reconciliation pass will look at. The cap is what keeps a pathological bucket from
#: turning a daily job into a permanent one.
DEFAULT_RECONCILE_MAX_OBJECTS = 20_000

#: The cleanup health surface (hld.md §11 — internal only, loopback in compose).
DEFAULT_HEALTH_PORT = 8083

#: Overdue-retention thresholds. `lld/05` §9: 0 is HEALTHY, 1-10 is DEGRADED, more than 10 -
#: or anything held longer than 30 days - is UNHEALTHY. The `warn` default is 1 rather than 10
#: because even one held clip means the invariant is costing disk, and the point of J5 is to
#: make that visible while it is still small.
DEFAULT_CLIPS_OVERDUE_WARN = 1
DEFAULT_CLIPS_OVERDUE_CRITICAL = 10
DEFAULT_OVERDUE_AGE_CRITICAL_DAYS = 30

#: `health.disk_warn_pct` / `disk_critical_pct` (lld/00 §4), shared by every service.
DEFAULT_DISK_WARN_PCT = 15.0
DEFAULT_DISK_CRITICAL_PCT = 5.0

EXIT_OK = 0
EXIT_CONFIG = 78
EXIT_UNAVAILABLE = 69

#: The `sweep_state` row recording when J1 last ran. That table is reused rather than a new one
#: created: it already carries `last_run_at`, `last_success_at` and `consecutive_failures`,
#: which is exactly what a staleness check needs.
LAST_RUN_NAME = "cleanup_local"

#: The `sweep_state` row recording when J3 last completed.
RECONCILE_RUN_NAME = "cleanup_reconcile"

#: The `sweep_state` row recording when J4 last completed.
HOUSEKEEP_RUN_NAME = "cleanup_housekeep"

#: The retention applied when a camera is not in `cameras.yml` — a retired camera. Mirrors
#: `RetentionConfig`'s default, and is deliberately a copy rather than an import so that
#: changing the config default cannot silently change what happens to retired footage.
DEFAULT_LOCAL_DAYS = 90
DEFAULT_RETIRE_GRACE_DAYS = 0

#: The same fallback for the cloud copy. `retention.cloud_days` defaults to 60.
DEFAULT_CLOUD_DAYS = 60

#: Housekeeping is daily (`lld/05` §6). Emptied directories and a week of health history are not
#: worth an hourly walk of the tree on a machine whose disks are the scarce resource.
DEFAULT_HOUSEKEEP_INTERVAL_S = 86400.0

#: Database backups kept by J4's rotation. Bounded rather than emptied, because
#: `./update.sh --rollback` restores from this directory and a rotation that reached zero would
#: delete the very artefact the rollback depends on.
DEFAULT_KEEP_DB_BACKUPS = 10

#: A `.tmp` may belong to a fetch happening right now. This is the belt-and-braces tier *behind*
#: the worker's own reaper, so it is generous: a day, not minutes.
TMP_AGE_S = 86400.0

#: `*.bad` files are the evidence of a fetch that produced something unplayable. A week is long
#: enough for someone to notice and short enough not to accumulate.
BAD_FILE_AGE_S = 7 * SECONDS_PER_DAY

#: J4's database windows (`lld/05` §6). Each is a different answer to "how long is this worth
#: keeping?": health history is a ring buffer, upload attempts are debugging, and audit events
#: are the record someone reads months later.
HEALTH_HISTORY_DAYS = 7.0
UPLOAD_ATTEMPT_DAYS = 30.0
SYSTEM_EVENT_DAYS = 90.0
PIN_DAYS = 30.0


class CleanupPathError(ValueError):
    """A clip path that will not be acted on.

    Raised rather than worked around: a `clips.path` that escapes `clips/` means either a bug
    or tampering, and the only safe response is to refuse and report. Nothing is deleted.
    """


@dataclass(frozen=True, slots=True)
class CleanupSettings:
    """Every input is an environment variable, so there are no CLI arguments to get wrong."""

    data_dir: Path
    cameras_path: Path
    db_path: Path
    run_interval_s: float
    batch_size: int
    dry_run: bool
    retire_grace_days: int
    health_host: str
    health_port: int
    scheduler_tick_s: float
    clips_overdue_warn: int
    clips_overdue_critical: int
    overdue_age_critical_days: int
    disk_warn_pct: float
    disk_critical_pct: float
    log_level: str | None
    #: Cloud retention (J2) runs in the same cycle as J1 (`lld/05` §8), so it has no cadence of
    #: its own. `dry_run` above applies to all three passes.
    delete_concurrency: int = DEFAULT_DELETE_CONCURRENCY
    #: Reconciliation (J3) is daily by default, and `0` disables it.
    reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S
    reconcile_max_objects: int = DEFAULT_RECONCILE_MAX_OBJECTS
    cloud_delete_failure_critical: int = DEFAULT_CLOUD_DELETE_FAILURE_CRITICAL
    #: Housekeeping (J4) is daily by default, and `0` disables it.
    housekeep_interval_s: float = DEFAULT_HOUSEKEEP_INTERVAL_S
    keep_db_backups: int = DEFAULT_KEEP_DB_BACKUPS
    #: Where `./update.sh` writes pre-upgrade copies. `None` means the conventional
    #: `<data_dir>/db-backups`, resolved by `db_backups_dir` so a caller that does not care
    #: never has to spell it out.
    db_backup_dir: Path | None = None

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / CLIPS_DIR_NAME

    @property
    def db_backups_dir(self) -> Path:
        return (
            self.db_backup_dir if self.db_backup_dir is not None else self.data_dir / "db-backups"
        )

    def configuration_problem(self) -> str | None:
        if self.run_interval_s <= 0:
            return "CCTV_CLEANUP_RUN_INTERVAL_S must be positive."
        if self.batch_size < 1:
            return "CCTV_CLEANUP_BATCH_SIZE must be at least 1."
        if self.retire_grace_days < 0:
            return "CCTV_CLEANUP_RETIRE_GRACE_DAYS cannot be negative."
        if self.health_port < 0:
            return "CCTV_CLEANUP_HEALTH_PORT cannot be negative."
        if self.clips_overdue_critical < self.clips_overdue_warn:
            return "CCTV_CLEANUP_OVERDUE_CRITICAL cannot be below CCTV_CLEANUP_OVERDUE_WARN."
        if self.disk_critical_pct >= self.disk_warn_pct:
            return "CCTV_HEALTH_DISK_CRITICAL_PCT must be below CCTV_HEALTH_DISK_WARN_PCT."
        if self.delete_concurrency < 1:
            return "CCTV_CLEANUP_DELETE_CONCURRENCY must be at least 1."
        if self.reconcile_interval_s < 0:
            return "CCTV_CLEANUP_RECONCILE_INTERVAL_S cannot be negative (0 disables it)."
        if self.reconcile_max_objects < 1:
            return "CCTV_CLEANUP_RECONCILE_MAX_OBJECTS must be at least 1."
        if self.housekeep_interval_s < 0:
            return "CCTV_CLEANUP_HOUSEKEEP_INTERVAL_S cannot be negative (0 disables it)."
        if self.keep_db_backups < 1:
            return "CCTV_CLEANUP_KEEP_DB_BACKUPS must be at least 1, or rotation has no floor."
        return None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> CleanupSettings:
        env = os.environ if environ is None else environ

        def number(name: str, default: float) -> float:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}={raw!r} is not a number.") from exc

        config_dir = Path(env.get("CCTV_CONFIG_DIR", "/config"))
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))

        return cls(
            data_dir=data_dir,
            cameras_path=Path(env.get("CCTV_CAMERAS", str(config_dir / "cameras.yml"))),
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            run_interval_s=number("CCTV_CLEANUP_RUN_INTERVAL_S", DEFAULT_RUN_INTERVAL_S),
            batch_size=int(number("CCTV_CLEANUP_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
            dry_run=(env.get("DRY_RUN") or "").strip().lower() in {"1", "true", "yes"},
            retire_grace_days=int(
                number("CCTV_CLEANUP_RETIRE_GRACE_DAYS", DEFAULT_RETIRE_GRACE_DAYS)
            ),
            health_host=(env.get("CCTV_HEALTH_HOST") or "0.0.0.0").strip(),
            health_port=int(number("CCTV_CLEANUP_HEALTH_PORT", DEFAULT_HEALTH_PORT)),
            scheduler_tick_s=number("CCTV_CLEANUP_TICK_S", DEFAULT_SCHEDULER_TICK_S),
            clips_overdue_warn=int(number("CCTV_CLEANUP_OVERDUE_WARN", DEFAULT_CLIPS_OVERDUE_WARN)),
            clips_overdue_critical=int(
                number("CCTV_CLEANUP_OVERDUE_CRITICAL", DEFAULT_CLIPS_OVERDUE_CRITICAL)
            ),
            overdue_age_critical_days=int(
                number(
                    "CCTV_CLEANUP_OVERDUE_AGE_CRITICAL_DAYS",
                    DEFAULT_OVERDUE_AGE_CRITICAL_DAYS,
                )
            ),
            disk_warn_pct=number("CCTV_HEALTH_DISK_WARN_PCT", DEFAULT_DISK_WARN_PCT),
            disk_critical_pct=number("CCTV_HEALTH_DISK_CRITICAL_PCT", DEFAULT_DISK_CRITICAL_PCT),
            delete_concurrency=int(
                number("CCTV_CLEANUP_DELETE_CONCURRENCY", DEFAULT_DELETE_CONCURRENCY)
            ),
            cloud_delete_failure_critical=int(
                number(
                    "CCTV_CLEANUP_CLOUD_DELETE_CRITICAL",
                    DEFAULT_CLOUD_DELETE_FAILURE_CRITICAL,
                )
            ),
            reconcile_interval_s=number(
                "CCTV_CLEANUP_RECONCILE_INTERVAL_S", DEFAULT_RECONCILE_INTERVAL_S
            ),
            reconcile_max_objects=int(
                number("CCTV_CLEANUP_RECONCILE_MAX_OBJECTS", DEFAULT_RECONCILE_MAX_OBJECTS)
            ),
            housekeep_interval_s=number(
                "CCTV_CLEANUP_HOUSEKEEP_INTERVAL_S", DEFAULT_HOUSEKEEP_INTERVAL_S
            ),
            keep_db_backups=int(number("CCTV_CLEANUP_KEEP_DB_BACKUPS", DEFAULT_KEEP_DB_BACKUPS)),
            db_backup_dir=(
                Path(env["CCTV_DB_BACKUP_DIR"]) if env.get("CCTV_DB_BACKUP_DIR") else None
            ),
            log_level=env.get("CCTV_LOG_LEVEL") or None,
        )


def resolve_within_clips_dir(clips_dir: Path, relpath: str) -> Path:
    """Resolve a clip's path and refuse anything outside `clips/` (guard 4).

    The path stored in the database is relative to the data directory — `clips/<camera>/…` —
    so it is joined to the clips directory's parent, then resolved. Resolution follows
    symlinks, so a symlink pointing outside the tree is refused too, which a purely lexical
    check would miss.

    Raises:
        CleanupPathError: the path is empty, absolute, or resolves outside `clips/`.
    """
    if not relpath or not str(relpath).strip():
        raise CleanupPathError("A clip row has an empty path.")

    candidate = Path(relpath)
    if candidate.is_absolute():
        raise CleanupPathError(
            f"Clip path {relpath!r} is absolute. Paths are stored relative to the data "
            f"directory so a restore to a different mount point still works."
        )

    root = clips_dir.resolve()
    resolved = (clips_dir.parent / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise CleanupPathError(
            f"Clip path {relpath!r} resolves to {resolved}, which is outside {root}. "
            f"Nothing was deleted."
        )
    return resolved


@dataclass(frozen=True, slots=True)
class RetentionRun:
    """What one J1 pass did, or would have done under `DRY_RUN`."""

    cameras: int = 0
    considered: int = 0
    deleted: int = 0
    missing: int = 0
    refused: int = 0
    would_delete: int = 0
    bytes_freed: int = 0
    dry_run: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0
    #: The cloud pass that followed this one, or `None` when it did not run.
    cloud: CloudRun | None = None
    #: The reconciliation pass, when one was due in this cycle.
    reconcile: ReconcileRun | None = None
    #: The housekeeping pass, when one was due in this cycle.
    housekeep: HousekeepRun | None = None

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


class LocalRetention:
    """J1: delete local clip files past `local_days`, and only when it is safe to.

    Instantiated once and called from one place, because cleanup is single-instance by
    construction (LLD 05 §8).
    """

    def __init__(
        self,
        *,
        settings: CleanupSettings,
        logger: Any | None = None,
        clock: Any = timeutil.now,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._logger = logger

    # -- retention windows ---------------------------------------------------------------

    def cutoff_for(
        self, camera: str, *, config: CamerasConfig, retired: bool, now: Instant
    ) -> Instant:
        """The oldest `start_time` that survives, for one camera.

        A camera that is still in `cameras.yml` uses its configured `local_days`. A **retired**
        camera — removed from the config — has no window of its own, so it falls back to the
        default plus `retire_grace_days`. That is what stops a camera's footage from becoming
        immortal by virtue of the camera no longer being configured ([INV-13]).

        Note that this depends only on the camera's *configuration*, never on whether it is
        currently `enabled`: a camera switched off for the season keeps its own retention.
        """
        configured = config.cameras.get(camera)
        if configured is not None:
            days = float(configured.retention.local_days)
        else:
            grace = self._settings.retire_grace_days if retired else 0
            days = float(DEFAULT_LOCAL_DAYS + grace)
        return now - days * SECONDS_PER_DAY

    # -- the run -------------------------------------------------------------------------

    def run_once(self, conn: Any, config: CamerasConfig) -> RetentionRun:
        """One J1 pass over every known camera, in batches, committing per batch."""
        started = self._clock()
        totals = _Counters()
        cameras = db.known_cameras(conn)

        for row in cameras:
            camera = str(row["name"])
            retired = row["retired_at"] is not None
            cutoff = self.cutoff_for(camera, config=config, retired=retired, now=started)
            self._expire_camera(conn, camera=camera, cutoff=cutoff, totals=totals)

        finished = self._clock()
        run = RetentionRun(
            cameras=len(cameras),
            considered=totals.considered,
            deleted=totals.deleted,
            missing=totals.missing,
            refused=totals.refused,
            would_delete=totals.dry_run_deleted,
            bytes_freed=totals.bytes_freed,
            dry_run=self._settings.dry_run,
            started_at=started,
            finished_at=finished,
        )
        # No summary line here: `CleanupService` logs one per pass whether or not anything
        # happened, which is a heartbeat as well as a report. Logging here too would double
        # every line on the runs that matter.
        return run

    def _expire_camera(self, conn: Any, *, camera: str, cutoff: Instant, totals: _Counters) -> None:
        cursor: tuple[float, int] | None = None
        while True:
            batch = db.local_expiry_batch(
                conn,
                camera=camera,
                cutoff=cutoff,
                limit=self._settings.batch_size,
                after=cursor,
            )
            if not batch:
                return
            totals.considered += len(batch)
            # One transaction per batch, not per clip and not for the whole run: bounded
            # rollback, bounded lock, observable progress. The asymmetry is deliberate — it is
            # always safer for the database to under-report a deletion (the file is gone, the
            # row still says it exists) than to over-report one. J3 finds the former.
            with db.write_txn(conn):
                for clip in batch:
                    self._expire_one(conn, clip, totals=totals)
            # Advance the cursor past this page. Deliberately **not** "the set shrank": a
            # refused clip, or every clip at all under DRY_RUN, keeps its row eligible, so a
            # shrink-based loop would return this page forever. See `db.local_expiry_batch`.
            last = batch[-1]
            cursor = (float(last["start_time"]), int(last["id"]))

    def _expire_one(self, conn: Any, clip: Any, *, totals: _Counters) -> None:
        clip_id = int(clip["id"])
        now = self._clock()

        # Guard 2: re-read and re-check inside the write transaction. The page query ran
        # outside it, so an uploader could have moved the clip since.
        row = conn.execute(
            "SELECT id, upload_state, cloud_key, uploaded_at, path, sidecar_path, size_bytes "
            "FROM clips WHERE id = ?",
            (clip_id,),
        ).fetchone()
        if row is None:
            return
        state = str(row["upload_state"])
        if state not in db.LOCAL_EXPIRY_STATES:
            self._log(
                "CLEANUP_CLIP_NO_LONGER_DELETABLE",
                clip_id=clip_id,
                state=state,
            )
            return

        # Guard 3: `UPLOADED` means "an object exists at the deterministic key". Without both
        # of these the row is claiming a backup we cannot point at, so the file stays.
        if state == "UPLOADED" and (not row["cloud_key"] or row["uploaded_at"] is None):
            totals.refused += 1
            self._log(
                "CLEANUP_REFUSED_UNPROVABLE_BACKUP",
                clip_id=clip_id,
                state=state,
                reason="UPLOADED without a cloud_key and uploaded_at",
            )
            return

        try:
            target = resolve_within_clips_dir(self._settings.clips_dir, str(row["path"]))
        except CleanupPathError as exc:
            # Guard 4. A poisoned path is a serious finding, not a crash: refuse this clip,
            # report it, and keep the rest of the run moving.
            totals.refused += 1
            self._log(
                "CLEANUP_REFUSED_PATH_ESCAPE",
                clip_id=clip_id,
                path=str(row["path"]),
                reason=str(exc),
            )
            return

        if not target.exists():
            # Already gone — a crash between unlink and commit, or a manual removal. Record it
            # and move on; this is not an error, it is the desired state being reached.
            totals.missing += 1
            db.mark_local_deleted(conn, clip_id=clip_id, now=now)
            db.record_finding(
                conn,
                kind="missing_local_file",
                subject=str(row["path"]),
                detail={"clip_id": clip_id, "camera": str(clip["camera"])},
                now=now,
            )
            return

        size = int(row["size_bytes"] or 0)

        if self._settings.dry_run:
            # Guard 5. Log the intention with everything an operator needs to judge it, and
            # change nothing — not even the row.
            totals.dry_run_deleted += 1
            self._log(
                "CLEANUP_WOULD_DELETE",
                clip_id=clip_id,
                path=str(row["path"]),
                state=state,
                count=size,
            )
            return

        # Order matters (§3.2): clip, then sidecar, then the row. A crash after the clip
        # leaves an orphan sidecar that J4 reaps; a crash after the sidecar leaves a row
        # pointing at nothing, which J3 reports. The reverse order would leave a clip with no
        # metadata, and the sidecar is what makes a future recovery possible.
        _unlink(target)
        sidecar = self._sidecar_path(row, target=target)
        if sidecar.exists():
            _unlink(sidecar)

        db.mark_local_deleted(conn, clip_id=clip_id, now=now)
        totals.deleted += 1
        totals.bytes_freed += size
        self._log(
            "CLIP_LOCAL_EXPIRED",
            clip_id=clip_id,
            camera=str(clip["camera"]),
            path=str(row["path"]),
            state=state,
            count=size,
        )

    def _sidecar_path(self, row: Any, *, target: Path) -> Path:
        """The sidecar to remove alongside the clip.

        The recorded `sidecar_path` is preferred; otherwise it is derived with the one path
        function ([INV-5](../../../internal/architecture/invariants.md)) rather than by string
        surgery, so the two can never disagree about where a sidecar lives. Deriving it from
        the already-resolved target means it cannot escape `clips/` either.
        """
        recorded = row["sidecar_path"]
        if recorded:
            try:
                return resolve_within_clips_dir(self._settings.clips_dir, str(recorded))
            except CleanupPathError:
                # A poisoned sidecar path is not worth failing the clip over: the clip is the
                # only irreplaceable artefact, and an orphan sidecar is reaped by J4.
                pass
        sibling = paths.sidecar_relpath(PurePosixPath(target.name))
        return target.with_name(sibling.name)

    def _log(self, message: str, **extra: Any) -> None:
        if self._logger is None:
            return
        self._logger.info(message, extra=extra)


@dataclass
class CloudRun:
    """What one J2 pass did, or would have done under `DRY_RUN`."""

    cameras: int = 0
    considered: int = 0
    deleted: int = 0
    failed: int = 0
    would_delete: int = 0
    bytes_freed: int = 0
    dry_run: bool = False
    disabled: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


@dataclass(frozen=True, slots=True)
class HousekeepRun:
    """What one J4 pass did, or would have done under `DRY_RUN`.

    Every count is separate rather than one total, because the numbers answer different
    questions: a climbing `empty_dirs_removed` is normal, a climbing `tmp_files_removed` means
    fetches are dying mid-write, and `rows_pruned` is the only signal that the database is being
    kept bounded.
    """

    empty_dirs_removed: int = 0
    bad_files_removed: int = 0
    tmp_files_removed: int = 0
    db_backups_removed: int = 0
    rows_pruned: Mapping[str, int] = field(default_factory=dict)
    bytes_freed: int = 0
    refused: int = 0
    dry_run: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_pruned.values())

    def summary(self) -> str:
        parts = [
            f"{self.empty_dirs_removed} empty dirs",
            f"{self.tmp_files_removed} stale tmp",
        ]
        if self.bad_files_removed:
            parts.append(f"{self.bad_files_removed} .bad files")
        if self.db_backups_removed:
            parts.append(f"{self.db_backups_removed} old db backups")
        if self.rows_pruned:
            rows = ", ".join(
                f"{table}={count}" for table, count in sorted(self.rows_pruned.items())
            )
            parts.append(f"rows pruned: {rows}")
        return "; ".join(parts)


class Housekeeper:
    """J4: keep the filesystem tidy and the database bounded (`lld/05` §6).

    The rules were chosen so that nothing here can lose a clip, which is why the list is short
    and full of age gates:

    * **Never touched**: anything under `media/` or `frigate/`, and any `.mp4` or `.json` under
      `clips/`. A clip's file and its sidecar are retention's business, not housekeeping's.
    * **Age-gated**: a `.tmp` may belong to a fetch happening right now, so it has to be old
      before it is stale. `*.bad` files are diagnostic evidence and keep their value for a week.
    * **Bounded, not emptied**: the newest N database backups survive, so rotation can never
      leave zero.

    It runs on its own daily cadence rather than with J1's hourly one: emptying a directory or
    prunning a week of health history is not worth an hourly walk of the tree on a machine whose
    disks are the scarce resource.
    """

    def __init__(
        self,
        *,
        settings: CleanupSettings,
        logger: Any | None = None,
        clock: Any = timeutil.now,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._logger = logger

    def run_once(self, conn: Any) -> HousekeepRun:
        started = self._clock()
        dry_run = self._settings.dry_run
        rows: dict[str, int] = {}

        def prune(name: str, function: Any, *, days: float) -> None:
            count = 0 if dry_run else function(conn, before=started - days * SECONDS_PER_DAY)
            if count:
                rows[name] = count

        prune("health_history", db.prune_health_history, days=HEALTH_HISTORY_DAYS)
        prune("upload_attempts", db.prune_upload_attempts, days=UPLOAD_ATTEMPT_DAYS)
        prune("system_events", db.prune_system_events, days=SYSTEM_EVENT_DAYS)
        prune("pinned_events", db.prune_settled_pins, days=PIN_DAYS)

        bad, bad_bytes = self._remove_files(
            suffix=".bad", min_age_s=BAD_FILE_AGE_S, now=started, dry_run=dry_run
        )
        tmp, tmp_bytes = self._reap_tmp(now=started, dry_run=dry_run)
        dirs = self._remove_empty_dirs(dry_run=dry_run)
        backups, backup_bytes = self._rotate_db_backups(dry_run=dry_run)

        return HousekeepRun(
            empty_dirs_removed=dirs,
            bad_files_removed=bad,
            tmp_files_removed=tmp,
            db_backups_removed=backups,
            rows_pruned=rows,
            bytes_freed=bad_bytes + tmp_bytes + backup_bytes,
            dry_run=dry_run,
            started_at=started,
            finished_at=self._clock(),
        )

    # -- the filesystem ------------------------------------------------------------------

    def _reap_tmp(self, *, now: Instant, dry_run: bool) -> tuple[int, int]:
        """Stale scratch files under `clips/`. Belt-and-braces with the worker's own reaper."""
        clips_dir = self._settings.clips_dir
        if not clips_dir.is_dir():
            return 0, 0
        if dry_run:
            # Counting without deleting, so a rehearsal reports the same shape as a real run.
            stale = [
                path
                for path in clips_dir.rglob(f"*{atomic.TMP_MARKER}-*")
                if path.is_file() and (now - path.stat().st_mtime) >= TMP_AGE_S
            ]
            return len(stale), sum(_size_of(path) for path in stale)
        removed = atomic.reap_tmp(clips_dir, max_age_s=TMP_AGE_S, now=now)
        return len(removed), 0

    def _remove_files(
        self, *, suffix: str, min_age_s: float, now: Instant, dry_run: bool
    ) -> tuple[int, int]:
        """Delete old files with this suffix under `clips/`, and report the bytes.

        Only ever used for `*.bad`. A `.mp4` and a `.json` are deliberately not expressible
        here: the function takes a suffix it was called with, and the two callers that matter
        pass nothing a clip could be named.
        """
        clips_dir = self._settings.clips_dir
        if not clips_dir.is_dir() or suffix in {".mp4", ".json"}:
            return 0, 0
        removed = 0
        freed = 0
        for path in clips_dir.rglob(f"*{suffix}"):
            if not path.is_file():
                continue
            try:
                if (now - path.stat().st_mtime) < min_age_s:
                    continue
            except OSError:
                continue
            size = _size_of(path)
            if dry_run:
                removed += 1
                freed += size
                continue
            if atomic.discard(path):
                removed += 1
                freed += size
        return removed, freed

    def _remove_empty_dirs(self, *, dry_run: bool) -> int:
        """`clips/<camera>/YYYY/MM/DD/` and upward, deepest first.

        Deepest first, so a day directory that empties its month does not need a second pass to
        take the month with it. `clips/` itself is never a candidate: it is the mount point for
        a volume, and removing it would turn a tidy-up into an outage.
        """
        clips_dir = self._settings.clips_dir
        if not clips_dir.is_dir():
            return 0
        directories = sorted(
            (path for path in clips_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        removed = 0
        for directory in directories:
            try:
                if any(directory.iterdir()):
                    continue
            except OSError:
                continue
            if dry_run:
                removed += 1
                continue
            try:
                directory.rmdir()
            except OSError:
                # Something appeared between the check and the call. Not an error.
                continue
            removed += 1
        return removed

    def _rotate_db_backups(self, *, dry_run: bool) -> tuple[int, int]:
        """Keep the newest `keep_db_backups` files in `db-backups/`.

        Bounded rather than emptied: a rotation that could reach zero would delete the very
        artefact `./update.sh --rollback` depends on.
        """
        directory = self._settings.db_backups_dir
        keep = max(1, self._settings.keep_db_backups)
        if not directory.is_dir():
            return 0, 0
        backups = sorted(
            (path for path in directory.glob("*.db") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        expired = backups[keep:]
        if dry_run:
            return len(expired), sum(_size_of(path) for path in expired)
        removed = 0
        freed = 0
        for path in expired:
            size = _size_of(path)
            if atomic.discard(path):
                removed += 1
                freed += size
        return removed, freed


class CloudRetention:
    """J2: delete cloud objects past `cloud_days`, and only when the provider confirms it.

    Three behaviours here are the whole point of the class:

    * the cutoff is measured from **`uploaded_at`**, not `start_time` (`lld/05` §4.3);
    * a `DELETE` the provider accepts is not a deletion until a read-back says the object is
      gone — otherwise the bytes are merely hidden and still billed (§4.1);
    * a clip whose upload never succeeded is never considered, because the cloud copy would be
      the only copy ([INV-1](../../../internal/architecture/invariants.md)).
    """

    def __init__(
        self,
        *,
        settings: CleanupSettings,
        adapter: storage.StorageAdapter | None,
        location: str,
        logger: Any | None = None,
        clock: Any = timeutil.now,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._location = location
        self._clock = clock
        self._logger = logger
        #: Consecutive failures, for the health check. Reset by any successful deletion.
        self.consecutive_failures = 0

    @property
    def enabled(self) -> bool:
        return self._adapter is not None

    def cutoff_for(
        self, camera: str, *, config: CamerasConfig, retired: bool, now: Instant
    ) -> Instant:
        """The oldest `uploaded_at` that survives, for one camera.

        A retired camera falls back to the default window plus the retire grace, exactly as
        local retention does: footage does not become immortal because its camera left the
        config.
        """
        configured = config.cameras.get(camera)
        if configured is not None:
            days = float(configured.retention.cloud_days)
        else:
            grace = self._settings.retire_grace_days if retired else 0
            days = float(DEFAULT_CLOUD_DAYS + grace)
        return now - days * SECONDS_PER_DAY

    # -- the run -------------------------------------------------------------------------

    def run_once(self, conn: Any, config: CamerasConfig) -> CloudRun:
        """One J2 pass over every known camera, deleting in batches."""
        started = self._clock()
        if self._adapter is None:
            # DISABLED, not "clean": no bucket means nothing was ever uploaded, so there is
            # nothing to expire and nothing to claim.
            return CloudRun(disabled=True, started_at=started, finished_at=started)

        cameras = db.known_cameras(conn)
        run = CloudRun(cameras=len(cameras), dry_run=self._settings.dry_run, started_at=started)
        for row in cameras:
            camera = str(row["name"])
            retired = row["retired_at"] is not None
            cutoff = self.cutoff_for(camera, config=config, retired=retired, now=started)
            self._expire_camera(conn, camera=camera, cutoff=cutoff, run=run)
        run.finished_at = self._clock()
        return run

    def _expire_camera(self, conn: Any, *, camera: str, cutoff: Instant, run: CloudRun) -> None:
        cursor: tuple[float, int] | None = None
        while True:
            batch = db.cloud_expiry_batch(
                conn,
                camera=camera,
                cutoff=cutoff,
                limit=self._settings.batch_size,
                after=cursor,
            )
            if not batch:
                return
            run.considered += len(batch)
            outcomes = self._delete_all(batch)
            with db.write_txn(conn):
                for clip, outcome in zip(batch, outcomes, strict=True):
                    self._record(conn, clip, outcome, run=run, camera=camera)
            last = batch[-1]
            cursor = (float(last["uploaded_at"]), int(last["id"]))

    def _delete_all(self, batch: list[Any]) -> list[Any]:
        """Delete a batch, four at a time, and never raise: an outcome per clip."""
        assert self._adapter is not None
        results: list[Any] = []
        with ThreadPoolExecutor(max_workers=self._settings.delete_concurrency) as pool:
            futures = [pool.submit(self._delete_one, clip) for clip in batch]
            for future in futures:
                results.append(future.result())
        return results

    def _delete_one(self, clip: Any) -> Any:
        """`DeleteResult`, or the `StorageError` that stopped it."""
        assert self._adapter is not None
        key = str(clip["cloud_key"] or "")
        if not key:
            # Derive rather than skip: INV-5 makes the key a function of the path, and a row
            # without one is a bug worth surviving rather than skipping.
            key = paths.cloud_key_for_clip_path(self._location, str(clip["path"]))
            self._log("CLOUD_KEY_DERIVED", clip_id=int(clip["id"]), path=key)
        if self._settings.dry_run:
            return storage.DeleteResult(key=key, deleted=True)
        try:
            return self._adapter.delete_object(key=key)
        except storage.StorageError as exc:
            return exc

    def _record(self, conn: Any, clip: Any, outcome: Any, *, run: CloudRun, camera: str) -> None:
        clip_id = int(clip["id"])
        now = self._clock()

        # Guard: re-read inside the transaction. The page query ran outside it, so the row may
        # have changed — and deleting a cloud object for a clip that is no longer UPLOADED
        # would be acting on a stale decision.
        row = conn.execute(
            "SELECT upload_state, cloud_deleted_at, size_bytes FROM clips WHERE id = ?",
            (clip_id,),
        ).fetchone()
        if row is None or str(row["upload_state"]) not in db.CLOUD_EXPIRY_STATES:
            return
        if row["cloud_deleted_at"] is not None:
            return

        key = getattr(outcome, "key", "") or str(clip["cloud_key"] or "")

        if isinstance(outcome, storage.DeleteResult) and outcome.deleted:
            if self._settings.dry_run:
                run.would_delete += 1
                return
            db.mark_cloud_deleted(conn, clip_id=clip_id, now=now)
            run.deleted += 1
            run.bytes_freed += int(row["size_bytes"] or 0)
            self.consecutive_failures = 0
            self._log("CLIP_CLOUD_EXPIRED", clip_id=clip_id, camera=camera, path=key)
            return

        # Either the provider refused, or it accepted and left the object there. Both mean the
        # object is still billed, so the state stays UPLOADED and the next run tries again.
        detail = (
            str(outcome)
            if isinstance(outcome, Exception)
            else (getattr(outcome, "detail", None) or "the object is still present")
        )
        run.failed += 1
        self.consecutive_failures += 1
        db.record_finding(
            conn,
            kind="cloud_delete_failed",
            subject=key,
            detail=detail[:500],
            now=now,
        )
        if self._logger is not None:
            self._logger.warning(
                "cloud deletion failed; the object is still billed",
                extra={
                    "clip_id": clip_id,
                    "camera": camera,
                    "path": key,
                    "reason": detail,
                },
            )

    def _log(self, message: str, **extra: Any) -> None:
        if self._logger is None:
            return
        self._logger.info(message, extra=extra)


class _Counters:
    """Mutable tallies for one run. A dataclass would only add ceremony."""

    __slots__ = (
        "bytes_freed",
        "considered",
        "deleted",
        "dry_run_deleted",
        "missing",
        "refused",
    )

    def __init__(self) -> None:
        self.considered = 0
        self.deleted = 0
        self.missing = 0
        self.refused = 0
        self.bytes_freed = 0
        self.dry_run_deleted = 0


def _size_of(path: Path) -> int:
    """A file's size, or 0 if it vanished. Only used for reporting bytes freed."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink(path: Path) -> None:
    """Remove a file, tolerating a race with another deleter.

    Two cleanups should never run at once (LLD 05 §8), and J1 already checked `exists()` —
    but a missing file at this point still means the desired state holds, so it is not an
    error worth failing a batch over.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return


# ---------------------------------------------------------------------------------------
# Health (INV-6, lld/05 §9)
# ---------------------------------------------------------------------------------------


class CleanupHealth:
    """The component's answer to "are you actually working?".

    [INV-6](../../../internal/architecture/invariants.md) requires every component to expose a
    health state, and this is the surface that makes J1's trade honest: the clips being
    *held* are reported with their count, their size and their age, so "retention is running"
    can never quietly mean "retention is running and the disk is filling" (LLD 05 §7).

    Every non-healthy check names a code from `health.CODES`. A check that would need a new
    code is a check worth discussing rather than inventing a string for.
    """

    def __init__(
        self,
        *,
        settings: CleanupSettings,
        config: CamerasConfig,
        cloud: CloudRetention | None = None,
        started_at: Instant | None = None,
        clock: Any = timeutil.now,
    ) -> None:
        self._settings = settings
        self._config = config
        self._cloud = cloud
        self._clock = clock
        self._started_at = self._clock() if started_at is None else started_at
        # Built here rather than injected, so the retention windows this reports on cannot
        # come from a different set of settings than the ones it was configured with. Two
        # objects that *should* agree but are passed separately eventually do not.
        self._retention = LocalRetention(settings=settings, clock=clock)

    # -- the report ---------------------------------------------------------------------

    def report(self) -> health.HealthReport:
        checks = (
            self._state_db_check(),
            self._clips_dir_check(),
            self._last_run_check(),
            self._overdue_check(),
            self._disk_check(),
            self._storage_check(),
            self._reconciliation_check(),
        )
        return health.HealthReport(
            component=COMPONENT,
            checks=checks,
            summary=self._summary(checks),
            since=self._started_at,
            uptime_s=max(0.0, self._clock() - self._started_at),
        )

    def _readonly(self) -> Any:
        return db.open_db(self._settings.db_path, readonly=True)

    def _state_db_check(self) -> health.Check:
        try:
            conn = self._readonly()
            try:
                conn.execute("SELECT COUNT(*) FROM clips").fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, db.DbError, OSError) as exc:
            return health.Check(
                name="state_db",
                state=health.UNHEALTHY,
                code="DB_UNAVAILABLE",
                detail=f"cannot read the state store: {exc}",
            )
        return health.Check(name="state_db", state=health.HEALTHY)

    def _clips_dir_check(self) -> health.Check:
        clips = self._settings.clips_dir
        if not clips.is_dir():
            return health.Check(
                name="clips_dir",
                state=health.UNHEALTHY,
                code="DISK_CRITICAL",
                detail=f"{clips} does not exist, so nothing can be expired",
            )
        if not os.access(clips, os.W_OK):
            return health.Check(
                name="clips_dir",
                state=health.UNHEALTHY,
                code="DISK_CRITICAL",
                detail=f"{clips} is not writable, so retention cannot delete anything",
            )
        return health.Check(name="clips_dir", state=health.HEALTHY)

    def _last_run_check(self) -> health.Check:
        interval = self._settings.run_interval_s
        try:
            conn = self._readonly()
            try:
                row = db.get_watermark(conn, LAST_RUN_NAME)
            finally:
                conn.close()
        except (sqlite3.Error, db.DbError, OSError) as exc:
            return health.Check(
                name="last_run",
                state=health.UNKNOWN,
                code="CLEANUP_STALE",
                detail=f"cannot read when cleanup last ran: {exc}",
            )

        if row is None or row["last_success_at"] is None:
            # A service that has never completed a run is not healthy, however recently it
            # started: INV-6 forbids "no information" rendering as good news.
            return health.Check(
                name="last_run",
                state=health.DEGRADED,
                code="CLEANUP_STALE",
                detail="cleanup has not completed a run yet",
            )

        age = max(0.0, self._clock() - float(row["last_success_at"]))
        detail = f"last successful run {_human(age)} ago"
        if age < 2 * interval:
            return health.Check(name="last_run", state=health.HEALTHY, detail=detail)
        if age < 6 * interval:
            return health.Check(
                name="last_run",
                state=health.DEGRADED,
                code="CLEANUP_STALE",
                detail=detail,
            )
        failures = int(row["consecutive_failures"] or 0)
        return health.Check(
            name="last_run",
            state=health.UNHEALTHY,
            code="CLEANUP_STALE",
            detail=f"{detail}; {failures} consecutive failure(s)",
        )

    def overdue(self) -> db.OverdueSummary:
        """Every camera's held clips, summed. Reported per scrape rather than stored, because
        a count is cheap and an indexed query cannot go stale."""
        count = 0
        held = 0
        oldest_start: Instant | None = None
        oldest_overdue: float | None = None
        try:
            conn = self._readonly()
            try:
                now = self._clock()
                for camera in db.known_cameras(conn):
                    name = str(camera["name"])
                    cutoff = self._retention.cutoff_for(
                        name,
                        config=self._config,
                        retired=camera["retired_at"] is not None,
                        now=now,
                    )
                    summary = db.overdue_local(conn, camera=name, cutoff=cutoff)
                    count += summary.count
                    held += summary.bytes_held
                    if summary.oldest_start_time is not None and (
                        oldest_start is None or summary.oldest_start_time < oldest_start
                    ):
                        oldest_start = summary.oldest_start_time
                    if summary.oldest_overdue_s is not None and (
                        oldest_overdue is None or summary.oldest_overdue_s > oldest_overdue
                    ):
                        oldest_overdue = summary.oldest_overdue_s
            finally:
                conn.close()
        except (sqlite3.Error, db.DbError, OSError):
            return db.OverdueSummary()
        return db.OverdueSummary(
            count=count,
            bytes_held=held,
            oldest_start_time=oldest_start,
            oldest_overdue_s=oldest_overdue,
        )

    def _overdue_check(self) -> health.Check:
        summary = self.overdue()
        detail = f"{summary.count} clip(s) held, {_human_bytes(summary.bytes_held)}"
        if summary.oldest_overdue_s is not None:
            detail = f"{detail}, oldest overdue by {_human(summary.oldest_overdue_s)}"

        if summary.count == 0:
            return health.Check(name="clips_overdue_retention", state=health.HEALTHY, detail=detail)

        too_old = summary.oldest_overdue_s is not None and summary.oldest_overdue_s > (
            self._settings.overdue_age_critical_days * SECONDS_PER_DAY
        )
        return health.Check(
            name="clips_overdue_retention",
            state=(
                health.UNHEALTHY
                if summary.count > self._settings.clips_overdue_critical or too_old
                else health.DEGRADED
            ),
            code="CLIPS_OVERDUE_RETENTION",
            detail=detail,
        )

    def _disk_check(self) -> health.Check:
        try:
            usage = shutil.disk_usage(self._settings.data_dir)
        except OSError as exc:
            return health.Check(
                name="disk_free",
                state=health.UNKNOWN,
                code="DISK_PRESSURE",
                detail=f"cannot measure free space: {exc}",
            )
        if usage.total <= 0:
            return health.Check(
                name="disk_free",
                state=health.UNKNOWN,
                code="DISK_PRESSURE",
                detail="the filesystem reports no size",
            )

        free_pct = usage.free / usage.total * 100.0
        detail = f"{free_pct:.1f}% free ({_human_bytes(usage.free)} of {_human_bytes(usage.total)})"
        if free_pct < self._settings.disk_critical_pct:
            return health.Check(
                name="disk_free",
                state=health.UNHEALTHY,
                code="DISK_CRITICAL",
                detail=detail,
            )
        if free_pct < self._settings.disk_warn_pct:
            return health.Check(
                name="disk_free",
                state=health.DEGRADED,
                code="DISK_PRESSURE",
                detail=detail,
            )
        return health.Check(name="disk_free", state=health.HEALTHY, detail=detail)

    def _storage_check(self) -> health.Check:
        """Whether cloud retention can run, and whether it is actually deleting.

        Three states, deliberately distinct:

        * no bucket configured → `DISABLED`, because that is a supported choice and not a fault;
        * bucket configured but deletions failing → `UNHEALTHY`, because every failed deletion
          is an object still being billed;
        * otherwise `HEALTHY`.
        """
        if self._cloud is None or not self._cloud.enabled:
            return health.Check(
                name="cloud_retention",
                state=health.DISABLED,
                detail="no bucket is configured, so there is nothing to expire",
            )
        failures = self._cloud.consecutive_failures
        if failures >= self._settings.cloud_delete_failure_critical:
            return health.Check(
                name="cloud_retention",
                state=health.UNHEALTHY,
                code="CLOUD_DELETE_FAILING",
                detail=(
                    f"{failures} consecutive cloud deletions failed; those objects are still "
                    f"being billed. See the cloud_delete_failed findings."
                ),
            )
        if failures:
            return health.Check(
                name="cloud_retention",
                state=health.DEGRADED,
                code="CLOUD_DELETE_FAILING",
                detail=f"{failures} cloud deletion(s) failed; retried on the next pass",
            )
        return health.Check(
            name="cloud_retention",
            state=health.HEALTHY,
            detail="deleting objects past retention and confirming each deletion",
        )

    def _reconciliation_check(self) -> health.Check:
        """Is the database still telling the truth about the bucket?

        Disabled when reconciliation is off; stale when it has not run for twice its interval;
        unhealthy when there are open findings that mean *we believe something false* — a row
        claiming an object that is not there, a checksum that disagrees, a key that does not match
        its path. Warnings alone are reported, not alarmed on: an orphan object costs money, but a
        dangling row costs footage you thought was backed up.
        """
        interval = self._settings.reconcile_interval_s
        if interval <= 0:
            return health.Check(
                name="reconciliation",
                state=health.DISABLED,
                detail="reconciliation is disabled (CCTV_CLEANUP_RECONCILE_INTERVAL_S=0)",
            )
        try:
            conn = self._readonly()
            try:
                row = db.get_watermark(conn, RECONCILE_RUN_NAME)
                critical = db.open_findings(conn, kinds=CRITICAL_KINDS)
            finally:
                conn.close()
        except (sqlite3.Error, db.DbError, OSError) as exc:
            return health.Check(
                name="reconciliation",
                state=health.UNKNOWN,
                code="DB_UNAVAILABLE",
                detail=f"cannot read the findings: {exc}",
            )

        if critical:
            return health.Check(
                name="reconciliation",
                state=health.UNHEALTHY,
                code="RECONCILE_FINDINGS",
                detail=(
                    f"{critical} unresolved finding(s) where the database and the bucket "
                    f"disagree. See reconciliation_findings."
                ),
            )
        if row is None or row["last_success_at"] is None:
            # `UNKNOWN` still needs a code: alerting rules match on codes, not prose. "Has not
            # run yet" is the strongest form of stale.
            return health.Check(
                name="reconciliation",
                state=health.UNKNOWN,
                code="RECONCILE_STALE",
                detail="has not completed a pass yet",
            )
        age = self._clock() - float(row["last_success_at"])
        if age >= 2 * interval:
            return health.Check(
                name="reconciliation",
                state=health.DEGRADED,
                code="RECONCILE_STALE",
                detail=f"last completed {age / 3600:.0f}h ago; it is set to run daily",
            )
        return health.Check(
            name="reconciliation",
            state=health.HEALTHY,
            detail=f"last completed {age / 3600:.1f}h ago, no critical findings",
        )

    def _summary(self, checks: Sequence[health.Check]) -> str:
        by_name = {check.name: check for check in checks}
        return (
            f"{by_name['clips_overdue_retention'].detail}; "
            f"{by_name['last_run'].detail}; {by_name['disk_free'].detail}"
        )

    def gauges(self) -> dict[str, float | int]:
        summary = self.overdue()
        free_pct = 0.0
        try:
            usage = shutil.disk_usage(self._settings.data_dir)
            if usage.total > 0:
                free_pct = round(usage.free / usage.total * 100.0, 2)
        except OSError:
            free_pct = 0.0
        return {
            "cctv_clips_overdue_retention": summary.count,
            "cctv_clips_overdue_bytes": summary.bytes_held,
            "cctv_disk_free_pct": free_pct,
        }


# ---------------------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------------------


class CleanupService:
    """Runs J1 on its cadence, and records that it did."""

    def __init__(
        self,
        *,
        settings: CleanupSettings,
        config: CamerasConfig,
        adapter: storage.StorageAdapter | None = None,
        clock: Any = timeutil.now,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock
        # Constructed from the same settings the service runs with. Injecting a ready-made
        # `LocalRetention` would let a caller hand in a dry-run setting and a live runner —
        # a combination that deletes files while claiming to be a rehearsal.
        self._retention = LocalRetention(settings=settings, logger=logger, clock=clock)
        self._cloud = CloudRetention(
            settings=settings,
            adapter=adapter,
            location=config.site.location,
            logger=logger,
            clock=clock,
        )
        self._reconciler = Reconciler(
            data_dir=settings.data_dir,
            location=config.site.location,
            adapter=adapter,
            batch_size=settings.batch_size,
            max_objects=settings.reconcile_max_objects,
            concurrency=settings.delete_concurrency,
            logger=logger,
            clock=clock,
        )
        self._housekeeper = Housekeeper(settings=settings, logger=logger, clock=clock)

    @property
    def cloud(self) -> CloudRetention:
        """The J2 pass, exposed for the health surface and for tests."""
        return self._cloud

    def run_once(self) -> RetentionRun:
        """One pass of J1 then J2, on its own connection, with the outcome recorded either way.

        J1 then J2, sequentially, never concurrently (`lld/05` §8): local deletion must not race
        the cloud pass deciding whether the local file still exists.
        """
        conn = db.open_db(self._settings.db_path)
        try:
            run = self._retention.run_once(conn, self._config)
            # `RetentionRun` is frozen; the cloud pass belongs to the same cycle, so the run
            # is rebuilt with it rather than mutated.
            cloud_run = self._cloud.run_once(conn, self._config)
            run = replace(run, cloud=cloud_run)
            if self._reconcile_due(conn, now=cloud_run.finished_at):
                reconcile_run = self._reconciler.run_once(
                    conn, self._config, dry_run=self._settings.dry_run
                )
                run = replace(run, reconcile=reconcile_run)
                if not self._settings.dry_run:
                    db.set_watermark(
                        conn,
                        name=RECONCILE_RUN_NAME,
                        watermark=reconcile_run.finished_at,
                        note=reconcile_run.summary()[:200],
                        now=self._clock(),
                    )
            if self._due(
                conn,
                HOUSEKEEP_RUN_NAME,
                self._settings.housekeep_interval_s,
                now=run.finished_at,
            ):
                housekeep_run = self._housekeeper.run_once(conn)
                run = replace(run, housekeep=housekeep_run)
                if not self._settings.dry_run:
                    db.set_watermark(
                        conn,
                        name=HOUSEKEEP_RUN_NAME,
                        watermark=housekeep_run.finished_at,
                        note=housekeep_run.summary()[:200],
                        now=self._clock(),
                    )
        except Exception as exc:
            # Recorded, then re-raised: the caller decides whether to keep going, and the
            # staleness check must see that a pass *failed* rather than never happened.
            db.set_watermark(
                conn,
                name=LAST_RUN_NAME,
                watermark=self._clock(),
                succeeded=False,
                note=f"{type(exc).__name__}: {exc}"[:200],
                now=self._clock(),
            )
            raise
        cloud = run.cloud
        db.set_watermark(
            conn,
            name=LAST_RUN_NAME,
            watermark=run.finished_at,
            succeeded=True,
            note=(
                f"deleted={run.deleted} missing={run.missing} refused={run.refused} "
                f"would_delete={run.would_delete} "
                f"cloud_deleted={cloud.deleted} cloud_failed={cloud.failed}"
            )[:200],
            now=self._clock(),
        )
        return run

    def _reconcile_due(self, conn: Any, *, now: Instant) -> bool:
        """Daily by default (`lld/05` §5)."""
        return self._due(conn, RECONCILE_RUN_NAME, self._settings.reconcile_interval_s, now=now)

    def _due(self, conn: Any, name: str, interval: float, *, now: Instant) -> bool:
        """Whether a job with this watermark and cadence should run now.

        `0` disables the job entirely. Anything else: due when there is no watermark, or when the
        last *successful* one is older than the interval — which is why a failed pass leaves the
        watermark alone rather than resetting the clock.
        """
        if interval <= 0:
            return False
        row = db.get_watermark(conn, name)
        if row is None or row["last_success_at"] is None:
            return True
        return (now - float(row["last_success_at"])) >= interval

    def run(self, stop: threading.Event) -> None:
        """Pass immediately, then on the interval, until asked to stop.

        Immediately, because a service that waited a full interval before its first pass would
        leave a freshly started install holding expired clips for an hour for no reason.
        """
        while not stop.is_set():
            started = self._clock()
            try:
                run = self.run_once()
            except Exception:
                # The outermost loop of a process meant to run for years: log and keep going.
                # A failed pass leaves the watermark untouched, so nothing is skipped.
                logger.exception("cleanup pass failed; the next one will retry")
            else:
                self._log_run(run)
            self._wait(stop, spent=max(0.0, self._clock() - started))

    def _wait(self, stop: threading.Event, *, spent: float) -> None:
        """Sleep out the rest of the interval, waking promptly on shutdown.

        In small ticks rather than one long wait, so a `SIGTERM` during a quiet hour is acted
        on within a second: a service that will not exit is one Docker eventually `SIGKILL`s.
        """
        remaining = max(0.0, self._settings.run_interval_s - spent)
        tick = max(0.01, self._settings.scheduler_tick_s)
        while remaining > 0 and not stop.is_set():
            nap = min(tick, remaining)
            stop.wait(nap)
            remaining -= nap

    def _log_run(self, run: RetentionRun) -> None:
        logger.info(
            "cleanup pass",
            extra={
                "count": run.deleted + run.missing,
                "duration_s": round(run.duration_s, 3),
                "state": (
                    f"cameras={run.cameras} considered={run.considered} "
                    f"deleted={run.deleted} missing={run.missing} "
                    f"refused={run.refused} would_delete={run.would_delete} "
                    f"freed={run.bytes_freed}"
                ),
            },
        )
        if run.housekeep is not None:
            logger.info(
                "housekeeping pass",
                extra={
                    "count": run.housekeep.total_rows,
                    "duration_s": round(run.housekeep.duration_s, 3),
                    "state": run.housekeep.summary(),
                },
            )


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / SECONDS_PER_DAY:.1f}d"


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _install_signal_handlers(stop: threading.Event) -> None:
    """Turn SIGTERM/SIGINT into a cooperative shutdown.

    Deliberately duplicated from the uploader rather than extracted: a shared module for
    fifteen lines would be indirection without insight, and the two services want different
    things from their loops. A third service needing it is the moment to extract it.
    """

    def request_stop(signum: int, _frame: object) -> None:
        if not stop.is_set():
            logger.info("shutdown requested", extra={"reason": signal.Signals(signum).name})
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)


def main(argv: Sequence[str] | None = None) -> int:
    """LLD 00 §3's service skeleton: configure, validate, serve health, then work."""
    del argv  # every input is an environment variable; see CleanupSettings

    try:
        settings = CleanupSettings.from_environ()
    except ConfigError as exc:
        print(f"cctv-cleanup: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        configure_logging(settings.log_level)
    except ValueError as exc:
        print(f"cctv-cleanup: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    problem = settings.configuration_problem()
    if problem is not None:
        logger.error("configuration is unusable", extra={"reason": problem})
        return EXIT_CONFIG

    if settings.dry_run:
        logger.warning(
            "DRY_RUN is set: every pass reports what it would delete and deletes nothing"
        )

    logger.info(
        "starting",
        extra={
            "host": socket.gethostname(),
            "state": "dry-run" if settings.dry_run else "live",
        },
    )

    try:
        config = load_cameras_config(settings.cameras_path)
    except (ConfigError, OSError) as exc:
        logger.error(
            "configuration is unusable",
            extra={"path": str(settings.cameras_path), "reason": str(exc)},
        )
        return EXIT_CONFIG

    for warning in config.warnings:
        logger.warning(warning)

    conn = None
    try:
        conn = db.open_db(settings.db_path)
        version = db.migrate(conn)
        db.integrity_check(conn)
    except (db.DbError, sqlite3.Error) as exc:
        logger.error(
            "state store is unusable",
            extra={"path": str(settings.db_path), "reason": str(exc)},
        )
        return EXIT_UNAVAILABLE
    finally:
        if conn is not None:
            conn.close()

    logger.info("state store ready", extra={"state": f"schema-{version}"})

    # The adapter is optional on purpose: no bucket is a supported state, and J2 then reports
    # DISABLED rather than pretending there is nothing to expire.
    adapter = _build_adapter()
    service = CleanupService(settings=settings, config=config, adapter=adapter)
    reporter = CleanupHealth(settings=settings, config=config, cloud=service.cloud)

    stop = threading.Event()
    _install_signal_handlers(stop)

    server = None
    if settings.health_port > 0:
        server, _thread = health.start_health_server(
            port=settings.health_port,
            host=settings.health_host,
            snapshot=reporter.report,
            gauges=reporter.gauges,
        )
        logger.info(
            "health endpoint listening",
            extra={"host": settings.health_host, "count": settings.health_port},
        )
    else:
        logger.warning("no health endpoint: CCTV_CLEANUP_HEALTH_PORT is 0")

    try:
        service.run(stop)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        logger.info("stopped")

    return EXIT_OK


def _build_adapter() -> storage.StorageAdapter | None:
    """The storage adapter, or `None` when cloud backup is not configured.

    Imported lazily so that the `storage` extra remains optional for a local-only site: a
    cleanup service with no bucket must start fine on an image without `boto3`.
    """
    from .uploader import UploadSettings, build_adapter

    try:
        settings = UploadSettings.from_environ()
    except ConfigError as exc:
        logger.warning("cloud retention disabled: %s", exc)
        return None
    if not settings.storage_configured:
        logger.info("cloud retention disabled: no bucket is configured")
        return None
    try:
        return build_adapter(settings)
    except storage.StorageBackendMissingError as exc:
        logger.warning("cloud retention disabled: %s", exc)
        return None
