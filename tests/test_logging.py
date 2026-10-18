"""Tests for the logging layer.

[INV-2](../../internal/architecture/invariants.md) is the whole point: no secret ever reaches a
log sink, including the ones that ship logs off the machine. Three things here are
load-bearing, and each has a way of looking correct while being broken:

1. `redact_url` must strip the userinfo of a camera URL whose **password contains an `@`**.
   A camera password is chosen by a person, so `p@ssw0rd` is a password that occurs.
2. The filter must redact `record.args` and not only `record.msg`, because
   `logger.info("connecting to %s", url)` is the idiomatic call and keeps the credential in
   the arguments until the formatter joins them.
3. A level that is not a level must raise. A typo'd level that silently becomes INFO is a
   log level the operator believes they set.

Every test that logs reads its own buffer, so the assertions are about what a *sink* would
receive rather than about what the call site passed.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cctv.logging import LOG_LEVEL_ENV, configure_logging, redact_url

PASSWORD = "hunter2"
USER = "cctv"
HOST = "192.168.1.50"
CAMERA_URL = f"rtsp://{USER}:{PASSWORD}@{HOST}:554/stream1"


def emit(
    message: object,
    *args: object,
    level: int = logging.INFO,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Log one line into a buffer and return the parsed JSON object."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    logging.getLogger("cctv.test").log(level, message, *args, extra=extra)
    return parse_only_line(stream)


def parse_only_line(stream: io.StringIO) -> dict[str, Any]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one log line, got {lines!r}"
    return json.loads(lines[0])


# --------------------------------------------------------------------------------------
# redact_url
# --------------------------------------------------------------------------------------


def test_the_credential_is_removed_and_the_host_survives() -> None:
    redacted = redact_url(CAMERA_URL)

    assert PASSWORD not in redacted
    assert USER not in redacted
    assert HOST in redacted
    assert redacted == f"rtsp://***@{HOST}:554/stream1"


def test_a_password_containing_an_at_sign_leaves_no_fragment_behind() -> None:
    """`userinfo@host` is split on the **last** `@`, because a password may contain one.

    A rule that stops at the first `@` replaces `user:p@` and publishes the tail of the
    password — which is worse than not redacting at all, because the line *looks* redacted.
    """
    redacted = redact_url("rtsp://admin:p@ssw0rd@cam.local/stream")

    assert "ssw0rd" not in redacted
    assert "admin" not in redacted
    assert "cam.local" in redacted


def test_a_url_without_userinfo_is_left_alone() -> None:
    assert redact_url(f"rtsp://{HOST}:554/stream1") == f"rtsp://{HOST}:554/stream1"


def test_a_bare_host_and_port_is_not_mistaken_for_userinfo() -> None:
    """The scheme is required. Otherwise this would be redacted into nonsense."""
    assert redact_url("192.168.1.50:554") == "192.168.1.50:554"


def test_every_url_in_a_message_is_redacted() -> None:
    """A message is a sentence, and a sentence can carry two cameras."""
    text = f"trying {CAMERA_URL} then rtsp://other:secret@cam.local/live"

    redacted = redact_url(text)

    assert PASSWORD not in redacted
    assert "secret" not in redacted
    assert HOST in redacted
    assert "cam.local" in redacted


def test_a_string_that_is_not_a_url_is_untouched() -> None:
    assert redact_url("cameras.front-door.clip.post_roll_s must be positive") == (
        "cameras.front-door.clip.post_roll_s must be positive"
    )


def test_the_scheme_is_kept_so_the_line_still_says_what_it_was() -> None:
    """The host and the scheme are the useful parts; only the credential goes."""
    assert redact_url(CAMERA_URL).startswith("rtsp://***@")


# --------------------------------------------------------------------------------------
# The filter — the message *and* the arguments
# --------------------------------------------------------------------------------------


def test_a_credential_interpolated_into_the_message_is_redacted() -> None:
    payload = emit(f"connecting to {CAMERA_URL}")

    assert PASSWORD not in json.dumps(payload)
    assert HOST in payload["msg"]


def test_a_credential_passed_as_an_argument_is_redacted() -> None:
    """The idiomatic call, and the one a message-only filter misses entirely: the credential
    sits in `record.args` until the formatter joins them."""
    payload = emit("connecting to %s", CAMERA_URL)

    assert PASSWORD not in json.dumps(payload)
    assert payload["msg"] == f"connecting to rtsp://***@{HOST}:554/stream1"


def test_a_credential_passed_as_a_mapping_argument_is_redacted() -> None:
    payload = emit("connecting to %(url)s", {"url": CAMERA_URL})

    assert PASSWORD not in json.dumps(payload)
    assert HOST in payload["msg"]


def test_arguments_that_are_not_strings_are_left_alone() -> None:
    """The filter walks the args, so a non-string has to survive the walk unchanged.
    `redact_url` applied to everything would raise on the int, and that exception would be
    swallowed by the logging machinery — losing the line and printing a warning nobody
    reads."""
    payload = emit("%s after %d attempts", CAMERA_URL, 3)

    assert payload["msg"] == f"rtsp://***@{HOST}:554/stream1 after 3 attempts"


def test_a_mapping_argument_keeps_its_non_string_values() -> None:
    payload = emit("clip %(path)s attempt %(n)d", {"path": CAMERA_URL, "n": 2})

    assert PASSWORD not in json.dumps(payload)
    assert payload["msg"].endswith("attempt 2")


def test_an_extra_field_is_redacted_too() -> None:
    """`extra={"path": url}` is how the worker logs a clip, and a path can be a URL."""
    payload = emit("fetching", extra={"path": CAMERA_URL})

    assert PASSWORD not in json.dumps(payload)
    assert payload["path"] == f"rtsp://***@{HOST}:554/stream1"


def test_a_message_containing_a_percent_sign_without_arguments_is_untouched() -> None:
    """`getMessage()` only formats when there are args; a literal `100%` must not raise
    inside the logging call, which would be a log line that crashes the worker."""
    payload = emit("coverage 100% of the window")

    assert payload["msg"] == "coverage 100% of the window"


# --------------------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------------------


def test_the_payload_is_one_json_object_with_the_documented_identity_fields() -> None:
    payload = emit("hello")

    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cctv.test"


def test_the_timestamp_is_utc_and_marked_as_such() -> None:
    """INV-12 makes UTC the storage timezone, and a log line is read next to database rows.
    A local-time log is one that has to be converted in the reader's head first."""
    before = datetime.now(UTC) - timedelta(seconds=1)
    payload = emit("hello")
    after = datetime.now(UTC) + timedelta(seconds=1)

    assert payload["ts"].endswith("Z")
    stamp = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert stamp.utcoffset() == timedelta(0)
    assert before <= stamp <= after


def test_an_allowlisted_field_is_promoted_with_its_type_intact() -> None:
    """Structured rather than formatted, so the value stays queryable."""
    payload = emit(
        "review handled",
        extra={
            "camera": "front-door",
            "review_id": "r-1",
            "coverage": 0.99,
            "attempts": 2,
        },
    )

    assert payload["camera"] == "front-door"
    assert payload["review_id"] == "r-1"
    assert payload["coverage"] == 0.99
    assert payload["attempts"] == 2


def test_a_field_that_is_not_on_the_allowlist_is_not_emitted() -> None:
    """An allowlist, not "everything on the record but the internals": the deny-list version
    starts emitting whatever a library attaches, which is how a token ends up in a log line.
    """
    payload = emit("hello", extra={"session_token": "tk_live_do_not_log_me"})

    assert "session_token" not in payload
    assert "tk_live_do_not_log_me" not in json.dumps(payload)


def test_an_allowlisted_field_that_was_not_provided_is_absent_rather_than_null() -> None:
    payload = emit("hello")

    assert "camera" not in payload


def test_a_value_of_an_unexpected_type_degrades_to_a_string_rather_than_raising() -> None:
    """A log line that crashes the worker is worse than a slightly ugly one."""
    payload = emit("uh oh", extra={"camera": object()})

    assert isinstance(payload["camera"], str)


def test_the_traceback_is_included_when_there_is_one() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    try:
        raise ValueError("nope")
    except ValueError:
        logging.getLogger("cctv.test").exception("it broke")

    payload = parse_only_line(stream)
    assert payload["msg"] == "it broke"
    assert "ValueError: nope" in payload["exception"]


# --------------------------------------------------------------------------------------
# configure_logging
# --------------------------------------------------------------------------------------


def test_the_default_level_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)

    configure_logging()

    assert logging.getLogger().level == logging.INFO


def test_an_empty_setting_falls_back_to_the_default() -> None:
    """`CCTV_LOG_LEVEL=` in a `.env` file is a commented-out line, not a request for a
    level named ""."""
    configure_logging("")

    assert logging.getLogger().level == logging.INFO


def test_the_level_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "warning")

    configure_logging()

    assert logging.getLogger().level == logging.WARNING


def test_the_level_name_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "dEbUg")

    configure_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_an_explicit_level_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")

    configure_logging("ERROR")

    assert logging.getLogger().level == logging.ERROR


def test_an_unknown_level_raises_and_names_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "chatty")

    with pytest.raises(ValueError) as excinfo:
        configure_logging()

    message = str(excinfo.value)
    assert LOG_LEVEL_ENV in message
    assert "chatty" in message
    assert "INFO" in message  # the message lists the levels that would have worked


def test_an_unknown_level_passed_directly_raises_too() -> None:
    with pytest.raises(ValueError, match="not a log level"):
        configure_logging("chatty")


def test_existing_handlers_are_replaced_rather_than_added_to() -> None:
    """So importing a library that called `basicConfig` cannot leave a second,
    differently-formatted stream attached — which is how half a log ends up as JSON."""
    sentinel = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(sentinel)

    configure_logging("INFO", stream=io.StringIO())

    assert sentinel not in root.handlers
    assert len(root.handlers) == 1


def test_configuring_twice_leaves_one_handler() -> None:
    configure_logging("INFO", stream=io.StringIO())
    configure_logging("INFO", stream=io.StringIO())

    assert len(logging.getLogger().handlers) == 1


def test_the_sink_is_the_given_stream_and_stdout_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    stream = io.StringIO()

    configure_logging("INFO", stream=stream)
    logging.getLogger("cctv.test").info("hello")

    assert json.loads(stream.getvalue())["msg"] == "hello"
    assert stdout.getvalue() == ""


def test_stdout_is_the_default_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    configure_logging("INFO")
    logging.getLogger("cctv.test").info("hello")

    assert json.loads(stdout.getvalue())["msg"] == "hello"
