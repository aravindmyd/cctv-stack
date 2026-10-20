"""Guards for the docs split.

`docs/` is the **published** site: a user installing or operating the stack. The design
record (PRD, architecture, contracts, LLDs, ADRs) lives in `internal/`, outside MkDocs'
`docs_dir`, and is never built. These tests exist because that boundary is easy to erode
by accident — one internal link in a runbook puts an unpublished page in front of a user
as a 404, and one stray directory in `docs/` publishes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"

# Directories and files that belong to the design record, not to the site.
INTERNAL_NAMES = {"adr", "architecture", "plans", "prd.md", "now.md"}

MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")


def _nav_strings(node: Any) -> list[str]:
    """Yield every string in the nav tree, in document order."""
    if isinstance(node, dict):
        return [value for child in node.values() for value in _nav_strings(child)]
    if isinstance(node, list):
        return [value for child in node for value in _nav_strings(child)]
    return [node] if isinstance(node, str) else []


def test_docs_dir_holds_nothing_from_the_design_record() -> None:
    offenders = [
        page.relative_to(ROOT).as_posix()
        for page in DOCS.rglob("*")
        if page != DOCS and page.relative_to(DOCS).parts[0] in INTERNAL_NAMES
    ]

    assert not offenders, "these belong under internal/, not in the published tree: " + ", ".join(
        offenders
    )


def test_nav_references_only_published_pages() -> None:
    nav = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))["nav"]
    targets = [item for item in _nav_strings(nav) if item.endswith(".md")]
    docs_root = DOCS.resolve()

    assert targets, "the nav is empty"
    for target in targets:
        page = (DOCS / target).resolve()
        assert page.is_file(), f"nav points at a missing page: {target}"
        assert page.is_relative_to(docs_root), f"nav escapes docs_dir: {target}"


def test_published_pages_do_not_link_outside_the_published_tree() -> None:
    docs_root = DOCS.resolve()
    offenders: list[str] = []

    for page in sorted(DOCS.rglob("*.md")):
        for target in MARKDOWN_LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(EXTERNAL_PREFIXES):
                continue
            destination = (page.parent / target.split("#", 1)[0]).resolve()
            if not destination.is_relative_to(docs_root):
                offenders.append(f"{page.relative_to(ROOT).as_posix()} -> {target}")

    assert not offenders, "published pages must not link to unpublished docs: " + ", ".join(
        offenders
    )


def test_every_line_of_env_example_is_a_comment_blank_or_a_setting() -> None:
    """`.env.example` is read by humans and copied by `install.sh`, so a bare prose line is a
    bug: it is either a lost `#` (a comment that became a setting) or a lost setting."""
    offenders: list[str] = []
    for number, line in enumerate(
        (ROOT / ".env.example").read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", stripped):
            continue
        offenders.append(f"line {number}: {stripped}")

    assert not offenders, "not a comment, a blank line or KEY=value:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------------------
# The install guide has to agree with what the stack actually publishes
# ---------------------------------------------------------------------------------------

COMPOSE = ROOT / "compose.yml"
INSTALL_GUIDE = DOCS / "install.md"
README = ROOT / "README.md"

#: `http://host:PORT` / `https://127.0.0.1:PORT` — a URL an operator could actually paste.
HOST_URL = re.compile(r"https?://(?:127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}):(\d+)")


#: Ports that legitimately appear in a URL in the docs but are not published by this stack.
#: `5000` is Frigate's internal port: the pip path points at a Frigate the operator runs
#: themselves, and we never publish it because Frigate treats requests on it as anonymous admin.
UNPUBLISHED_BUT_DOCUMENTED = {5000}


def _published_ports() -> set[int]:
    """The *host* ports the compose file publishes, with the env default resolved.

    `"${GATUS_PORT:-3001}:8080"` publishes 3001 on the host and forwards to 8080 in the
    container, so the host port is the second-to-last element. Parsed rather than hard-coded,
    because the whole point is to notice when `compose.yml` and the guide drift apart.
    """
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    ports: set[int] = set()
    for body in (document.get("services") or {}).values():
        for mapping in (body or {}).get("ports") or []:
            # Splitting on ":" would break `${NAME:-8083}` into pieces, so peel the container
            # port off the end first and then the host part, which may be an env default.
            head, _, container = str(mapping).rpartition(":")
            if not container.isdigit() or not head:
                continue  # a bare container port: reachable inside the network, not published
            # `${GATUS_PORT:-3001}` puts a colon inside the expression, so take the default out
            # of the expansion rather than splitting on that colon.
            host = head.rpartition(":-")[2].rstrip("}") if ":-" in head else head.rsplit(":", 1)[-1]
            if host.isdigit():
                ports.add(int(host))
    return ports


def test_every_published_port_appears_in_the_install_guide() -> None:
    """A guide that omits a URL leaves an operator with a running service they cannot find."""
    text = INSTALL_GUIDE.read_text(encoding="utf-8")

    missing = sorted(port for port in _published_ports() if str(port) not in text)

    assert not missing, f"published but not documented in docs/install.md: {missing}"


def test_the_install_guide_names_no_url_that_is_not_published() -> None:
    """The other direction, which is worse: a URL that looks real and refuses the connection."""
    text = INSTALL_GUIDE.read_text(encoding="utf-8")
    published = _published_ports()

    offenders = sorted(
        {int(port) for port in HOST_URL.findall(text)} - published - UNPUBLISHED_BUT_DOCUMENTED,
    )

    assert not offenders, f"docs/install.md shows host URLs on unpublished ports: {offenders}"


def test_the_readme_urls_are_ports_that_exist() -> None:
    text = README.read_text(encoding="utf-8")
    published = _published_ports()

    offenders = sorted(
        {int(port) for port in HOST_URL.findall(text)} - published - UNPUBLISHED_BUT_DOCUMENTED
    )

    assert not offenders, f"README.md shows host URLs on unpublished ports: {offenders}"


def test_the_readme_points_at_the_full_install_guide() -> None:
    """The README is the front door; the guide is the manual. One has to lead to the other."""
    text = README.read_text(encoding="utf-8")

    assert "docs/install.md" in text
