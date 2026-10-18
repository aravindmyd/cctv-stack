"""The clip-worker service — a review becomes a file at the canonical path.

This is the v0.1 happy path and deliberately nothing else. No upload, no retention, no
health surface. That narrowing is safe rather than merely convenient: with nothing deleting
anything, [INV-1](../../../internal/architecture/invariants.md) holds trivially and the two
riskiest failure modes in the design — deleting a clip that was never backed up, and a disk
that fills with no signal — are simply not present yet.

## Two inputs, one worker

```text
frigate/reviews (MQTT)  ──→  ReviewDispatcher  ──┐
                                                 ├──→  decide()  ──→  ClipMaterialiser
GET /api/review (sweep)  ────────────────────────┘
```

Neither alone is sufficient. MQTT alone loses clips whenever the broker or this process is
down, and Frigate closes a review on restart **without publishing `end` at all** — measured,
with the subscriber attached to a broker that never restarted (integration contract §3.5).
The sweep alone would work, but would delay every clip by its interval. So MQTT is the
latency optimisation and **the sweep is the guarantee**.

## Why both run on one thread

They share a `sqlite3` connection and a materialiser, and a connection is not something to
hand between threads and hope. Rather than sprinkle locks or open a second write connection,
the MQTT callback does one thing — `queue.Queue.put_nowait`, which *is* thread-safe — and
**this thread owns every database write**.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import db, timeutil
from .config import CamerasConfig, ConfigError, load_cameras_config
from .dispatch import ReviewDispatcher
from .frigate import FrigateClient
from .logging import configure_logging
from .materialise import ClipMaterialiser, FrigateSource
from .mqtt import MqttUnavailable, ReviewSubscriber
from .reviews import ReviewCandidate, decide
from .sweep import ReviewSweep, SweepResult

__all__ = ["EXIT_CONFIG", "EXIT_UNAVAILABLE", "ClipWorker", "Settings", "main"]

logger = logging.getLogger(__name__)

#: `EX_CONFIG` / `EX_TEMPFAIL` from `sysexits.h`, per LLD 00 §3. The distinction is what
#: lets a supervisor decide whether restarting could help: a bad config file never becomes
#: good, a database that is briefly locked might.
EXIT_CONFIG = 78
EXIT_UNAVAILABLE = 75

DEFAULT_SWEEP_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Every input is an environment variable, so there are no CLI arguments to get wrong."""

    cameras_path: Path
    data_dir: Path
    db_path: Path
    frigate_url: str
    #: `None` means sweep-only. Expressed as "no host configured" rather than
    #: "mqtt_enabled: false" because an empty value and a false value would otherwise be
    #: three states to reason about instead of two.
    mqtt_host: str | None
    mqtt_port: int
    mqtt_topic_prefix: str
    mqtt_username: str | None
    mqtt_password: str | None
    sweep_interval_s: float
    log_level: str | None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        config_dir = Path(env.get("CCTV_CONFIG_DIR", "/config"))
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))
        # An empty string is treated as absent rather than as a host named "" — which is
        # the shape `CCTV_MQTT_HOST=` takes in a `.env` file when someone comments it out
        # with a value still on the line.
        mqtt_host = (env.get("CCTV_MQTT_HOST") or "").strip() or None

        return cls(
            cameras_path=Path(env.get("CCTV_CAMERAS", str(config_dir / "cameras.yml"))),
            data_dir=data_dir,
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            frigate_url=env.get("CCTV_FRIGATE_URL", "http://frigate:5000"),
            mqtt_host=mqtt_host,
            mqtt_port=int(env.get("CCTV_MQTT_PORT", "1883")),
            mqtt_topic_prefix=env.get("CCTV_MQTT_TOPIC_PREFIX", "frigate"),
            mqtt_username=(env.get("CCTV_MQTT_USERNAME") or "").strip() or None,
            mqtt_password=env.get("CCTV_MQTT_PASSWORD") or None,
            sweep_interval_s=float(env.get("CCTV_SWEEP_INTERVAL_S", str(DEFAULT_SWEEP_INTERVAL_S))),
            log_level=env.get("CCTV_LOG_LEVEL") or None,
        )


class ClipWorker:
    """One thread that turns reviews into clips, from two sources."""

    def __init__(
        self,
        *,
        config: CamerasConfig,
        conn: sqlite3.Connection,
        sweep: ReviewSweep,
        materialiser: ClipMaterialiser,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        clock: Callable[[], float] = timeutil.now,
        max_pending: int = 1000,
    ) -> None:
        if sweep_interval_s <= 0:
            raise ValueError("sweep_interval_s must be positive.")

        self.config = config
        self.conn = conn
        self.sweep = sweep
        self.materialiser = materialiser
        self.sweep_interval_s = sweep_interval_s
        self._clock = clock
        self._last_sweep: float | None = None

        # The worker owns the dispatcher so the handler and the queue cannot be wired to
        # different objects. The alternative — building the dispatcher in `main()` and the
        # worker around it — needs the worker to exist first, which is circular.
        self.dispatcher = ReviewDispatcher(handler=self.handle, max_pending=max_pending)

    # -----------------------------------------------------------------------------------
    # The handler. Runs on this thread, never on paho's.
    # -----------------------------------------------------------------------------------

    def handle(self, candidate: ReviewCandidate) -> None:
        """Decide, then materialise, one candidate."""
        camera = self.config.cameras.get(candidate.camera)
        if camera is None or not camera.enabled:
            # An unknown camera is dropped with a warning and no retry loop (M2.7).
            # Retrying would not help: it will not be in `cameras.yml` on the next attempt
            # either, and a retry loop here is one that runs forever.
            logger.warning(
                "review for a camera that is not enabled",
                extra={"camera": candidate.camera, "review_id": candidate.review_id},
            )
            return

        verdict = decide(
            candidate,
            severities=camera.clip.severities,
            objects=camera.clip.objects,
            known_review_ids=db.known_review_ids(self.conn, [candidate.review_id]),
        )
        if not verdict.materialise:
            logger.info(
                "review skipped",
                extra={
                    "camera": candidate.camera,
                    "review_id": candidate.review_id,
                    "reason": verdict.reason,
                },
            )
            return

        outcome = self.materialiser.materialise(
            candidate,
            pre_roll_s=camera.clip.pre_roll_s,
            post_roll_s=camera.clip.post_roll_s,
        )
        logger.info(
            "review handled",
            extra={
                "camera": candidate.camera,
                "review_id": candidate.review_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "path": outcome.path,
                "coverage": outcome.coverage_ratio,
                "attempts": outcome.fetch_attempts,
            },
        )

    # -----------------------------------------------------------------------------------
    # Scheduling
    # -----------------------------------------------------------------------------------

    def sweep_once(self) -> SweepResult | None:
        """One reconciliation pass. Never raises: a failed sweep must not stop the worker."""
        try:
            result = self.sweep.run(config=self.config)
        except Exception as exc:
            # A sweep that raises must not stop the worker: the sweep is the guarantee, and
            # a worker that exits because the guarantee failed guarantees nothing.
            logger.exception("sweep raised", extra={"reason": str(exc)})
            return None

        if result.failure is not None:
            logger.warning("sweep could not complete", extra={"reason": result.failure})
        else:
            logger.info(
                "sweep: %d considered, %d materialised, %d skipped, %d deferred, %d failed",
                result.considered,
                result.materialised,
                result.skipped,
                result.deferred,
                result.failed,
            )
        return result

    def tick(self, *, timeout: float = 1.0) -> None:
        """One unit of work: drain one queued candidate, and sweep when due.

        `work_once` blocks for up to `timeout`, which is what stops this loop from spinning
        when there is nothing to do — so the sweep is checked at most `timeout` late.
        """
        self.dispatcher.work_once(timeout=timeout)

        now = self._clock()
        if self._last_sweep is None or (now - self._last_sweep) >= self.sweep_interval_s:
            # Recorded *before* running, so a sweep slower than the interval does not queue
            # up a second one the moment it returns.
            self._last_sweep = now
            self.sweep_once()

    def run(self, stop: threading.Event, *, timeout: float = 1.0) -> None:
        """Block until `stop` is set.

        A sweep runs immediately, before the loop. Anything that happened while this process
        was down is already late, and MQTT takes a moment to establish — waiting a full
        interval before recovering would mean every restart has a hole in it.
        """
        self.sweep_once()
        self._last_sweep = self._clock()

        while not stop.is_set():
            try:
                self.tick(timeout=timeout)
            except Exception:
                # Deliberately broad and deliberately not fatal. This is the outermost loop
                # of a service that is expected to run for years; the alternative is that an
                # unhandled edge case in one review takes down every camera until someone
                # notices. The traceback is logged, so the bug is not hidden — only survived.
                logger.exception("worker tick failed; continuing")


# ---------------------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------------------


def _install_signal_handlers(stop: threading.Event) -> None:
    """Turn SIGTERM/SIGINT into a cooperative shutdown.

    Cooperative because the alternative is a `KeyboardInterrupt` raised *inside* a fetch,
    which leaves a `.tmp` file and a half-written clip. The reaper would eventually clean it
    up; stopping between units of work means it never happens.
    """

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("shutdown requested", extra={"reason": signal.Signals(signum).name})
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)


def build_worker(settings: Settings, config: CamerasConfig, conn: sqlite3.Connection) -> ClipWorker:
    """Wire the client, source, materialiser and sweep into one worker.

    Separate from `main()` so a test can build the whole graph without a process.
    """
    client = FrigateClient(base_url=settings.frigate_url)
    materialiser = ClipMaterialiser(
        conn=conn,
        source=FrigateSource(client),
        data_dir=settings.data_dir,
        location=config.site.location,
        zone=config.site.timezone,
    )
    sweep = ReviewSweep(conn=conn, client=client, materialiser=materialiser)
    return ClipWorker(
        config=config,
        conn=conn,
        sweep=sweep,
        materialiser=materialiser,
        sweep_interval_s=settings.sweep_interval_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """LLD 00 §3's common service skeleton, minus the parts v0.1 does not have."""
    del argv  # every input is an environment variable; see Settings

    settings = Settings.from_environ()

    try:
        configure_logging(settings.log_level)
    except ValueError as exc:
        print(f"cctv-clip-worker: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    logger.info("starting", extra={"host": socket.gethostname()})

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

    if not config.enabled_cameras():
        logger.error("no cameras are enabled, so nothing can ever be recorded")
        return EXIT_CONFIG

    try:
        conn = db.open_db(settings.db_path)
        version = db.migrate(conn)
    except (db.DbError, sqlite3.Error) as exc:
        logger.error(
            "state store is unusable",
            extra={"path": str(settings.db_path), "reason": str(exc)},
        )
        return EXIT_UNAVAILABLE

    logger.info("state store ready", extra={"state": f"schema-{version}"})

    for name, camera in config.enabled_cameras().items():
        db.ensure_camera(conn, name=name, display_name=camera.display_name, enabled=True)

    worker = build_worker(settings, config, conn)

    stop = threading.Event()
    _install_signal_handlers(stop)

    subscriber: ReviewSubscriber | None = None
    if settings.mqtt_host is not None:
        subscriber = ReviewSubscriber(
            dispatcher=worker.dispatcher,
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            topic_prefix=settings.mqtt_topic_prefix,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
        )
        try:
            subscriber.start()
        except MqttUnavailable as exc:
            logger.error("cannot subscribe to the broker", extra={"reason": str(exc)})
            conn.close()
            return EXIT_UNAVAILABLE
    else:
        logger.warning(
            "CCTV_MQTT_HOST is unset: sweep-only, so every clip waits up to %s s to be noticed",
            settings.sweep_interval_s,
        )

    try:
        worker.run(stop)
    finally:
        if subscriber is not None:
            subscriber.stop()
        conn.close()
        logger.info("stopped")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
