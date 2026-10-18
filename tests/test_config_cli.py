"""Tests for `cctv-config` — validate our config, and generate Frigate's from it.

[INV-3](../../docs/architecture/invariants.md) says camera configuration exists in exactly
one place and that everything else is generated, so the generated file is a *derived*
artefact with two properties that matter more than its contents:

**It is deterministic.** Same `cameras.yml`, byte-identical output. A generated file that
reorders itself makes every regeneration a diff, and a diff nobody reads is a diff nobody
reviews.

**It is obviously incomplete where it is incomplete.** The host profile is v0.1's
generic/CPU one; a config that is missing a profile is better than one that quietly picked
the wrong one, so the tests pin exactly what *is* emitted rather than that "something"
was.

`validate` runs the same projection as `generate` on purpose, which is why one of these
tests fails to start on a site that would lose footage.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from cctv.config import CamerasConfig, ConfigError, load_cameras_config
from cctv.config_cli import build_frigate_config, main, render

MAIN_URL = "rtsp://192.168.1.50:554/stream1"
SUB_URL = "rtsp://192.168.1.50:554/stream2"
#: Frigate's internal go2rtc restream. The camera's ffmpeg inputs read this, not the camera.
RESTREAM = "rtsp://127.0.0.1:8554"

HEAD = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
defaults:
  detect:
    objects: [person, car]
    min_score: 0.4
  clip:
    pre_roll_s: 5
    post_roll_s: 10
cameras:
"""

FRONT_DOOR = """\
front-door:
  rtsp:
    main: rtsp://192.168.1.50:554/stream1
    sub: rtsp://192.168.1.50:554/stream2
"""


def cameras(*blocks: str) -> str:
    """`HEAD` plus camera blocks, with indentation handled in one place.

    Splicing YAML by hand is how a test passes for the wrong reason — see `test_config.py`,
    where a first draft put `record:` at the wrong depth and YAML read it as a camera named
    `record`.
    """
    body = "".join(
        textwrap.indent(textwrap.dedent(block).strip("\n") + "\n", "  ") for block in blocks
    )
    return HEAD + body


def with_camera(extra: str, *, block: str = FRONT_DOOR) -> str:
    """Add camera-level keys to the last camera in `block`."""
    return HEAD + textwrap.indent(
        textwrap.dedent(block).strip("\n")
        + "\n"
        + textwrap.indent(textwrap.dedent(extra).strip("\n"), "  ")
        + "\n",
        "  ",
    )


BASE = cameras(FRONT_DOOR)
NO_SUB = cameras(FRONT_DOOR.replace(f"    sub: {SUB_URL}\n", ""))

#: Every camera retired. Written out rather than assembled, because the point of the test
#: that uses it is that the file is otherwise perfectly valid.
ONLY_DISABLED = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
cameras:
  front-door:
    enabled: false
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
"""


def make_config(tmp_path: Path, text: str = BASE) -> CamerasConfig:
    path = tmp_path / "cameras.yml"
    path.write_text(text, encoding="utf-8")
    return load_cameras_config(path, environ={})


def write(tmp_path: Path, text: str = BASE) -> Path:
    path = tmp_path / "cameras.yml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def entry(tmp_path: Path) -> dict[str, Any]:
    """The generated camera entry for the one camera in `BASE`."""
    return build_frigate_config(make_config(tmp_path))["cameras"]["front-door"]


# --------------------------------------------------------------------------------------
# Order — the file is read by a human before it is read by Frigate
# --------------------------------------------------------------------------------------


def test_the_file_leads_with_the_process_wide_settings(tmp_path: Path) -> None:
    """Pinned as a list, not as a set: the emitted file has to read top-down in the order an
    operator wants to check it, and `to_frigate` settles its keys in a different order.
    """
    out = build_frigate_config(make_config(tmp_path))

    assert list(out) == [
        "version",
        "mqtt",
        "go2rtc",
        "auth",
        "detectors",
        "objects",
        "cameras",
    ]


def test_a_camera_entry_is_the_projection_then_the_host_profile(
    entry: dict[str, Any],
) -> None:
    """`record` comes from `to_frigate`; the rest is the v0.1 profile, appended. Pinning the
    whole order is what makes a regeneration diff reviewable."""
    assert list(entry) == ["record", "ffmpeg", "live", "detect", "objects", "snapshots"]


def test_the_output_is_byte_identical_for_the_same_input(tmp_path: Path) -> None:
    path = write(tmp_path)

    first = render(load_cameras_config(path, environ={}), cameras_path=path)
    second = render(load_cameras_config(path, environ={}), cameras_path=path)

    assert first == second


def test_the_rendered_body_round_trips_to_the_built_config(tmp_path: Path) -> None:
    """The header is comment-only, so the file Frigate reads is exactly what this module
    built — not a re-serialisation that drifted."""
    config = make_config(tmp_path)

    rendered = render(config, cameras_path=tmp_path / "cameras.yml")

    assert yaml.safe_load(rendered) == build_frigate_config(config)


# --------------------------------------------------------------------------------------
# The host profile
# --------------------------------------------------------------------------------------


def test_a_sub_stream_becomes_the_detect_input_and_the_main_is_kept_for_record(
    entry: dict[str, Any],
) -> None:
    """The sub-stream is already the low-resolution stream the camera produces for this
    purpose, so decoding it is cheaper than downscaling the main one.

    Both inputs read the *local restream*, not the camera: that is what keeps the camera at
    two connections while go2rtc also feeds the live view."""
    assert entry["ffmpeg"] == {
        "inputs": [
            {
                "path": f"{RESTREAM}/front-door",
                "input_args": "preset-rtsp-restream",
                "roles": ["record"],
            },
            {
                "path": f"{RESTREAM}/front-door_sub",
                "input_args": "preset-rtsp-restream",
                "roles": ["detect"],
            },
        ]
    }


def test_without_a_sub_stream_one_input_takes_both_roles(tmp_path: Path) -> None:
    """Two inputs reading the same URL would be two decoders for one scene."""
    entry = build_frigate_config(make_config(tmp_path, NO_SUB))["cameras"]["front-door"]

    assert entry["ffmpeg"] == {
        "inputs": [
            {
                "path": f"{RESTREAM}/front-door",
                "input_args": "preset-rtsp-restream",
                "roles": ["record", "detect"],
            }
        ]
    }


def test_the_camera_urls_are_opened_by_go2rtc_not_by_a_second_ffmpeg_reader(
    tmp_path: Path,
) -> None:
    """Frigate's UI can only play a go2rtc stream; without one it silently falls back to
    jsmpeg on the *detect* role, which is the low-resolution sub stream. Emitting the main
    stream is what fixes that.

    It must not cost extra connections: if the ffmpeg inputs also opened the camera, the
    camera would see four connections instead of two, which is the limit
    `runbooks/install.md` warns about.
    """
    out = build_frigate_config(make_config(tmp_path))

    assert out["go2rtc"]["streams"] == {
        "front-door": MAIN_URL,
        "front-door_sub": SUB_URL,
    }
    paths = [source["path"] for source in out["cameras"]["front-door"]["ffmpeg"]["inputs"]]
    assert paths == [f"{RESTREAM}/front-door", f"{RESTREAM}/front-door_sub"]


def test_the_default_dashboard_plays_the_main_stream_first(tmp_path: Path) -> None:
    """Frigate's default "All Cameras" dashboard always plays the first entry, so `Main`
    has to be first or the high-quality stream is only reachable per camera."""
    entry = build_frigate_config(make_config(tmp_path))["cameras"]["front-door"]

    assert entry["live"] == {"streams": {"Main": "front-door", "Sub": "front-door_sub"}}
    assert next(iter(entry["live"]["streams"])) == "Main"


def test_without_a_sub_stream_there_is_only_a_main_live_stream(tmp_path: Path) -> None:
    out = build_frigate_config(make_config(tmp_path, NO_SUB))

    assert out["go2rtc"]["streams"] == {"front-door": MAIN_URL}
    assert out["cameras"]["front-door"]["live"] == {"streams": {"Main": "front-door"}}


def test_a_disabled_camera_gets_no_restream_and_no_live_stream(tmp_path: Path) -> None:
    """A retired camera contributes nothing to go2rtc either, so its URL does not linger in
    the generated file as dead plumbing."""
    text = cameras(
        FRONT_DOOR,
        """\
        retired:
          enabled: false
          rtsp:
            main: rtsp://192.168.1.52:554/stream1
        """,
    )
    out = build_frigate_config(make_config(tmp_path, text))

    assert "retired" not in out["go2rtc"]["streams"]
    assert "retired" not in out["cameras"]


@pytest.mark.parametrize("text", [BASE, NO_SUB])
def test_exactly_one_input_holds_each_role(tmp_path: Path, text: str) -> None:
    inputs = build_frigate_config(make_config(tmp_path, text))["cameras"]["front-door"]["ffmpeg"][
        "inputs"
    ]
    roles = [role for source in inputs for role in source["roles"]]

    assert roles.count("detect") == 1
    assert roles.count("record") == 1


def test_the_detect_block_carries_the_configured_resolution(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        with_camera("detect:\n  width: 640\n  height: 360\n  fps: 7\n  min_initialized: 3"),
    )

    entry = build_frigate_config(config)["cameras"]["front-door"]

    assert entry["detect"] == {
        "enabled": True,
        "width": 640,
        "height": 360,
        "fps": 7,
        "min_initialized": 3,
    }


def test_every_tracked_label_gets_the_threshold_it_was_configured_with(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        with_camera("detect:\n  objects: [person, car]\n  min_score: 0.55"),
    )

    entry = build_frigate_config(config)["cameras"]["front-door"]

    assert entry["objects"] == {
        "track": ["person", "car"],
        "filters": {"person": {"min_score": 0.55}, "car": {"min_score": 0.55}},
    }


def test_the_track_list_is_the_sorted_union_of_the_enabled_cameras(
    tmp_path: Path,
) -> None:
    """Frigate's `objects.track` is process-wide, so it is the union of every camera's
    labels. Sorted, because a set's iteration order is not stable across runs and this file
    has to be reproducible."""
    text = cameras(
        FRONT_DOOR,
        """\
        garage:
          rtsp:
            main: rtsp://192.168.1.51:554/stream1
          detect:
            objects: [dog, person]
        """,
    )
    out = build_frigate_config(make_config(tmp_path, text))

    assert out["objects"]["track"] == ["car", "dog", "person"]


def test_a_disabled_camera_contributes_nothing(tmp_path: Path) -> None:
    """Not its labels either: a tracked label with no camera behind it makes Frigate work for
    detections that can never arrive, and hides the fact that the camera is off."""
    text = cameras(
        FRONT_DOOR,
        """\
        retired:
          enabled: false
          rtsp:
            main: rtsp://192.168.1.52:554/stream1
          detect:
            objects: [person, horse]
        """,
    )
    out = build_frigate_config(make_config(tmp_path, text))

    assert list(out["cameras"]) == ["front-door"]
    assert "horse" not in out["objects"]["track"]


def test_snapshots_carry_the_retain_window(tmp_path: Path) -> None:
    config = make_config(tmp_path, with_camera("snapshots:\n  retain_days: 14"))

    entry = build_frigate_config(config)["cameras"]["front-door"]

    assert entry["snapshots"] == {"enabled": True, "retain": {"default": 14}}


def test_the_mqtt_and_auth_blocks_are_explicit(tmp_path: Path) -> None:
    """`auth.enabled: false` is only safe because Frigate's port 5000 is never published —
    the header says so, and a site that publishes it has a remote admin bypass."""
    out = build_frigate_config(make_config(tmp_path))

    assert out["mqtt"] == {"enabled": True, "host": "mosquitto", "port": 1883}
    assert out["auth"] == {"enabled": False}
    assert out["detectors"] == {"cpu": {"type": "cpu", "num_threads": 2}}
    assert out["version"] == "0.18-0"


def test_the_ui_login_is_emitted_when_the_site_asks_for_it(tmp_path: Path) -> None:
    """The switch has to survive regeneration. If it lived only in the generated file, the
    next `docker compose up` would silently turn the UI back into an open one."""
    text = "frigate:\n  auth:\n    enabled: true\n" + BASE

    out = build_frigate_config(make_config(tmp_path, text))

    assert out["auth"] == {"enabled": True}


def test_the_generated_file_carries_no_top_level_timezone(tmp_path: Path) -> None:
    """Frigate 0.18 refuses the whole config when a top-level `timezone` is present
    (`Extra inputs are not permitted`) and then starts in safe mode with zero cameras —
    observed live, which is how this test came to exist. `site.timezone` still drives our
    own clip paths; Frigate's zone comes from the container `TZ`.
    """
    out = build_frigate_config(make_config(tmp_path))

    assert "timezone" not in out
    # The cameras are what has to survive the removal. An absent key is trivially absent.
    assert list(out["cameras"]) == ["front-door"]


# --------------------------------------------------------------------------------------
# What `to_frigate` projected must survive the reassembly
# --------------------------------------------------------------------------------------


def test_a_visit_gap_is_projected_into_frigates_own_cutoff(
    tmp_path: Path,
) -> None:
    """`cutoff_time` is Frigate's merge window, so it defines what a visit *is* — by the time
    a review reaches us it is too late to regroup it. Ours must not be re-implemented.
    """
    config = make_config(tmp_path, with_camera("clip:\n  visit_gap_s: 45"))

    entry = build_frigate_config(config)["cameras"]["front-door"]

    assert entry["review"] == {
        "alerts": {"cutoff_time": 45},
        "detections": {"cutoff_time": 45},
    }


def test_no_visit_gap_key_is_emitted_when_none_was_configured(
    entry: dict[str, Any],
) -> None:
    """Frigate's own defaults are 40 s for alerts and 30 s for detections, and emitting ours
    would silently change behaviour the operator did not ask to change."""
    assert "review" not in entry


def test_frigate_side_retention_survives_the_reassembly(tmp_path: Path) -> None:
    """The claim that `build_frigate_config` is not silently lossy: a key `to_frigate` emits
    reaches the file even though this module reassembles the config around it."""
    config = make_config(tmp_path, with_camera("record:\n  mode: continuous"))

    entry = build_frigate_config(config)["cameras"]["front-door"]

    assert entry["record"]["continuous"]["days"] == 7


def test_a_record_mode_that_leaves_no_recovery_budget_is_refused(
    tmp_path: Path,
) -> None:
    """`record.mode: off` is a documented value, and with it Frigate keeps nothing: the
    clip-worker's outage window (INV-9) shrinks to zero and any missed review is lost
    footage. Refused at generation time rather than discovered during an outage."""
    config = make_config(tmp_path, with_camera("record:\n  mode: off"))

    with pytest.raises(ConfigError, match="min_headroom_days"):
        build_frigate_config(config)


# --------------------------------------------------------------------------------------
# The header
# --------------------------------------------------------------------------------------


def test_the_header_says_the_file_is_generated_and_derived(tmp_path: Path) -> None:
    path = write(tmp_path)

    rendered = render(load_cameras_config(path, environ={}), cameras_path=path)

    assert rendered.startswith("# GENERATED FILE — DO NOT EDIT.")
    assert str(path) in rendered  # names the file it was generated from
    assert "INV-3" in rendered  # editing this one is wasted work
    assert "INV-2" in rendered  # and it carries credentials, so it must not be committed


def test_the_header_warns_about_the_published_port(tmp_path: Path) -> None:
    """The one sentence standing between `auth.enabled: false` and a remote admin bypass,
    plus the pointer to the procedure that turns the login on."""
    rendered = render(make_config(tmp_path), cameras_path=tmp_path / "cameras.yml")

    assert "frigate.auth.enabled" in rendered
    assert "5000" in rendered
    assert "frigate-authentication.md" in rendered


# --------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------


def test_validate_reports_the_enabled_cameras_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    assert main(["validate", "--cameras", str(path)]) == 0

    out = capsys.readouterr().out
    assert "ok: 1 camera(s) enabled: front-door" in out
    assert "ok: timezone Asia/Kolkata" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_validate_refuses_a_site_that_would_lose_footage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason `validate` runs the projection rather than only parsing: this config parses
    perfectly well and cannot work."""
    text = with_camera("record:\n  mode: motion").replace(
        "defaults:\n", "min_headroom_days: 30\ndefaults:\n"
    )
    path = write(tmp_path, text)

    assert main(["validate", "--cameras", str(path)]) == 1

    assert "error:" in capsys.readouterr().err


def test_validate_refuses_a_config_with_no_enabled_cameras(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every camera retired, so nothing can ever be recorded. A start-up that succeeds and
    quietly records nothing is the worst of the options available here."""
    path = write(tmp_path, ONLY_DISABLED)

    assert main(["validate", "--cameras", str(path)]) == 1

    assert "no cameras are enabled" in capsys.readouterr().err


def test_validate_reports_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", "--cameras", str(tmp_path / "not-here.yml")]) == 1

    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------------------


def test_generate_writes_the_file_and_says_where(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path)
    out = tmp_path / "nested" / "frigate" / "config.yml"

    assert main(["generate", "--cameras", str(path), "--out", str(out)]) == 0

    assert capsys.readouterr().out.strip() == f"wrote {out}"
    assert yaml.safe_load(out.read_text(encoding="utf-8")) == build_frigate_config(
        load_cameras_config(path, environ={})
    )


def test_generate_writes_a_file_only_its_owner_can_read(tmp_path: Path) -> None:
    """RTSP URLs carry a username and password, so the file is 0600. This narrows who on the
    host can read it; it is not a substitute for keeping it out of version control."""
    path = write(tmp_path)
    out = tmp_path / "config.yml"

    main(["generate", "--cameras", str(path), "--out", str(out)])

    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_generate_tightens_an_existing_file(tmp_path: Path) -> None:
    """A 0644 file left by a previous version, or by hand, must not stay 0644 — `write_text`
    preserves the mode of an existing file."""
    path = write(tmp_path)
    out = tmp_path / "config.yml"
    out.write_text("stale", encoding="utf-8")
    out.chmod(0o644)

    main(["generate", "--cameras", str(path), "--out", str(out)])

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["cameras"]


def test_generate_refuses_an_unworkable_site(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = with_camera("record:\n  mode: off")
    path = write(tmp_path, text)
    out = tmp_path / "config.yml"

    assert main(["generate", "--cameras", str(path), "--out", str(out)]) == 1

    assert "error:" in capsys.readouterr().err
    assert not out.exists()


def test_a_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main([])

    assert "usage:" in capsys.readouterr().err
