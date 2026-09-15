from __future__ import annotations

from pathlib import Path

import yaml

from paulsha_cortex.monitor.correlation import (
    SourceLink,
    load_work_item_overrides,
    read_frontmatter_work_item,
)
from paulsha_cortex.monitor.providers import RepoWorkProvider


REPO_ROOT = Path(__file__).resolve().parents[1]
TODO_REF = "docs/superpowers/workstreams/r3-testpilot-case-corpus/todo.md"


def test_r3_case_corpus_work_item_tracks_its_proposed_candidate_inventory_todo() -> None:
    """#904 RED: the accepted plan says this proposed todo is the single case inventory source."""

    todo = REPO_ROOT / TODO_REF
    overrides = load_work_item_overrides(REPO_ROOT)
    links = overrides.work_items["r3-testpilot-case-corpus"].links

    assert SourceLink("path", TODO_REF) in links
    assert read_frontmatter_work_item(todo) == "r3-testpilot-case-corpus"

    frontmatter = yaml.safe_load(todo.read_text(encoding="utf-8").split("---\n", 2)[1])
    assert frontmatter["status"] == "proposed"

    sources = RepoWorkProvider(REPO_ROOT, repo="hamanpaul/paulsha-cortex").scan().sources
    assert any(source.kind == "todo" and source.ref == TODO_REF for source in sources)
