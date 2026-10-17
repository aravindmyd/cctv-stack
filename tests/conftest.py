"""Shared fixtures — exactly two, and both for the same reason.

`cctv.logging.configure_logging` mutates the **root** logger on purpose: it replaces the
handlers so a library that called `basicConfig` cannot leave a second, differently-formatted
stream attached. That is right for a process and hostile to a test session, because the root
logger outlives the test that broke it — a handler left pointing at a closed buffer makes
every later test's log output vanish, and the resulting failures point at the wrong tests.

So `_root_logger_restored` is autouse rather than opt-in, and `logs` is the fixture a test
requests when it wants to *read* what was logged.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from cctv.logging import configure_logging

__all__ = ["CapturedLogs"]


class CapturedLogs:
    """The JSON log lines written during one test, parsed back into objects."""

    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    def messages(self) -> list[str]:
        return [record["msg"] for record in self.records()]

    def last(self) -> dict[str, Any]:
        records = self.records()
        assert records, "nothing was logged"
        return records[-1]


@pytest.fixture(autouse=True)
def _root_logger_restored() -> Iterator[None]:
    """Put the root logger back exactly as it was, whatever a test did to it."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level

    yield

    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def logs() -> Iterator[CapturedLogs]:
    """Install JSON logging into a buffer, and hand back what was written.

    At DEBUG, deliberately: the alternative is a test that silently asserts nothing because
    the line it was looking for was filtered out by the default level.
    """
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    yield CapturedLogs(stream)
