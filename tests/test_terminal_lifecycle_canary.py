from __future__ import annotations

from pathlib import Path


def test_terminal_lifecycle_canary_docs_record_the_full_closure_contract() -> None:
    docs = (
        Path(__file__).resolve().parents[1] / "docs" / "unified-work-lifecycle.md"
    ).read_text(encoding="utf-8")
    heading = "## Terminal lifecycle canary"

    assert heading in docs, "missing dedicated terminal lifecycle canary section"

    section = docs.split(heading, 1)[1].split("\n## ", 1)[0]
    required_terms = (
        "`terminal-lifecycle-canary`",
        "issue #31",
        "`planner`",
        "`builder`",
        "`reviewer`",
        "independence domain",
        "`agy/google`",
        "heterogeneous brainstorm",
        "docs-only",
        "OpenSpec validation",
        "policy",
        "preflight",
        "ForeignReview",
        "current-HEAD",
        "`needs_human`",
        "archive",
        "merge commit",
        "CompletionRecord",
        "`done`",
    )
    missing = [term for term in required_terms if term not in section]

    assert not missing, f"terminal lifecycle canary section is missing: {missing}"


def test_terminal_lifecycle_canary_archived_tasks_preserve_pre_archive_gates() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    active_change = repo_root / "openspec" / "changes" / "terminal-lifecycle-canary"
    archived_tasks = list(
        (repo_root / "openspec" / "changes" / "archive").glob(
            "*-terminal-lifecycle-canary/tasks.md"
        )
    )

    assert not active_change.exists()
    assert len(archived_tasks) == 1

    tasks = archived_tasks[0].read_text(encoding="utf-8")

    assert "- [ ]" not in tasks
    assert "change-specific changelog fragment" in tasks
    for manager_gate in ("ForeignReview", "archive", "merge commit", "done projection"):
        assert manager_gate not in tasks

    assert (repo_root / "changelog.d" / "terminal-lifecycle-canary.md").is_file()
    assert not (repo_root / "changelog.d" / "31-terminal-lifecycle-canary.md").exists()
