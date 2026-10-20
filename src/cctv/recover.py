"""Recovery from the bucket (M7 scenarios B and C, `lld/08` §6.2 and §6.3).

Two operations, and they answer two different disasters.

**`clips` — get the footage back.** The bucket is the backup for footage; that is the whole
design position in [`backup`][cctv.backup]. So when the local tree is gone, recovery is a prefix
download. Object keys are `<location>/clips/` + the local relative path
([ADR-0006](../../../internal/adr/0006-canonical-clip-layout.md)), which means restoring is
"strip the prefix, write the file". No mapping table, no manifest resolution, nothing that can
itself drift and be wrong at 2 a.m. That property is why the key was built as a concatenation.

**`rebuild` — get the *bookkeeping* back.** `state.db` is not in the bucket, and without it
nothing knows which clips should exist or whether they reached B2. The last resort is to
reconstruct it from the two things that survive: the local tree (with its `.mp4.json` sidecars)
and the objects themselves, whose user metadata carries camera, location, start and end times
and the review id — see
[`cloud-object-layout`](../../../internal/architecture/contracts/cloud-object-layout.md) §3.2.

The safety rule for `rebuild` is exactly one sentence: **a clip whose history is unknown is
`PENDING`, never `UPLOADED`.** `PENDING` means the uploader will send it, idempotently, to a
deterministic key ([INV-7](../invariants.md)) — so a guess in that direction costs one duplicate
PUT that the provider deduplicates anyway. A guess in the other direction marks a clip as backed
up when it is not, which is the failure [INV-1](../invariants.md) exists to prevent.

Neither operation deletes anything, and `rebuild` refuses to overwrite a database that already
has rows unless asked, because a half-broken database is `restore-state`'s problem, not this
one's.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from . import atomic, db, paths, storage, timeutil
from .config import CamerasConfig, ConfigError, load_cameras_config
from .reconcile import sha256_of
from .timeutil import Instant

__all__ = [
    "DEFAULT_MAX_OBJECTS",
    "RECOVERED_REVIEW_PREFIX",
    "ClipRestoreResult",
    "ReconstructResult",
    "RecoverError",
    "main",
    "reconstruct",
    "restore_clips",
]

#: How many objects one run will consider. The same cap reconciliation uses, for the same
#: reason: a pathological bucket must not turn a recovery into something that never ends. A
#: site larger than this recovers in more than one run, and the report says it was truncated.
DEFAULT_MAX_OBJECTS = 20_000

#: Prefix for a review id we had to invent, because neither the sidecar nor the object carried
#: one. Visible in the database on purpose: a row with this prefix means "we know the file, we
#: do not know the event", and an operator reading `state.db` should be able to tell.
RECOVERED_REVIEW_PREFIX = "recovered:"

#: Parallel downloads. Recovery is network-bound and measured in hours, so this is the one
#: place where concurrency changes the answer to "is this possible overnight?".
DEFAULT_DOWNLOAD_CONCURRENCY = 4


class RecoverError(RuntimeError):
    """A recovery that must not proceed."""


@dataclass(frozen=True, slots=True)
class ClipRestoreResult:
    """What a clip restore actually did. `failed` is never silent."""

    listed: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    bytes_already_present: int = 0
    #: Objects whose key is not `<location>/clips/<canonical clip path>`. Downloaded anyway —
    #: footage is footage — and listed so a human can look at them.
    not_canonical: tuple[str, ...] = ()
    #: Keys refused outright: absolute, containing `..`, or not under the prefix at all.
    refused: tuple[str, ...] = ()
    failures: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False
    dry_run: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "listed": self.listed,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_already_present": self.bytes_already_present,
            "not_canonical": list(self.not_canonical),
            "refused": list(self.refused),
            "failures": dict(self.failures),
            "truncated": self.truncated,
            "dry_run": self.dry_run,
            "duration_s": round(self.duration_s, 3),
        }

    def summary(self) -> str:
        verb = "would download" if self.dry_run else "downloaded"
        parts = [f"{self.listed} objects listed", f"{self.downloaded} {verb}"]
        if self.skipped:
            parts.append(f"{self.skipped} already present")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.refused:
            parts.append(f"{self.refused} refused")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ReconstructResult:
    """The four categories of `lld/08` §6.3, plus how many were guesses."""

    local_files: int = 0
    objects: int = 0
    #: object present, local file present      → `UPLOADED`
    both: int = 0
    #: object present, local file absent       → `UPLOADED` + `local_deleted_at`
    cloud_only: int = 0
    #: object absent, local file present       → `PENDING` (the safe guess)
    local_only: int = 0
    #: Clips already in the database; a re-run does not duplicate them.
    already_present: int = 0
    #: Rows whose `start_time` came from the path rather than from a sidecar or object metadata.
    derived_start_time: int = 0
    #: Rows whose `sha256` is the one the uploader recorded, not one computed here.
    unverified_hashes: int = 0
    orphan_sidecars: tuple[str, ...] = ()
    #: Clips whose path is not canonical and cannot be read as a time — nothing says when they
    #: began, so no row is invented for them.
    unusable: tuple[str, ...] = ()
    #: Clips belonging to a camera that `cameras.yml` no longer lists. Skipped rather than added,
    #: because the camera table is generated from the configuration ([INV-3](../invariants.md)).
    unknown_camera: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    skipped_cloud: bool = False
    truncated: bool = False
    dry_run: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def inserted(self) -> int:
        return self.both + self.cloud_only + self.local_only

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_files": self.local_files,
            "objects": self.objects,
            "both": self.both,
            "cloud_only": self.cloud_only,
            "local_only": self.local_only,
            "already_present": self.already_present,
            "derived_start_time": self.derived_start_time,
            "unverified_hashes": self.unverified_hashes,
            "orphan_sidecars": list(self.orphan_sidecars),
            "unusable": list(self.unusable),
            "unknown_camera": list(self.unknown_camera),
            "refused": list(self.refused),
            "skipped_cloud": self.skipped_cloud,
            "truncated": self.truncated,
            "dry_run": self.dry_run,
            "duration_s": round(self.duration_s, 3),
        }

    def summary(self) -> str:
        return (
            f"{self.both} where both copies exist, {self.cloud_only} cloud-only, "
            f"{self.local_only} local-only, {self.already_present} already recorded"
        )


# ---------------------------------------------------------------------------------------
# Clip restore — the footage
# ---------------------------------------------------------------------------------------


def _split_key(key: str, prefix: str) -> PurePosixPath | None:
    """`<prefix><relative>` → the relative path, or `None` when the key is not safe to write.

    The listing comes from a bucket, which is remote input even when it is our own bucket. A key
    containing `..` or an absolute path would write outside `data/clips/`, so those are refused
    rather than sanitised: a recovery that overwrites `/etc` is worse than a recovery that
    reports one object it did not take.
    """
    if not key.startswith(prefix):
        return None
    tail = key[len(prefix) :]
    if not tail or tail.endswith("/"):
        return None
    relative = PurePosixPath(tail)
    if relative.is_absolute() or ".." in relative.parts or "\\" in tail:
        return None
    if any(part in {"", "."} for part in relative.parts):
        return None
    return relative


def restore_clips(
    *,
    data_dir: Path,
    location: str,
    adapter: storage.ObjectReader | None,
    concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    dry_run: bool = False,
    logger: Any | None = None,
    clock: Callable[[], Instant] = timeutil.now,
    progress: Callable[[int, int], None] | None = None,
) -> ClipRestoreResult:
    """Download every object under `<location>/clips/` into the local tree.

    Skips an object whose local file is already the same size — a recovery run twice should
    cost a listing, not 66 GB of egress. Downloads atomically, so an interrupted run leaves no
    half-clip at a canonical path; the scratch file is discarded and the next run re-fetches it.

    `dry_run` lists and totals without writing, because the honest first step of a 66 GB
    recovery is to say how much 66 GB is.
    """
    if adapter is None:
        raise RecoverError(
            "No storage bucket is configured, so there is nothing to recover footage from. "
            "A local-only site keeps its only copy on this machine; its protection is "
            "./backup.sh --include-clips, not this."
        )
    started = clock()
    prefix = f"{location}/{paths.CLOUD_CLIPS_PREFIX}/"
    clips_dir = Path(data_dir) / paths.CLOUD_CLIPS_PREFIX

    listed = 0
    skipped = 0
    bytes_already_present = 0
    not_canonical: list[str] = []
    refused: list[str] = []
    failures: dict[str, str] = {}
    pending: list[tuple[str, PurePosixPath, int]] = []

    try:
        objects = adapter.list_objects(prefix=prefix)
        for info in objects:
            if listed >= max_objects:
                break
            listed += 1
            relative = _split_key(info.key, prefix)
            if relative is None:
                refused.append(info.key)
                continue
            if not paths.is_canonical_relpath(relative):
                not_canonical.append(info.key)
            target = clips_dir.joinpath(*relative.parts)
            if _already_present(target, info.size):
                skipped += 1
                bytes_already_present += info.size
                continue
            pending.append((info.key, relative, info.size))
    except storage.StorageError as exc:
        raise RecoverError(f"Listing {prefix!r} failed: {exc}") from exc

    truncated = listed >= max_objects
    if truncated and logger is not None:
        logger.warning(
            "recovery listing truncated",
            extra={"reason": f"stopped at {max_objects} objects; run again to continue"},
        )
    if refused and logger is not None:
        logger.warning("refused keys", extra={"count": len(refused)})

    downloaded = 0
    total_bytes = 0
    if dry_run:
        total_bytes = sum(size for _, _, size in pending)
        downloaded = len(pending)
    elif pending:
        downloaded, total_bytes, failures = _download_all(
            adapter,
            pending=pending,
            clips_dir=clips_dir,
            concurrency=concurrency,
            logger=logger,
            progress=progress,
        )

    return ClipRestoreResult(
        listed=listed,
        downloaded=downloaded,
        skipped=skipped,
        failed=len(failures),
        bytes_downloaded=total_bytes,
        bytes_already_present=bytes_already_present,
        not_canonical=tuple(not_canonical),
        refused=tuple(refused),
        failures=failures,
        truncated=truncated,
        dry_run=dry_run,
        started_at=started,
        finished_at=clock(),
    )


def _already_present(target: Path, expected_size: int) -> bool:
    """Whether the local file is the one the bucket has.

    Size only. Comparing bytes would mean downloading the object to decide whether to download
    it — the one check that cannot pay for itself.
    """
    try:
        return target.is_file() and target.stat().st_size == expected_size
    except OSError:
        return False


def _download_all(
    adapter: storage.ObjectReader,
    *,
    pending: list[tuple[str, PurePosixPath, int]],
    clips_dir: Path,
    concurrency: int,
    logger: Any | None,
    progress: Callable[[int, int], None] | None,
) -> tuple[int, int, dict[str, str]]:
    """Fetch what is missing, bounded, and collect failures instead of stopping on them.

    One unreachable object must not abandon the other 19 999: the whole point of a recovery is
    that it gets as much back as it can and tells you precisely what it could not.
    """
    done = 0
    total_bytes = 0
    failures: dict[str, str] = {}
    workers = max(1, concurrency)

    def fetch(item: tuple[str, PurePosixPath, int]) -> tuple[str, int, str | None]:
        key, relative, size = item
        target = clips_dir.joinpath(*relative.parts)
        try:
            with atomic.atomic_writer(target) as handle:
                written = adapter.download_object(key=key, destination=handle)
        except (storage.StorageError, OSError) as exc:
            return key, 0, str(exc)
        if written != size:
            # The provider said one size and delivered another. The file is already in place at
            # this point, and it is the right bytes for whatever length they are — keep it and
            # report, because the alternative is deleting recovered footage over a metadata
            # disagreement.
            return key, written, f"provider listed {size} bytes, {written} arrived"
        return key, written, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, written, problem in pool.map(fetch, pending):
            if problem is not None:
                # A size disagreement still leaves usable footage on disk.
                if written:
                    total_bytes += written
                    done += 1
                failures[key] = problem
                if logger is not None:
                    logger.warning(
                        "recovery download problem",
                        extra={"key": key, "reason": problem},
                    )
            else:
                total_bytes += written
                done += 1
            if progress is not None:
                progress(done + len(failures), len(pending))
    return done, total_bytes, failures


# ---------------------------------------------------------------------------------------
# Rebuild — the bookkeeping
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LocalClip:
    relative: PurePosixPath
    size: int
    sha256: str | None
    sidecar: Mapping[str, Any] | None


def _local_inventory(
    data_dir: Path,
    *,
    on_sidecar_without_clip: Callable[[str], None] | None = None,
) -> dict[PurePosixPath, _LocalClip]:
    """Every `*.mp4` under `clips/`, with its sidecar when it has one.

    Hashes are computed here and now: the manifest is exactly the artefact whose accuracy is
    in question (`lld/08` §6.3), so a hash read out of it would be a claim, not evidence.
    """
    clips_dir = Path(data_dir) / paths.CLOUD_CLIPS_PREFIX
    found: dict[PurePosixPath, _LocalClip] = {}
    if not clips_dir.is_dir():
        return found

    sidecars: set[PurePosixPath] = set()
    for path in sorted(clips_dir.rglob("*")):
        if not path.is_file() or atomic.is_tmp(path):
            continue
        relative = PurePosixPath(path.relative_to(clips_dir).as_posix())
        if path.name.endswith(".json"):
            sidecars.add(PurePosixPath(relative.as_posix()[: -len(".json")]))
            continue
        if path.suffix != paths.CLIP_EXT:
            continue
        payload = _read_sidecar(path)
        found[relative] = _LocalClip(
            relative=relative,
            size=path.stat().st_size,
            sha256=sha256_of(path),
            sidecar=payload,
        )

    for sidecar_only in sorted(sidecars - set(found)):
        if on_sidecar_without_clip is not None:
            on_sidecar_without_clip(sidecar_only.as_posix())
    return found


def _read_sidecar(clip_path: Path) -> Mapping[str, Any] | None:
    sidecar = clip_path.with_name(clip_path.name + ".json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _cloud_inventory(
    adapter: storage.ObjectReader | None,
    *,
    location: str,
    max_objects: int,
    logger: Any | None = None,
) -> tuple[dict[PurePosixPath, storage.ObjectInfo], bool]:
    """Objects under the clips prefix, `HEAD`ed so their metadata comes back with them.

    `list_objects` cannot answer this: S3 user metadata is not in a listing, only in a `HEAD`.
    That is one request per object, which is why this is the recovery path of last resort and
    not something a status check does.
    """
    if adapter is None:
        return {}, False
    prefix = f"{location}/{paths.CLOUD_CLIPS_PREFIX}/"
    found: dict[PurePosixPath, storage.ObjectInfo] = {}
    listed = 0
    truncated = False
    for info in adapter.list_objects(prefix=prefix):
        if listed >= max_objects:
            truncated = True
            break
        listed += 1
        relative = _split_key(info.key, prefix)
        if relative is None or not paths.is_canonical_relpath(relative):
            continue
        try:
            detailed = adapter.head_object(key=info.key)
        except storage.StorageError as exc:
            if logger is not None:
                logger.warning(
                    "could not read object metadata",
                    extra={"key": info.key, "reason": str(exc)},
                )
            detailed = None
        found[relative] = detailed if detailed is not None else info
    return found, truncated


def _instant_from_relpath(relative: PurePosixPath, zone: ZoneInfo) -> Instant | None:
    """`camera/2026/09/11/18-32-45.mp4` → the instant, read as local wall-clock time.

    A third choice behind the sidecar and the object metadata, used only when neither survived.
    The path *is* local time by construction
    ([ADR-0017](../../../internal/adr/0017-time-handling.md)), so reading it back through the
    site's zone is exact except on a DST fallback day, where two instants share one wall-clock
    reading. `fold=0` takes the earlier one, which means retention can act up to an hour early
    on those clips and never an hour late — the direction that keeps the file, not the one that
    deletes it.
    """
    try:
        parsed = paths.parse_clip_relpath(relative)
    except paths.PathError:
        return None
    hour, minute, second = (int(part) for part in parsed.time_parts)
    year, month, day = (int(part) for part in parsed.date_parts)
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=zone)
    except ValueError:
        return None
    return moment.timestamp()


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def reconstruct(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    config: CamerasConfig,
    adapter: storage.ObjectReader | None,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    dry_run: bool = False,
    allow_existing: bool = False,
    logger: Any | None = None,
    clock: Callable[[], Instant] = timeutil.now,
) -> ReconstructResult:
    """Rebuild `clips` rows from the local tree and the bucket.

    Refuses a database that already has rows unless `allow_existing`, and skips any clip whose
    path is already recorded — so a run interrupted halfway can be repeated without
    duplicating anything.
    """
    started = clock()
    state = db.counts_by_state(conn)
    if state and not allow_existing and not dry_run:
        raise RecoverError(
            f"{sum(state.values())} clips are already in this database. Rebuilding is for a "
            "database that is empty or lost; a damaged one is ./restore.sh state's problem. "
            "Pass --into-existing to add the clips it does not have."
        )

    orphan_sidecars: list[str] = []
    local = _local_inventory(
        data_dir, on_sidecar_without_clip=lambda rel: orphan_sidecars.append(rel)
    )
    cloud, truncated = _cloud_inventory(
        adapter, location=config.site.location, max_objects=max_objects, logger=logger
    )
    existing = db.clip_paths(conn)
    configured = set(config.cameras)
    for camera in sorted(configured):
        db.ensure_camera(conn, name=camera)

    both = cloud_only = local_only = already = derived = unverified = 0
    unusable: list[str] = []
    unknown_camera: list[str] = []
    for relative in sorted(set(local) | set(cloud)):
        row_path = f"{paths.CLOUD_CLIPS_PREFIX}/{relative.as_posix()}"
        if row_path in existing:
            already += 1
            continue
        if not paths.is_canonical_relpath(relative):
            # Something is on disk or in the bucket that is not a clip. Its path cannot be read
            # as a time either, so no row is invented for it.
            unusable.append(row_path)
            continue
        if relative.parts[0] not in configured:
            # The footage is real and stays where it is; it just has no camera to belong to
            # until an operator puts that camera back in cameras.yml. Reported, never silently
            # dropped, and never a camera row invented for it (INV-3).
            unknown_camera.append(row_path)
            continue
        local_clip = local.get(relative)
        obj = cloud.get(relative)
        record, source = _classify(relative, local_clip, obj, config=config)
        if record is None:
            unusable.append(row_path)
            continue
        if source == "derived":
            derived += 1
        if record.sha256 is not None and local_clip is None:
            unverified += 1
        if local_clip is not None and obj is not None:
            both += 1
        elif obj is not None:
            cloud_only += 1
        else:
            local_only += 1

        if dry_run:
            continue
        clip_id, _created = db.insert_clip(conn, record)
        if obj is not None:
            # The object exists, so record where it is and when it arrived. Without the key this
            # row is invisible to reconciliation; without the object's own time it is invisible
            # to cloud retention for another `cloud_days`.
            db.record_recovered_object(
                conn,
                clip_id=clip_id,
                cloud_key=obj.key,
                cloud_etag=obj.etag,
                cloud_size_bytes=obj.size,
                uploaded_at=obj.last_modified if obj.last_modified else started,
            )
        if obj is not None and local_clip is None:
            # The file is gone and the object is the only copy. Recording that on the row is
            # what stops INV-1 from being violated by a later cleanup pass that would otherwise
            # believe there is a local file to delete.
            with db.write_txn(conn):
                db.mark_local_deleted(conn, clip_id=clip_id, now=started)

    return ReconstructResult(
        local_files=len(local),
        objects=len(cloud),
        both=both,
        cloud_only=cloud_only,
        local_only=local_only,
        already_present=already,
        derived_start_time=derived,
        unverified_hashes=unverified,
        orphan_sidecars=tuple(orphan_sidecars),
        unusable=tuple(unusable),
        unknown_camera=tuple(unknown_camera),
        skipped_cloud=adapter is None,
        truncated=truncated,
        dry_run=dry_run,
        started_at=started,
        finished_at=clock(),
    )


def _classify(
    relative: PurePosixPath,
    local_clip: _LocalClip | None,
    obj: storage.ObjectInfo | None,
    *,
    config: CamerasConfig,
) -> tuple[db.ClipRecord | None, str]:
    """One clip, from whichever of the two surviving sources still describe it.

    Returns the record and where its `start_time` came from — `"sidecar"`, `"metadata"`,
    `"derived"` — or `(None, "unusable")` when no source knows when the clip began and the path
    cannot be read as a time either.
    """
    sidecar = local_clip.sidecar if local_clip is not None else None
    metadata = dict(obj.metadata) if obj is not None else {}

    start = _float_or_none(sidecar.get("review_start_time")) if sidecar else None
    source = "sidecar"
    if start is None:
        start = _float_or_none(metadata.get("start-time"))
        source = "metadata"
    if start is None:
        start = _instant_from_relpath(relative, config.site.timezone)
        source = "derived"
    if start is None:
        return None, "unusable"

    end = _float_or_none(sidecar.get("review_end_time")) if sidecar else None
    if end is None:
        end = _float_or_none(metadata.get("end-time"))
    if end is None or end < start:
        end = start

    review_id = _review_id(sidecar, metadata, relative)
    severity = _severity(sidecar)
    event_ids = _string_list(sidecar.get("event_ids")) if sidecar else ()
    if not event_ids and metadata.get("event-id"):
        event_ids = (str(metadata["event-id"]),)
    zones = _string_list(sidecar.get("zones")) if sidecar else ()
    if not zones and metadata.get("zones"):
        zones = tuple(part.strip() for part in str(metadata["zones"]).split(",") if part.strip())

    uploaded = obj is not None
    # The two categories the whole operation turns on: an object means the row is UPLOADED,
    # and no object means PENDING — never the other way round.
    upload_state = "UPLOADED" if uploaded else "PENDING"
    recorded_at = obj.last_modified if obj is not None and obj.last_modified else None

    return (
        db.ClipRecord(
            review_id=review_id,
            camera=relative.parts[0],
            severity=severity,
            start_time=start,
            end_time=end,
            path=f"{paths.CLOUD_CLIPS_PREFIX}/{relative.as_posix()}",
            event_ids=event_ids,
            zones=zones,
            label=_label(sidecar, metadata),
            duration_s=_first_float(
                sidecar.get("duration_s") if sidecar else None,
                metadata.get("duration-s"),
            ),
            requested_start_ts=(
                _float_or_none(sidecar.get("requested_start_ts")) if sidecar else None
            ),
            requested_end_ts=(_float_or_none(sidecar.get("requested_end_ts")) if sidecar else None),
            pre_roll_s=_float_or_none(sidecar.get("pre_roll_s")) if sidecar else None,
            post_roll_s=_float_or_none(sidecar.get("post_roll_s")) if sidecar else None,
            max_gap_s=_float_or_none(sidecar.get("max_gap_s")) if sidecar else None,
            has_gaps=bool(sidecar.get("has_gaps")) if sidecar else False,
            has_audio=bool(sidecar.get("has_audio")) if sidecar else False,
            video_codec=_str_or_none(sidecar.get("video_codec")) if sidecar else None,
            audio_codec=_str_or_none(sidecar.get("audio_codec")) if sidecar else None,
            degraded=bool(sidecar.get("degraded")) if sidecar else False,
            long_clip=bool(sidecar.get("long_clip")) if sidecar else False,
            # A size and a hash are only known when the bytes are here; for a cloud-only clip
            # the hash is the one the uploader recorded, which is why the report counts them.
            size_bytes=(local_clip.size if local_clip is not None else (obj.size if obj else None)),
            sha256=_sha256_for(local_clip, metadata),
            sidecar_path=(
                f"{paths.CLOUD_CLIPS_PREFIX}/{paths.sidecar_relpath(relative).as_posix()}"
                if local_clip is not None and sidecar is not None
                else None
            ),
            materialised_at=recorded_at,
            upload_state=upload_state,
        ),
        source,
    )


def _review_id(
    sidecar: Mapping[str, Any] | None,
    metadata: Mapping[str, str],
    relative: PurePosixPath,
) -> str:
    """The dedup key, from the sidecar, the object, or invented and labelled as such."""
    if sidecar:
        found = _str_or_none(sidecar.get("review_id"))
        if found:
            return found
    recorded = metadata.get("review-id")
    if recorded:
        return str(recorded)
    return f"{RECOVERED_REVIEW_PREFIX}{relative.as_posix()}"


def _severity(sidecar: Mapping[str, Any] | None) -> str:
    """`alert` or `detection`. Object metadata does not carry it, so cloud-only clips default.

    `detection` rather than `alert` because it is the lower-stakes of two true-ish answers: a
    recovered clip does not raise a notification, and claiming an alert status for 60 days of
    footage would make the alert channel meaningless.
    """
    found = _str_or_none(sidecar.get("severity")) if sidecar else None
    return found if found in {"alert", "detection"} else "detection"


def _label(sidecar: Mapping[str, Any] | None, metadata: Mapping[str, str]) -> str | None:
    if sidecar:
        found = _str_or_none(sidecar.get("label"))
        if found:
            return found
    recorded = metadata.get("label")
    return str(recorded) if recorded else None


def _sha256_for(local_clip: _LocalClip | None, metadata: Mapping[str, str]) -> str | None:
    """The local hash when the file is here, otherwise the one recorded on the object."""
    if local_clip is not None:
        return local_clip.sha256
    recorded = metadata.get("sha256")
    return str(recorded) if recorded else None


def _first_float(*values: object) -> float | None:
    for value in values:
        found = _float_or_none(value)
        if found is not None:
            return found
    return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item))


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoverSettings:
    """Everything from the environment, like every other entry point (ADR-0018)."""

    data_dir: Path
    config_dir: Path
    db_path: Path
    max_objects: int = DEFAULT_MAX_OBJECTS
    concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> RecoverSettings:
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("CCTV_DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            config_dir=Path(env.get("CCTV_CONFIG_DIR", "/config")),
            db_path=Path(env.get("CCTV_DB", str(data_dir / "cctv.db"))),
            max_objects=_int_env(env, "CCTV_RECOVER_MAX_OBJECTS", DEFAULT_MAX_OBJECTS),
            concurrency=_int_env(env, "CCTV_RECOVER_CONCURRENCY", DEFAULT_DOWNLOAD_CONCURRENCY),
        )


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise RecoverError(f"{name} must be a positive integer, got {raw!r}.") from exc


def _load_config(settings: RecoverSettings) -> CamerasConfig:
    path = settings.config_dir / "cameras.yml"
    if not path.is_file():
        raise RecoverError(
            f"{path} is missing. Recovery needs the site configuration before anything else: "
            "restore it with ./restore.sh config, then run this again."
        )
    try:
        return load_cameras_config(path)
    except ConfigError as exc:
        raise RecoverError(f"{path} is not usable: {exc}") from exc


def _build_adapter(
    environ: Mapping[str, str] | None = None,
) -> storage.ObjectReader | None:
    """The adapter, or `None` on a site with no bucket.

    Imported lazily so a site without the `storage` extra can still run the local half of a
    rebuild.
    """
    from .uploader import UploadSettings, build_adapter

    try:
        settings = UploadSettings.from_environ(None if environ is None else dict(environ))
    except ConfigError:
        return None
    if not settings.storage_configured:
        return None
    return build_adapter(settings)


def main(argv: list[str] | None = None) -> int:
    """`cctv-recover clips|rebuild`."""
    import argparse
    import logging

    parser = argparse.ArgumentParser(prog="cctv-recover")
    sub = parser.add_subparsers(dest="command", required=True)

    clips_p = sub.add_parser("clips", help="download every object into data/clips/")
    clips_p.add_argument("--dry-run", action="store_true")
    clips_p.add_argument("--json", action="store_true")

    rebuild_p = sub.add_parser("rebuild", help="reconstruct the clips table")
    rebuild_p.add_argument("--dry-run", action="store_true")
    rebuild_p.add_argument("--into-existing", action="store_true")
    rebuild_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    logger = logging.getLogger("cctv.recover")

    try:
        settings = RecoverSettings.from_environ()
        config = _load_config(settings)
        adapter = _build_adapter()

        if args.command == "clips":
            result = restore_clips(
                data_dir=settings.data_dir,
                location=config.site.location,
                adapter=adapter,
                concurrency=settings.concurrency,
                max_objects=settings.max_objects,
                dry_run=args.dry_run,
                logger=logger,
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print(result.summary())
                if result.dry_run and result.downloaded:
                    print(
                        f"Would fetch about {result.bytes_downloaded / 1e9:.1f} GB of egress. "
                        "Re-run without --dry-run to start."
                    )
                for key, reason in list(result.failures.items())[:20]:
                    print(f"  failed: {key}: {reason}", file=sys.stderr)
            return 1 if result.failed else 0

        conn = db.open_db(settings.db_path)
        try:
            db.migrate(conn)
            result = reconstruct(
                conn,
                data_dir=settings.data_dir,
                config=config,
                adapter=adapter,
                max_objects=settings.max_objects,
                dry_run=args.dry_run,
                allow_existing=args.into_existing,
                logger=logger,
            )
        finally:
            conn.close()
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(result.summary())
            if result.skipped_cloud:
                print("No bucket configured: only the local tree was used.")
            if result.derived_start_time:
                print(
                    f"{result.derived_start_time} clips had no recorded start time; it was read "
                    "from the path in the site timezone."
                )
            if result.unverified_hashes:
                print(
                    f"{result.unverified_hashes} cloud-only clips carry the hash the uploader "
                    "recorded, not one computed here."
                )
            for relative in result.orphan_sidecars[:20]:
                print(f"  sidecar without a clip: {relative}", file=sys.stderr)
            for path in result.unusable[:20]:
                print(f"  not a usable clip path: {path}", file=sys.stderr)
            for path in result.unknown_camera[:20]:
                print(
                    f"  camera not in cameras.yml, left alone: {path}",
                    file=sys.stderr,
                )
        return 0
    except (RecoverError, db.DbError) as exc:
        print(f"cctv-recover: {exc}", file=sys.stderr)
        return 78
