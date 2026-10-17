"""Tests for reading ffprobe output, including the gap detector that does not work."""

from __future__ import annotations

import json

import pytest

from cctv import ffprobe

HEALTHY_CLIP = {
    "format": {"duration": "30.033000", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "avg_frame_rate": "25/1",
            "r_frame_rate": "25/1",
        },
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "packets": [
        {"pts_time": "0.000"},
        {"pts_time": "0.040"},
        # The measured 3.55 s jump in a clip that is completely fine, against a 2.55 s
        # keyframe interval. This is why nothing gates on the gap.
        {"pts_time": "3.590"},
        {"pts_time": "3.630"},
    ],
}


def test_parses_a_healthy_clip() -> None:
    facts = ffprobe.parse_probe(HEALTHY_CLIP)
    assert facts.duration_s == pytest.approx(30.033)
    assert facts.video_codec == "h264"
    assert facts.audio_codec == "aac"
    assert facts.has_audio is True
    assert facts.packet_count == 4
    assert facts.max_packet_gap_s == pytest.approx(3.55)
    assert facts.keyframe_interval_s == pytest.approx(0.04)


def test_a_clip_with_no_audio_stream_is_distinguishable() -> None:
    """'The camera has no microphone' and 'we dropped the audio' are different problems."""
    payload = {**HEALTHY_CLIP, "streams": [HEALTHY_CLIP["streams"][0]]}
    facts = ffprobe.parse_probe(payload)
    assert facts.has_audio is False
    assert facts.audio_codec is None
    assert facts.video_codec == "h264"


def test_a_missing_duration_stays_unmeasurable() -> None:
    """`None`, never `0.0`: zero would look like a catastrophically short clip."""
    facts = ffprobe.parse_probe({"format": {}, "streams": HEALTHY_CLIP["streams"]})
    assert facts.duration_s is None
    assert facts.coverage_against(45.0) is None


def test_an_unreadable_duration_string_is_unmeasurable() -> None:
    payload = {**HEALTHY_CLIP, "format": {"duration": "N/A"}}
    assert ffprobe.parse_probe(payload).duration_s is None


def test_empty_payload_does_not_raise() -> None:
    facts = ffprobe.parse_probe({})
    assert facts.duration_s is None
    assert facts.packet_count == 0
    assert facts.has_audio is False
    assert facts.max_packet_gap_s is None


# --------------------------------------------------------------------------------------
# The gap detector — advisory, and the tests say why
# --------------------------------------------------------------------------------------


def test_a_healthy_clip_has_a_gap_larger_than_the_keyframe_interval() -> None:
    """The measurement that invalidated packet-gap truncation detection.

    A 2.0 s threshold would flag this clip. So would a 3.0 s one. The likely cause is PTS
    discontinuities at segment joins, which `-c copy` reproduces because it re-encodes
    nothing.
    """
    facts = ffprobe.parse_probe(HEALTHY_CLIP)
    assert facts.max_packet_gap_s is not None
    assert facts.max_packet_gap_s > 3.0
    assert facts.keyframe_interval_s == pytest.approx(0.04)
    assert facts.max_packet_gap_s > facts.keyframe_interval_s * 80


def test_looks_truncated_ignores_the_gap_entirely() -> None:
    """The same clip with the same 3.55 s gap is judged on coverage and considered fine."""
    facts = ffprobe.parse_probe(HEALTHY_CLIP)
    assert ffprobe.looks_truncated(facts, requested_s=30.0) is False


def test_looks_truncated_flags_a_genuinely_short_clip() -> None:
    payload = {**HEALTHY_CLIP, "format": {"duration": "12.0"}}
    facts = ffprobe.parse_probe(payload)
    assert ffprobe.looks_truncated(facts, requested_s=45.0) is True


def test_looks_truncated_treats_unmeasurable_as_worth_retrying() -> None:
    facts = ffprobe.parse_probe({})
    assert ffprobe.looks_truncated(facts, requested_s=45.0) is True


def test_looks_truncated_accepts_erring_outward() -> None:
    payload = {**HEALTHY_CLIP, "format": {"duration": "50.0"}}
    facts = ffprobe.parse_probe(payload)
    assert ffprobe.looks_truncated(facts, requested_s=45.0) is False


def test_max_packet_gap_of_one_packet_is_unmeasurable_not_zero() -> None:
    """`0.0` would read as a passing check, which is the opposite of the truth."""
    assert ffprobe.max_packet_gap([]) is None
    assert ffprobe.max_packet_gap([1.0]) is None


def test_max_packet_gap_handles_out_of_order_timestamps() -> None:
    """Segment joins can present packets out of order; the gap is still the real gap."""
    assert ffprobe.max_packet_gap([3.0, 0.0, 1.0, 0.5]) == pytest.approx(2.0)


def test_packet_times_fall_back_to_dts() -> None:
    payload = {
        **HEALTHY_CLIP,
        "packets": [{"dts_time": "0.0"}, {"dts_time": "1.0"}, {"pts_time": "4.0"}],
    }
    facts = ffprobe.parse_probe(payload)
    assert facts.packet_count == 3
    assert facts.max_packet_gap_s == pytest.approx(3.0)


# --------------------------------------------------------------------------------------
# Command construction and coverage arithmetic
# --------------------------------------------------------------------------------------


def test_probe_command_is_an_argument_list() -> None:
    """A list, never a string: nothing is ever shell-interpreted."""
    command = ffprobe.build_probe_command("/data/clips/a b/18-45-00.mp4")
    assert isinstance(command, list)
    assert command[0] == "ffprobe"
    assert command[-1] == "/data/clips/a b/18-45-00.mp4"
    assert "-show_packets" in command


def test_coverage_ratio_is_none_rather_than_infinite() -> None:
    assert ffprobe.coverage_ratio(10.0, 0.0) is None
    assert ffprobe.coverage_ratio(None, 10.0) is None
    assert ffprobe.coverage_ratio(45.0, 45.0) == pytest.approx(1.0)


def test_probe_json_round_trips() -> None:
    """The parser takes what `ffprobe -of json` actually produces."""
    facts = ffprobe.parse_probe(json.loads(json.dumps(HEALTHY_CLIP)))
    assert facts.duration_s == pytest.approx(30.033)
