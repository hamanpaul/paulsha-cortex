from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest


# 本檔是外部 CLI 的整合測試：`openspec` 是 npm 套件（@fission-ai/openspec），
# 不在 Python 專案的依賴樹裡。缺 CLI 時明確 skip 並附原因（測試輸出可見），
# 而不是硬 assert 讓整個 CI 紅燈。
requires_openspec_cli = pytest.mark.skipif(
    shutil.which("openspec") is None,
    reason="需要 openspec CLI（npm 套件 @fission-ai/openspec），此環境未安裝",
)


def _seed_demo_openspec_change(root: Path, change_id: str = "docs-archived-spec-purpose-red") -> Path:
    change_root = root / "openspec" / "changes" / change_id
    (change_root / "specs" / "demo-capability").mkdir(parents=True)

    (change_root / "proposal.md").write_text(
        """---
status: accepted
work_item: docs-archived-spec-purpose-red
---

## Goals

- Ensure `openspec archive` can generate a capability spec.

## Why

This minimal change is for regression testing `openspec archive` behavior in a
reproducible way.

## What Changes

- Add a placeholder capability spec.
""",
        encoding="utf-8",
    )
    (change_root / "design.md").write_text("## Decisions\n\n- No-op for test setup.\n", encoding="utf-8")
    (change_root / "tasks.md").write_text("- [x] Task complete\n", encoding="utf-8")
    (change_root / "specs" / "demo-capability" / "spec.md").write_text(
        """## ADDED Requirements

### Requirement: Archive demo capability

The archive command MUST preserve a capability spec.

#### Scenario: Archive a change

- **WHEN** archive is executed
- **THEN** a demo spec is updated
""",
        encoding="utf-8",
    )
    return change_root


def _archive_change(repo_root: Path, change_id: str) -> Path:
    result = subprocess.run(
        ["openspec", "archive", "-y", change_id],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    return repo_root / "openspec" / "specs" / "demo-capability" / "spec.md"


@requires_openspec_cli
def test_archive_spec_does_not_keep_tbd_purpose(tmp_path: Path) -> None:
    change_id = "docs-archived-spec-purpose-red"
    repo_root = tmp_path / "repo"
    _seed_demo_openspec_change(repo_root, change_id)
    spec_path = _archive_change(repo_root, change_id)

    content = spec_path.read_text(encoding="utf-8")
    match = re.search(r"^## Purpose\n([\s\S]*?)\n##", content, re.M)
    assert match is not None, "Archived spec must include a Purpose section"
    purpose = match.group(1).strip().splitlines()[0].strip()
    assert "TBD" not in purpose
