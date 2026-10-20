"""Tests for M7 recovery: `clips` (footage back) and `rebuild` (bookkeeping back).

Two properties carry the whole design and are asserted more than once each, because they are
the ones whose failure loses data rather than time:

* a rebuild never guesses `UPLOADED` — a clip whose history is unknown is `PENDING`, so the
  uploader sends it again to a deterministic key and the provider deduplicates (INV-7);
* a restore never writes outside the clips tree, and never leaves a half-clip at a canonical
  path (a truncated file that looks complete would upload back over a good object).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cctv import config, db, storage, timeutil
from cctv.recover import (
    RECOVERED_REVIEW_PREFIX,
    RecoverError,
    main,
    reconstruct,
    restore_clips,
)

NOW = 1_790_000_000.0
DAY = 86400.0
CAMERA = "front-door"
OTHER_CAMERA = "gate"
LOCATION = "parents-home"

CAMERAS = f"""\
site:
  location: {LOCATION}
  timezone: UTC
cameras:
  {CAMERA}:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
  {OTHER_CAMERA}:
    rtsp:
      main: rtsp://192.168.1.60:554/stream1
      sub: rtsp://192.168.1.60:554/stream2
"""

PREFIX = f"{LOCATION}/clips/"


class FakeBucket:
    """A bucket that can be listed, `HEAD`ed and downloaded. Never written to."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.downloads: list[str] = []
        self.heads: list[str] = []
        self.puts: list[str] = []
        self.deletes: list[str] = []
        self.fail_downloads: set[str] = set()
        self.truncate_downloads: set[str] = set()

    def add(
        self,
        relpath: str,
        *,
        body: bytes = b"a plausible clip",
        metadata: dict[str, str] | None = None,
        last_modified: float | None = None,
    ) -> str:
        key = PREFIX + relpath
        self.objects[key] = {
            "body": body,
            "metadata": dict(metadata or {}),
            "last_modified": last_modified,
        }
        return key

    def add_key(self, key: str, *, body: bytes = b"x") -> str:
        self.objects[key] = {"body": body, "metadata": {}, "last_modified": None}
        return key

    def list_objects(self, *, prefix: str, max_keys: int = 1000) -> Any:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                entry = self.objects[key]
                yield storage.ObjectInfo(
                    key=key,
                    size=len(entry["body"]),
                    last_modified=entry["last_modified"],
                )

    def head_object(self, *, key: str) -> storage.ObjectInfo | None:
        self.heads.append(key)
        entry = self.objects.get(key)
        if entry is None:
            return None
        return storage.ObjectInfo(
            key=key,
            size=len(entry["body"]),
            metadata=dict(entry["metadata"]),
            last_modified=entry["last_modified"],
        )

    def download_object(self, *, key: str, destination: Any) -> int:
        self.downloads.append(key)
        if key in self.fail_downloads:
            destination.write(b"partial")
            raise storage.TransientStorageError(f"{key} reset the connection")
        body = self.objects[key]["body"]
        if key in self.truncate_downloads:
            body = body[: len(body) // 2]
        destination.write(body)
        return len(body)

    # Recovery must never write to the bucket.
    def put_object(self, **kwargs: Any) -> Any:  # pragma: no cover - asserted below
        self.puts.append(str(kwargs.get("key")))
        raise AssertionError("recovery must never upload")

    def delete_object(self, *, key: str) -> Any:  # pragma: no cover - asserted below
        self.deletes.append(key)
        raise AssertionError("recovery must never delete")

    def presign_get(self, *, key: str, ttl_s: int) -> str:  # pragma: no cover - unused
        return key

    def healthcheck(self) -> Any:  # pragma: no cover - unused
        raise AssertionError("recovery must never probe health")


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True)
        self.clips_dir = self.data_dir / "clips"
        self.db_path = self.data_dir / "cctv.db"
        self.cameras_path = root / "cameras.yml"
        self.cameras_path.write_text(CAMERAS, encoding="utf-8")
        self.config = config.load_cameras_config(self.cameras_path, environ={})
        self.bucket = FakeBucket()
        conn = db.open_db(self.db_path)
        db.migrate(conn)
        conn.close()

    # -- arrange -------------------------------------------------------------------------

    def write_clip(
        self,
        relpath: str,
        *,
        body: bytes = b"a plausible clip",
        sidecar: dict[str, Any] | None = None,
    ) -> Path:
        target = self.clips_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if sidecar is not None:
            sidecar_path = target.with_name(target.name + ".json")
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        return target

    def sidecar_for(
        self,
        relpath: str,
        *,
        review_id: str = "r1",
        start: float | None = None,
        sha256: str | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "review_id": review_id,
            "camera": relpath.split("/")[0],
            "severity": "alert",
            "review_start_time": NOW - 10 * DAY if start is None else start,
            "review_end_time": (NOW - 10 * DAY if start is None else start) + 20,
            "duration_s": 21.5,
            "coverage_ratio": 1.02,
            "has_audio": True,
            "audio_codec": "aac",
            "video_codec": "h264",
            "event_ids": ["e-1", "e-2"],
            "zones": ["driveway"],
            "path": f"clips/{relpath}",
        }
        if sha256 is not None:
            payload["sha256"] = sha256
        payload.update(overrides)
        return payload

    # -- act -----------------------------------------------------------------------------

    def restore(self, **kwargs: Any) -> Any:
        return restore_clips(
            data_dir=self.data_dir,
            location=LOCATION,
            adapter=self.bucket,
            clock=lambda: NOW,
            **kwargs,
        )

    def rebuild(self, **kwargs: Any) -> Any:
        kwargs.setdefault("adapter", self.bucket)
        conn = db.open_db(self.db_path)
        try:
            db.migrate(conn)
            return reconstruct(
                conn,
                data_dir=self.data_dir,
                config=self.config,
                clock=lambda: NOW,
                **kwargs,
            )
        finally:
            conn.close()

    # -- assert --------------------------------------------------------------------------

    def rows(self) -> list[Any]:
        conn = db.open_db(self.db_path, readonly=True)
        try:
            return list(conn.execute("SELECT * FROM clips ORDER BY path"))
        finally:
            conn.close()

    def files(self) -> list[str]:
        if not self.clips_dir.is_dir():
            return []
        return sorted(
            p.relative_to(self.clips_dir).as_posix()
            for p in self.clips_dir.rglob("*")
            if p.is_file()
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(timeutil, "now", lambda: NOW)
    return Env(tmp_path)


# ---------------------------------------------------------------------------------------
# clips — the footage
# ---------------------------------------------------------------------------------------


def test_every_object_lands_at_the_path_its_key_predicts(env: Env) -> None:
    for hour in ("00-00-00", "00-05-00", "00-10-00"):
        env.bucket.add(f"{CAMERA}/2026/01/01/{hour}.mp4")

    result = env.restore()

    assert result.downloaded == 3
    assert result.failed == 0
    assert env.files() == [
        f"{CAMERA}/2026/01/01/00-00-00.mp4",
        f"{CAMERA}/2026/01/01/00-05-00.mp4",
        f"{CAMERA}/2026/01/01/00-10-00.mp4",
    ]


def test_the_bytes_on_disk_are_the_bytes_in_the_bucket(env: Env) -> None:
    body = b"the exact clip bytes"
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=body)

    env.restore()

    assert (env.clips_dir / f"{CAMERA}/2026/01/01/00-00-00.mp4").read_bytes() == body


def test_an_object_already_present_at_the_right_size_is_not_fetched_again(env: Env) -> None:
    body = b"already here"
    env.write_clip(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=body)
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=body)

    result = env.restore()

    assert result.skipped == 1
    assert result.downloaded == 0
    assert env.bucket.downloads == []
    assert result.bytes_already_present == len(body)


def test_a_file_of_the_wrong_size_is_replaced_rather_than_trusted(env: Env) -> None:
    env.write_clip(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=b"short")
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=b"the full clip")

    result = env.restore()

    assert result.downloaded == 1
    assert (env.clips_dir / f"{CAMERA}/2026/01/01/00-00-00.mp4").read_bytes() == b"the full clip"


def test_a_failed_download_leaves_nothing_at_the_canonical_path(env: Env) -> None:
    key = env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4")
    env.bucket.fail_downloads.add(key)

    result = env.restore()

    assert result.failed == 1
    assert key in result.failures
    assert env.files() == []
    assert list(env.data_dir.rglob("*.tmp*")) == []


def test_one_unreachable_object_does_not_abandon_the_others(env: Env) -> None:
    bad = env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4")
    env.bucket.add(f"{CAMERA}/2026/01/01/00-05-00.mp4")
    env.bucket.add(f"{CAMERA}/2026/01/01/00-10-00.mp4")
    env.bucket.fail_downloads.add(bad)

    result = env.restore()

    assert result.failed == 1
    assert result.downloaded == 2
    assert env.files() == [
        f"{CAMERA}/2026/01/01/00-05-00.mp4",
        f"{CAMERA}/2026/01/01/00-10-00.mp4",
    ]


def test_a_truncated_download_is_kept_and_reported(env: Env) -> None:
    key = env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=b"1234567890")
    env.bucket.truncate_downloads.add(key)

    result = env.restore()

    # The file that arrived is real bytes of a real clip. Deleting it would destroy the only
    # copy of whatever it is over a provider metadata disagreement.
    assert (env.clips_dir / f"{CAMERA}/2026/01/01/00-00-00.mp4").read_bytes() == b"12345"
    assert key in result.failures
    assert "12345" in result.failures[key] or "5 arrived" in result.failures[key]


def test_a_dry_run_writes_nothing_and_still_reports_the_volume(env: Env) -> None:
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=b"x" * 1000)
    env.bucket.add(f"{CAMERA}/2026/01/01/00-05-00.mp4", body=b"x" * 500)

    result = env.restore(dry_run=True)

    assert result.dry_run
    assert result.downloaded == 2
    assert result.bytes_downloaded == 1500
    assert env.bucket.downloads == []
    assert env.files() == []


def test_a_key_that_climbs_out_of_the_tree_is_refused(env: Env) -> None:
    env.bucket.add_key(f"{PREFIX}{CAMERA}/../../../../etc/passwd")
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4")

    result = env.restore()

    assert len(result.refused) == 1
    assert result.downloaded == 1
    assert not (env.root / "etc").exists()


def test_an_object_outside_the_prefix_is_refused(env: Env) -> None:
    env.bucket.add_key("somebody-else/clips/front-door/2026/01/01/00-00-00.mp4")

    result = env.restore()

    assert result.listed == 0
    assert result.downloaded == 0
    assert result.refused == ()


def test_a_non_canonical_key_is_still_downloaded_and_reported(env: Env) -> None:
    env.bucket.add("front-door/2026/01/01/00-00-00.mp4.tmp-123")
    missing = env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4")
    env.bucket.add_key(f"{PREFIX}notes-from-the-installer.txt")

    result = env.restore()

    # Footage is footage: nothing is thrown away, and the odd ones are named.
    assert result.downloaded == 3
    assert f"{PREFIX}notes-from-the-installer.txt" in result.not_canonical
    assert missing in env.bucket.downloads


def test_the_listing_cap_is_reported_rather_than_silent(env: Env) -> None:
    for hour in range(5):
        env.bucket.add(f"{CAMERA}/2026/01/01/0{hour}-00-00.mp4")

    result = env.restore(max_objects=3)

    assert result.listed == 3
    assert result.truncated
    assert result.downloaded == 3


def test_a_site_with_no_bucket_is_told_where_its_protection_lives(env: Env) -> None:
    with pytest.raises(RecoverError) as caught:
        restore_clips(data_dir=env.data_dir, location=LOCATION, adapter=None)

    assert "--include-clips" in str(caught.value)


def test_restoring_never_touches_the_bucket(env: Env) -> None:
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4")

    env.restore()

    assert env.bucket.puts == []
    assert env.bucket.deletes == []


# ---------------------------------------------------------------------------------------
# rebuild — the bookkeeping
# ---------------------------------------------------------------------------------------


def test_a_clip_with_both_copies_is_recorded_uploaded(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    body = b"both copies exist"
    env.write_clip(relpath, body=body, sidecar=env.sidecar_for(relpath, review_id="r-both"))
    env.bucket.add(relpath, body=body, last_modified=NOW - 5 * DAY)

    result = env.rebuild()

    assert result.both == 1
    (row,) = env.rows()
    assert row["upload_state"] == "UPLOADED"
    assert row["review_id"] == "r-both"
    assert row["cloud_key"] == PREFIX + relpath
    assert row["path"] == f"clips/{relpath}"


def test_a_cloud_only_clip_is_uploaded_and_marked_locally_deleted(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.bucket.add(
        relpath,
        body=b"the bucket is the only copy",
        metadata={
            "review-id": "r-cloud",
            "camera": CAMERA,
            "start-time": f"{NOW - 20 * DAY:.6f}",
            "end-time": f"{NOW - 20 * DAY + 30:.6f}",
        },
        last_modified=NOW - 20 * DAY,
    )

    result = env.rebuild()

    assert result.cloud_only == 1
    (row,) = env.rows()
    # DELETED_LOCAL, which is what "the object is the last copy" genuinely means — and it is
    # what stops INV-1 being violated by a cleanup pass looking for a file to unlink.
    assert row["upload_state"] == "DELETED_LOCAL"
    assert row["local_deleted_at"] is not None
    assert row["review_id"] == "r-cloud"


def test_a_local_only_clip_is_pending_never_uploaded(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.write_clip(relpath, sidecar=env.sidecar_for(relpath, review_id="r-local"))

    result = env.rebuild()

    assert result.local_only == 1
    (row,) = env.rows()
    # The safe direction: the uploader will send it to a deterministic key, idempotently
    # (INV-7). The unsafe direction would claim a backup that does not exist.
    assert row["upload_state"] == "PENDING"
    assert row["cloud_key"] is None
    assert row["uploaded_at"] is None


def test_the_sidecar_supplies_what_object_metadata_cannot(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.write_clip(relpath, sidecar=env.sidecar_for(relpath, review_id="r-fields"))

    env.rebuild()

    (row,) = env.rows()
    assert row["severity"] == "alert"
    assert row["has_audio"] == 1
    assert row["audio_codec"] == "aac"
    assert row["duration_s"] == pytest.approx(21.5)
    assert json.loads(row["event_ids"]) == ["e-1", "e-2"]
    assert json.loads(row["zones"]) == ["driveway"]
    assert row["sidecar_path"] == f"clips/{relpath}.json"


def test_the_object_metadata_reconstructs_the_row_when_the_disk_is_gone(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.bucket.add(
        relpath,
        metadata={
            "review-id": "r-meta",
            "camera": CAMERA,
            "location": LOCATION,
            "start-time": f"{NOW - 3 * DAY:.6f}",
            "end-time": f"{NOW - 3 * DAY + 42:.6f}",
            "label": "person",
            "zones": "driveway,porch",
            "sha256": "a" * 64,
        },
        last_modified=NOW - 3 * DAY,
    )

    env.rebuild()

    (row,) = env.rows()
    assert row["review_id"] == "r-meta"
    assert row["start_time"] == pytest.approx(NOW - 3 * DAY)
    assert row["end_time"] == pytest.approx(NOW - 3 * DAY + 42)
    assert row["label"] == "person"
    assert json.loads(row["zones"]) == ["driveway", "porch"]
    # Object metadata has no severity, so a cloud-only clip defaults to the quieter answer.
    assert row["severity"] == "detection"


def test_the_object_is_headed_because_a_listing_carries_no_metadata(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    key = env.bucket.add(relpath)

    env.rebuild()

    assert env.bucket.heads == [key]


def test_the_hash_of_a_local_file_is_recomputed_not_read_from_the_sidecar(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    body = b"the actual bytes"
    lying = "0" * 64
    env.write_clip(relpath, body=body, sidecar=env.sidecar_for(relpath, sha256=lying))

    env.rebuild()

    (row,) = env.rows()
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["sha256"] != lying


def test_a_cloud_only_row_carries_the_recorded_hash_and_is_counted_as_unverified(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.bucket.add(relpath, metadata={"review-id": "r1", "sha256": "b" * 64})

    result = env.rebuild()

    assert result.unverified_hashes == 1
    (row,) = env.rows()
    assert row["sha256"] == "b" * 64


def test_uploaded_at_comes_from_the_object_not_from_now(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.bucket.add(relpath, metadata={"review-id": "r1"}, last_modified=NOW - 40 * DAY)

    env.rebuild()

    (row,) = env.rows()
    # Cloud retention expires on `uploaded_at` (lld/05 §4.3). A rebuild that stamped `now`
    # would restart every object's life and quietly extend it by another `cloud_days`.
    assert row["uploaded_at"] == pytest.approx(NOW - 40 * DAY)


def test_a_clip_with_no_start_time_anywhere_is_read_from_its_path(env: Env) -> None:
    # No sidecar and no object: the path is the only thing left, and it *is* local time.
    env.write_clip(f"{CAMERA}/2026/01/01/00-00-00.mp4")

    result = env.rebuild()

    assert result.derived_start_time == 1
    (row,) = env.rows()
    assert row["start_time"] == pytest.approx(1767225600.0)  # 2026-01-01T00:00:00Z
    assert row["review_id"].startswith(RECOVERED_REVIEW_PREFIX)


def test_a_file_whose_path_cannot_be_read_as_a_time_is_reported_not_invented(env: Env) -> None:
    env.clips_dir.mkdir(parents=True, exist_ok=True)
    (env.clips_dir / "junk.mp4").write_bytes(b"not a clip")

    result = env.rebuild()

    assert result.unusable == ("clips/junk.mp4",)
    assert env.rows() == []


def test_a_sidecar_without_its_clip_is_reported(env: Env) -> None:
    env.clips_dir.mkdir(parents=True, exist_ok=True)
    target = env.clips_dir / f"{CAMERA}/2026/01/01/00-00-00.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_name(target.name + ".json").write_text("{}", encoding="utf-8")

    result = env.rebuild()

    assert result.orphan_sidecars == (f"{CAMERA}/2026/01/01/00-00-00.mp4",)
    assert env.rows() == []


def test_a_camera_no_longer_in_the_configuration_is_left_alone(env: Env) -> None:
    env.cameras_path.write_text(
        CAMERAS.replace(
            f"""  {OTHER_CAMERA}:
    rtsp:
      main: rtsp://192.168.1.60:554/stream1
      sub: rtsp://192.168.1.60:554/stream2
""",
            "",
        ),
        encoding="utf-8",
    )
    env.config = config.load_cameras_config(env.cameras_path, environ={})
    env.write_clip(f"{OTHER_CAMERA}/2026/01/01/00-00-00.mp4")

    result = env.rebuild()

    assert result.unknown_camera == (f"clips/{OTHER_CAMERA}/2026/01/01/00-00-00.mp4",)
    # INV-3: the camera table is generated from cameras.yml, so nothing invents a camera.
    assert env.rows() == []
    assert (env.clips_dir / f"{OTHER_CAMERA}/2026/01/01/00-00-00.mp4").exists()


def test_rebuilding_refuses_a_database_that_already_has_rows(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.write_clip(relpath, sidecar=env.sidecar_for(relpath, review_id="r-existing"))
    env.rebuild()

    with pytest.raises(RecoverError) as caught:
        env.rebuild()

    assert "restore.sh state" in str(caught.value)


def test_into_existing_adds_only_the_clips_it_does_not_have(env: Env) -> None:
    first = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    second = f"{CAMERA}/2026/01/01/00-05-00.mp4"
    env.write_clip(first, sidecar=env.sidecar_for(first, review_id="r-first"))
    env.rebuild()
    env.write_clip(second, sidecar=env.sidecar_for(second, review_id="r-second"))

    result = env.rebuild(allow_existing=True)

    assert result.already_present == 1
    assert result.local_only == 1
    assert [row["review_id"] for row in env.rows()] == ["r-first", "r-second"]


def test_a_second_rebuild_does_not_duplicate_anything(env: Env) -> None:
    for hour in ("00-00-00", "00-05-00"):
        relpath = f"{CAMERA}/2026/01/01/{hour}.mp4"
        env.write_clip(relpath, sidecar=env.sidecar_for(relpath, review_id=f"r-{hour}"))
    env.rebuild()

    result = env.rebuild(allow_existing=True)

    assert result.already_present == 2
    assert len(env.rows()) == 2


def test_a_dry_run_writes_no_rows_but_reports_the_categories(env: Env) -> None:
    both = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.write_clip(both)
    env.bucket.add(both)
    env.bucket.add(f"{CAMERA}/2026/01/01/00-05-00.mp4", metadata={"review-id": "r-cloud"})

    result = env.rebuild(dry_run=True)

    assert result.both == 1
    assert result.cloud_only == 1
    assert env.rows() == []


def test_a_local_only_site_rebuilds_from_disk_and_says_the_cloud_was_skipped(env: Env) -> None:
    relpath = f"{CAMERA}/2026/01/01/00-00-00.mp4"
    env.write_clip(relpath, sidecar=env.sidecar_for(relpath, review_id="r-local"))

    result = env.rebuild(adapter=None)

    assert result.skipped_cloud
    assert result.local_only == 1
    assert env.rows()[0]["upload_state"] == "PENDING"


def test_the_listing_cap_is_reported(env: Env) -> None:
    for hour in range(4):
        env.bucket.add(f"{CAMERA}/2026/01/01/0{hour}-00-00.mp4", metadata={"review-id": f"r{hour}"})

    result = env.rebuild(max_objects=2)

    assert result.truncated
    assert result.objects == 2


def test_rebuilding_never_uploads_or_deletes(env: Env) -> None:
    env.write_clip(f"{CAMERA}/2026/01/01/00-00-00.mp4")
    env.bucket.add(f"{CAMERA}/2026/01/01/00-05-00.mp4", metadata={"review-id": "r-cloud"})

    env.rebuild()

    assert env.bucket.puts == []
    assert env.bucket.deletes == []


# ---------------------------------------------------------------------------------------
# The round trip, and the CLI
# ---------------------------------------------------------------------------------------


def test_restore_then_rebuild_recreates_the_rows_the_original_had(env: Env) -> None:
    """Scenario B end to end: the local tree is gone, the bucket is not."""
    relpaths = [f"{CAMERA}/2026/01/0{day}/00-00-00.mp4" for day in (1, 2, 3)]
    for index, relpath in enumerate(relpaths):
        env.bucket.add(
            relpath,
            body=f"clip {index}".encode(),
            metadata={
                "review-id": f"r-{index}",
                "camera": CAMERA,
                "start-time": f"{NOW - (index + 1) * DAY:.6f}",
                "end-time": f"{NOW - (index + 1) * DAY + 20:.6f}",
            },
            last_modified=NOW - (index + 1) * DAY,
        )

    restored = env.restore()
    env.rebuild()

    assert restored.downloaded == 3
    rows = env.rows()
    assert [row["path"] for row in rows] == [f"clips/{relpath}" for relpath in relpaths]
    assert {row["upload_state"] for row in rows} == {"UPLOADED"}
    assert [row["review_id"] for row in rows] == ["r-0", "r-1", "r-2"]


def test_the_cli_reports_a_dry_run_without_credentials(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.bucket.add(f"{CAMERA}/2026/01/01/00-00-00.mp4", body=b"x" * 100)
    monkeypatch.setenv("CCTV_DATA_DIR", str(env.data_dir))
    monkeypatch.setenv("CCTV_CONFIG_DIR", str(env.root))
    monkeypatch.setenv("CCTV_DB", str(env.db_path))

    def fake_adapter(environ: Any = None) -> Any:
        return env.bucket

    monkeypatch.setattr("cctv.recover._build_adapter", fake_adapter)

    code = main(["clips", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "would download" in out or "1 would download" in out
    assert env.bucket.downloads == []


def test_the_cli_says_which_configuration_file_is_missing(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCTV_DATA_DIR", str(env.data_dir))
    monkeypatch.setenv("CCTV_CONFIG_DIR", str(env.root / "elsewhere"))
    monkeypatch.setenv("CCTV_DB", str(env.db_path))

    code = main(["clips"])

    assert code == 78
    assert "restore.sh config" in capsys.readouterr().err
