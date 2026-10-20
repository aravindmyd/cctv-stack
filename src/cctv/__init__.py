"""The shared library for the CCTV stack.

Four services import this one package — clip-worker, uploader, cleanup, and
status-collector — and its job is to make the invariants *structural* rather than
aspirational. If there is literally one implementation of a rule, the rule cannot
drift between components.

See `internal/architecture/lld/00-overview.md` §2 for the module map and the invariants
each module enforces.

Nothing in this package reaches for the network at import time. It is tested without
Frigate, without Docker, and without B2.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.9.0"
