"""Writing files so that a crash never leaves a half-written clip.

A clip appears in the canonical tree only by `rename`, which is atomic within a
filesystem. Everything else — the download, the remux — happens at a temporary path that
no other component looks at. This is what makes a killed clip-worker leave a `.tmp` file
to be reaped rather than a truncated `.mp4` that every later check will believe.

The ordering matters and is easy to get subtly wrong:

```text
open tmp  →  write  →  flush  →  fsync(file)  →  close  →  rename  →  fsync(dir)
```

`fsync` on the file is not enough on its own. The rename itself lives in the directory
entry, so without the final `fsync` on the directory a power cut can leave the file
present with its old name — or absent entirely — even though its contents were durable.

[INV-4](../../../internal/architecture/invariants.md) puts all state under the host data
directory, and [INV-1](../../../internal/architecture/invariants.md) means an interrupted
materialisation is a retry rather than a loss, which only holds if a partial file cannot
be mistaken for a complete one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from .timeutil import Instant
from .timeutil import now as _now

__all__ = [
    "TMP_MARKER",
    "atomic_writer",
    "discard",
    "fsync_directory",
    "is_tmp",
    "promote",
    "reap_tmp",
    "tmp_path_for",
    "write_atomic",
]

#: The documented temporary suffix. The pid is appended so that two workers fetching the
#: same clip — which the lease should prevent, but should is not cannot — do not write to
#: the same scratch file.
TMP_MARKER = ".tmp"


def tmp_path_for(path: Path) -> Path:
    return path.with_name(f"{path.name}{TMP_MARKER}-{os.getpid()}")


def is_tmp(path: Path) -> bool:
    return TMP_MARKER in path.name


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry, so a rename survives a power cut.

    Best-effort by design: some filesystems and platforms refuse `O_DIRECTORY`, and
    failing here would turn a working system into a broken one for a durability
    improvement rather than a correctness requirement.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def atomic_writer(path: Path, *, mode: int = 0o644) -> Iterator[BinaryIO]:
    """Yield a temporary file that becomes `path` on a clean exit.

    Streaming rather than buffering in memory, because a long review can be hundreds of
    megabytes and the fetch should never have to be held whole.

    On any exception the temporary file is removed, so a failed fetch leaves nothing
    behind for the reaper to find.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)

    try:
        # A `with` block rather than a bare handle. The file must be closed *before* the
        # rename, and a bare handle turns that ordering into something a reader has to
        # verify rather than something the code states.
        with open(tmp, "wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    os.chmod(tmp, mode)
    os.replace(tmp, path)
    fsync_directory(path.parent)


def write_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """The whole-buffer form, for sidecars and other small files."""
    with atomic_writer(path, mode=mode) as handle:
        handle.write(data)


def promote(scratch: Path, target: Path, *, mode: int = 0o644) -> None:
    """Publish a finished scratch file as `target`.

    Exists because a clip may be fetched more than once and the *best* attempt wins, not
    the last one ([INV-14](../../../internal/architecture/invariants.md)). Retrying straight
    onto the canonical path would let a worse second attempt overwrite a better first one,
    which is the opposite of retry-then-keep-best. So each attempt lands on its own scratch
    path and only the winner is promoted — and it still appears in the tree solely via a
    rename, never by being written there.
    """
    scratch, target = Path(scratch), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(scratch, mode)
    os.replace(scratch, target)
    fsync_directory(target.parent)


def discard(path: Path) -> bool:
    """Remove a scratch file. Returns whether it is now gone.

    Deliberately forgiving: a scratch file that another process already removed is a
    success, not an error.
    """
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def reap_tmp(directory: Path, *, max_age_s: float, now: Instant | None = None) -> list[Path]:
    """Delete stale temporary files, and return what was removed.

    **Age-gated, and that is the point.** A temporary file that appeared seconds ago may
    belong to a fetch happening right now; deleting on sight would corrupt work in
    progress. `max_age_s` must therefore exceed the longest plausible fetch — which for a
    long review is minutes, not seconds, so the reaper's default is generous rather than
    tidy.

    Unlink failures are swallowed: on a shared filesystem another worker may have got there
    first, and that is a success, not an error.
    """
    now = _now() if now is None else now
    if max_age_s <= 0:
        raise ValueError("max_age_s must be positive, or the reaper races live fetches.")

    removed: list[Path] = []
    if not directory.is_dir():
        return removed

    for candidate in directory.rglob(f"*{TMP_MARKER}-*"):
        if not candidate.is_file():
            continue
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue
        if age < max_age_s:
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)

    return removed
