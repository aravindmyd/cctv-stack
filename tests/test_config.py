"""Tests for the config model. INV-3 plus the four documented schema rules."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from cctv import config
from cctv.config import ConfigError

BASE = """\
site:
  location: parents-home
  timezone: Asia/Kolkata
defaults:
  detect:
    objects: [person]
  clip:
    pre_roll_s: 5
    post_roll_s: 10
  retention:
    local_days: 90
    cloud_days: 60
cameras:
  front-door:
    rtsp:
      main: rtsp://192.168.1.50:554/stream1
      sub: rtsp://192.168.1.50:554/stream2
"""


def load(tmp_path: Path, text: str = BASE, **kwargs):
    path = tmp_path / "cameras.yml"
    path.write_text(text, encoding="utf-8")
    kwargs.setdefault("environ", {})
    return config.load_cameras_config(path, **kwargs)


def with_camera(extra: str, *, head: str = BASE) -> str:
    """Append camera-level keys to `front-door`, with indentation handled in one place.

    Splicing YAML by hand is how a test passes for the wrong reason: the first draft of
    this file put `record:` at the wrong depth, and YAML cheerfully read it as a camera
    named `record`.
    """
    return head + textwrap.indent(textwrap.dedent(extra).strip("\n") + "\n", "    ")


# --------------------------------------------------------------------------------------
# Rule 1: unknown keys are a hard error
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        BASE + "bogus_top_level: 1\n",
        BASE.replace("  front-door:", "  front-door:\n    bogs: 1"),
        BASE.replace("    objects: [person]", "    objects: [person]\n    bogus: 1"),
        BASE.replace("    pre_roll_s: 5", "    pre_roll_s: 5\n    preroll: 5"),
    ],
)
def test_unknown_keys_are_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, text)


def test_unknown_key_error_names_the_key_and_where(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, BASE.replace("    pre_roll_s: 5", "    preroll_s: 5"))
    message = str(excinfo.value)
    assert "preroll_s" in message
    assert "cameras.front-door.clip" in message


# --------------------------------------------------------------------------------------
# Rule 2: lists replace, they do not concatenate
# --------------------------------------------------------------------------------------


def test_camera_object_list_replaces_the_default(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        BASE
        + """\
  garage:
    rtsp:
      main: rtsp://192.168.1.51:554/stream1
    detect:
      objects: [person, car]
""",
    )
    assert cfg.cameras["garage"].detect.objects == ("person", "car")
    assert cfg.cameras["front-door"].detect.objects == ("person",)


def test_overriding_one_key_does_not_disturb_its_siblings(tmp_path: Path) -> None:
    cfg = load(tmp_path, BASE.replace("    post_roll_s: 10", "    post_roll_s: 25"))
    clip = cfg.cameras["front-door"].clip
    assert clip.post_roll_s == 25
    assert clip.pre_roll_s == 5  # inherited, not reset


# --------------------------------------------------------------------------------------
# Rule 3: null means "use the inherited value"
# --------------------------------------------------------------------------------------


def test_null_inherits_rather_than_resets(tmp_path: Path) -> None:
    """`post_roll_s: null` on a camera keeps the default's 10s.

    Dropping the key instead of skipping it would make `null` mean "reset to the built-in
    default", which is a different and much more surprising thing.
    """
    cfg = load(tmp_path, with_camera("clip:\n  post_roll_s: null"))
    assert cfg.cameras["front-door"].clip.post_roll_s == 10
    assert cfg.cameras["front-door"].clip.pre_roll_s == 5


def test_null_inside_defaults_falls_back_to_the_built_in_value(tmp_path: Path) -> None:
    """In `defaults` there is nothing to inherit, so `null` means "not set here".

    A "motion" block written as a single-line null is a realistic typo, and the two
    levels must not behave differently.
    """
    text = BASE.replace("  clip:\n    pre_roll_s: 5\n    post_roll_s: 10\n", "  clip: null\n")
    cfg = load(tmp_path, text)
    assert cfg.cameras["front-door"].clip.pre_roll_s == 5
    assert cfg.cameras["front-door"].clip.post_roll_s == 10


# --------------------------------------------------------------------------------------
# Rule 4: an unresolved ${VAR} is a hard error, never an empty string
# --------------------------------------------------------------------------------------


def test_env_reference_is_resolved(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        BASE.replace(
            "main: rtsp://192.168.1.50:554/stream1",
            "main: rtsp://${CAM_USER}:${CAM_PASS}@192.168.1.50:554/stream1",
        ),
        environ={"CAM_USER": "admin", "CAM_PASS": "hunter2"},
    )
    assert cfg.cameras["front-door"].rtsp_main == "rtsp://admin:hunter2@192.168.1.50:554/stream1"


def test_missing_env_reference_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(
            tmp_path,
            BASE.replace(
                "main: rtsp://192.168.1.50:554/stream1",
                "main: rtsp://${CAM_USER}@192.168.1.50:554/stream1",
            ),
            environ={},
        )
    message = str(excinfo.value)
    assert "CAM_USER" in message
    assert ".env" in message


def test_env_reference_is_never_replaced_with_empty_string(tmp_path: Path) -> None:
    """The whole point: an empty password must fail loudly, not authenticate oddly."""
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            BASE.replace(
                "main: rtsp://192.168.1.50:554/stream1",
                "main: rtsp://${CAM_PASS}@192.168.1.50:554/stream1",
            ),
            environ={"UNRELATED": "x"},
        )


# --------------------------------------------------------------------------------------
# site
# --------------------------------------------------------------------------------------


def test_timezone_is_required_and_never_guessed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("site.timezone is required")):
        load(tmp_path, BASE.replace("  timezone: Asia/Kolkata\n", ""))


def test_timezone_offset_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="UTC offset"):
        load(tmp_path, BASE.replace("timezone: Asia/Kolkata", "timezone: '+05:30'"))


def test_differing_display_timezone_warns_but_loads(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        BASE.replace(
            "  timezone: Asia/Kolkata",
            "  timezone: Asia/Kolkata\n  display_timezone: Australia/Sydney",
        ),
    )
    assert cfg.site.timezone.key == "Asia/Kolkata"
    assert cfg.site.display_timezone.key == "Australia/Sydney"
    assert any("display_timezone" in w for w in cfg.warnings)


def test_display_timezone_defaults_to_timezone(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.site.display_timezone.key == cfg.site.timezone.key
    assert cfg.warnings == ()


def test_invalid_location_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="location"):
        load(tmp_path, BASE.replace("location: parents-home", "location: Parents Home"))


# --------------------------------------------------------------------------------------
# frigate — process-wide Frigate settings, kept out of camera inheritance
# --------------------------------------------------------------------------------------


def test_the_frigate_ui_login_is_off_by_default(tmp_path: Path) -> None:
    """Off is the shipped default; `docs/frigate-ui.md` is how it goes on."""
    assert load(tmp_path).frigate_auth.enabled is False


def test_the_frigate_ui_login_can_be_enabled(tmp_path: Path) -> None:
    cfg = load(tmp_path, "frigate:\n  auth:\n    enabled: true\n" + BASE)

    assert cfg.frigate_auth.enabled is True


def test_a_frigate_auth_typo_is_refused(tmp_path: Path) -> None:
    """Rule 1 applies here too: `enable:` would otherwise leave the UI open while the
    operator believes it is locked."""
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, "frigate:\n  auth:\n    enable: true\n" + BASE)


def test_frigate_auth_needs_a_real_boolean(tmp_path: Path) -> None:
    """YAML 1.2 is in force, so `yes` is the string `"yes"` and is refused rather than
    guessed at."""
    with pytest.raises(ConfigError, match="must be"):
        load(tmp_path, "frigate:\n  auth:\n    enabled: yes\n" + BASE)


# --------------------------------------------------------------------------------------
# Cross-field rules
# --------------------------------------------------------------------------------------


def test_clip_objects_must_be_tracked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("clip.objects")):
        load(tmp_path, with_camera("clip:\n  objects: [car]"))


def test_notify_labels_must_be_tracked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("notify.labels")):
        load(tmp_path, with_camera("notify:\n  labels: [car]"))


def test_empty_severities_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("clip.severities is empty")):
        load(tmp_path, with_camera("clip:\n  severities: []"))


def test_unknown_severity_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("clip.severities")):
        load(tmp_path, with_camera("clip:\n  severities: [alert, alarm]"))


def test_invalid_record_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("record.mode")):
        load(tmp_path, with_camera("record:\n  mode: always"))


def test_mode_off_is_the_string_off_not_a_boolean(tmp_path: Path) -> None:
    """PyYAML implements YAML 1.1, where `off` is `False` — and `off` is our documented value.

    Without a 1.2 loader this raises "False is not one of [...]", which points at the mode
    rather than at the parser, and is a genuinely baffling message to receive.
    """
    cfg = load(tmp_path, with_camera("record:\n  mode: off"))
    assert cfg.cameras["front-door"].record.mode == "off"


@pytest.mark.parametrize("word", ["on", "off", "yes", "no"])
def test_yaml_11_boolean_words_stay_strings(tmp_path: Path, word: str) -> None:
    cfg = load(
        tmp_path,
        BASE.replace("  front-door:", f"  front-door:\n    display_name: '{word}'"),
    )
    assert cfg.cameras["front-door"].display_name == word


def test_real_booleans_still_work(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_camera("enabled: false"))
    assert cfg.cameras["front-door"].enabled is False


def test_yaml_11_boolean_words_are_refused_where_a_boolean_is_wanted(
    tmp_path: Path,
) -> None:
    """`enabled: no` must fail loudly rather than be guessed at."""
    with pytest.raises(ConfigError, match="must be `true` or `false`"):
        load(tmp_path, with_camera("snapshots:\n  enabled: no"))


def test_enabled_camera_requires_a_main_stream(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("rtsp.main is required")):
        load(tmp_path, BASE.replace("    main: rtsp://192.168.1.50:554/stream1\n", ""))


def test_bad_rtsp_scheme_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="rtsp://"):
        load(
            tmp_path,
            BASE.replace("rtsp://192.168.1.50:554/stream1", "http://192.168.1.50/stream1"),
        )


def test_detection_on_main_stream_warns(tmp_path: Path) -> None:
    cfg = load(tmp_path, BASE.replace("      sub: rtsp://192.168.1.50:554/stream2\n", ""))
    assert any("rtsp.sub" in w for w in cfg.warnings)


def test_retention_bounds_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("retention.local_days")):
        load(tmp_path, BASE.replace("    local_days: 90", "    local_days: 0"))


def test_invalid_camera_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Camera name"):
        load(tmp_path, BASE.replace("  front-door:", "  Front-Door:"))


def test_no_cameras_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least one camera"):
        load(tmp_path, BASE[: BASE.index("cameras:")] + "cameras: {}\n")


# --------------------------------------------------------------------------------------
# The review projection — the bridge from `visit_gap_s` to Frigate
# --------------------------------------------------------------------------------------


def test_visit_gap_is_projected_into_both_cutoff_keys(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_camera("clip:\n  visit_gap_s: 120"))
    generated = config.to_frigate(cfg)
    review = generated["cameras"]["front-door"]["review"]
    assert review["alerts"]["cutoff_time"] == 120
    assert review["detections"]["cutoff_time"] == 120


def test_visit_gap_absent_emits_no_review_key(tmp_path: Path) -> None:
    """Leaving it unset must let Frigate's own 40/30 defaults stand, not impose ours."""
    generated = config.to_frigate(load(tmp_path))
    assert "review" not in generated["cameras"]["front-door"]


def test_visit_gap_bounds_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="visit_gap_s"):
        load(tmp_path, with_camera("clip:\n  visit_gap_s: 1"))


def test_pre_roll_over_thirty_seconds_warns(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_camera("clip:\n  pre_roll_s: 45"))
    assert any("pre_roll_s" in w for w in cfg.warnings)


def test_clip_objects_do_not_reach_frigate(tmp_path: Path) -> None:
    """`clip.objects` filters at our layer, so Frigate's own review list stays complete."""
    cfg = load(tmp_path, with_camera("clip:\n  objects: [person]"))
    generated = config.to_frigate(cfg)
    assert "objects" not in generated["cameras"]["front-door"]
    assert "label" not in str(generated["cameras"]["front-door"].get("review", {}))


# --------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------


def test_generated_config_is_deterministic(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert config.to_frigate(cfg) == config.to_frigate(cfg)
    assert list(config.to_frigate(cfg)["cameras"]) == ["front-door"]


def test_the_site_timezone_is_not_pushed_into_frigates_config(tmp_path: Path) -> None:
    """Frigate 0.18 has no top-level `timezone` key and refuses the entire config if one is
    present, starting in safe mode with no cameras.

    Regression guard: the key is absent on purpose, not by oversight.
    """
    assert "timezone" not in config.to_frigate(load(tmp_path))


def test_disabled_camera_is_omitted_from_frigate(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        BASE
        + """\
  old-garage:
    enabled: false
    rtsp:
      main: rtsp://192.168.1.99:554/stream1
""",
    )
    assert "old-garage" not in config.to_frigate(cfg)["cameras"]
    assert "old-garage" not in cfg.enabled_cameras()
    assert cfg.cameras["old-garage"].rtsp_main == "rtsp://192.168.1.99:554/stream1"


def test_recording_off_is_refused_as_insufficient_headroom(tmp_path: Path) -> None:
    """With no Frigate-side recordings there is nothing to cut a clip from (INV-9)."""
    cfg = load(tmp_path, with_camera("record:\n  mode: off"))
    with pytest.raises(ConfigError, match="min_headroom_days"):
        config.to_frigate(cfg)


def test_short_headroom_can_be_accepted_explicitly(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_camera("record:\n  mode: off"))
    generated = config.to_frigate(cfg, allow_short_headroom=True)
    assert generated["cameras"]["front-door"]["record"] == {"enabled": False}


def test_default_recording_is_two_days_of_continuous_footage(tmp_path: Path) -> None:
    """[ADR-0022] Two days of everything, so a quiet incident still has video."""
    cfg = load(tmp_path)
    record = config.to_frigate(cfg)["cameras"]["front-door"]["record"]

    assert cfg.cameras["front-door"].record.mode == "continuous"
    assert record["continuous"]["days"] == 2
    # Motion footage outlives the 24/7 churn, which is the point of keeping both.
    assert record["motion"]["days"] == 7
    assert record["alerts"]["retain"]["days"] == 30
    assert record["detections"]["retain"]["days"] == 14


def test_motion_mode_is_still_available_and_keeps_three_days(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_camera("record:\n  mode: motion"))
    record = config.to_frigate(cfg)["cameras"]["front-door"]["record"]
    assert record["continuous"]["days"] == 0
    assert record["motion"]["days"] == 3


@pytest.mark.parametrize("mode", ["motion", "continuous", "alerts", "detections"])
def test_every_enabled_mode_has_a_non_zero_window(tmp_path: Path, mode: str) -> None:
    cfg = load(tmp_path, with_camera(f"record:\n  mode: {mode}"))
    assert config.to_frigate(cfg)["cameras"]["front-door"]["record"]["enabled"] is True


def test_multiple_cameras_do_not_leak_settings(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        BASE
        + """\
  garage:
    rtsp:
      main: rtsp://192.168.1.51:554/stream1
    clip:
      visit_gap_s: 90
""",
    )
    generated = config.to_frigate(cfg)
    assert generated["cameras"]["garage"]["review"]["alerts"]["cutoff_time"] == 90
    assert "review" not in generated["cameras"]["front-door"]
