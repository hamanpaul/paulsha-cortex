"""#938：同內容的 planning authority refs 在 envelope 上限內只計量一次。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager


def _run_with_inputs(
    tmp_path: Path,
    files: tuple[tuple[str, bytes], ...],
) -> tuple[Path, SimpleNamespace, tuple[str, ...]]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    authority = []
    refs = []
    for ref, data in files:
        target = repo_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        refs.append(ref)
        authority.append(
            SimpleNamespace(
                ref=ref,
                kind="spec",
                baseline_sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    run = SimpleNamespace(
        run_id="workflow-" + "a" * 20,
        work_id="demo",
        repo="hamanpaul/paulsha-cortex",
        source_revision="rev-demo",
        workspace_root=str(tmp_path / "operator"),
        planning_authority=tuple(authority),
    )
    return repo_root, run, tuple(refs)


def test_duplicate_sha256_inputs_count_once_and_keep_each_snapshot_row(tmp_path: Path) -> None:
    same_content = b"a" * 70_000
    other_content = b"b" * 60_000
    files = (
        ("openspec/changes/demo/design.md", same_content),
        ("docs/superpowers/specs/demo-design.md", same_content),
        ("docs/superpowers/plans/demo.md", other_content),
    )
    repo_root, run, refs = _run_with_inputs(tmp_path, files)

    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=repo_root,
        patterns=refs,
        coordinator_root=tmp_path / "coordinator",
    )

    assert tuple(row["path"] for row in snapshot) == refs
    assert len({row["sha256"] for row in snapshot}) == 2


def test_overflow_error_lists_limit_and_each_ref_byte_size(tmp_path: Path) -> None:
    same_content = b"a" * 70_000
    other_content = b"b" * 62_000
    files = (
        ("openspec/changes/demo/design.md", same_content),
        ("docs/superpowers/specs/demo-design.md", same_content),
        ("openspec/changes/demo/tasks.md", other_content),
    )
    repo_root, run, refs = _run_with_inputs(tmp_path, files)

    with pytest.raises(ValueError) as exc_info:
        manager._workflow_input_snapshot(
            run=run,
            repo_root=repo_root,
            patterns=refs,
            coordinator_root=tmp_path / "coordinator",
        )

    message = str(exc_info.value)
    assert "limit=131072 bytes" in message
    for ref, data in files:
        assert f"{ref}={len(data)} bytes" in message
    assert manager.WORKFLOW_INPUT_ENVELOPE_MAX_BYTES == 131_072
