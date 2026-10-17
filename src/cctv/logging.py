"""JSON logging for every service.

Structured rather than formatted, for two reasons that are both about INV-2:

1. **A camera's RTSP URL is a credential.** `rtsp://user:pass@host` is the normal form, so
   a URL printed "just for debugging" leaks a password into every log sink that touches it
   — including the ones that ship logs off the machine. [`redact_url`][cctv.logging.redact_url]
   strips the userinfo and keeps the host, because the host is the useful part.

2. **Fields stay fields.** A message interpolated together cannot be filtered, counted or
   redacted afterwards. `extra={"camera": name}` produces a queryable key; the same value
   pasted into an f-string produces a sentence.

The redaction is a `logging.Filter` rather than a helper the call sites must remember,
because a rule that depends on every caller remembering it is a rule that will be broken
once, silently, in the one code path nobody reviewed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["LOG_LEVEL_ENV", "configure_logging", "redact_url"]

LOG_LEVEL_ENV = "CCTV_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"

#: Extra fields promoted into the JSON object. An allowlist, not "everything on the
#: record minus the internals": the deny-list version silently starts emitting whatever a
#: library decides to attach, which is how a session token ends up in a log line.
_FIELDS = (
    "camera",
    "review_id",
    "clip_id",
    "path",
    "status",
    "reason",
    "coverage",
    "duration_s",
    "attempts",
    "count",
    "state",
    "topic",
    "host",
)

#: `scheme://user:password@host` → `scheme://***@host`.
#:
#: The userinfo runs to the **last** `@` before the path, which is why this is `[^/\s]*@`
#: rather than `[^/@\s]+@`. A camera password is chosen by a person, so one containing an `@`
#: (`p@ssw0rd`) is a password that occurs — and stopping at the first `@` would publish its
#: tail, on a line that *looks* redacted, which is worse than not redacting at all.
#: Excluding `/` and whitespace is what keeps the match inside one URL.
_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")


def redact_url(value: str) -> str:
    """Remove credentials from anything that looks like a URL, leaving the host.

    Applies to every URL in the string, because a log message is a sentence and a sentence
    can carry more than one camera.
    """
    return _USERINFO_RE.sub(r"\g<scheme>***@", value)


class _RedactingFilter(logging.Filter):
    """Scrub credentials from the message *and* from the arguments.

    Both, deliberately. `logger.info("connecting to %s", url)` is the idiomatic way to log
    a value, and a message-only filter would miss it entirely — the credential is in
    `record.args` until the formatter joins them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_url(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_url(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_url(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # UTC always. INV-12 makes UTC the storage timezone, and a log line is read
            # next to database rows, so a local-time log is a log that has to be converted
            # in the reader's head before it can be compared to anything.
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for field in _FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                # Redacted here as well as in the filter, because an `extra` value never
                # travels through `record.msg` or `record.args` — it is set as an attribute.
                # `logger.info("fetching", extra={"path": url})` is the idiomatic way to log
                # a clip, and the two places that can put a string into a log line have to
                # apply the same rule or the rule has a hole in it.
                payload[field] = redact_url(value) if isinstance(value, str) else value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # `default=str` so an unexpected type degrades to its repr instead of raising
        # inside the logging call — a log line that crashes the worker is worse than a
        # slightly ugly one.
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None, *, stream: Any = None) -> None:
    """Install the JSON handler as the root configuration.

    Replaces existing handlers, so importing a library that called `basicConfig` cannot
    leave a second, differently-formatted stream attached.

    Raises:
        ValueError: the level is not a name `logging` recognises. Raised rather than
            ignored because a typo'd level that silently becomes INFO is a log level the
            operator believes they set.
    """
    requested = level if level is not None else os.environ.get(LOG_LEVEL_ENV, "")
    resolved = (requested or DEFAULT_LEVEL).upper()

    known = logging.getLevelNamesMapping()
    if resolved not in known:
        raise ValueError(
            f"{LOG_LEVEL_ENV}={requested!r} is not a log level. "
            f"Expected one of: {', '.join(sorted(known))}."
        )

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(known[resolved])
