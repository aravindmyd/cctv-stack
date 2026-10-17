"""The uploader state machine against a **real** object store (M3.4, M3.5, M3.10, M3.12).

These are the M3 exit criteria the unit suite could not certify: the unit tests prove the
state machine against an in-process fake, which is strong evidence about our logic and no
evidence about real HTTP, real concurrency, or a real provider's error responses. Here the
adapter is `S3StorageAdapter` talking to a running server, one clip is a real file with a
real SHA-256, and the "crash" is a closed connection.

The fault injection in `FlakyAdapter` wraps a **real** adapter rather than replacing it, so
a failure it injects is the only fake part: when the outage ends, the recovery path performs
genuine uploads to genuine storage.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from cctv import db, storage, uploader

pytestmark = pytest.mark.integration

CAMERA = "front-door"
LOCATION = "integration"

SLOW = (os.environ.get("CCTV_TEST_SLOW") or "").strip().lower() in {"1", "true", "yes"}


def relpath(index: int) -> str:
    return f"clips/{CAMERA}/2026/09/12/18-45-{index:02d}.mp4"


class FlakyAdapter:
    """A real adapter that can be taken offline, so the outage is the only pretend part."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.down = False
        self.puts = 0

    def _guard(self) -> None:
        if self.down:
            raise storage.TransientStorageError("simulated provider outage")

    def put_object(self, **kwargs: Any) -> Any:
        self._guard()
        self.puts += 1
        return self._inner.put_object(**kwargs)

    def head_object(self, **kwargs: Any) -> Any:
        self._guard()
        return self._inner.head_object(**kwargs)

    def delete_object(self, **kwargs: Any) -> Any:
        self._guard()
        return self._inner.delete_object(**kwargs)

    def list_objects(self, **kwargs: Any) -> Any:
        self._guard()
        return self._inner.list_objects(**kwargs)

    def presign_get(self, **kwargs: Any) -> Any:
        self._guard()
        return self._inner.presign_get(**kwargs)

    def healthcheck(self) -> Any:
        self._guard()
        return self._inner.healthcheck()


def local_files(live: Any) -> list[Path]:
    return [live.data_dir / str(live.row(clip_id)["path"]) for clip_id in live.created]


# ---------------------------------------------------------------------------------------
# M3.1 / M3.2 — the key is deterministic and equals the documented layout
# ---------------------------------------------------------------------------------------


def test_a_clip_lands_at_the_deterministic_cloud_key(live: Any) -> None:
    clip_id, _ = live.add_clip(review_id="rev-key")

    outcomes = live.drain()

    expected = f"{LOCATION}/clips/{CAMERA}/2026/09/12/18-45-00.mp4"
    assert len(outcomes) == 1 and outcomes[0].ok
    assert outcomes[0].cloud_key == expected
    assert live.object_keys() == [expected], "the key is <location>/clips/ + the local path"
    assert live.row(clip_id)["cloud_key"] == expected
    assert live.row(clip_id)["upload_state"] == "UPLOADED"


# ---------------------------------------------------------------------------------------
# M3.4 — an outage, then recovery, with nothing duplicated
# ---------------------------------------------------------------------------------------


def test_an_outage_then_recovery_uploads_every_clip_exactly_once(live: Any) -> None:
    """[M3.4] the provider is unreachable for simulated hours; every clip lands afterwards.

    The local file is the only copy while the cloud is down, so [INV-1] is asserted here as
    well as in the unit suite: an outage must never cost a clip, and recovery must never
    produce a second object for one that already uploaded.
    """
    total = 8
    for index in range(total):
        live.add_clip(review_id=f"rev-{index}", relpath=relpath(index))

    flaky = FlakyAdapter(live.adapter)
    live.rebuild(flaky)

    # --- the provider is down, and clips keep arriving ---------------------------------
    flaky.down = True
    for _ in range(4):
        live.claim_all()
        live.clock.advance(3600)

    assert live.object_keys() == [], "nothing reached the cloud while it was unreachable"
    assert flaky.puts == 0
    assert set(live.states()) == {"RETRY_WAIT"}, "every clip is held, not failed"
    assert all(path.exists() for path in local_files(live)), "[INV-1]: no local clip was lost"
    assert all(live.row(cid)["uploaded_at"] is None for cid in live.created)

    # --- the provider recovers ---------------------------------------------------------
    flaky.down = False
    live.drain()

    keys = live.object_keys()
    assert len(keys) == total, "every clip was uploaded on recovery"
    assert len(set(keys)) == total, "and no clip produced a second object"
    assert set(live.states()) == {"UPLOADED"}
    assert flaky.puts == total, "one successful PUT per clip, and not one more"


def test_a_lease_held_when_a_worker_dies_is_reclaimed(live: Any) -> None:
    """A worker killed mid-upload must not wedge a clip forever.

    The claim takes a lease; the next worker reclaims it once the lease expires. Without
    this, one `kill -9` would strand a clip until an operator intervened.
    """
    clip_id, _ = live.add_clip(review_id="rev-lease")

    # Claim without doing the work, exactly as a worker killed after claiming would.
    row = db.claim_next_upload(
        live.conn,
        owner="doomed-worker",
        lease_seconds=live.settings.lease_seconds,
        cloud_key_prefix=f"{LOCATION}/",
        now=live.clock(),
    )
    assert row is not None
    assert live.row(clip_id)["upload_state"] == "UPLOADING"

    live.clock.advance(live.settings.lease_seconds + 1)
    live.drain()

    assert live.row(clip_id)["upload_state"] == "UPLOADED"
    assert len(live.object_keys()) == 1


# ---------------------------------------------------------------------------------------
# M3.10 — a restart mid-backlog resumes without re-uploading
# ---------------------------------------------------------------------------------------


def test_a_restart_mid_backlog_resumes_without_reuploading(live: Any) -> None:
    total = 6
    for index in range(total):
        live.add_clip(review_id=f"rev-{index}", relpath=relpath(index))

    for _ in range(2):  # two clips before the process dies
        outcome = live.uploader.upload_one(live.conn, owner="integration:1")
        assert outcome is not None and outcome.ok
    completed = {cid: live.row(cid)["uploaded_at"] for cid in live.created[:2]}
    assert len(live.object_keys()) == 2

    live.reopen()  # a restart: new connection, same database and bucket
    live.drain()

    assert len(live.object_keys()) == total
    assert set(live.states()) == {"UPLOADED"}
    for clip_id, uploaded_at in completed.items():
        assert live.row(clip_id)["uploaded_at"] == uploaded_at, (
            "a clip that finished before the restart keeps its original upload time: it was "
            "not silently uploaded a second time"
        )
    assert live.claim_all() == [], "a further pass has nothing left to do"


# ---------------------------------------------------------------------------------------
# M3.5 — a rejected credential stops, and preserves the clip
# ---------------------------------------------------------------------------------------


def test_a_revoked_credential_stops_instead_of_retrying_forever(
    live: Any, endpoint: str, region: str
) -> None:
    clip_id, target = live.add_clip(review_id="rev-auth")
    rejected = storage.S3StorageAdapter(
        bucket=live.bucket,
        access_key_id="revoked-key",
        secret_access_key="revoked-secret",
        endpoint_url=endpoint,
        region=region,
    )
    live.rebuild(rejected)

    outcome = live.uploader.upload_one(live.conn, owner="integration:1")

    assert outcome is not None and outcome.status == uploader.PERMANENT
    row = live.row(clip_id)
    assert row["upload_state"] == "FAILED_PERMANENT"
    assert row["last_error_class"] == "auth", "a 403 is classified as auth, not transient"
    assert row["next_attempt_at"] is None, "and is not scheduled for another attempt"
    assert target.exists(), "[INV-1]: the local file is preserved for a human to resolve"
    assert live.object_keys() == []


# ---------------------------------------------------------------------------------------
# M3.12 — many workers, many clips, exactly once
# ---------------------------------------------------------------------------------------


def upload_concurrently(live: Any, *, clips: int, workers: int) -> None:
    for index in range(clips):
        live.add_clip(
            review_id=f"rev-{index:04d}",
            relpath=f"clips/{CAMERA}/2026/09/12/bulk-{index:04d}.mp4",
            body=f"clip number {index}".encode(),
        )

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            connection = db.open_db(live.settings.db_path)
            try:
                while True:
                    outcome = live.uploader.upload_one(connection, owner=f"worker:{index}")
                    if outcome is None:
                        return
            finally:
                connection.close()
        except BaseException as exc:  # re-raised on the test thread below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads), "a worker did not finish"

    keys = live.object_keys()
    assert len(keys) == clips, f"{clips} clips in, {len(keys)} objects out"
    assert len(set(keys)) == clips, "no clip was uploaded twice"
    assert set(live.states()) == {"UPLOADED"}
    rows = live.conn.execute(
        "SELECT COUNT(*) AS n FROM clips WHERE upload_state = 'UPLOADED'"
    ).fetchone()
    assert int(rows["n"]) == clips


def test_four_workers_upload_forty_clips_exactly_once(live: Any) -> None:
    upload_concurrently(live, clips=40, workers=4)


@pytest.mark.slow
def test_full_scale_eight_workers_five_hundred_clips(live: Any) -> None:
    """[M3.12] the criterion as written. Opt in with `CCTV_TEST_SLOW=1`."""
    if not SLOW:
        pytest.skip("set CCTV_TEST_SLOW=1 to run the 8 x 500 soak against the live endpoint")
    upload_concurrently(live, clips=500, workers=8)
