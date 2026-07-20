from __future__ import annotations

import re
from pathlib import Path

from paulsha_cortex.monitor.correlation import (
    SourceLink,
    load_work_item_overrides,
    read_frontmatter_work_item,
)
from paulsha_cortex.monitor.providers import RepoWorkProvider


def test_terminal_canary_has_one_complete_confirmed_todo() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    todo_ref = "docs/superpowers/workstreams/terminal-lifecycle-canary/todo.md"
    todo = repo_root / todo_ref

    overrides = load_work_item_overrides(repo_root)
    links = overrides.work_items["terminal-lifecycle-canary"].links
    assert SourceLink("path", todo_ref) in links
    assert read_frontmatter_work_item(todo) == "terminal-lifecycle-canary"

    sources = RepoWorkProvider(repo_root, repo="hamanpaul/paulsha-cortex").scan().sources
    assert any(source.kind == "todo" and source.ref == todo_ref for source in sources)

    states = re.findall(
        r"(?m)^\s*[-*]\s+\[([ xX])\]\s+",
        todo.read_text(encoding="utf-8"),
    )
    assert states and all(state.lower() == "x" for state in states)
