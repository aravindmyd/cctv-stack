"""Reconciliation (J3) — the enforcement mechanism for INV-10.

The two copies of the truth are the bucket listing and `state.db`. They drift for reasons that
are all real: a restore from a backup that was older than the bucket, an object expired by a
provider rule the database did not know about, a crash between a write and its row, a key
constructed differently by an older version. Reconciliation **reports** that drift.

It never repairs it. Every repair is a guess about which side is right — deleting an "orphan"
object may destroy the only surviving copy of a clip whose row was lost; re-uploading a "dangling"
row may be correct or pointless. A human with the finding in front of them makes a better decision
than a heuristic, so the output is `reconciliation_findings` and nothing else.

Six findings, from `lld/05` §5.1:

| Finding | Meaning | Severity |
|---|---|---|
| `dangling_row` | the row says there is an object; there is not | critical |
| `checksum_mismatch` | the stored object is not the bytes we recorded | critical |
| `key_mismatch` | `cloud_key` is not the key this version would build | critical |
| `orphan_object` | an object no row references | warning — cost |
| `unexpected_local_file` | a file under `clips/` with no row | warning |
| `missing_local_file` | a row whose file is gone | warning |

Cost is bounded twice: the listing is capped (`max_objects`) and paged, and the per-object `HEAD`
that compares checksums runs with the same bounded concurrency as cloud deletion. A site large
enough to need more than the cap reconciles in more than one pass rather than in one long one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, paths, storage, timeutil
from .config import CamerasConfig
from .timeutil import Instant

__all__ = [
    "CRITICAL_KINDS",
    "DEFAULT_MAX_OBJECTS",
    "FINDING_LABELS",
    "ReconcileRun",
    "Reconciler",
]

#: Findings that mean "we believe something that is not true". These are what a health check
#: should shout about; the rest are cost and curiosity.
CRITICAL_KINDS: frozenset[str] = frozenset({"dangling_row", "checksum_mismatch", "key_mismatch"})

#: How many objects one pass will look at. 5 000 objects is a cheap daily walk; the cap exists
#: so a pathological bucket cannot turn a daily pass into an hourly one.
DEFAULT_MAX_OBJECTS = 20_000

FINDING_LABELS: Mapping[str, str] = {
    "orphan_object": "objects with no row",
    "dangling_row": "rows with no object",
    "checksum_mismatch": "checksum or size mismatches",
    "unexpected_local_file": "local files with no row",
    "missing_local_file": "rows with no local file",
    "key_mismatch": "keys that do not match their path",
}

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class ReconcileRun:
    """What one pass looked at, and what it found. Counts are *new* findings."""

    objects_listed: int = 0
    rows_checked: int = 0
    local_files_scanned: int = 0
    findings: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False
    skipped_cloud: bool = False
    dry_run: bool = False
    started_at: Instant = 0.0
    finished_at: Instant = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def total_findings(self) -> int:
        return sum(self.findings.values())

    @property
    def critical_findings(self) -> int:
        return sum(count for kind, count in self.findings.items() if kind in CRITICAL_KINDS)

    def summary(self) -> str:
        if not self.total_findings:
            return f"{self.rows_checked} rows, {self.objects_listed} objects, no drift"
        parts = ", ".join(
            f"{count} {FINDING_LABELS[kind]}"
            for kind, count in sorted(self.findings.items())
            if count
        )
        return f"{parts}"


class Reconciler:
    """Compares the bucket and the database, and writes findings. Deletes nothing."""

    def __init__(
        self,
        *,
        data_dir: Path,
        location: str,
        adapter: storage.StorageAdapter | None,
        batch_size: int = 500,
        max_objects: int = DEFAULT_MAX_OBJECTS,
        concurrency: int = 4,
        logger: Any | None = None,
        clock: Callable[[], Instant] = timeutil.now,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._location = location
        self._adapter = adapter
        self._batch_size = max(1, batch_size)
        self._max_objects = max(1, max_objects)
        self._concurrency = max(1, concurrency)
        self._logger = logger
        self._clock = clock

    # -- the run -------------------------------------------------------------------------

    def run_once(self, conn: Any, config: CamerasConfig, *, dry_run: bool = False) -> ReconcileRun:
        started = self._clock()
        counts: dict[str, int] = dict.fromkeys(FINDING_LABELS, 0)
        truncated = False

        rows = self._rows_with_cloud_key(conn)
        for row in rows:
            self._check_key(row, conn, counts, dry_run=dry_run)

        listed = 0
        if self._adapter is None:
            skipped_cloud = True
        else:
            skipped_cloud = False
            listed, truncated = self._compare_cloud(conn, rows, counts, dry_run=dry_run)

        scanned = self._compare_local(conn, counts, dry_run=dry_run)

        return ReconcileRun(
            objects_listed=listed,
            rows_checked=len(rows),
            local_files_scanned=scanned,
            findings={kind: count for kind, count in counts.items() if count},
            truncated=truncated,
            skipped_cloud=skipped_cloud,
            dry_run=dry_run,
            started_at=started,
            finished_at=self._clock(),
        )

    # -- rows and keys -------------------------------------------------------------------

    def _rows_with_cloud_key(self, conn: Any) -> list[Any]:
        rows: list[Any] = []
        after = 0
        while True:
            batch = db.clip_rows_with_cloud_key(conn, limit=self._batch_size, after_id=after)
            if not batch:
                return rows
            rows.extend(batch)
            after = int(batch[-1]["id"])

    def _expected_key(self, row: Any) -> str:
        """The key this version would build from the row's own path (INV-5)."""
        return paths.cloud_key_for_clip_path(self._location, str(row["path"]))

    def _check_key(self, row: Any, conn: Any, counts: dict[str, int], *, dry_run: bool) -> None:
        recorded = str(row["cloud_key"])
        expected = self._expected_key(row)
        if recorded == expected:
            return
        self._record(
            conn,
            "key_mismatch",
            subject=recorded,
            detail={"clip_id": int(row["id"]), "recorded": recorded, "expected": expected},
            counts=counts,
            dry_run=dry_run,
        )

    # -- the cloud -----------------------------------------------------------------------

    def _compare_cloud(
        self, conn: Any, rows: Sequence[Any], counts: dict[str, int], *, dry_run: bool
    ) -> tuple[int, bool]:
        assert self._adapter is not None
        by_key = {str(row["cloud_key"]): row for row in rows}
        prefix = f"{self._location}/clips/"
        truncated = False
        listed = 0
        seen: set[str] = set()
        size_mismatched: set[str] = set()

        for obj in self._adapter.list_objects(prefix=prefix, max_keys=1000):
            if listed >= self._max_objects:
                truncated = True
                break
            listed += 1
            seen.add(obj.key)
            row = by_key.get(obj.key)
            if row is None:
                self._record(
                    conn,
                    "orphan_object",
                    subject=obj.key,
                    detail={"size": obj.size},
                    counts=counts,
                    dry_run=dry_run,
                )
                continue
            recorded_size = row["size_bytes"]
            if recorded_size is not None and int(recorded_size) != obj.size:
                # A size difference already answers the question; `HEAD`ing it would only say
                # the same thing with an extra request.
                size_mismatched.add(obj.key)
                self._record(
                    conn,
                    "checksum_mismatch",
                    subject=obj.key,
                    detail={
                        "clip_id": int(row["id"]),
                        "reason": "size",
                        "recorded": int(recorded_size),
                        "object": obj.size,
                    },
                    counts=counts,
                    dry_run=dry_run,
                )

        # A row that believes an object exists, where the listing does not have it. Only for the
        # states whose object is still supposed to be there: `EXPIRED_CLOUD` and `PURGED` have
        # no object by design, and calling that "dangling" would be noise on every correct clip.
        for key, row in by_key.items():
            if key in seen or listed >= self._max_objects:
                continue
            if str(row["upload_state"]) not in db.CLOUD_EXPIRY_STATES:
                continue
            self._record(
                conn,
                "dangling_row",
                subject=key,
                detail={
                    "clip_id": int(row["id"]),
                    "state": str(row["upload_state"]),
                    "uploaded_at": row["uploaded_at"],
                },
                counts=counts,
                dry_run=dry_run,
            )

        self._compare_checksums(conn, rows, seen - size_mismatched, counts, dry_run=dry_run)
        return listed, truncated

    def _compare_checksums(
        self,
        conn: Any,
        rows: Sequence[Any],
        seen: set[str],
        counts: dict[str, int],
        *,
        dry_run: bool,
    ) -> None:
        """`HEAD` each listed object and compare the recorded `sha256` metadata.

        The listing has no metadata, so this is one request per object. It runs with bounded
        concurrency and a fixed budget for the same reason cloud deletion does: a daily job must
        not turn a rate limit into a failure.
        """
        assert self._adapter is not None
        by_key = {str(row["cloud_key"]): row for row in rows}
        targets = [key for key in seen if key in by_key][: self._max_objects]
        if not targets:
            return
        adapter = self._adapter
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            heads = list(pool.map(lambda key: adapter.head_object(key=key), targets))
        for key, head in zip(targets, heads, strict=True):
            if head is None:
                # The listing had it and the HEAD did not: someone deleted it between the two,
                # which cloud retention does on its own schedule. Not drift.
                continue
            recorded = str(by_key[key]["sha256"] or "")
            stored = str((head.metadata or {}).get("sha256") or "")
            if recorded and stored and stored.lower() != recorded.lower():
                self._record(
                    conn,
                    "checksum_mismatch",
                    subject=key,
                    detail={
                        "clip_id": int(by_key[key]["id"]),
                        "reason": "sha256",
                        "recorded": recorded,
                        "object": stored,
                    },
                    counts=counts,
                    dry_run=dry_run,
                )

    # -- the local tree ------------------------------------------------------------------

    def _compare_local(self, conn: Any, counts: dict[str, int], *, dry_run: bool) -> int:
        clips_dir = self._data_dir / "clips"
        known: set[str] = set()
        after = 0
        missing: list[str] = []
        while True:
            batch = db.clip_rows_with_local_file(conn, limit=self._batch_size, after_id=after)
            if not batch:
                break
            for row in batch:
                relpath = str(row["path"])
                known.add(relpath)
                if not (self._data_dir / relpath).exists():
                    missing.append(relpath)
            after = int(batch[-1]["id"])

        for relpath in missing:
            self._record(
                conn,
                "missing_local_file",
                subject=relpath,
                detail={"reason": "the row has no local_deleted_at and no file"},
                counts=counts,
                dry_run=dry_run,
            )

        scanned = 0
        if not clips_dir.is_dir():
            return scanned
        for path in sorted(clips_dir.rglob("*.mp4")):
            scanned += 1
            relative = path.relative_to(self._data_dir).as_posix()
            if relative in known:
                continue
            self._record(
                conn,
                "unexpected_local_file",
                subject=relative,
                detail={"size": path.stat().st_size},
                counts=counts,
                dry_run=dry_run,
            )
        return scanned

    # -- writing -------------------------------------------------------------------------

    def _record(
        self,
        conn: Any,
        kind: str,
        *,
        subject: str,
        detail: Mapping[str, Any] | None,
        counts: dict[str, int],
        dry_run: bool,
    ) -> None:
        if dry_run:
            counts[kind] = counts.get(kind, 0) + 1
            return
        # One transaction per finding: findings are rare, and a long transaction over a 20 000
        # object walk would hold the write lock for the whole pass.
        with db.write_txn(conn):
            created = db.record_finding_once(
                conn, kind=kind, subject=subject, detail=dict(detail or {}), now=self._clock()
            )
        if created:
            counts[kind] = counts.get(kind, 0) + 1
            if self._logger is not None:
                self._logger.warning(
                    "reconciliation finding",
                    extra={"kind": kind, "subject": subject, "reason": dict(detail or {})},
                )


def sha256_of(path: Path) -> str:
    """A convenience used by tests and by anyone verifying a file by hand."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
