"""The single clip-path function.

Every component that needs a clip's path calls this module. Nothing re-implements it —
because the moment cleanup and clip-worker disagree about a path, cleanup stops being
able to find clips, and it fails *silently*, by not deleting. That is the failure
[INV-5](../../../internal/architecture/invariants.md) exists to prevent, which is why
[ADR-0006](../../../internal/adr/0006-canonical-clip-layout.md) puts this in one place.

```text
local:   data/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
cloud:   <location>/clips/<camera>/YYYY/MM/DD/HH-MM-SS.mp4
sidecar: <camera>/YYYY/MM/DD/HH-MM-SS.mp4.json
```

## What "pure function" means here, and what it does not

The path is derived from `camera`, the **review** start instant, and a timezone — never
from wall-clock-at-write-time and never from a counter. So materialisation is idempotent:
run it twice, get the same path.

Two things are worth being precise about, because both are easy to get wrong:

**The path is forward-derivable but not invertible.** Given the components you cannot
recover the instant, for two independent reasons: the timezone is not in the path, and
during a DST fallback two distinct instants render to the *same* components. This is
exactly why the database stores the instant rather than re-deriving it from filenames.

**A path can be taken, and that is not knowable from the arguments.** Two reviews on one
camera can start in the same second. So there is no single "the" path function: there is a
*primary* candidate and a *colliding* candidate, both pure, and the writer tries them in
order ([ADR-0006](../../../internal/adr/0006-canonical-clip-layout.md)). The colliding form
appends a deterministic tail of the review id — deterministic being the whole point, so
the collision is still a pure function of the metadata.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from .timeutil import Instant, render

__all__ = [
    "CLIP_EXT",
    "CLOUD_CLIPS_PREFIX",
    "COLLISION_SUFFIX_LEN",
    "ParsedClipPath",
    "PathError",
    "candidate_relpaths",
    "clip_cloud_key",
    "colliding_relpath",
    "collision_suffix",
    "date_parts",
    "is_canonical_relpath",
    "parse_clip_relpath",
    "primary_relpath",
    "sidecar_relpath",
    "validate_camera_name",
    "validate_location",
]

CLIP_EXT = ".mp4"
SIDECAR_EXT = ".mp4.json"
CLOUD_CLIPS_PREFIX = "clips"

#: Frigate appends six random base-36 characters to a review id, so the last six are
#: already a unique, filesystem-safe tail. We do not depend on that format — see
#: [`collision_suffix`][cctv.paths.collision_suffix].
COLLISION_SUFFIX_LEN = 6

# 1-32 characters, lowercase alphanumeric with interior hyphens. Deliberately *not*
# ADR-0006's regex, which requires at least two characters and would reject the
# perfectly reasonable camera name `a`; the ADR needs the same one-character fix.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

_CLIP_NAME_RE = re.compile(
    r"^(?P<h>[01][0-9]|2[0-3])-(?P<mi>[0-5][0-9])-(?P<s>[0-5][0-9])"
    rf"(?:-(?P<suffix>[a-z0-9]{{{COLLISION_SUFFIX_LEN}}}))?{re.escape(CLIP_EXT)}$"
)


class PathError(ValueError):
    """A path that cannot be derived or parsed. Raised at config load or on read."""


def validate_slug(value: str, *, what: str) -> str:
    """Validate a name that appears in filesystem paths and cloud object keys.

    Uppercase is rejected rather than lowercased: silently rewriting the operator's
    camera name means the name in `cameras.yml`, the name in the path, and the name in
    Frigate's config would be three different strings, and debugging that is worse than
    a validation error at install time.
    """
    if not isinstance(value, str) or not value:
        raise PathError(f"{what} is required.")
    if not _SLUG_RE.match(value):
        raise PathError(
            f"{what} {value!r} is not valid. It must match {_SLUG_RE.pattern}: "
            f"lowercase letters, digits and interior hyphens, 1-32 characters. "
            f"{what}s appear in filesystem paths and cloud object keys, so uppercase "
            f"causes case-collision bugs on macOS and a leading or trailing hyphen "
            f"breaks round trips through some archive tools."
        )
    return value


def validate_camera_name(camera: str) -> str:
    """Validate a camera name. Cameras are data, never code."""
    return validate_slug(camera, what="Camera name")


def validate_location(location: str) -> str:
    """Validate a site location name, which becomes the cloud key prefix."""
    return validate_slug(location, what="Site location")


def date_parts(start: Instant, zone: ZoneInfo) -> tuple[str, str, str, str]:
    """Render an instant into the four path components.

    This is the only place in the codebase where a path component is produced from an
    instant, which is what makes the timezone policy auditable rather than distributed.
    """
    local = render(start, zone)
    return (
        f"{local.year:04d}",
        f"{local.month:02d}",
        f"{local.day:02d}",
        f"{local.hour:02d}-{local.minute:02d}-{local.second:02d}",
    )


def collision_suffix(review_id: str) -> str:
    """A deterministic tail for a colliding path.

    Frigate's review ids end in six random base-36 characters, so taking the last six
    alphanumerics gives exactly the documented suffix while staying independent of the
    id's overall format. An id that is too short, or whose tail is not alphanumeric,
    falls back to a hash rather than to a counter — a counter would be stateful, and a
    stateful suffix is not derivable from metadata, which would break
    [INV-5](../../../internal/architecture/invariants.md).
    """
    if not isinstance(review_id, str) or not review_id:
        raise PathError("A collision suffix requires a non-empty review id.")

    tail = "".join(ch for ch in review_id.lower() if ch.isascii() and ch.isalnum())
    if len(tail) < COLLISION_SUFFIX_LEN:
        digest = hashlib.sha256(review_id.encode("utf-8")).hexdigest()
        return digest[:COLLISION_SUFFIX_LEN]
    return tail[-COLLISION_SUFFIX_LEN:]


def primary_relpath(camera: str, start: Instant, zone: ZoneInfo) -> PurePosixPath:
    """The path to try first: `HH-MM-SS.mp4`.

    Always POSIX, never `Path`. A clip path is also a cloud object key, and object keys
    use forward slashes regardless of the host OS — so building it with a Windows-aware
    type would work on Linux and quietly produce backslashes on a contributor's laptop.
    """
    camera = validate_camera_name(camera)
    year, month, day, hms = date_parts(start, zone)
    return PurePosixPath(camera, year, month, day, f"{hms}{CLIP_EXT}")


def colliding_relpath(camera: str, start: Instant, zone: ZoneInfo, review_id: str) -> PurePosixPath:
    """The path to use when the primary one is already taken: `HH-MM-SS-xxxxxx.mp4`."""
    base = primary_relpath(camera, start, zone)
    return base.with_name(f"{base.stem}-{collision_suffix(review_id)}{CLIP_EXT}")


def candidate_relpaths(
    camera: str, start: Instant, zone: ZoneInfo, review_id: str
) -> tuple[PurePosixPath, PurePosixPath]:
    """Both candidates, in the order the writer must try them.

    Exposed as one function so that the collision protocol lives in a single place. A
    caller that builds the colliding form itself has re-implemented part of this
    module, which is what [INV-5](../../../internal/architecture/invariants.md) forbids.
    """
    return (
        primary_relpath(camera, start, zone),
        colliding_relpath(camera, start, zone, review_id),
    )


def sidecar_relpath(clip: PurePosixPath) -> PurePosixPath:
    """`HH-MM-SS.mp4` → `HH-MM-SS.mp4.json`, beside the clip.

    Suffixing rather than replacing the extension keeps the two files adjacent in a
    directory listing and makes it impossible to mistake a sidecar for media.
    """
    return clip.with_name(clip.name + ".json")


def clip_cloud_key(location: str, relpath: PurePosixPath) -> str:
    """Derive the object key from the local path.

    Deliberately a concatenation rather than a parallel scheme: if the key were built
    independently it could drift from the path, and then restore would need a mapping
    layer that can itself drift. As it is, recovery is "strip the prefix, write the
    file" ([ADR-0006](../../../internal/adr/0006-canonical-clip-layout.md)).
    """
    location = validate_location(location)
    return f"{location}/{CLOUD_CLIPS_PREFIX}/{_as_posix(relpath)}"


def _as_posix(value: PurePosixPath | str) -> str:
    return value.as_posix() if isinstance(value, PurePosixPath) else PurePosixPath(value).as_posix()


@dataclass(frozen=True, slots=True)
class ParsedClipPath:
    """The components recovered from a path.

    Note the absence of an `instant` field. Recovering one is impossible without the
    timezone, and ambiguous even with it during a DST fallback — see the module
    docstring. Callers that need the instant read it from the database.
    """

    camera: str
    date_parts: tuple[str, str, str]
    time_parts: tuple[str, str, str]
    collision_suffix: str | None

    @property
    def collided(self) -> bool:
        return self.collision_suffix is not None


def parse_clip_relpath(relpath: PurePosixPath | str) -> ParsedClipPath:
    """Parse `camera/YYYY/MM/DD/HH-MM-SS[-xxxxxx].mp4`.

    Used by restore and by the verification pass, which walk a tree they did not write —
    a `restic` restore, or a bucket listing. It validates rather than trusting, because
    a file that does not parse means either a bug or something a human put there.

    Raises:
        PathError: the path is not a canonical clip path.
    """
    path = PurePosixPath(_as_posix(relpath))

    if len(path.parts) != 5:
        raise PathError(
            f"{path} is not a canonical clip path. Expected "
            f"<camera>/YYYY/MM/DD/HH-MM-SS{CLIP_EXT}, found {len(path.parts)} components."
        )

    camera, year, month, day, name = path.parts
    validate_camera_name(camera)

    if not (
        re.fullmatch(r"\d{4}", year)
        and re.fullmatch(r"\d{2}", month)
        and re.fullmatch(r"\d{2}", day)
    ):
        raise PathError(f"{path} has a malformed date component.")

    # Range-check via the calendar rather than by comparing against 12 and 31. A clip is
    # always derived from an instant, so its date always exists — which means `02-30` is
    # proof that something other than this module wrote the file.
    try:
        _dt.date(int(year), int(month), int(day))
    except ValueError as exc:
        raise PathError(f"{path} has a date that does not exist: {exc}") from exc

    match = _CLIP_NAME_RE.match(name)
    if match is None:
        raise PathError(
            f"{path} has a malformed filename. Expected HH-MM-SS{CLIP_EXT}, optionally "
            f"with a {COLLISION_SUFFIX_LEN}-character collision suffix."
        )

    return ParsedClipPath(
        camera=camera,
        date_parts=(year, month, day),
        time_parts=(match["h"], match["mi"], match["s"]),
        collision_suffix=match["suffix"],
    )


def is_canonical_relpath(relpath: PurePosixPath | str) -> bool:
    """Non-raising form of [`parse_clip_relpath`][cctv.paths.parse_clip_relpath]."""
    try:
        parse_clip_relpath(relpath)
    except PathError:
        return False
    return True
