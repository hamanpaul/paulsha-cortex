"""Tests for cross-repo fanout plan pinning (#286)."""

import os
from pathlib import Path
import pytest

from git_fixtures import make_fake_repo
from paulsha_cortex.coordinator import autonomy


def _v1_verification_block(*, docs_class: str = "code") -> str:
    return (
        "target_branch: main\n"
        "verification:\n"
        f"  docs_class: {docs_class}\n"
        "  required_artifacts: []\n"
        "  checks:\n"
        "    - kind: persona-scope\n"
        "    - kind: command\n"
        "      name: policy\n"
        "      argv: [python3, -m, pytest, -q]\n"
        "      cwd: .\n"
        "      timeout_seconds: 30\n"
        "  tests: []\n"
        "  full_suite:\n"
        "    argv: [python3, -m, pytest, -q]\n"
        "    cwd: .\n"
        "    timeout_seconds: 60\n"
        "    baseline: no-regression"
    )


def test_infer_repo_root_cross_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_infer_repo_root 應對 spec 檔解析其所屬 repo_root，無視 manager 的 PSC_REPO_ROOT 設定。"""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    make_fake_repo(repo_a)
    make_fake_repo(repo_b)

    spec_path = repo_a / "specs" / "feature-cross.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(f"slice_id: cross\ndispatch: auto\nplan: docs/plan.md\n{_v1_verification_block()}\n")

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_b.resolve()))

    inferred = autonomy._infer_repo_root(spec_path)
    assert inferred == repo_a.resolve(), f"Expected {repo_a.resolve()}, got {inferred}"


def test_pin_dispatch_inputs_cross_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pin_dispatch_inputs 應正確將 plan path 解析至 spec 所屬的 repo_a。"""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    make_fake_repo(repo_a)
    make_fake_repo(repo_b)

    specs_dir = repo_a / "specs"
    specs_dir.mkdir(parents=True)
    spec_path = specs_dir / "feature-cross.md"
    spec_path.write_text(
        "---\n"
        "slice_id: feature-cross\n"
        "dispatch: auto\n"
        "plan: docs/superpowers/plans/feature-cross-plan.md\n"
        f"{_v1_verification_block()}\n"
        "---\n"
    )

    plan_path = repo_a / "docs" / "superpowers" / "plans" / "feature-cross-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_text = "# Cross Repo Plan\n"
    plan_path.write_text(plan_text)

    # PSC_REPO_ROOT 指向 repo_b
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_b.resolve()))

    meta = autonomy.parse_spec_frontmatter(spec_path)
    pinned = autonomy.pin_dispatch_inputs(meta)

    assert pinned["spec_path"] == str(spec_path.resolve())
    assert pinned["plan_path"] == str(plan_path.resolve())
    assert (repo_b.resolve() / "docs" / "superpowers" / "plans" / "feature-cross-plan.md").as_posix() not in pinned["plan_path"]


def test_ready_and_fanout_consistency_cross_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """斷言 ready 與 fanout (pinning) 對同一組跨 repo 輸入給出一致判定與相同的 repo_root。"""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    make_fake_repo(repo_a)
    make_fake_repo(repo_b)

    specs_dir = repo_a / "specs"
    specs_dir.mkdir(parents=True)
    spec_path = specs_dir / "feature-cross.md"
    spec_path.write_text(
        "---\n"
        "slice_id: feature-cross\n"
        "dispatch: auto\n"
        "plan: docs/superpowers/plans/feature-cross-plan.md\n"
        f"{_v1_verification_block()}\n"
        "---\n"
    )

    plan_path = repo_a / "docs" / "superpowers" / "plans" / "feature-cross-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("# Plan\n")

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_b.resolve()))

    metas = autonomy.scan_specs(specs_dir)
    ready = autonomy.ready_units(metas, is_satisfied=lambda _: True)
    assert len(ready) == 1
    assert ready[0]["slice_id"] == "feature-cross"

    # fanout 階段 (pin_dispatch_inputs) 不應拋出 FileNotFoundError / ValueError
    pinned = autonomy.pin_dispatch_inputs(ready[0])
    assert pinned["plan_path"] == str(plan_path.resolve())


def test_ready_and_fanout_missing_plan_consistency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """當 plan 檔不存在時，pin_dispatch_inputs 指向 repo_a 的正確報錯路徑，落實 fail-closed 判準。"""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    make_fake_repo(repo_a)
    make_fake_repo(repo_b)

    specs_dir = repo_a / "specs"
    specs_dir.mkdir(parents=True)
    spec_path = specs_dir / "feature-missing.md"
    spec_path.write_text(
        "---\n"
        "slice_id: feature-missing\n"
        "dispatch: auto\n"
        "plan: docs/superpowers/plans/non-existent.md\n"
        f"{_v1_verification_block()}\n"
        "---\n"
    )

    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_b.resolve()))

    meta = autonomy.parse_spec_frontmatter(spec_path)
    with pytest.raises(ValueError) as excinfo:
        autonomy.pin_dispatch_inputs(meta)
    # 報錯訊息必須顯示 repo_a 的正確路徑，而非 repo_b 錯的路徑
    expected_path = (repo_a / "docs" / "superpowers" / "plans" / "non-existent.md").resolve().as_posix()
    assert expected_path in str(excinfo.value)
