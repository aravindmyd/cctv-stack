"""Tests for the notification test message (`lld/07` §6.2).

The point of the feature is that a broken channel is discovered at install time rather than
during an incident, so the tests are about the *report*: what it says for each way a channel can
be wrong, and what it must never say — a bot token.

No test here touches a network. The transport is injected, exactly as `doctor`'s probes are, so
the suite cannot fail for a reason unrelated to this code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from cctv import notify
from cctv.notify import NotifySettings, main, send_test

TOKEN = "123456789:AAH-this-is-the-secret-part"
CHAT = "-100200300"


def settings(**overrides: Any) -> NotifySettings:
    base: dict[str, Any] = {
        "ntfy_url": "http://ntfy:80",
        "ntfy_topic": "cctv",
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "location": "parents-home",
        "timeout_s": 5.0,
    }
    base.update(overrides)
    return NotifySettings(**base)


class FakeTransport:
    """Records requests and answers with whatever the test says the provider said."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.answers: dict[str, tuple[int, str]] = {}
        self.raises: dict[str, BaseException] = {}

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, str]:
        self.requests.append(request)
        host = urllib.parse.urlsplit(request.full_url).netloc
        if host in self.raises:
            raise self.raises[host]
        return self.answers.get(host, (200, ""))

    def bodies(self) -> list[str]:
        return [request.data.decode("utf-8") for request in self.requests if request.data]


def ntfy(outcomes: list[notify.TestOutcome]) -> notify.TestOutcome:
    return next(outcome for outcome in outcomes if outcome.channel == "ntfy")


# ---------------------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------------------


def test_a_test_message_reaches_ntfy() -> None:
    transport = FakeTransport()

    outcome = ntfy(send_test(settings(), transport=transport))

    # The Telegram channel is reported as skipped alongside, which is why this selects.
    assert outcome.ok
    assert outcome.detail == ""
    (request,) = transport.requests
    assert request.full_url == "http://ntfy:80/cctv"
    assert request.get_method() == "POST"


def test_the_message_says_which_site_it_came_from() -> None:
    transport = FakeTransport()

    send_test(settings(location="parents-home"), transport=transport)

    body = transport.bodies()[0]
    assert "parents-home" in body
    assert "test" in body


def test_the_same_topic_gatus_alerts_to_is_used() -> None:
    transport = FakeTransport()

    send_test(settings(ntfy_topic="front-door"), transport=transport)

    # One topic, not two: a test message must arrive where the real ones will.
    assert transport.requests[0].full_url == "http://ntfy:80/front-door"


def test_a_trailing_slash_on_the_server_url_does_not_produce_a_double_slash() -> None:
    transport = FakeTransport()
    from_environ = NotifySettings.from_environ(
        {"CCTV_STATUS_NTFY_URL": "http://ntfy:80/", "CCTV_STATUS_NTFY_TOPIC": "cctv"}
    )

    send_test(from_environ, transport=transport)

    assert transport.requests[0].full_url == "http://ntfy:80/cctv"


def test_an_ntfy_rejection_is_reported_with_the_reason() -> None:
    transport = FakeTransport()
    transport.answers["ntfy:80"] = (404, "topic not found")

    outcome = ntfy(send_test(settings(), transport=transport))

    assert not outcome.ok
    assert "404" in outcome.detail
    assert "topic not found" in outcome.detail


def test_an_unreachable_ntfy_names_the_server() -> None:
    transport = FakeTransport()
    transport.raises["ntfy:80"] = urllib.error.URLError("connection refused")

    outcome = ntfy(send_test(settings(), transport=transport))

    assert not outcome.ok
    assert "http://ntfy:80" in outcome.detail
    assert "connection refused" in outcome.detail


# ---------------------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------------------


def test_telegram_is_skipped_rather_than_failed_when_it_is_not_configured() -> None:
    outcomes = send_test(settings(), transport=FakeTransport())

    telegram = next(outcome for outcome in outcomes if outcome.channel == "telegram")
    assert telegram.skipped
    assert not telegram.ok
    assert "not configured" in telegram.detail
    # "Not configured" is a choice; reporting it as a failure trains an operator to ignore the
    # output, which is the opposite of the point.
    assert "FAILED" not in telegram.describe()


def test_a_configured_telegram_channel_delivers_and_does_not_leak_the_token() -> None:
    transport = FakeTransport()
    transport.answers["api.telegram.org"] = (200, json.dumps({"ok": True}))

    outcomes = send_test(
        settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT), transport=transport
    )

    telegram = next(outcome for outcome in outcomes if outcome.channel == "telegram")
    assert telegram.ok
    assert TOKEN not in telegram.detail
    request = next(r for r in transport.requests if "telegram" in r.full_url)
    assert request.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(request.data)["chat_id"] == CHAT


def test_telegram_refusal_is_reported_from_its_own_description_without_the_token() -> None:
    transport = FakeTransport()
    transport.answers["api.telegram.org"] = (
        400,
        json.dumps({"ok": False, "description": "Bad Request: chat not found"}),
    )

    outcomes = send_test(
        settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT), transport=transport
    )

    telegram = next(outcome for outcome in outcomes if outcome.channel == "telegram")
    assert not telegram.ok
    assert "chat not found" in telegram.detail
    assert TOKEN not in telegram.detail


def test_a_transport_failure_cannot_leak_the_token_through_the_exception() -> None:
    transport = FakeTransport()
    # The realistic leak: an exception whose message embeds the request URL, which contains the
    # token in its path. Userinfo scrubbing would not touch that.
    transport.raises["api.telegram.org"] = urllib.error.URLError(
        f"failed to fetch https://api.telegram.org/bot{TOKEN}/sendMessage"
    )

    outcomes = send_test(
        settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT), transport=transport
    )

    telegram = next(outcome for outcome in outcomes if outcome.channel == "telegram")
    assert not telegram.ok
    assert TOKEN not in telegram.detail
    assert "***" in telegram.detail


def test_half_a_telegram_configuration_is_a_configuration_error() -> None:
    assert settings(telegram_bot_token=TOKEN).configuration_problem() is not None
    assert settings(telegram_chat_id=CHAT).configuration_problem() is not None
    assert settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT).configuration_problem() is None
    assert settings().configuration_problem() is None


def test_one_broken_channel_does_not_hide_the_other() -> None:
    transport = FakeTransport()
    transport.answers["api.telegram.org"] = (401, json.dumps({"ok": False, "description": "no"}))
    transport.answers["ntfy:80"] = (200, "ok")

    outcomes = send_test(
        settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT), transport=transport
    )

    by_name = {outcome.channel: outcome for outcome in outcomes}
    assert by_name["ntfy"].ok
    assert not by_name["telegram"].ok


# ---------------------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------------------


def test_the_cli_lists_every_channel_with_its_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport()
    monkeypatch.setattr(notify, "_urllib_transport", transport)
    monkeypatch.setenv("CCTV_STATUS_NTFY_TOPIC", "cctv")
    monkeypatch.delenv("CCTV_NOTIFY_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CCTV_NOTIFY_TELEGRAM_CHAT_ID", raising=False)

    code = main(["test"])

    out = capsys.readouterr().out
    assert code == 0
    assert "ntfy: delivered" in out
    assert "telegram: skipped" in out


def test_the_cli_exits_78_on_a_half_configured_telegram(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCTV_NOTIFY_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("CCTV_NOTIFY_TELEGRAM_CHAT_ID", raising=False)

    code = main(["test"])

    assert code == 78
    assert "CCTV_NOTIFY_TELEGRAM_CHAT_ID" in capsys.readouterr().err


def test_the_cli_does_not_serialise_a_token_in_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport()
    transport.answers["api.telegram.org"] = (200, json.dumps({"ok": True}))
    monkeypatch.setenv("CCTV_NOTIFY_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("CCTV_NOTIFY_TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr(notify, "_urllib_transport", transport)

    main(["test", "--json"])

    printed = capsys.readouterr().out
    assert TOKEN not in printed
    assert json.loads(printed)


def test_a_custom_message_body_is_used_verbatim() -> None:
    transport = FakeTransport()

    send_test(settings(), text="hello from the installer", transport=transport)

    assert transport.bodies()[0] == "hello from the installer"
