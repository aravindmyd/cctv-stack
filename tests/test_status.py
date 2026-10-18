"""Tests for the aggregate status collector (M5, lld/06).

The collector's job is judgement, so the tests are mostly about *what it says* rather than
what it returns: every one of lld/06 §3.3's conditions gets a fixture, and the headline is
asserted to name that specific problem. A status page that says "OK" while a camera is dead is
the failure this whole module exists to prevent, and it cannot be caught by asserting on state
strings alone.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cctv import db, health, status


class Clock:
    """A clock a test can move. Instant maths is otherwise untestable without sleeping."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


HEAD = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
defaults:
  detect:
    objects: [person]
  clip:
    pre_roll_s: 5
    post_roll_s: 10
cameras:
  front-door:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
  garage:
    rtsp:
      main: rtsp://192.168.1.51:554/stream1
"""


def make_config(tmp_path: Path):
    from cctv.config import load_cameras_config

    path = tmp_path / "cameras.yml"
    path.write_text(HEAD, encoding="utf-8")
    return load_cameras_config(path, environ={})


@pytest.fixture
def settings(tmp_path: Path) -> status.StatusSettings:
    return status.StatusSettings(
        data_dir=tmp_path,
        db_path=tmp_path / "cctv.db",
        collect_interval_s=30.0,
        disk_interval_s=0.0,
        mosquitto_host=None,
        component_urls={
            "clip-worker": "http://worker:8080",
            "uploader": "http://uploader:8082",
            "cleanup": "http://cleanup:8083",
        },
        camera_quiet_min_events_7d=5,
    )


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    """The collector reads real SQL, so the tests use a real database rather than a fake."""
    path = tmp_path / "cctv.db"
    conn = db.open_db(path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    return path


def healthy_payload(name: str, *, version: str | None = None) -> dict[str, Any]:
    return {
        "component": name,
        "version": version,
        "state": "HEALTHY",
        "summary": f"{name} is fine",
        "checks": [],
    }


def empty_backlog(**overrides: Any) -> db.QueueSummary:
    base: dict[str, Any] = dict(
        counts={},
        oldest_pending_age_s=None,
        uploaded_24h=0,
        last_successful_upload_at=None,
    )
    base.update(overrides)
    return db.QueueSummary(**base)


def summary(
    *,
    cameras: tuple[status.CameraStatus, ...] = (),
    components: tuple[status.ComponentStatus, ...] = (),
    disk: status.DiskFacts | None = None,
    projection: status.StorageProjection | None = None,
    backlog: db.QueueSummary | None = None,
    now: float = 1_700_000_000.0,
    unobserved: tuple[str, ...] = (),
) -> status.StatusSummary:
    return status.StatusSummary(
        site="parents-home",
        generated_at=now,
        components=components,
        cameras=cameras,
        disk=disk,
        projection=projection or status.StorageProjection(None, None, "no history"),
        backlog=backlog if backlog is not None else empty_backlog(),
        local_days=90,
        cloud_days=60,
        next_cleanup_at=None,
        data_dir="/data",
        unobserved=unobserved,
    )


HEALTHY_DISK = status.DiskFacts(
    free_bytes=400 * 1024**3,
    total_bytes=500 * 1024**3,
    clips_bytes=10 * 1024**3,
    media_bytes=0,
)


# ---------------------------------------------------------------------------------------
# The answer string. One test per condition, because "it returned a string" is not the claim.
# ---------------------------------------------------------------------------------------


def test_the_answer_names_the_offline_camera() -> None:
    s = summary(
        cameras=(status.CameraStatus(name="garage", state=status.OFFLINE),),
        disk=HEALTHY_DISK,
    )

    assert "garage" in s.answer
    assert "offline" in s.answer.lower()
    assert s.overall == health.UNHEALTHY


def test_the_answer_names_disk_critical() -> None:
    tiny = status.DiskFacts(
        free_bytes=2 * 1024**3, total_bytes=500 * 1024**3, clips_bytes=1, media_bytes=1
    )

    s = summary(disk=tiny, projection=status.StorageProjection(10.0, 1.0))

    assert "disk" in s.answer.lower()
    assert "3.0 GiB" in s.answer or "2.0 GiB" in s.answer
    assert s.overall == health.UNHEALTHY


def test_the_answer_names_clips_that_will_never_upload() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        backlog=empty_backlog(counts={"FAILED_PERMANENT": 2}),
    )

    assert "2" in s.answer
    assert "not reaching the cloud" in s.answer
    assert s.overall == health.DEGRADED


def test_the_answer_names_a_stalled_backup() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        backlog=empty_backlog(
            counts={"PENDING": 3},
            oldest_pending_age_s=4000.0,
            last_successful_upload_at=1_700_000_000.0 - 4000.0,
        ),
    )

    assert "Cloud backup stalled" in s.answer
    assert s.overall == health.DEGRADED


def test_the_answer_names_the_clip_worker_when_it_is_down() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        components=(
            status.ComponentStatus(name="clip-worker", state=health.HEALTHY),
            status.ComponentStatus(name="uploader", state=health.HEALTHY),
            status.ComponentStatus(name="cleanup", state=health.HEALTHY),
        ),
        cameras=(status.CameraStatus(name="front-door", state=status.ONLINE),),
    )

    assert "all working" in s.answer

    dead = status.ComponentStatus(name="clip-worker", state=health.UNHEALTHY)
    s2 = summary(disk=HEALTHY_DISK, components=(dead,))

    assert "clips are not being created" in s2.answer
    assert s2.overall == health.UNHEALTHY


def test_the_answer_names_retention_when_cleanup_is_unobserved() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        components=(status.ComponentStatus(name="cleanup", state=health.UNKNOWN),),
    )

    assert "Retention has not run recently" in s.answer


def test_the_answer_names_a_quiet_camera() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        cameras=(status.CameraStatus(name="garage", state=status.QUIET),),
    )

    assert "garage" in s.answer
    assert "no clips recently" in s.answer
    assert s.overall == health.DEGRADED


def test_the_answer_admits_when_frigate_cannot_be_observed() -> None:
    """Partial observation is stated, not glossed: no Frigate means no camera evidence."""
    s = summary(
        disk=HEALTHY_DISK,
        components=(
            status.ComponentStatus(
                name="frigate",
                state=health.UNHEALTHY,
                code="FRIGATE_UNREACHABLE",
                summary="Frigate did not answer /api/stats",
                observed=False,
            ),
        ),
        cameras=(status.CameraStatus(name="front-door", state=status.UNKNOWN_CAMERA),),
    )

    assert "cannot be verified" in s.answer
    assert s.overall == health.UNHEALTHY


def test_the_healthy_answer_says_what_is_working_not_just_ok() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        components=(
            status.ComponentStatus(name="clip-worker", state=health.HEALTHY),
            status.ComponentStatus(name="uploader", state=health.HEALTHY),
            status.ComponentStatus(name="cleanup", state=health.HEALTHY),
        ),
        cameras=(status.CameraStatus(name="front-door", state=status.ONLINE),),
    )

    assert s.answer != "OK"
    assert "Recording" in s.answer
    assert s.overall == health.HEALTHY


# ---------------------------------------------------------------------------------------
# Overall derivation
# ---------------------------------------------------------------------------------------


def test_overall_is_unknown_when_a_component_is_unobserved() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        components=(status.ComponentStatus(name="uploader", state=health.UNKNOWN),),
    )

    assert s.overall == health.UNKNOWN


def test_overall_is_unknown_without_a_disk_reading() -> None:
    assert summary().overall == health.UNKNOWN


def test_a_stale_backup_past_the_critical_window_is_unhealthy() -> None:
    s = summary(
        disk=HEALTHY_DISK,
        backlog=empty_backlog(
            counts={"PENDING": 1},
            last_successful_upload_at=1_700_000_000.0 - 7 * 3600,
        ),
    )

    assert s.overall == health.UNHEALTHY


def test_an_empty_backlog_is_never_called_stalled() -> None:
    """A fresh install with nothing to upload is healthy, not alarming."""
    s = summary(disk=HEALTHY_DISK)

    assert s.backlog_stalled is False
    assert s.overall == health.HEALTHY


# ---------------------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------------------


def test_a_camera_offline_within_the_grace_window_is_still_online(settings) -> None:
    camera = status.camera_status(
        name="garage",
        stats={"cameras": {"garage": {"camera_fps": 0.0, "detection_fps": 0.0}}},
        last_clip_at=None,
        clip_times=[],
        offline_since=1_700_000_000.0,
        now=1_700_000_000.0 + 60.0,
        settings=settings,
    )

    assert camera.state == status.ONLINE


def test_a_camera_offline_beyond_the_grace_window_is_offline(settings) -> None:
    camera = status.camera_status(
        name="garage",
        stats={"cameras": {"garage": {"camera_fps": 0.0, "detection_fps": 0.0}}},
        last_clip_at=None,
        clip_times=[],
        offline_since=1_700_000_000.0,
        now=1_700_000_000.0 + settings.camera_offline_grace_s + 1,
        settings=settings,
    )

    assert camera.state == status.OFFLINE
    assert camera.offline_since == 1_700_000_000.0


def test_a_silent_camera_becomes_quiet(settings) -> None:
    """Events stopped while the camera stayed online — the failure the page exists to catch."""
    now = 1_700_000_000.0
    times = [now - 10 * 86400 + i * 1800.0 for i in range(20)]
    last = times[-1]

    camera = status.camera_status(
        name="front-door",
        stats={"cameras": {"front-door": {"camera_fps": 10.0, "detection_fps": 5.0}}},
        last_clip_at=last - 3 * 86400.0,
        clip_times=[t - 3 * 86400.0 for t in times],
        offline_since=None,
        now=now,
        settings=settings,
    )

    assert camera.state == status.QUIET


def test_a_camera_with_little_history_is_exempt_from_quiet(settings) -> None:
    now = 1_700_000_000.0
    camera = status.camera_status(
        name="front-door",
        stats={"cameras": {"front-door": {"camera_fps": 10.0, "detection_fps": 5.0}}},
        last_clip_at=now - 30 * 86400.0,
        clip_times=[now - 30 * 86400.0],
        offline_since=None,
        now=now,
        settings=settings,
    )

    assert camera.state == status.ONLINE


# ---------------------------------------------------------------------------------------
# Storage projection
# ---------------------------------------------------------------------------------------


def test_projection_is_suppressed_without_enough_history() -> None:
    samples = [
        db.DiskSample(at=0.0, free_bytes=1, clips_bytes=0, media_bytes=0),
        db.DiskSample(at=86400.0, free_bytes=1, clips_bytes=1024**3, media_bytes=0),
    ]

    projection = status.storage_projection(samples, free_bytes=10 * 1024**3, min_days_history=3.0)

    assert projection.days_to_full is None
    assert projection.reason is not None
    assert "history" in projection.reason


def test_projection_computes_days_from_the_trailing_growth_rate() -> None:
    day = 86400.0
    samples = [
        db.DiskSample(at=0.0, free_bytes=0, clips_bytes=0, media_bytes=0),
        db.DiskSample(at=5 * day, free_bytes=0, clips_bytes=5 * 1024**3, media_bytes=0),
    ]

    projection = status.storage_projection(samples, free_bytes=10 * 1024**3, min_days_history=3.0)

    assert projection.growth_bytes_per_day == pytest.approx(1024**3)
    assert projection.days_to_full == pytest.approx(10.0)


def test_a_flat_disk_is_not_projected_to_fill() -> None:
    day = 86400.0
    samples = [
        db.DiskSample(at=0.0, free_bytes=0, clips_bytes=1024**3, media_bytes=0),
        db.DiskSample(at=5 * day, free_bytes=0, clips_bytes=1024**3, media_bytes=0),
    ]

    projection = status.storage_projection(samples, free_bytes=1024**3, min_days_history=3.0)

    assert projection.days_to_full is None
    assert projection.reason == "footage is not growing"


# ---------------------------------------------------------------------------------------
# Collection against fakes
# ---------------------------------------------------------------------------------------


def make_collector(
    tmp_path: Path,
    settings: status.StatusSettings,
    *,
    clock: Clock,
    health_by_name: Mapping[str, Mapping[str, Any]] | None = None,
    stats: Mapping[str, Any] | None = None,
    stats_raises: bool = False,
    disk: status.DiskFacts | None = None,
):
    config = make_config(tmp_path)
    payloads = dict(health_by_name or {})

    def probe_health(name: str, _url: str) -> Mapping[str, Any]:
        payload = payloads.get(name)
        if payload is None:
            raise OSError(f"{name} unreachable")
        return payload

    def probe_stats(_url: str) -> Mapping[str, Any]:
        if stats_raises:
            raise OSError("frigate unreachable")
        return stats or {"cameras": {"front-door": {}, "garage": {}}}

    return status.Collector(
        settings=settings,
        config=config,
        clock=clock,
        probe_health=probe_health,
        probe_stats=probe_stats,
        probe_disk=lambda _path: disk or HEALTHY_DISK,
        probe_mqtt=lambda _host, _port: True,
    )


def test_collection_reports_partial_observation(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    clock = Clock()
    collector = make_collector(
        tmp_path,
        settings,
        clock=clock,
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
        stats_raises=True,
    )

    result = collector.collect()

    assert result.component("frigate") is not None
    assert result.component("frigate").state == health.UNHEALTHY
    assert result.component("uploader").state == health.HEALTHY
    assert result.disk is not None
    assert any("frigate" in entry for entry in result.unobserved)
    # Nothing is known about the cameras, so they are not reported as online.
    assert {camera.state for camera in result.cameras} == {status.UNKNOWN_CAMERA}
    assert "cannot be verified" in result.answer


def test_a_component_with_no_health_endpoint_is_not_probed(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    """#1: clip-worker has no /health, so probing it reported UNKNOWN forever."""
    probed: list[str] = []

    def probe(name: str, _url: str) -> Mapping[str, Any]:
        probed.append(name)
        return healthy_payload(name)

    collector = status.Collector(
        settings=settings,
        config=make_config(tmp_path),
        clock=Clock(),
        probe_health=probe,
        probe_stats=lambda _url: {"cameras": {}},
        probe_disk=lambda _path: HEALTHY_DISK,
        probe_mqtt=lambda _host, _port: True,
    )

    result = collector.collect()

    assert "clip-worker" not in probed
    assert result.component("clip-worker") is not None
    assert result.component("clip-worker").state == health.HEALTHY
    assert result.component("clip-worker").summary == "no clips yet"


def test_clip_worker_goes_degraded_when_every_camera_with_history_is_quiet(
    settings: status.StatusSettings,
) -> None:
    now = 1_700_000_000.0
    times = [now - 10 * 86400 + i * 1800.0 for i in range(20)]
    # Both cameras produced events once, then stopped three days ago.
    camera = status.camera_status(
        name="front-door",
        stats={},
        last_clip_at=times[-1] - 3 * 86400.0,
        clip_times=[t - 3 * 86400.0 for t in times],
        offline_since=None,
        now=now,
        settings=settings,
    )
    assert camera.state == status.QUIET

    derived = status._clip_worker_component((camera,))

    assert derived.state == health.DEGRADED
    assert derived.code == "CAMERA_QUIET"
    assert derived.summary == "no clips are being produced"


def test_a_component_that_stops_reporting_becomes_unknown_not_healthy(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    clock = Clock()
    calls = {"n": 0}

    def probe(name: str, _url: str) -> Mapping[str, Any]:
        if name == "uploader":
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("gone")
        return healthy_payload(name)

    collector = status.Collector(
        settings=settings,
        config=make_config(tmp_path),
        clock=clock,
        probe_health=probe,
        probe_stats=lambda _url: {"cameras": {}},
        probe_disk=lambda _path: HEALTHY_DISK,
        probe_mqtt=lambda _host, _port: True,
    )

    first = collector.collect()
    assert first.component("uploader").state == health.HEALTHY

    second = collector.collect()
    # One miss is a blip: the last honest report stands.
    assert second.component("uploader").state == health.HEALTHY

    third = collector.collect()
    assert third.component("uploader").state == health.UNKNOWN
    assert third.component("uploader").observed is False


def test_a_version_mismatch_degrades_the_component(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    clock = Clock()
    collector = make_collector(
        tmp_path,
        settings,
        clock=clock,
        health_by_name={
            "uploader": healthy_payload("uploader", version="0.2.9"),
            "cleanup": healthy_payload("cleanup"),
        },
    )

    result = collector.collect()

    worker = result.component("uploader")
    assert worker.state == health.DEGRADED
    assert "0.2.9" in worker.summary


def test_mqtt_is_disabled_rather_than_unhealthy_when_not_configured(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    collector = make_collector(
        tmp_path,
        settings,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )

    result = collector.collect()

    assert result.component("mosquitto").state == health.DISABLED


def test_an_unreadable_database_is_reported_and_the_page_still_renders(
    tmp_path: Path, settings: status.StatusSettings
) -> None:
    broken = status.StatusSettings(
        data_dir=settings.data_dir,
        db_path=tmp_path / "missing-dir" / "cctv.db",
        disk_interval_s=0.0,
        mosquitto_host=None,
        component_urls=settings.component_urls,
    )
    collector = make_collector(
        tmp_path,
        broken,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )

    result = collector.collect()

    assert result.component("state-db").state == health.UNHEALTHY
    assert result.component("state-db").code == "DB_UNAVAILABLE"
    assert status.render_html(result, settings=broken)


# ---------------------------------------------------------------------------------------
# The HTTP surfaces
# ---------------------------------------------------------------------------------------


def test_the_json_and_the_page_cannot_disagree(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    collector = make_collector(
        tmp_path,
        settings,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )
    result = collector.collect()

    payload = result.to_dict()

    assert payload["answer"] == result.answer
    assert payload["overall"] == result.overall
    assert payload["generated_at"] == result.generated_at


def test_the_html_page_has_no_scripts_and_no_controls(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    collector = make_collector(
        tmp_path,
        settings,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )
    page = status.render_html(collector.collect(), settings=settings)

    assert "<script" not in page.lower()
    assert "<form" not in page.lower()
    assert "<button" not in page.lower()
    # The answer is the headline, above the details.
    assert page.index("Recording") < page.index("<table")


def test_status_is_served_as_json_or_html_on_the_same_path() -> None:
    page = "<!doctype html><title>CCTV</title>"
    server, _thread = health.start_health_server(
        port=0,
        host="127.0.0.1",
        snapshot=lambda: health.HealthReport(component="status-collector", checks=()),
        status=lambda: {"overall": "HEALTHY", "answer": "fine"},
        status_html=lambda: page,
    )
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["overall"] == "HEALTHY"

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/status", headers={"Accept": "text/html"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers["Content-Type"].startswith("text/html")
            assert response.read().decode() == page
    finally:
        server.shutdown()
        server.server_close()


def test_the_status_route_is_absent_on_a_service_that_does_not_mount_it() -> None:
    server, _thread = health.start_health_server(
        port=0,
        host="127.0.0.1",
        snapshot=lambda: health.HealthReport(component="uploader", checks=()),
    )
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5)
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------------------
# The collector's own health
# ---------------------------------------------------------------------------------------


def test_the_health_report_uses_the_contract_check_names(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    from cctv.status_service import StatusService

    collector = make_collector(
        tmp_path,
        settings,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )
    service = StatusService(settings=settings, config=make_config(tmp_path), collector=collector)
    service.collect_once()

    names = {check.name for check in service.report().checks}

    assert names == {"state_db", "frigate_api", "mqtt", "collector_freshness"}


def test_a_stale_collection_makes_the_collector_unhealthy(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    from cctv.status_service import StatusService

    clock = Clock()
    collector = make_collector(
        tmp_path,
        settings,
        clock=clock,
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )
    service = StatusService(
        settings=settings,
        config=make_config(tmp_path),
        collector=collector,
        clock=clock,
    )
    service.collect_once()

    clock.advance(10 * settings.collect_interval_s)
    report = service.report()

    freshness = next(c for c in report.checks if c.name == "collector_freshness")
    assert freshness.state == health.UNHEALTHY
    assert freshness.code == "COLLECTOR_STALE"
    assert report.state == health.UNHEALTHY


def test_the_gauges_have_bounded_cardinality(
    tmp_path: Path, settings: status.StatusSettings, migrated: Path
) -> None:
    from cctv.status_service import StatusService

    collector = make_collector(
        tmp_path,
        settings,
        clock=Clock(),
        health_by_name={
            "clip-worker": healthy_payload("clip-worker"),
            "uploader": healthy_payload("uploader"),
            "cleanup": healthy_payload("cleanup"),
        },
    )
    service = StatusService(settings=settings, config=make_config(tmp_path), collector=collector)
    service.collect_once()

    gauges = service.gauges()

    assert "cctv_disk_free_bytes" in gauges
    assert any(key.startswith('cctv_camera_online{camera="') for key in gauges)
    # Nothing carries a clip path or an event id, which would grow without bound.
    assert not any("/clips/" in key for key in gauges)
