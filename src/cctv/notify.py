"""Notification channels, and the one thing worth doing at install time: sending a real message.

`lld/07` §6.2 is blunt about why this exists. A notification channel that is misconfigured and
discovered *during an incident* is worthless — you find out at the worst possible moment, and
the alert you needed is the one that never arrived. Discovered during install, it is a
five-second fix. So the installer sends a real message and reports what happened, per channel.

Two channels, because the stack ships one and can talk to the other:

* **ntfy** runs in the default stack (`compose.yml`), so "configured" is not a question — it is
  there, and the only thing that can be wrong is the URL or the topic.
* **Telegram** is optional, configured by a bot token and a chat id, and is the only channel
  that needs to reach the public internet.

Three rules, each about a way this goes wrong rather than a way it works:

**A credential never appears in a message.** A Telegram bot token is in the *URL path*, so
scrubbing userinfo (what [`logging.redact_url`][cctv.logging.redact_url] does, correctly, for
camera URLs) would not touch it. Errors here are built from status codes and the provider's own
`description` field, never from a formatted request, and the token is also substituted out as a
last resort ([INV-2](../../../internal/architecture/invariants.md)).

**"Not configured" is not "failed".** A site with no Telegram token is a choice, and reporting
it as a failure would train an operator to ignore the output. But a site with *half* a Telegram
configuration is a mistake, and that is reported as one — the same distinction the uploader makes
between `DISABLED` and exit 78.

**The transport is injected.** Every test in `tests/test_notify.py` runs without a network,
because a suite that needs one is a suite that fails for reasons unrelated to the code.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_NTFY_TOPIC",
    "DEFAULT_NTFY_URL",
    "DEFAULT_TIMEOUT_S",
    "NotifyError",
    "NotifySettings",
    "TestOutcome",
    "channels",
    "main",
    "send_test",
]

#: The ntfy container's address inside the compose network. The published port is for phones,
#: not for us.
DEFAULT_NTFY_URL = "http://ntfy:80"

#: The topic Gatus already alerts to. One topic, not two: `status` generates Gatus's config from
#: this same setting, so a test message arrives where the real ones will.
DEFAULT_NTFY_TOPIC = "cctv"

DEFAULT_TIMEOUT_S = 10.0

TELEGRAM_API = "https://api.telegram.org"


class NotifyError(RuntimeError):
    """A message that did not arrive, with a reason an operator can act on."""


#: A request, and the status and body that came back. Anything that raises is a transport
#: failure; anything that returns a status is a provider answer we can explain.
Transport = Callable[[urllib.request.Request, float], "tuple[int, str]"]


@dataclass(frozen=True, slots=True)
class NotifySettings:
    """Every input is an environment variable, so there are no CLI arguments to get wrong."""

    ntfy_url: str = DEFAULT_NTFY_URL
    ntfy_topic: str = DEFAULT_NTFY_TOPIC
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    #: Included in the message, so a phone subscribed to two sites can tell them apart.
    location: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> NotifySettings:
        env = os.environ if environ is None else environ

        def clean(name: str) -> str | None:
            value = (env.get(name) or "").strip()
            return value or None

        return cls(
            ntfy_url=(clean("CCTV_STATUS_NTFY_URL") or DEFAULT_NTFY_URL).rstrip("/"),
            ntfy_topic=clean("CCTV_STATUS_NTFY_TOPIC") or DEFAULT_NTFY_TOPIC,
            telegram_bot_token=clean("CCTV_NOTIFY_TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=clean("CCTV_NOTIFY_TELEGRAM_CHAT_ID"),
            location=clean("CCTV_LOCATION"),
            timeout_s=_timeout(env),
        )

    def configuration_problem(self) -> str | None:
        """A half-configured channel, which is a mistake rather than a choice.

        Both Telegram values or neither. `send_test` reports the asymmetric case as the failure
        it is, and this exists so the installer can say so before it even tries.
        """
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            missing = (
                "CCTV_NOTIFY_TELEGRAM_CHAT_ID"
                if self.telegram_bot_token
                else "CCTV_NOTIFY_TELEGRAM_BOT_TOKEN"
            )
            return f"Telegram is half configured: {missing} is empty, so nothing is sent."
        return None


def _timeout(env: Mapping[str, str]) -> float:
    raw = (env.get("CCTV_NOTIFY_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------------------
# The channels
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """What one channel did, in one line an operator can read."""

    channel: str
    ok: bool
    skipped: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "skipped": self.skipped,
            "detail": self.detail,
        }

    def describe(self) -> str:
        if self.skipped:
            return f"{self.channel}: skipped ({self.detail})"
        return f"{self.channel}: {'delivered' if self.ok else 'FAILED — ' + self.detail}"


@dataclass(frozen=True, slots=True)
class _Channel:
    name: str
    #: `None` when the channel is not configured at all; a reason string when it is.
    skip_reason: str | None = None
    send: Callable[[str], None] = field(default=lambda _text: None)


def _urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, str]:
    """POST the request and return the status and body.

    An `HTTPError` is a *delivered answer* — the provider said no — so it is returned rather than
    raised, and the caller turns it into a sentence. A `URLError` is a transport failure and
    propagates, because "cannot resolve api.telegram.org" needs a different fix from "Telegram
    says chat not found".
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
        return int(exc.code), body


def _ntfy_channel(settings: NotifySettings, transport: Transport) -> _Channel:
    url = f"{settings.ntfy_url}/{settings.ntfy_topic}"

    def send(text: str) -> None:
        request = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                # The title is what a phone shows as the notification heading, so it says what
                # the message is rather than repeating the body.
                "Title": "cctv-stack test",
                "Priority": "default",
                "Tags": "white_check_mark",
            },
        )
        try:
            status, body = transport(request, settings.timeout_s)
        except (urllib.error.URLError, OSError) as exc:
            raise NotifyError(f"cannot reach ntfy at {settings.ntfy_url}: {exc}") from exc
        if status >= 400:
            raise NotifyError(f"ntfy answered {status}: {body.strip()[:200] or 'no detail'}")

    return _Channel(name="ntfy", send=send)


def _telegram_channel(settings: NotifySettings, transport: Transport) -> _Channel:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token and not chat_id:
        return _Channel(
            name="telegram",
            skip_reason="not configured; set CCTV_NOTIFY_TELEGRAM_BOT_TOKEN and _CHAT_ID",
        )
    if not token or not chat_id:
        return _Channel(
            name="telegram",
            skip_reason="half configured; both the bot token and the chat id are needed",
        )

    def send(text: str) -> None:
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        payload = json.dumps(
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            status, body = transport(request, settings.timeout_s)
        except (urllib.error.URLError, OSError) as exc:
            raise NotifyError(f"cannot reach {TELEGRAM_API}: {_scrub(str(exc), token)}") from exc
        detail = _telegram_detail(body)
        if status >= 400 or detail:
            raise NotifyError(
                f"Telegram answered {status}: {detail or 'no description'}"
                if status >= 400
                else f"Telegram refused the message: {detail}"
            )

    return _Channel(name="telegram", send=send)


def _telegram_detail(body: str) -> str:
    """Telegram's own `description` when it refused, or `""` when it accepted.

    From the body rather than the exception, because the exception's URL contains the bot token
    and a message must not ([INV-2](../../../internal/architecture/invariants.md)).
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return "" if not body.strip() else "unreadable response"
    if not isinstance(payload, dict):
        return "unreadable response"
    if payload.get("ok") is True:
        return ""
    return str(payload.get("description") or "refused with no description")


def _scrub(text: str, secret: str | None) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def channels(settings: NotifySettings, *, transport: Transport | None = None) -> list[_Channel]:
    """Every channel this site could use, configured or not."""
    send = transport or _urllib_transport
    return [_ntfy_channel(settings, send), _telegram_channel(settings, send)]


# ---------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------


def default_text(settings: NotifySettings, *, now: float | None = None) -> str:
    """What the test message says.

    Deliberately recognisable rather than generic. Someone who subscribes to several sites needs
    to know which one this came from, and someone who receives it days later needs to know it was
    a test rather than an incident.
    """
    import time

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(time.time() if now is None else now))
    where = f" for {settings.location}" if settings.location else ""
    return (
        f"cctv-stack test{where} at {stamp}.\n"
        "If you can read this, alerts from this installation will reach this channel."
    )


def send_test(
    settings: NotifySettings,
    *,
    text: str | None = None,
    transport: Transport | None = None,
) -> list[TestOutcome]:
    """Send one real message to every configured channel, and report each outcome.

    Never raises for a channel failure: an installation with a broken Telegram chat should still
    find out that ntfy works, so each channel is independent and the failures are data.
    """
    body = text or default_text(settings)
    outcomes: list[TestOutcome] = []
    for channel in channels(settings, transport=transport):
        if channel.skip_reason is not None:
            outcomes.append(
                TestOutcome(
                    channel=channel.name, ok=False, skipped=True, detail=channel.skip_reason
                )
            )
            continue
        try:
            channel.send(body)
        except NotifyError as exc:
            outcomes.append(TestOutcome(channel=channel.name, ok=False, detail=str(exc)))
        else:
            outcomes.append(TestOutcome(channel=channel.name, ok=True))
    return outcomes


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """`cctv-notify test` — send a real message to every configured channel."""
    import argparse

    parser = argparse.ArgumentParser(prog="cctv-notify")
    sub = parser.add_subparsers(dest="command", required=True)
    test_p = sub.add_parser("test", help="send a real message to every configured channel")
    test_p.add_argument("--json", action="store_true")
    test_p.add_argument("--text", default=None, help="override the message body")
    args = parser.parse_args(argv)

    settings = NotifySettings.from_environ()
    problem = settings.configuration_problem()
    if problem is not None:
        print(f"cctv-notify: {problem}", file=sys.stderr)
        return 78

    outcomes = send_test(settings, text=args.text)
    if args.json:
        print(json.dumps([outcome.to_dict() for outcome in outcomes], indent=2))
    else:
        for outcome in outcomes:
            print(outcome.describe())
        if not any(not outcome.skipped for outcome in outcomes):
            print(
                "No notification channel is configured. Alerts will be generated and not "
                "delivered anywhere an operator can see them."
            )
    return 0 if all(outcome.ok or outcome.skipped for outcome in outcomes) else 1
