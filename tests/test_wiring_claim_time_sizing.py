"""#208 收口 wiring 1：claim 時計算 sizing 並寫入 run。

落點：``work_bridge.start_canonical_workflow``（組 run 前嘗試算好 sizing，
fail-soft 注入 ``_manager_create_workflow_run(..., sizing_score=, sizing_band=)``）
與 ``work_actions._claim_action``（回傳 dict 帶 ``sizing_unavailable`` 可觀測標記）。

驗收條件對應：
1. 能取得既有 plan artifact（openspec tasks.md 已落地）＋ deck combo 資訊時，
   claim 產生的 run 確實掛上 sizing_score/sizing_band（「確實發生」而非「允許
   發生」）。
2. plan 缺 domain_breadth/state_consistency 宣告欄位（舊 plan）時 fail-soft：
   run 不掛 band（None），行為與現行完全相同。
3. ``_claim_action`` 回傳的 run dict 帶 ``sizing_unavailable`` 可觀測標記，
   如實反映 sizing 是否算得出來。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from paulsha_cortex.coordinator import work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.coordinator.registry import JobRegistry


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:acme/demo.git"],
        check=True,
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def _write_planning_docs(root: Path, *, declare_sizing_dimensions: bool) -> None:
    base = root / "openspec/changes/work"
    base.mkdir(parents=True, exist_ok=True)
    (base / "proposal.md").write_text(
        "---\nstatus: accepted\n---\n# Proposal\n## Requirements\nReady.\n",
        encoding="utf-8",
    )
    (base / "design.md").write_text(
        "---\nstatus: accepted\n---\n# Design\n## Decisions\nReady.\n",
        encoding="utf-8",
    )
    plan_frontmatter = "status: accepted\n"
    if declare_sizing_dimensions:
        plan_frontmatter += "domain_breadth: 1\nstate_consistency: 1\n"
    (base / "tasks.md").write_text(
        f"---\n{plan_frontmatter}---\n# Tasks\n## Task 1\nBuild.\n",
        encoding="utf-8",
    )


def _snapshot(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "work",
                        "mapped_issues": [14],
                        "mapped_prs": [],
                        "mapped_openspec": ["work"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": [
                            "github_issue:acme/demo#14@issue-open",
                            "openspec:acme/demo:work@spec-1",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _authority(tmp_path: Path):
    snapshot = _snapshot(tmp_path / "snapshot.json")
    return load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)


def test_claim_time_sizing_computed_when_plan_and_combo_available(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _write_planning_docs(root, declare_sizing_dimensions=True)
    authority = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "a" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=root,
        needs_human_reason="missing_issue",
    )

    # feature-oneshot combo (real, checked-in): gate_spine=4, cards=11,
    # persona_binding_count=11 → acceptance_surfaces=2, spec_stability=0,
    # orchestration=2；加上宣告的 domain_breadth=1/state_consistency=1 → total=6.
    assert run.sizing_score == 6
    assert run.sizing_band == "yellow"
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.sizing_score == 6
    assert persisted.sizing_band == "yellow"


def test_claim_time_sizing_fails_soft_when_plan_lacks_declared_dimensions(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    _write_planning_docs(root, declare_sizing_dimensions=False)
    authority = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "b" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=root,
        needs_human_reason="missing_issue",
    )

    assert run.sizing_score is None
    assert run.sizing_band is None


def test_claim_time_sizing_fails_soft_when_no_plan_artifact_on_disk(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    # 沒有寫任何 openspec/changes/work/* 檔案：_artifact_rows 回傳空清單。
    authority = _authority(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "c" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=root,
        needs_human_reason="missing_issue",
    )

    assert run.sizing_score is None
    assert run.sizing_band is None


def test_current_sizing_snapshot_helper_is_fail_soft_on_missing_combo(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _write_planning_docs(root, declare_sizing_dimensions=True)
    rows = [
        {"kind": "spec", "ref": "openspec/changes/work/proposal.md"},
        {"kind": "design", "ref": "openspec/changes/work/design.md"},
        {"kind": "plan", "ref": "openspec/changes/work/tasks.md"},
    ]
    score, band = work_bridge.current_sizing_snapshot(
        workspace_root=root, combo_name="does-not-exist-combo", artifact_rows=rows
    )
    assert score is None
    assert band is None


# ---------------------------------------------------------------------------
# _claim_action 的可觀測標記：sizing_unavailable 如實反映 sizing 是否算得出來
# ---------------------------------------------------------------------------


def test_claim_action_marks_sizing_unavailable_when_starter_yields_no_band(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import work_actions

    authority = _authority(tmp_path)

    class _Run:
        def to_dict(self):
            return {
                "run_id": "workflow-" + "d" * 20,
                "repo": "acme/demo",
                "work_id": "work",
                "claim_key": "claim:v1:" + "d" * 64,
                "status": "ongoing",
                "sizing_score": None,
                "sizing_band": None,
            }

    result = work_actions._claim_action(
        args={"action": "start", "issue": 14},
        authority=authority,
        now_epoch=200,
        state_path=tmp_path / "runs.json",
        workflow_starter=lambda *_args: _Run(),
    )
    assert result["action"] == "claim"
    assert result["run"]["sizing_unavailable"] is True


def test_claim_action_marks_sizing_available_when_starter_yields_a_band(
    tmp_path: Path,
) -> None:
    from paulsha_cortex.coordinator import work_actions

    authority = _authority(tmp_path)

    class _Run:
        def to_dict(self):
            return {
                "run_id": "workflow-" + "e" * 20,
                "repo": "acme/demo",
                "work_id": "work",
                "claim_key": "claim:v1:" + "e" * 64,
                "status": "ongoing",
                "sizing_score": 2,
                "sizing_band": "green",
            }

    result = work_actions._claim_action(
        args={"action": "start", "issue": 14},
        authority=authority,
        now_epoch=200,
        state_path=tmp_path / "runs.json",
        workflow_starter=lambda *_args: _Run(),
    )
    assert result["action"] == "claim"
    assert result["run"]["sizing_unavailable"] is False
