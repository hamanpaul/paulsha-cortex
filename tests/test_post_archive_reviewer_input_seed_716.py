"""#716：post-archive reviewer 不得從 operator_root 補入過期的 active OpenSpec 檔。

live canary（run 37297832560）：build → verification → code-review →
adversarial-review → Manager `openspec-archive`（把 `openspec/changes/<change>/`
搬進 `openspec/changes/archive/<date>-<change>/`）→ post-archive verification。
pre-archive verification PASS；post-archive verification 卻連兩輪 FAIL，理由都是
「`openspec/changes/<change>/tasks.md` 仍未勾選」——builder 修復輪確認候選裡的
archive tasks.md 全部 `[x]`、什麼都沒改，下一輪照樣 FAIL。

根因：#219 的 `_reviewer_input_patterns` 把每個 planning authority ref 都加成
reviewer 輸入；post-archive 候選樹已沒有 active 路徑，`_workflow_input_snapshot`
於是退回 operator_root（`run.workspace_root`，來源樹仍是 claim 當下未勾選的
active 檔，hash 恰等於 baseline），把它當未追蹤檔 seed 進 reviewer 的候選樹，再由
`_create_reviewer_sandbox` 物化進 sandbox——verifier 看到一個「復活」的 active
change 與官方 archive 並存，contract 的 source_material 也是同一份過期內容。

本檔沿用 `test_builder_tasks_tick_verify_dispatch.py`（#296）的 reviewer 派工序列
（authority map → input snapshot → authority proof → sandbox → 重驗），以真 git
repo 重現 archive commit 之後的候選樹。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import review as foreign_review
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep

CHANGE = "demo-716"
ACTIVE_DIR = f"openspec/changes/{CHANGE}"
TASKS_REF = f"{ACTIVE_DIR}/tasks.md"
PROPOSAL_REF = f"{ACTIVE_DIR}/proposal.md"
ARCHIVE_ENTRY = f"2026-10-05-{CHANGE}"
ARCHIVED_TASKS = f"openspec/changes/archive/{ARCHIVE_ENTRY}/tasks.md"
ARCHIVED_PROPOSAL = f"openspec/changes/archive/{ARCHIVE_ENTRY}/proposal.md"

TASKS_BASELINE = """---
status: accepted
work_item: demo-716
---

# Tasks

- [ ] 1.1 RED：新增測試。
- [ ] 1.2 GREEN：實作。
"""
TASKS_TICKED = TASKS_BASELINE.replace("- [ ] 1.1", "- [x] 1.1").replace("- [ ] 1.2", "- [x] 1.2")
PROPOSAL_BASELINE = "# Proposal\n\nAccepted scope: deployment canary probe.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, ref: str, text: str) -> None:
    target = root / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _candidate_repo(
    tmp_path: Path,
    *,
    archived: dict[str, str] | None = None,
    archive_entries: tuple[str, ...] = (ARCHIVE_ENTRY,),
) -> tuple[Path, str]:
    """真 git repo：base（claim 當下的 active change）→ build 勾選 → Manager archive。

    archive commit 刪掉 active 目錄、把（可被測試替換的）內容落在
    `openspec/changes/archive/<entry>/`，與官方 `openspec archive` 搬移後的形狀相同。
    """

    repo = tmp_path / "candidate"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "canary@example.invalid")
    _git(repo, "config", "user.name", "Canary")
    _write(repo, TASKS_REF, TASKS_BASELINE)
    _write(repo, PROPOSAL_REF, PROPOSAL_BASELINE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _write(repo, TASKS_REF, TASKS_TICKED)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "build: tick task checkboxes")
    shutil.rmtree(repo / ACTIVE_DIR)
    contents = archived or {"tasks.md": TASKS_TICKED, "proposal.md": PROPOSAL_BASELINE}
    for entry in archive_entries:
        for name, text in contents.items():
            _write(repo, f"openspec/changes/archive/{entry}/{name}", text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"chore(openspec): archive {CHANGE}")
    return repo, _git(repo, "rev-parse", "HEAD")


def _operator_root(tmp_path: Path) -> Path:
    """來源樹（`run.workspace_root`）：停在 claim 當下，active change 未勾選。"""

    root = tmp_path / "operator"
    _write(root, TASKS_REF, TASKS_BASELINE)
    _write(root, PROPOSAL_REF, PROPOSAL_BASELINE)
    return root


def _archive_step(gate_result: str) -> WorkflowStep:
    return WorkflowStep(
        phase="ship",
        persona="manager",
        card="openspec-archive",
        executor="cortex-manager",
        model="deterministic",
        domain="cortex",
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _run(operator_root: Path, candidate: str, *, steps: tuple[WorkflowStep, ...]):
    return SimpleNamespace(
        run_id="workflow-" + "7" * 20,
        work_id="demo-716",
        repo="hamanpaul/paulsha-cortex",
        source_revision="2" * 64,
        candidate_head=candidate,
        workspace_root=str(operator_root),
        openspec_refs=(CHANGE,),
        steps=steps,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=TASKS_REF, kind="plan", work_id="demo-716",
                baseline_sha256=_sha(TASKS_BASELINE),
            ),
            PlanningArtifactAuthority(
                ref=PROPOSAL_REF, kind="spec", work_id="demo-716",
                baseline_sha256=_sha(PROPOSAL_BASELINE),
            ),
        ),
    )


def _dispatch_reviewer_like(
    run, *, repo: Path, coordinator_root: Path, archive_applied: bool | None = None
):
    """`manager._dispatch_workflow_card` 的 `persona == "reviewer"` 分支逐步重現。

    review 卡在 deck 預設沒有 `requires`，planning authority 全靠 #219 的
    `_reviewer_input_patterns` 補進輸入——那正是本缺陷的入口。
    """

    kwargs = {} if archive_applied is None else {"archive_applied": archive_applied}
    root = repo.resolve()
    authority_map = manager._authority_map_with_checkbox_tolerance(
        run, candidate_root=root, **kwargs
    )
    patterns = manager._reviewer_input_patterns(run, ())
    input_snapshot = manager._workflow_input_snapshot(
        run=run, repo_root=root, patterns=patterns,
        coordinator_root=coordinator_root, **kwargs,
    )
    foreign_review.verify_authority_in_input_snapshot(
        authority=authority_map, input_snapshot=input_snapshot,
    )
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run, step=SimpleNamespace(card="verification"), executor="codex",
        candidate_root=root, coordinator_root=coordinator_root,
        input_snapshot=input_snapshot, job_id="wf-reviewer-716",
    )
    manager._validate_workflow_input_snapshot(
        checkout, list(input_snapshot), coordinator_root=coordinator_root,
    )
    return sandbox, checkout, input_snapshot


@pytest.mark.parametrize(
    "archive_evidence",
    ["declared-archive-step", "dispatch-archive-applied"],
)
def test_post_archive_reviewer_reads_candidate_archive_not_stale_operator_copy(
    tmp_path: Path, archive_evidence: str
) -> None:
    """live canary 的形狀：候選只剩官方 archive（tasks 全勾），來源樹仍是未勾選的
    active 檔。reviewer 不得拿到任何 active change 檔；snapshot／source_material 的
    authority 必須是候選自己的 archive 副本。

    `dispatch-archive-applied`：未宣告 `openspec-archive` 卡的 combo（fix-standard）
    靠 archive job 證據判定，dispatch 以 `archive_applied=` 傳入同一個事實。
    """

    operator_root = _operator_root(tmp_path)
    repo, candidate = _candidate_repo(tmp_path)
    coordinator_root = tmp_path / "coordinator"
    if archive_evidence == "declared-archive-step":
        run = _run(operator_root, candidate, steps=(_archive_step("passed"),))
        archive_applied = None
    else:
        run = _run(operator_root, candidate, steps=())
        archive_applied = True

    sandbox, checkout, input_snapshot = _dispatch_reviewer_like(
        run, repo=repo, coordinator_root=coordinator_root, archive_applied=archive_applied
    )
    try:
        # 候選樹與 reviewer sandbox 都不得長出 active change 目錄。
        assert not (repo / ACTIVE_DIR).exists()
        assert not (checkout / ACTIVE_DIR).exists()
        rows = {row["path"]: row for row in input_snapshot}
        assert set(rows) == {ARCHIVED_TASKS, ARCHIVED_PROPOSAL}
        assert all(row["authority"] == "planning-authority" for row in rows.values())
        assert rows[ARCHIVED_TASKS]["sha256"] == _sha(TASKS_TICKED)
        assert rows[ARCHIVED_PROPOSAL]["sha256"] == _sha(PROPOSAL_BASELINE)
        # contract 的 source_material 由 content envelope 組成：必須是已勾選的 archive 內容。
        envelope = manager._read_workflow_input_content(
            rows[ARCHIVED_TASKS], run=run, coordinator_root=coordinator_root
        )
        assert envelope["content"] == TASKS_TICKED
        assert (checkout / ARCHIVED_TASKS).read_text(encoding="utf-8") == TASKS_TICKED
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_post_archive_ignores_stale_active_seed_left_in_reused_review_tree(
    tmp_path: Path,
) -> None:
    """同一個 candidate 的 review 卡共用同一棵候選樹（#650）。修正前 seed 進去的
    過期 active 檔（未追蹤）仍留在樹上時，snapshot 也不得把它當 authority。"""

    operator_root = _operator_root(tmp_path)
    repo, candidate = _candidate_repo(tmp_path)
    _write(repo, TASKS_REF, TASKS_BASELINE)
    _write(repo, PROPOSAL_REF, PROPOSAL_BASELINE)
    run = _run(operator_root, candidate, steps=(_archive_step("passed"),))
    coordinator_root = tmp_path / "coordinator"

    sandbox, checkout, input_snapshot = _dispatch_reviewer_like(
        run, repo=repo, coordinator_root=coordinator_root
    )
    try:
        assert {row["path"] for row in input_snapshot} == {ARCHIVED_TASKS, ARCHIVED_PROPOSAL}
        assert not (checkout / ACTIVE_DIR).exists()
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


@pytest.mark.parametrize(
    "archived",
    [
        {
            "tasks.md": TASKS_TICKED.replace("GREEN：實作。", "GREEN：偷改任務語意。"),
            "proposal.md": PROPOSAL_BASELINE,
        },
        {
            "tasks.md": TASKS_TICKED,
            "proposal.md": PROPOSAL_BASELINE.replace("canary probe.", "偷改規格範圍。"),
        },
    ],
    ids=["tasks-substantive-edit", "proposal-edit"],
)
def test_post_archive_archived_planning_drift_still_fails_closed(
    tmp_path: Path, archived: dict[str, str]
) -> None:
    """archive 副本沿用 pre-archive 的 #310 規則：tasks.md 只容忍 checkbox 差異，
    其他 byte 差異（含 kind=spec 的 proposal）一律 `WorkflowPlanningInputDrift`。"""

    operator_root = _operator_root(tmp_path)
    repo, candidate = _candidate_repo(tmp_path, archived=archived)
    run = _run(operator_root, candidate, steps=(_archive_step("passed"),))

    with pytest.raises(manager.WorkflowPlanningInputDrift, match="planning input drift"):
        _dispatch_reviewer_like(run, repo=repo, coordinator_root=tmp_path / "coordinator")
    assert not (repo / ACTIVE_DIR).exists()


@pytest.mark.parametrize("shape", ["ambiguous", "symlink-entry", "missing-file"])
def test_post_archive_unresolvable_archive_fails_closed(tmp_path: Path, shape: str) -> None:
    """archive 對應不唯一、entry 是 symlink、或唯一 entry 缺該檔：一律 fail-closed，
    不退回從 operator_root seed 過期的 active 檔。"""

    operator_root = _operator_root(tmp_path)
    if shape == "ambiguous":
        repo, candidate = _candidate_repo(
            tmp_path, archive_entries=(ARCHIVE_ENTRY, f"2026-10-06-{CHANGE}")
        )
        expected = "ambiguous"
    elif shape == "symlink-entry":
        repo, _ = _candidate_repo(tmp_path, archive_entries=())
        _write(repo, "elsewhere/tasks.md", TASKS_TICKED)
        _write(repo, "elsewhere/proposal.md", PROPOSAL_BASELINE)
        (repo / "openspec" / "changes" / "archive").mkdir(parents=True)
        (repo / "openspec" / "changes" / "archive" / ARCHIVE_ENTRY).symlink_to(
            "../../../elsewhere"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "symlinked archive entry")
        candidate = _git(repo, "rev-parse", "HEAD")
        expected = "symlink rejected"
    else:
        repo, candidate = _candidate_repo(tmp_path, archived={"tasks.md": TASKS_TICKED})
        expected = "planning input missing"
    run = _run(operator_root, candidate, steps=(_archive_step("passed"),))

    with pytest.raises(ValueError, match=expected):
        _dispatch_reviewer_like(run, repo=repo, coordinator_root=tmp_path / "coordinator")
    assert not (repo / ACTIVE_DIR).exists()


def test_without_manager_archive_evidence_absent_ref_is_still_seeded(tmp_path: Path) -> None:
    """pre-archive 行為不變：沒有 Manager archive 證據時，候選缺席的 authority ref
    照舊由 operator_root seed（即使候選樹裡剛好有同名的 archive 目錄）。"""

    operator_root = _operator_root(tmp_path)
    repo, candidate = _candidate_repo(tmp_path)
    run = _run(operator_root, candidate, steps=(_archive_step("pending"),))
    coordinator_root = tmp_path / "coordinator"

    sandbox, checkout, input_snapshot = _dispatch_reviewer_like(
        run, repo=repo, coordinator_root=coordinator_root
    )
    try:
        rows = {row["path"]: row for row in input_snapshot}
        assert set(rows) == {TASKS_REF, PROPOSAL_REF}
        assert rows[TASKS_REF]["sha256"] == _sha(TASKS_BASELINE)
        assert (repo / TASKS_REF).read_text(encoding="utf-8") == TASKS_BASELINE
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
