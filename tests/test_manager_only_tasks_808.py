from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions


@pytest.mark.parametrize(
    ("task", "expected_error"),
    [
        (
            "Manager 於交付前執行 authoritative preflight 並採信 Candidate。",
            None,
        ),
        ("Builder 完成實作並執行測試。", "openspec-tasks-incomplete"),
        ("Manager 更新文件。", "openspec-tasks-incomplete"),
    ],
)
def test_archive_gate_only_tolerates_manager_authoritative_preflight_task(
    tmp_path: Path, task: str, expected_error: str | None
) -> None:
    change = "demo"
    change_dir = tmp_path / "openspec" / "changes" / change
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(
        f"- [x] Builder 完成實作。\n- [ ] {task}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n- **demo**：完成修正\n", encoding="utf-8"
    )

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    if expected_error is None:
        work_actions._validate_local_archive_inputs(
            repo_root=tmp_path,
            change=change,
            runner=runner,
        )
    else:
        with pytest.raises(RuntimeError, match=expected_error):
            work_actions._validate_local_archive_inputs(
                repo_root=tmp_path,
                change=change,
                runner=runner,
            )
