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
