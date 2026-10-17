"""Turning a review item into a canonical clip, on disk and in the database.

This is the composition the rest of Phase 3 was built for. Everything subtle already lives
somewhere tested — the fetch range, the coverage decision, the atomic write, the probe
parsing, the trigger verdict — so this module is mostly *ordering*. The ordering is the
part that matters, though, and three of its rules are load-bearing:

1. **Pin before downloading, unpin after.** [INV-9]. A failed pin is fatal for that review,
   because downloading without a pin risks Frigate expiring the footage underneath us, and
   a missing clip is worse than a late one. Unpinning is best-effort: a stuck pin wastes
   Frigate's disk, it does not lose data, and the reaper collects it later.
2. **A row exists only once the file does.** The database is the record of what is on disk,
   so the insert happens after the rename. The reverse order would let a crash leave a row
   pointing at nothing, and every consumer of that table would believe it.
3. **A short clip is kept, not discarded.** [INV-14]. Attempts are ranked and the best one
   is promoted; retrying improves the outcome and discarding cannot. Each attempt lands on
   its own scratch path for exactly that reason — writing retries straight onto the
   canonical path would let a worse second attempt overwrite a better first one, which is
   the opposite of keeping the best.

**No network or subprocess work happens inside a database transaction.** The claim in
[`cctv.db`][cctv.db] commits before returning and the insert here opens its own short one,
so a slow fetch cannot hold the write lock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from zoneinfo import ZoneInfo

from . import atomic, db
from .clip import (
    DEFAULT_COVERAGE_POLICY,
    DEFAULT_LONG_CLIP_WARN_S,
    DEFAULT_SEGMENT_FLUSH_GRACE_S,
    CoveragePolicy,
    FetchPlan,
    Verdict,
    judge_coverage,
    pick_best,
    plan_fetch,
)
from .ffprobe import ClipFacts, ProbeError, run_probe
from .frigate import FrigateClient, FrigateError, FrigateUnavailable, NoRecordingsYet
from .paths import candidate_relpaths, clip_cloud_key, sidecar_relpath
from .reviews import ReviewCandidate
from .timeutil import Instant
from .timeutil import now as _now

__all__ = [
    "CREATED",
    "DEFERRED",
    "FAILED",
    "SKIPPED",
    "ClipMaterialiser",
    "ClipSource",
    "FrigateSource",
    "MaterialiseOutcome",
    "Prober",
]

CREATED = "created"
SKIPPED = "skipped"
#: Retryable — the caller should requeue this review rather than treat it as lost.
DEFERRED = "deferred"
FAILED = "failed"


class ClipSource(Protocol):
    """Where clips come from. Narrow on purpose, so a fake is three methods."""

    def retain(self, event_id: str) -> None: ...

    def unretain(self, event_id: str) -> None: ...

    def download(
        self,
        camera: str,
        start: Instant,
        end: Instant,
        sink: object,
        *,
        timeout_s: float,
    ) -> int: ...


class Prober(Protocol):
    def __call__(self, path: Path) -> ClipFacts: ...


@dataclass(frozen=True, slots=True)
class FrigateSource:
    """Adapts [`cctv.frigate.FrigateClient`][cctv.frigate.FrigateClient] to `ClipSource`.

    The indirection exists so the materialiser can be tested without a network, and so
    nothing in this module needs to know the client's exception hierarchy beyond
    `FrigateError`.
    """

    client: FrigateClient

    def retain(self, event_id: str) -> None:
        self.client.retain_event(event_id)

    def unretain(self, event_id: str) -> None:
        self.client.unretain_event(event_id)

    def download(
        self,
        camera: str,
        start: Instant,
        end: Instant,
        sink: object,
        *,
        timeout_s: float,
    ) -> int:
        return self.client.download_range(camera, start, end, sink, timeout_s=timeout_s)


@dataclass(frozen=True, slots=True)
class MaterialiseOutcome:
    """What happened, and why.

    `status` distinguishes four things that a boolean would conflate: a clip was made; it
    was deliberately not made; it could not be made and should be retried; it could not be
    made and will not be.
    """

    review_id: str
    status: str
    reason: str
    clip_id: int | None = None
    path: str | None = None
    coverage_ratio: float | None = None
    degraded: bool = False
    long_clip: bool = False
    fetch_attempts: int = 0

    @property
    def materialised(self) -> bool:
        return self.status == CREATED


@dataclass(frozen=True, slots=True)
class _Ranked:
    """A finished attempt, shaped so `pick_best` can rank it.

    Exists so the ranking rule lives in exactly one place. Re-deriving "best" here would
    mean two implementations of INV-14 that could disagree.
    """

    coverage_ratio: float | None
    duration_s: float | None
    scratch: Path
    facts: ClipFacts


class ClipMaterialiser:
    """Materialises review items into canonical clips."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        source: ClipSource,
        data_dir: Path,
        location: str,
        zone: ZoneInfo,
        prober: Prober = run_probe,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], Instant] = _now,
        policy: CoveragePolicy = DEFAULT_COVERAGE_POLICY,
        flush_grace_s: float = DEFAULT_SEGMENT_FLUSH_GRACE_S,
        long_clip_warn_s: float = DEFAULT_LONG_CLIP_WARN_S,
        backoff_s: Sequence[float] = (5.0, 15.0, 45.0),
        clips_dirname: str = "clips",
    ) -> None:
        self.conn = conn
        self.source = source
        self.data_dir = Path(data_dir)
        self.location = location
        self.zone = zone
        self.prober = prober
        self._sleep = sleep
        self._clock = clock
        self.policy = policy
        self.flush_grace_s = flush_grace_s
        self.long_clip_warn_s = long_clip_warn_s
        self.backoff_s = tuple(backoff_s)
        self.clips_dirname = clips_dirname

    # -----------------------------------------------------------------------------------

    def materialise(
        self,
        candidate: ReviewCandidate,
        *,
        pre_roll_s: float = 5.0,
        post_roll_s: float = 10.0,
    ) -> MaterialiseOutcome:
        """Fetch, verify, and store one review as a canonical clip."""
        if candidate.is_open:
            return MaterialiseOutcome(candidate.review_id, SKIPPED, "review is still open")

        plan = plan_fetch(
            candidate.review_id,
            start_time=candidate.start_time,
            end_time=candidate.end_time,
            pre_roll_s=pre_roll_s,
            post_roll_s=post_roll_s,
            segment_flush_grace_s=self.flush_grace_s,
        )

        delay = plan.seconds_until_ready(self._clock())
        if delay > 0:
            self._sleep(delay)

        pinned: list[str] = []
        try:
            for event_id in candidate.event_ids:
                try:
                    self.source.retain(event_id)
                except FrigateError as exc:
                    # Fatal for this review. Downloading without a pin risks Frigate
                    # expiring the footage first, and a missing clip beats a late one only
                    # if it is deliberate — which this is.
                    return MaterialiseOutcome(
                        candidate.review_id,
                        FAILED,
                        f"could not pin event {event_id}: {exc}",
                    )
                pinned.append(event_id)

            return self._fetch_and_store(candidate, plan, post_roll_s=post_roll_s)
        finally:
            for event_id in pinned:
                with suppress(FrigateError):
                    # Non-fatal by design: a stuck pin wastes disk, it does not lose data.
                    self.source.unretain(event_id)

    # -----------------------------------------------------------------------------------

    def _fetch_and_store(
        self, candidate: ReviewCandidate, plan: FetchPlan, *, post_roll_s: float
    ) -> MaterialiseOutcome:
        target = self._choose_target(candidate)
        ranked: list[_Ranked] = []
        last_error: str | None = None

        for attempt in range(1, self.policy.max_attempts + 1):
            scratch = target.with_name(f"{target.name}.tmp-attempt{attempt}")
            try:
                with atomic.atomic_writer(scratch) as handle:
                    self.source.download(
                        candidate.camera,
                        plan.requested_start,
                        plan.requested_end,
                        handle,
                        timeout_s=plan.timeout_s,
                    )
                facts = self.prober(scratch)
            except (NoRecordingsYet, FrigateUnavailable) as exc:
                # The flush race and transient outages. Both mean "ask again later".
                atomic.discard(scratch)
                last_error = str(exc)
                if attempt < self.policy.max_attempts:
                    self._sleep(self._backoff(attempt))
                continue
            except ProbeError as exc:
                atomic.discard(scratch)
                last_error = str(exc)
                if attempt < self.policy.max_attempts:
                    self._sleep(self._backoff(attempt))
                continue
            except FrigateError as exc:
                atomic.discard(scratch)
                return MaterialiseOutcome(candidate.review_id, FAILED, f"fetch failed: {exc}")

            coverage = facts.coverage_against(plan.requested_s)
            ranked.append(
                _Ranked(
                    coverage_ratio=coverage,
                    duration_s=facts.duration_s,
                    scratch=scratch,
                    facts=facts,
                )
            )

            verdict = judge_coverage(coverage, attempts_used=attempt, policy=self.policy)
            if verdict is Verdict.ACCEPT:
                break
            if verdict is Verdict.ACCEPT_DEGRADED:
                # Attempts exhausted and it is still short. Keep it, flag it, move on.
                break
            if attempt < self.policy.max_attempts:
                self._sleep(self._backoff(attempt))

        if not ranked:
            return MaterialiseOutcome(
                candidate.review_id,
                DEFERRED,
                last_error or "no attempt produced a clip",
            )

        chosen = pick_best(ranked)
        assert isinstance(chosen, _Ranked)  # pick_best returns from the input sequence
        for item in ranked:
            if item.scratch != chosen.scratch:
                atomic.discard(item.scratch)

        atomic.promote(chosen.scratch, target)
        return self._record(
            candidate,
            plan,
            target,
            chosen,
            post_roll_s=post_roll_s,
            fetch_attempts=len(ranked),
        )

    # -----------------------------------------------------------------------------------

    def _record(
        self,
        candidate: ReviewCandidate,
        plan: FetchPlan,
        target: Path,
        chosen: _Ranked,
        *,
        post_roll_s: float,
        fetch_attempts: int,
    ) -> MaterialiseOutcome:
        # Two related-but-different paths. `relative` is relative to DATA_DIR, which is
        # what the database stores. The cloud key is built from the path relative to the
        # *clips* directory, because `clip_cloud_key` adds the `clips` segment itself —
        # passing the DATA_DIR-relative form doubled it.
        relative = PurePosixPath(self.clips_dirname) / self._relative(target)
        relative_str = relative.as_posix()
        below_clips = self._relative(target)

        size_bytes = target.stat().st_size
        digest = _sha256(target)
        coverage = chosen.coverage_ratio
        degraded = coverage is None or coverage < self.policy.ok
        long_clip = (chosen.duration_s or 0.0) > self.long_clip_warn_s
        cloud_key = clip_cloud_key(self.location, below_clips)

        sidecar_payload = {
            "review_id": candidate.review_id,
            "camera": candidate.camera,
            "severity": candidate.severity,
            "review_start_time": candidate.start_time,
            "review_end_time": candidate.end_time,
            "requested_start_ts": plan.requested_start,
            "requested_end_ts": plan.requested_end,
            "requested_s": plan.requested_s,
            "duration_s": chosen.facts.duration_s,
            "coverage_ratio": coverage,
            "max_gap_s": chosen.facts.max_packet_gap_s,
            "has_gaps": False,  # see the note on ClipRecord below — the detector is unusable
            "video_codec": chosen.facts.video_codec,
            "audio_codec": chosen.facts.audio_codec,
            "has_audio": chosen.facts.has_audio,
            "event_ids": list(candidate.event_ids),
            "objects": list(candidate.objects),
            "zones": list(candidate.zones),
            "sub_labels": list(candidate.sub_labels),
            "post_roll_s": post_roll_s,
            "degraded": degraded,
            "long_clip": long_clip,
            "path": relative_str,
            "cloud_key": cloud_key,
            "size_bytes": size_bytes,
            "sha256": digest,
        }

        sidecar = sidecar_relpath(target)
        atomic.write_atomic(
            sidecar,
            json.dumps(sidecar_payload, indent=2, sort_keys=True).encode("utf-8"),
        )
        sidecar_relative = (PurePosixPath(self.clips_dirname) / self._relative(sidecar)).as_posix()

        clip_id, _created = db.insert_clip(
            self.conn,
            db.ClipRecord(
                review_id=candidate.review_id,
                camera=candidate.camera,
                severity=candidate.severity,
                start_time=candidate.start_time,
                end_time=candidate.end_time or candidate.start_time,
                path=relative_str,
                event_ids=candidate.event_ids,
                zones=candidate.zones,
                label=candidate.objects[0] if candidate.objects else None,
                duration_s=chosen.facts.duration_s,
                requested_start_ts=plan.requested_start,
                requested_end_ts=plan.requested_end,
                pre_roll_s=candidate.start_time - plan.requested_start,
                post_roll_s=post_roll_s,
                # Recorded because the data is real and a future detector may want it.
                # `has_gaps` stays false: packet-gap detection flags healthy clips on this
                # stack (a measured 3.55 s gap in a 30 s clip with a 2.55 s keyframe
                # interval), so setting it here would mark almost every clip as damaged and
                # make the flag meaningless. ADR-0015.
                max_gap_s=chosen.facts.max_packet_gap_s,
                has_gaps=False,
                has_audio=chosen.facts.has_audio,
                video_codec=chosen.facts.video_codec,
                audio_codec=chosen.facts.audio_codec,
                fetch_attempts=fetch_attempts,
                degraded=degraded,
                long_clip=long_clip,
                size_bytes=size_bytes,
                sha256=digest,
                sidecar_path=sidecar_relative,
                materialised_at=_now(),
            ),
            now=_now(),
        )

        return MaterialiseOutcome(
            review_id=candidate.review_id,
            status=CREATED,
            reason="ok" if not degraded else f"kept a short clip (coverage {coverage})",
            clip_id=clip_id,
            path=relative_str,
            coverage_ratio=coverage,
            degraded=degraded,
            long_clip=long_clip,
            fetch_attempts=fetch_attempts,
        )

    # -----------------------------------------------------------------------------------

    def _relative(self, target: Path) -> PurePosixPath:
        """`<camera>/YYYY/MM/DD/HH-MM-SS.mp4` — the part below the clips directory."""
        return PurePosixPath(target.relative_to(self.data_dir / self.clips_dirname).as_posix())

    def _choose_target(self, candidate: ReviewCandidate) -> Path:
        """The path to write, resolving the collision case.

        Two reviews on one camera can start in the same second, and a DST fallback makes it
        certain rather than rare. The colliding form is used when the primary path belongs
        to a *different* review, or when a file is there that no row accounts for — an
        orphan from a crash. Overwriting either would destroy a clip to make room, which is
        the one thing the design never does.
        """
        primary, colliding = candidate_relpaths(
            candidate.camera, candidate.start_time, self.zone, candidate.review_id
        )
        chosen = primary

        row = self.conn.execute(
            "SELECT review_id FROM clips WHERE path = ?",
            ((PurePosixPath(self.clips_dirname) / primary).as_posix(),),
        ).fetchone()
        if row is not None:
            if row["review_id"] != candidate.review_id:
                chosen = colliding
        elif (self.data_dir / self.clips_dirname / primary).exists():
            chosen = colliding

        return self.data_dir / self.clips_dirname / chosen

    def _backoff(self, attempt: int) -> float:
        if not self.backoff_s:
            return 0.0
        return self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)]


def _sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
