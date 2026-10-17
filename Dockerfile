# One image, several entrypoints (LLD 00 §2.2).
#
# Built with uv, run without it. ADR-0018 is explicit: uv is the development, CI and
# *build-stage* toolchain. Installing it into the runtime image would put a package manager
# inside the thing that is meant to be disposable, and `uv run` at container start-up would
# make the first clip of the day depend on a network round trip.

# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build

# Pinned by tag rather than `latest`: a build that silently changes toolchain version is a
# build whose failures cannot be reproduced.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, source second. Docker caches per layer, so a source edit then rebuilds
# only the cheap layer instead of re-resolving the whole dependency graph.
#
# README.md and LICENSE are build inputs, not documentation: pyproject.toml declares both,
# and hatchling refuses to build the wheel without them. Omitting them produced an image
# build that passed locally (where the files exist) and failed in CI with
# `OSError: License file does not exist: LICENSE`.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-install-project --no-dev --extra mqtt --extra storage

COPY src ./src

# `--no-editable` is not a preference, it is the whole point of this stage. uv installs the
# root project editable by default, which writes a `.pth` pointing at `/app/src` — so copying
# only `/app/.venv` into the runtime stage yields an image whose entry points exist, whose
# distribution metadata exists, and whose package is nowhere. The failure is
# `ModuleNotFoundError` from a script that looks perfectly installed.
#
# Installing the built wheel also means the image exercises the same artefact a release
# publishes, so packaging mistakes — a missing `migrations/*.sql`, for instance — are caught
# here rather than at a customer's site.
RUN uv sync --frozen --no-dev --extra mqtt --extra storage --no-editable

# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg is a genuine runtime dependency, not a convenience: `cctv.ffprobe` shells out to
# `ffprobe` to measure what actually came back from a fetch, and INV-14 — a short clip is
# retried, never silently kept as if it were whole — depends on that measurement.
#
# `python:3.12-slim` has neither binary. Without this line the worker still downloads every
# clip successfully, then fails to verify any of them, retries each one through the whole
# backoff sequence, and stores nothing. The unit tests could not catch it because they inject
# a fake prober; only running the container against a live Frigate does.
#
# Debian has no `ffprobe`-only package — it ships inside `ffmpeg`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# A fixed uid, so bind-mounted state on the host has predictable ownership. Without it the
# container writes as whichever uid the base image happened to pick, and "why can't the
# operator read their own clips" becomes a permissions investigation.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cctv

WORKDIR /app
COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CCTV_CONFIG_DIR=/config \
    CCTV_DATA_DIR=/data

# State lives on the host, never in the image (INV-4). Declared so it is obvious where the
# mounts are expected, and so an accidental `docker run` without them fails loudly rather
# than writing a database into a layer nobody will ever find.
VOLUME ["/config", "/data"]

USER cctv
CMD ["cctv-clip-worker"]
