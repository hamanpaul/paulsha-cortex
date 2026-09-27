"""#897／#937 部分回歸：live dogfood run `workflow-08ca9b35fc7955942a21` 顯示
`manager._validate_candidate_planning_authority()`（f0a37f72，#897／#937 部分）把
「pinned planning authority ref 在 candidate 裡缺席」一律判成 drift，卻沒有先問
「這個 ref 在這張 build 卡的 base 裡本來就缺席嗎」。

## 這個回歸要防的現場形狀

planner 依 combo 流程自行產生 `docs/superpowers/specs/<work>-spec.md`／
`-design.md`、`docs/superpovers/plans/<work>.md`，寫在 operator workspace
（``run.workspace_root``），**未 commit 進來源樹**，只是把 baseline sha256 釘進
``run.planning_authority``。build 卡的 `inputs` 只宣告這些路徑的 glob，
`_workflow_input_snapshot()` 在 provision 當下把符合的 authority 檔從
operator workspace seed 進 builder 工作區——但那是**寫入檔案系統**，不是
`git add`。tdd-red 這類只 commit 測試的卡因此在 candidate 的 git tree 裡完全沒有
這些檔案，`git show <candidate>:<ref>` 必然失敗。

修法前：不管 base 有沒有這個 ref，一律判 drift ⇒ run 卡進
`needs_human: resume-workflow-failed`，任何 planner 自產 spec/design 的 run 在
第一張 commit 卡就會被卡住。

修法後：只有「這張卡的 base **已經**有這個 ref、candidate 卻沒有」才是 drift
（builder 刪除了 pinned 檔案）；「base 本來就沒有（從未進版控）」是合法初始狀態，
builder 不可能刪除一個從來不存在於版控的東西。

base 的事實來源沿用既有推導（`_workflow_build_handoff_base` 同一套邏輯）：
``run.candidate_head``（上一張已採信 build 卡的 candidate）優先，缺席時退回這張
job 自己記錄的 ``dispatch_head``（僅在**第一張** build 卡成立，其餘卡的
``dispatch_head`` 是繼承自第一張卡的 provenance 值，不是這張卡的實際 clone
base——見 `candidate_base.py` 模組 docstring）。兩者都推不出時 fail-closed，
不得把「取不到 base」誤判成「合法缺席」。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import job_workspace, manager
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority

_BRANCH = "feature/harvest-planning-absent-in-base"
_JOB_ID = "harvest-planning-absent-in-base"
_KEY = "job-habsent-0001"

#: basename 刻意恰為 ``tasks.md``——checkbox 容忍（`_CHECKBOX_VOLATILE_PLAN_BASENAMES`）
#: 只認這個精確 basename 集合，用別的檔名不會走到那條容忍分支。
_TASKS_REF = "docs/superpowers/workstreams/absent-in-base-work/tasks.md"
_SPEC_REF = "docs/superpowers/specs/absent-in-base-work-spec.md"
_TASKS_BASELINE = "# Plan\n\n- [ ] 1. 保留任務文字。\n"
_SPEC_BASELINE = "# 規格\n\n候選必須保留 pinned bytes。\n"


# ---------------------------------------------------------------------------
# fixture helpers（沿用 test_bundle_commit_harvest_623.py 的寫法）
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "paulsha-cortex"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    _git(repo, "config", "user.email", "manager@example.invalid")
    _git(repo, "config", "user.name", "Cortex Manager")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _workspace(repo: Path, pool: Path) -> Path:
    return Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            _BRANCH, job_id=_JOB_ID
        )
    )


def _run_bundle_step(workspace: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", job_workspace.build_bundle_command(workspace=workspace, bundle=bundle)],
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )


def _dispatch(tmp_path: Path, key: str = _KEY) -> Path:
    return job_workspace.prepare_commit_spool(
        spool_key=key, coordinator_root=tmp_path / "coordinator"
    )


def _job(
    bundle: Path,
    *,
    branch: str = _BRANCH,
    workspace: Path | None = None,
    dispatch_head: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "job_id": _KEY,
        "branch": branch,
        "worktree": str(workspace) if workspace is not None else "",
        "log_path": f"/logs/workflow/{bundle.parent.name}.jsonl",
    }
    if dispatch_head is not None:
        row["dispatch_head"] = dispatch_head
    return row


def _authority() -> tuple[PlanningArtifactAuthority, ...]:
    return (
        PlanningArtifactAuthority(
            ref=_TASKS_REF,
            kind="plan",
            work_id="absent-in-base-work",
            baseline_sha256=hashlib.sha256(_TASKS_BASELINE.encode()).hexdigest(),
        ),
        PlanningArtifactAuthority(
            ref=_SPEC_REF,
            kind="spec",
            work_id="absent-in-base-work",
            baseline_sha256=hashlib.sha256(_SPEC_BASELINE.encode()).hexdigest(),
        ),
    )


def _write_operator_authority_files(repo: Path) -> None:
    """模擬 planner 把 spec/plan 寫進 operator workspace，但**不** commit。"""

    for ref, content in ((_TASKS_REF, _TASKS_BASELINE), (_SPEC_REF, _SPEC_BASELINE)):
        path = repo / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _seed_workspace_authority_files(workspace: Path) -> None:
    """模擬 `_workflow_input_snapshot()` 把 authority 檔 seed 進 builder 工作區
    （寫入檔案系統，不是 `git add`）。"""

    for ref, content in ((_TASKS_REF, _TASKS_BASELINE), (_SPEC_REF, _SPEC_BASELINE)):
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _commit_test_file_only(workspace: Path) -> str:
    """tdd-red 卡的典型行為：只 commit 測試，不動 seed 進來的 planning 檔。"""

    (workspace / "test_red.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    _git(workspace, "add", "test_red.py")
    _git(workspace, "commit", "-qm", "tdd-red: 新增失敗測試")
    return _git(workspace, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# 1. live 形狀：base 沒有、candidate 沒有 ⇒ 合法缺席，harvest 通過
# ---------------------------------------------------------------------------


def test_harvest_allows_authority_absent_from_both_base_and_candidate(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD")
    _write_operator_authority_files(repo)  # operator workspace == repo，未 commit

    workspace = _workspace(repo, tmp_path / "pool")
    _seed_workspace_authority_files(workspace)  # 模擬 seed，仍未 commit
    bundle = _dispatch(tmp_path)

    run = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,  # 第一張 build 卡：candidate 尚未錨定
        planning_authority=_authority(),
    )
    job = _job(bundle, workspace=workspace, dispatch_head=baseline)

    candidate = _commit_test_file_only(workspace)
    result = _run_bundle_step(workspace, bundle)
    assert result.returncode == 0, result.stderr

    harvested = manager._harvest_build_candidate(
        job, run=run, candidate=candidate, coordinator_root=tmp_path / "coordinator"
    )

    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


# ---------------------------------------------------------------------------
# 2. base 有、candidate 刪掉 ⇒ 仍是 drift（真的刪除）
# ---------------------------------------------------------------------------


def test_harvest_rejects_deletion_when_base_already_had_the_authority_ref(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    for ref, content in ((_TASKS_REF, _TASKS_BASELINE), (_SPEC_REF, _SPEC_BASELINE)):
        path = repo / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", _TASKS_REF, _SPEC_REF)
    _git(repo, "commit", "-qm", "pin planning baseline")
    baseline = _git(repo, "rev-parse", "HEAD")

    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    run = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,
        planning_authority=_authority(),
    )
    job = _job(bundle, workspace=workspace, dispatch_head=baseline)

    _git(workspace, "rm", "-q", _TASKS_REF)
    _git(workspace, "commit", "-qm", "builder: 誤刪 pinned 規劃文件")
    candidate = _git(workspace, "rev-parse", "HEAD")
    result = _run_bundle_step(workspace, bundle)
    assert result.returncode == 0, result.stderr

    with pytest.raises(ValueError, match="is missing from the candidate"):
        manager._harvest_build_candidate(
            job, run=run, candidate=candidate, coordinator_root=tmp_path / "coordinator"
        )

    # fail-closed：來源樹的 branch 不得被推進
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == baseline


# ---------------------------------------------------------------------------
# 3. candidate 改寫（非刪除）⇒ 既有 drift 規則不受影響
# ---------------------------------------------------------------------------


def test_harvest_still_rejects_a_rewritten_authority_file(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD")
    _write_operator_authority_files(repo)

    workspace = _workspace(repo, tmp_path / "pool")
    _seed_workspace_authority_files(workspace)
    bundle = _dispatch(tmp_path)
    run = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,
        planning_authority=_authority(),
    )
    job = _job(bundle, workspace=workspace, dispatch_head=baseline)

    rewritten = _TASKS_BASELINE.replace("保留任務文字", "改寫任務內容")
    (workspace / _TASKS_REF).write_text(rewritten, encoding="utf-8")
    # spec ref 原樣一併 commit：只讓 tasks ref 的改寫成為本測試唯一變數，
    # 不與「其他 authority ref 對 base 合法缺席」的另一條規則互相干擾。
    _git(workspace, "add", _TASKS_REF, _SPEC_REF)
    _git(workspace, "commit", "-qm", "builder: 改寫 pinned 規劃文件")
    candidate = _git(workspace, "rev-parse", "HEAD")
    result = _run_bundle_step(workspace, bundle)
    assert result.returncode == 0, result.stderr

    with pytest.raises(manager.WorkflowPlanningInputDrift):
        manager._harvest_build_candidate(
            job, run=run, candidate=candidate, coordinator_root=tmp_path / "coordinator"
        )
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == baseline


# ---------------------------------------------------------------------------
# 4. checkbox-only 變更 ⇒ 既有容忍規則不受影響
# ---------------------------------------------------------------------------


def test_harvest_still_allows_checkbox_only_change_when_absent_in_base(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD")
    _write_operator_authority_files(repo)

    workspace = _workspace(repo, tmp_path / "pool")
    _seed_workspace_authority_files(workspace)
    bundle = _dispatch(tmp_path)
    run = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,
        planning_authority=_authority(),
    )
    job = _job(bundle, workspace=workspace, dispatch_head=baseline)

    ticked = _TASKS_BASELINE.replace("- [ ]", "- [x]")
    (workspace / _TASKS_REF).write_text(ticked, encoding="utf-8")
    # spec ref 原樣一併 commit，理由同上一個測試。
    _git(workspace, "add", _TASKS_REF, _SPEC_REF)
    _git(workspace, "commit", "-qm", "builder: 勾選 pinned todo")
    candidate = _git(workspace, "rev-parse", "HEAD")
    result = _run_bundle_step(workspace, bundle)
    assert result.returncode == 0, result.stderr

    harvested = manager._harvest_build_candidate(
        job, run=run, candidate=candidate, coordinator_root=tmp_path / "coordinator"
    )
    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


# ---------------------------------------------------------------------------
# 5. base 取不到 ⇒ fail-closed（不得誤判為合法缺席）
# ---------------------------------------------------------------------------


def test_harvest_fails_closed_when_base_is_unavailable(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    _write_operator_authority_files(repo)
    baseline = _git(repo, "rev-parse", "HEAD")

    workspace = _workspace(repo, tmp_path / "pool")
    # ``ScriptWorktreeCreator.create()`` 在 provision 當下就把
    # ``refs/heads/<branch>`` 錨在 ``baseline``（#623 section 1）——這條 ref
    # 存在不代表 harvest 已經跑過，下面驗證的是它**維持原地不動**。
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == baseline
    _seed_workspace_authority_files(workspace)
    bundle = _dispatch(tmp_path)
    run = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,  # 尚未錨定
        planning_authority=_authority(),
    )
    # 這張 job 記錄沒有 dispatch_head（legacy job／記錄缺失）：base 完全推不出來。
    job = _job(bundle, workspace=workspace)

    candidate = _commit_test_file_only(workspace)
    result = _run_bundle_step(workspace, bundle)
    assert result.returncode == 0, result.stderr

    with pytest.raises(ValueError, match="base"):
        manager._harvest_build_candidate(
            job, run=run, candidate=candidate, coordinator_root=tmp_path / "coordinator"
        )
    # fail-closed：不得推進來源樹的 branch——維持在 provision 時的 baseline。
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == baseline


# ---------------------------------------------------------------------------
# 6. 中段 build 卡：base 是上一張已採信的 candidate（run.candidate_head），
#    不是 job 自己繼承自第一張卡的 dispatch_head。
# ---------------------------------------------------------------------------


def test_harvest_uses_candidate_head_as_base_for_a_later_build_card(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path)
    first_dispatch_head = _git(repo, "rev-parse", "HEAD")

    # 第一張 build 卡：commit 了 authority 檔本身（模擬「有一張卡已經把它落地」）。
    workspace = _workspace(repo, tmp_path / "pool")
    for ref, content in ((_TASKS_REF, _TASKS_BASELINE), (_SPEC_REF, _SPEC_BASELINE)):
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(workspace, "add", _TASKS_REF, _SPEC_REF)
    _git(workspace, "commit", "-qm", "card1: 落地 pinned 規劃文件")
    card1_candidate = _git(workspace, "rev-parse", "HEAD")
    bundle1 = _dispatch(tmp_path, key="job-habsent-0001")
    result1 = _run_bundle_step(workspace, bundle1)
    assert result1.returncode == 0, result1.stderr
    run_after_card1 = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=None,
        planning_authority=_authority(),
    )
    job1 = _job(bundle1, workspace=workspace, dispatch_head=first_dispatch_head)
    harvested1 = manager._harvest_build_candidate(
        job1,
        run=run_after_card1,
        candidate=card1_candidate,
        coordinator_root=tmp_path / "coordinator",
    )
    assert harvested1 == card1_candidate

    # 第二張 build 卡：從 card1 的 candidate 續建（同一顆工作區延伸，模擬中段卡），
    # 這張卡的 job 記錄「繼承」第一張卡的 dispatch_head（provenance，非實際 base）；
    # 真正的 base 是 run.candidate_head == card1_candidate，該 candidate 已含
    # authority 檔——builder 若刪除，仍須被拒收。
    _git(workspace, "rm", "-q", _TASKS_REF)
    _git(workspace, "commit", "-qm", "card2: 誤刪 pinned 規劃文件")
    card2_candidate = _git(workspace, "rev-parse", "HEAD")
    bundle2 = _dispatch(tmp_path, key="job-habsent-0002")
    result2 = _run_bundle_step(workspace, bundle2)
    assert result2.returncode == 0, result2.stderr

    run_before_card2 = SimpleNamespace(
        workspace_root=str(repo),
        candidate_head=card1_candidate,  # 上一張已採信的 candidate
        planning_authority=_authority(),
    )
    job2 = _job(
        bundle2,
        branch=_BRANCH,
        workspace=workspace,
        dispatch_head=first_dispatch_head,  # 繼承自第一張卡，不是這張卡的實際 base
    )

    with pytest.raises(ValueError, match="is missing from the candidate"):
        manager._harvest_build_candidate(
            job2,
            run=run_before_card2,
            candidate=card2_candidate,
            coordinator_root=tmp_path / "coordinator",
        )
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == card1_candidate
