"""issue #658：build 卡被採信之後即時回收它的工作區。

#648 把 canonical lane 的工作區改成 per-job，一個 run 因此會累積 N 棵約 35MB 的
clone；#649／#653（ship 段自己的樹）與 #650／#659（verify／review 的 candidate 樹）
把最後兩個下游消費端也搬走之後，**一張 build 卡被採信、`_harvest_build_candidate()`
走完之後，它的工作區已經沒有任何獨佔資訊**。本檔把「即時回收」釘成四組不變式：

1. **回收確實發生，且 run 仍走得完**：被採信的卡的工作區消失，後續卡照樣拿得到
   base 與內容（交接走的是 #637 的 bundle ＋ spool，不是磁碟殘留）。
2. **佔用不隨卡數線性成長**：任一時刻 pool 裡最多一棵 build 工作區。
3. **fail-closed**：未被採信的工作區、pool 以外的路徑、認不出形狀的目錄、成果沒
   回到來源樹的情形，一律**不回收**，且各自有具名理由。
4. **重入**：`retry-card`（#601／#545）與 `abandon`（#613／#527）在新模型下不產生
   新的死路。

外加一組契約測試：`worktree_reclaim` 的兩種 evidence 模型——`preserve`（預設，
#478 的語意逐字不變）與 `harvested`（#658 新增，只有已 harvest 的採信路徑能用）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex.coordinator import (
    gc,
    job_runner,
    job_workspace,
    manager,
    seams,
    terminal_contract,
    work_actions,
    worktree_reclaim,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


_REPO = "hamanpaul/paulsha-cortex"
_WORK_ID = "658-immediate-reclaim"


# ---------------------------------------------------------------------------
# fixtures（形狀沿用 tests/test_canonical_per_job_workspace_648.py：真 git repo、
# 真 `ScriptWorktreeCreator`、真 dispatch、真 bundle ＋ spool 交接）
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    _git(root, "config", "user.email", "manager@example.invalid")
    _git(root, "config", "user.name", "Cortex Manager")
    (root / "README.md").write_text("source\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _build_step(card: str, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card=card,
        executor="copilot" if gate_result == "passed" else None,
        model="gpt" if gate_result == "passed" else None,
        domain="openai" if gate_result == "passed" else None,
        inputs=(),
        outputs=(),
        commit_policy="required",
        # `test_policy="none"` ⇒ `expected_gate_names_for_test_policy()` 是空集合，
        # 空 ledger 即合法（#308）。本檔要驗的是回收，不是 gate 語意。
        test_policy="none",
        gate_result=gate_result,
    )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [{
            "executor": "copilot",
            "model_id": "gpt",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self) -> "_RecordingLauncher":
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self.calls.append({"slice_id": slice_id, "worktree": worktree})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _run_with_cards(
    registry: JobRegistry, *, workspace: Path, cards: tuple[str, ...]
) -> Any:
    return registry._manager_create_workflow_run(
        work_id=_WORK_ID,
        repo=_REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="build",
        steps=tuple(_build_step(card) for card in cards),
        issue_refs=(f"{_REPO}#658",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )


def _dispatch(
    registry: JobRegistry,
    *,
    run,
    workspace: Path,
    pool: Path,
    coordinator_root: Path,
    force_new_card: bool = False,
) -> dict[str, object]:
    creator = seams.ScriptWorktreeCreator(repo=workspace, wt_root=pool, base="main")
    dispatcher = type(
        "D",
        (),
        {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    dispatched = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=_identities(),
        launcher_factory=lambda _identity: _RecordingLauncher(),
        coordinator_root=coordinator_root,
        force_new_card=force_new_card,
    )
    assert dispatched is not None
    return registry.get_job(str(dispatched["job_id"]))


def _commit_in(clone: Path, *, filename: str) -> str:
    (clone / filename).write_text("candidate\n", encoding="utf-8")
    _git(clone, "add", filename)
    _git(clone, "commit", "-qm", f"add {filename}")
    return _git(clone, "rev-parse", "HEAD").lower()


def _harvest_through_the_spool(
    *, workspace: Path, clone: Path, branch: str, spool_key: str, coordinator_root: Path
) -> str:
    """完整跑一次 #637 的交接：builder 產 bundle → Manager 從那個檔 fetch。"""

    bundle = job_workspace.prepare_commit_spool(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    command = job_workspace.build_bundle_command(workspace=clone, bundle=bundle)
    produced = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert produced.returncode == 0, produced.stderr
    return job_workspace.harvest_branch(
        source_repo=workspace, bundle=bundle, branch=branch
    )


def _write_terminal(job: dict[str, object], *, run, candidate: str) -> None:
    """寫 job 的終局 JSONL ＋ 空 gate ledger（`test_policy="none"` 的合法形狀）。"""

    log = Path(str(job["log_path"]))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": run.run_id,
            "card_id": job["workflow_card"],
            "candidate": candidate,
            "outputs": [],
        }) + "\n",
        encoding="utf-8",
    )
    ledger = terminal_contract.gate_ledger_path(log)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({
            "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
            "kind": "workflow-gate-ledger",
            "slice_id": log.stem,
            "gates": [],
        }),
        encoding="utf-8",
    )


def _finish_card(
    registry: JobRegistry,
    *,
    run,
    job: dict[str, object],
    workspace: Path,
    coordinator_root: Path,
    filename: str,
    harvest: bool = True,
) -> tuple[Any, str, Path]:
    """跑完一張 build 卡的**正式**採信路徑，回傳 (run, candidate, 工作區路徑)。

    真的 commit、真的 bundle ＋ spool 交接、真的 `terminalize_workflow_job()`、真的
    `apply_workflow_action(action="advance")`——即時回收就掛在最後那一支裡面，測試
    因此驗的是生產路徑，不是直接呼叫回收函式。
    """

    clone = Path(str(job["worktree"]))
    candidate = _commit_in(clone, filename=filename)
    if harvest:
        _harvest_through_the_spool(
            workspace=workspace,
            clone=clone,
            branch=str(job["branch"]),
            spool_key=str(job["job_id"]),
            coordinator_root=coordinator_root,
        )
    _write_terminal(job, run=run, candidate=candidate)
    registry.update_headless_result(str(job["job_id"]), status="exited", exit_code=0)
    terminal = manager.terminalize_workflow_job(
        registry, job_id=str(job["job_id"]), coordinator_root=coordinator_root
    )
    manager.apply_workflow_action(
        registry,
        args={
            "action": "advance",
            "run_id": run.run_id,
            "card_id": str(job["workflow_card"]),
            "job_id": terminal["job_id"],
            "current_phase": "build",
        },
        identity_registry=_identities(),
        coordinator_root=coordinator_root,
        trusted_terminal=True,
    )
    return registry.get_workflow_run(run.run_id), candidate, clone


def _build_workspaces(pool: Path) -> list[Path]:
    return sorted(job_workspace.list_clone_workspaces(pool))


# ---------------------------------------------------------------------------
# 不變式 1／2：回收確實發生、run 仍走得完、佔用不隨卡數成長
# ---------------------------------------------------------------------------


def test_trusted_build_card_workspace_is_reclaimed_and_the_run_keeps_going(
    tmp_path: Path,
) -> None:
    """被採信的 build 卡的工作區當場消失，而後續卡照樣拿得到它的成果。

    這是本票的全部價值。**兩件事必須同時成立**：回收真的發生（不是「總有一天由
    `cortex work gc` 收」），以及回收之後這個 run 一個位元組都沒少——後續卡的 base
    來自來源樹的 `refs/heads/<branch>`（#637 的 bundle ＋ append-only spool 交接），
    與那棵被刪掉的樹無關。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    branch = str(job_one["branch"])
    run, candidate, first_clone = _finish_card(
        registry, run=run, job=job_one, workspace=workspace,
        coordinator_root=coordinator_root, filename="one.txt",
    )

    # 回收：目錄不在了，registry 也沒有殘留（clone 模型下本來就沒有 registry 記錄）。
    assert not first_clone.exists(), "被採信的 build 卡的工作區應該當場被回收"
    assert _build_workspaces(pool) == []
    # 成果完好：來源樹的 branch 就是被採信的 candidate。
    assert run.candidate_head == candidate
    assert job_workspace.source_branch_head(workspace, branch) == candidate

    # 後續卡照樣派得出去，且拿到的是帶著前一張卡成果的樹。
    job_two = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    second_clone = Path(str(job_two["worktree"]))
    assert second_clone != first_clone
    assert _git(second_clone, "rev-parse", "HEAD").lower() == candidate
    assert (second_clone / "one.txt").is_file()
    assert _git(second_clone, "rev-parse", job_workspace.BASE_REF).lower() == candidate


def test_workspace_footprint_does_not_grow_with_the_number_of_build_cards(
    tmp_path: Path,
) -> None:
    """三張 build 卡跑完，pool 裡任一時刻最多一棵工作區、結束時零棵。

    #648 之前 canonical lane 一個 run 一棵樹；per-job 之後是 N 棵（每棵約 35MB），
    #658 的驗收條款要求佔用**不隨 build 卡數線性成長**。這條用真的 per-job clone
    量測，不是對常數斷言。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    cards = ("worktree-isolation", "tdd-red", "subagent-build")
    run = _run_with_cards(registry, workspace=workspace, cards=cards)

    seen: list[Path] = []
    high_water = 0
    for index, card in enumerate(cards):
        job = _dispatch(
            registry, run=run, workspace=workspace, pool=pool,
            coordinator_root=coordinator_root,
        )
        assert job["workflow_card"] == card
        high_water = max(high_water, len(_build_workspaces(pool)))
        run, _candidate, clone = _finish_card(
            registry, run=run, job=job, workspace=workspace,
            coordinator_root=coordinator_root, filename=f"card-{index}.txt",
        )
        seen.append(clone)

    assert high_water == 1, "同時只該有一棵 build 工作區（正在跑的那一張卡）"
    assert len(set(seen)) == len(cards), "每張卡仍各自 provision 自己的樹（#648）"
    assert _build_workspaces(pool) == [], "最後一張卡被採信後 pool 應該是空的"
    # 三張卡的成果全都在來源樹裡（回收沒有銷毀任何被採信的東西）。
    assert run.candidate_head is not None
    tree = _git(workspace, "ls-tree", "--name-only", str(run.candidate_head))
    assert {f"card-{index}.txt" for index in range(len(cards))} <= set(tree.splitlines())


# ---------------------------------------------------------------------------
# 不變式 3：fail-closed——不該收的一律不收，且各有具名理由
# ---------------------------------------------------------------------------


def test_untrusted_build_workspace_is_never_reclaimed(tmp_path: Path) -> None:
    """卡沒被採信 ⇒ 工作區一個位元組都不動；`retry-card` 重派之後殘留仍在。

    #601 的生產現場是「重派需要前一次失敗嘗試的殘留被回收」。即時回收**刻意不碰**
    那條路徑：它只在採信之後跑，而失敗的 job 從來沒有被採信過。把這條釘住，是為了
    讓「即時回收」不會偷偷變成「job 退出就收」——那正是 #658 紅線點名的形狀（被回收
    的對象自己決定回收），而且會把 #601 重派要用的東西銷毀掉。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    run, _candidate, first_clone = _finish_card(
        registry, run=run, job=job_one, workspace=workspace,
        coordinator_root=coordinator_root, filename="one.txt",
    )
    assert not first_clone.exists()

    # 卡 2 首派失敗：exit_code=1，evidence 綁不上 ⇒ 從未被採信。
    job_two = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    failed_clone = Path(str(job_two["worktree"]))
    (failed_clone / "scratch.txt").write_text("half-done\n", encoding="utf-8")
    registry.update_headless_result(str(job_two["job_id"]), status="exited", exit_code=1)
    assert failed_clone.is_dir()
    assert (failed_clone / "scratch.txt").is_file()

    # `retry-card` 走真的原子重置 ＋ 真的重派。
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    run = registry._manager_reset_workflow_for_retry_card(
        run.run_id, expected_run_id=run.run_id, card="tdd-red"
    )
    job_retry = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root, force_new_card=True,
    )
    retry_clone = Path(str(job_retry["worktree"]))

    assert retry_clone != failed_clone, "#601：重派拿到自己的目錄（per-job 之後撞名消失）"
    assert failed_clone.is_dir(), "未採信的工作區不得被即時回收"
    assert (failed_clone / "scratch.txt").read_text(encoding="utf-8") == "half-done\n"
    # 重派的樹仍以最後一張**被採信**的 candidate 為 base。
    assert _git(retry_clone, "rev-parse", "HEAD").lower() == run.candidate_head


def test_reclaim_refuses_a_workspace_whose_name_is_not_the_job_segment(tmp_path: Path) -> None:
    """`job.worktree` 指到來源樹（#549 的資料語意地雷）時**拒絕回收**。

    #549 實測 `job.worktree` 會等於 run 的 `workspace_root`——那是 Manager 的
    durable state，遞迴刪除的爆炸半徑不可接受。判準刻意是「pool 的直接子項」而不是
    黑名單：provisioning 的唯一推導點 `job_workspace.workspace_path()` 產出的就是這
    個形狀，來源樹永遠不可能滿足它。
    """

    workspace = _source_repo(tmp_path / "source")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))
    candidate = _git(workspace, "rev-parse", "HEAD").lower()
    job = {
        "job_id": "wf-x-1",
        "workflow_card": "worktree-isolation",
        "branch": "feature/658",
        "worktree": str(workspace),
    }

    target, refusal = manager._trusted_build_workspace_target(
        job, run=run, candidate=candidate
    )

    assert target is None
    assert refusal == "workspace-name-not-derived-from-job-id"
    assert manager._reclaim_trusted_build_workspace(
        job, run=run, candidate=candidate
    ) is None
    assert (workspace / "README.md").is_file(), "來源樹一個位元組都不得被動到"


def test_reclaim_refuses_when_the_candidate_never_reached_the_source_tree(
    tmp_path: Path,
) -> None:
    """成果沒回到來源樹 ⇒ 不回收（`harvested` evidence 模型的前提當場複驗）。

    `EVIDENCE_HARVESTED` 之所以可以不做 preserve 封存，全部的正當性就在「每一樣受
    治理的東西都已經有第二份副本」。這裡把交接刻意跳過，回收必須拒絕——否則那顆
    commit 會隨 clone 的 object store 一起消失，正是 #478 契約要防的事。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))

    job = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    clone = Path(str(job["worktree"]))
    candidate = _commit_in(clone, filename="one.txt")  # 刻意不 harvest

    target, refusal = manager._trusted_build_workspace_target(
        job, run=run, candidate=candidate
    )

    assert target is None
    assert refusal == "candidate-not-in-source-repo"
    assert clone.is_dir()
    assert _git(clone, "rev-parse", "HEAD").lower() == candidate


def test_reclaim_refuses_a_directory_without_the_job_workspace_marker(
    tmp_path: Path,
) -> None:
    """pool 底下但認不出形狀的目錄一律不刪（#646 的紅線）。

    三分部署下 Manager 讀不進 `0700` 的 clone，`is_job_clone()` 因此回 False——這條
    同時是那個部署形態的行為規格：得到一個具名的 skip 理由，而不是一次注定失敗的
    `rmtree`。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))
    # 目錄名刻意就是那個 job_id 的片段——這樣通過第 2 條之後，擋下它的必然是
    # 標記檔那一條（#646 的紅線），而不是名字。
    stranger = job_workspace.workspace_path(pool, "wf-x-1")
    stranger.mkdir(parents=True)
    (stranger / "keep-me.txt").write_text("not ours\n", encoding="utf-8")

    target, refusal = manager._trusted_build_workspace_target(
        {
            "job_id": "wf-x-1",
            "workflow_card": "worktree-isolation",
            "branch": "feature/658",
            "worktree": str(stranger),
        },
        run=run,
        candidate=_git(workspace, "rev-parse", "HEAD").lower(),
    )

    assert target is None
    assert refusal == "workspace-not-a-job-clone"
    assert (stranger / "keep-me.txt").is_file()


def test_reclaim_failure_does_not_block_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回收失敗時採信照樣成立，且留下可操作的診斷。

    採信在回收之前就已經 durable。#658 驗收條款：「回收失敗**不得**擋住採信——但
    必須留下可操作的診斷」。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))

    job = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    clone = Path(str(job["worktree"]))

    def _explode(*_args: object, **_kwargs: object):
        raise OSError("simulated reclaim failure")

    monkeypatch.setattr(worktree_reclaim, "reclaim_worktree", _explode)

    run, candidate, _clone = _finish_card(
        registry, run=run, job=job, workspace=workspace,
        coordinator_root=coordinator_root, filename="one.txt",
    )

    assert run.candidate_head == candidate
    assert next(
        step for step in run.steps if step.card == "worktree-isolation"
    ).gate_result == "passed"
    assert "needs_human" not in run.facets
    assert clone.is_dir(), "回收失敗時工作區留著（交給 `cortex work gc`），不是半刪"


def test_reclaim_outcome_reaches_the_operator_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """成功與拒絕都寫進結構化 log——那是 operator 唯一的稽核面。"""

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))
    job = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )

    with caplog.at_level("INFO", logger="paulsha_cortex.coordinator.manager"):
        run, _candidate, _clone = _finish_card(
            registry, run=run, job=job, workspace=workspace,
            coordinator_root=coordinator_root, filename="one.txt",
        )
        text = caplog.text
    assert f"{manager.WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT}-reclaimed" in text

    caplog.clear()
    with caplog.at_level("INFO", logger="paulsha_cortex.coordinator.manager"):
        manager._reclaim_trusted_build_workspace(
            {"job_id": "x", "workflow_card": "c", "branch": "b", "worktree": str(workspace)},
            run=run,
            candidate=str(run.candidate_head),
        )
        text = caplog.text
    assert f"{manager.WORKFLOW_BUILD_WORKSPACE_RECLAIM_EVENT}-skipped" in text
    assert "workspace-name-not-derived-from-job-id" in text


# ---------------------------------------------------------------------------
# 不變式 4：abandon（#613／#527）的重入
# ---------------------------------------------------------------------------


def test_abandon_reclaim_is_a_no_op_after_immediate_reclaim(tmp_path: Path) -> None:
    """abandon 的工作區回收在即時回收之後回 `absent`——不是 `failed`。

    `work_actions._reclaim_abandoned_build_worktrees()` 掃的是這個 run 名下每一列
    job 記錄的 `worktree`。即時回收之後那些路徑已經不存在，`reclaim_worktree()` 的
    「registry 沒這筆、目錄也不在」判定為 `absent`（成功），因此 abandon 不會因為
    「東西已經被收掉了」而落一堆 failed 診斷——那正是 #658 要避免的新死路。

    **branch 仍留在來源樹上**：那是 #613 的範圍，即時回收一個 branch 名都沒碰。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    branch = str(job_one["branch"])
    run, candidate, first_clone = _finish_card(
        registry, run=run, job=job_one, workspace=workspace,
        coordinator_root=coordinator_root, filename="one.txt",
    )
    job_two = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    live_clone = Path(str(job_two["worktree"]))
    assert not first_clone.exists() and live_clone.is_dir()

    targets = [
        str(row["worktree"])
        for row in registry.list_jobs()
        if row.get("workflow_run_id") == run.run_id and row.get("worktree")
    ]
    results = worktree_reclaim.reclaim_worktrees(
        targets, repo_root=workspace, preserve_root=tmp_path / "evidence"
    )

    by_path = {result.path: result for result in results}
    assert by_path[str(first_clone)].status == worktree_reclaim.RECLAIM_ABSENT
    assert all(result.ok for result in results), [r.detail for r in results if not r.ok]
    assert not live_clone.exists(), "abandon 仍要收掉還活著的那一棵"
    # 走完整條 abandon 掛點也不得拋（best-effort 契約）。
    work_actions._reclaim_abandoned_build_worktrees(
        run, registry, state_path=state_path
    )
    # #613：branch 與它承載的 commit 原封不動——回收 branch 不在本票範圍。
    assert job_workspace.source_branch_head(workspace, branch) == candidate


def test_gc_still_protects_the_delivery_branch_when_no_workspace_is_left(
    tmp_path: Path,
) -> None:
    """所有 build 工作區被即時回收之後，`cortex work gc` 仍不會刪掉交付 branch。

    `gc` 的 branch 保護有兩條來源：掛在 keep worktree 上（`protected_branches`），
    以及 merge 判定。即時回收把第一條抽掉了——這條確認第二條仍然頂得住：run 還在飛，
    branch 未 merge 進 default branch ⇒ `keep`。少了它，一次不巧的 `gc --apply`
    會在 run 中途刪掉整條交付線。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))

    job = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    branch = str(job["branch"])
    run, candidate, clone = _finish_card(
        registry, run=run, job=job, workspace=workspace,
        coordinator_root=coordinator_root, filename="one.txt",
    )
    assert not clone.exists()

    artifacts = gc.scan(workspace, worktree_root=pool)

    assert [item for item in artifacts if item.kind == "worktree"] == []
    rows = [item for item in artifacts if item.kind == "branch" and item.branch == branch]
    assert rows and all(item.action == gc.ACTION_KEEP for item in rows), rows
    assert all(item.reason == gc.REASON_UNMERGED_CONTENT for item in rows), rows
    assert job_workspace.source_branch_head(workspace, branch) == candidate


# ---------------------------------------------------------------------------
# `worktree_reclaim` 的 evidence 模型契約（#658 改的那一條）
# ---------------------------------------------------------------------------


def _standalone_clone(tmp_path: Path) -> tuple[Path, Path]:
    """一棵真的 per-job clone（走 provisioning 的唯一推導點），回傳 (來源樹, clone)。"""

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    creator = seams.ScriptWorktreeCreator(repo=workspace, wt_root=pool, base="main")
    clone = Path(creator.create("feature/658-x", job_id="wf-abc-card-1"))
    return workspace, clone


def test_preserve_model_is_unchanged_for_untrusted_reclaim(tmp_path: Path) -> None:
    """預設模型仍逐字是 #478 的語意：未提交／未追蹤內容先封存再刪。

    #658 只**新增**一個具名模型，不改預設。`recover-pre-candidate`／`abandon`／
    #601 的殘留回收全部沿用預設，因此 #478 的資料遺失回報仍受同一條保護。
    """

    workspace, clone = _standalone_clone(tmp_path)
    (clone / "untracked.txt").write_text("operator work\n", encoding="utf-8")
    preserve_root = tmp_path / "evidence"

    result = worktree_reclaim.reclaim_worktree(
        clone, repo_root=workspace, preserve_root=preserve_root
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.evidence_model == worktree_reclaim.EVIDENCE_PRESERVE
    assert result.preserved_ref is not None and result.preserved_files == 1
    preserved = Path(result.preserved_ref) / "untracked.txt"
    assert preserved.read_text(encoding="utf-8") == "operator work\n"
    assert not clone.exists()
    assert result.to_dict()["evidence_model"] == worktree_reclaim.EVIDENCE_PRESERVE


def test_harvested_model_skips_preserve_but_keeps_the_head_archive_fuse(
    tmp_path: Path,
) -> None:
    """`harvested` 模型不做 preserve 封存，但 HEAD 封存**照樣跑**。

    這是 #658 對契約的實際改動與它的安全網：preserve 被放棄（論證見
    `worktree_reclaim` 模組 docstring），而 `archive_workspace_head()` 兩種模型下
    都跑——呼叫端的前提萬一不成立（commit 沒進來源樹），那顆 commit 仍救得回來。
    這裡刻意**不** harvest，就是為了讓保險絲真的動作一次。
    """

    workspace, clone = _standalone_clone(tmp_path)
    (clone / "scratch.txt").write_text("model scratch\n", encoding="utf-8")
    _git(clone, "add", "scratch.txt")
    _git(clone, "commit", "-qm", "scratch")
    head = _git(clone, "rev-parse", "HEAD").lower()
    (clone / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    preserve_root = tmp_path / "evidence"

    result = worktree_reclaim.reclaim_worktree(
        clone,
        repo_root=workspace,
        preserve_root=preserve_root,
        evidence_model=worktree_reclaim.EVIDENCE_HARVESTED,
    )

    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.evidence_model == worktree_reclaim.EVIDENCE_HARVESTED
    assert result.preserved_ref is None and result.preserved_files == 0
    assert not (preserve_root / "worktree-reclaim").exists()
    assert not clone.exists()
    # 保險絲：commit 不在來源樹時被拉進封存命名空間，沒有被 `rmtree` 一起銷毀。
    assert result.archived_ref is not None
    assert result.archived_ref.startswith(job_workspace.ARCHIVE_REF_PREFIX)
    assert _git(workspace, "rev-parse", result.archived_ref).lower() == head


def test_unknown_evidence_model_is_rejected(tmp_path: Path) -> None:
    """未知的 evidence 模型一律 raise——靜默退回預設會掩蓋「呼叫端沒表態」。"""

    workspace, clone = _standalone_clone(tmp_path)

    with pytest.raises(ValueError, match="unknown worktree reclaim evidence model"):
        worktree_reclaim.reclaim_worktree(
            clone, repo_root=workspace, evidence_model="best-effort"
        )
    assert clone.is_dir(), "被拒絕的呼叫不得有任何 side effect"


# ---------------------------------------------------------------------------
# 突變驗證的對照面：把回收拿掉，上面兩條不變式必須轉紅
# ---------------------------------------------------------------------------


def test_without_the_reclaim_call_the_pool_grows_with_every_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把即時回收停掉（模擬 #658 之前），pool 就會隨卡數成長。

    這條是上面兩條不變式的**對照**：沒有它，那兩條有可能因為「工作區從來沒被建出
    來」之類的原因假通過。
    """

    monkeypatch.setattr(
        manager, "_reclaim_trusted_build_workspace", lambda *_a, **_k: None
    )
    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    cards = ("worktree-isolation", "tdd-red")
    run = _run_with_cards(registry, workspace=workspace, cards=cards)

    for index, _card in enumerate(cards):
        job = _dispatch(
            registry, run=run, workspace=workspace, pool=pool,
            coordinator_root=coordinator_root,
        )
        run, _candidate, _clone = _finish_card(
            registry, run=run, job=job, workspace=workspace,
            coordinator_root=coordinator_root, filename=f"card-{index}.txt",
        )

    assert len(_build_workspaces(pool)) == len(cards)


# ---------------------------------------------------------------------------
# OS 層語意：單 UID 測不到，明確 skip（#638／#657 的教訓）
# ---------------------------------------------------------------------------


def test_unreadable_workspace_yields_a_named_refusal_not_a_doomed_rmtree(
    tmp_path: Path,
) -> None:
    """工作區讀不進去時得到具名的拒絕理由，而不是一次注定失敗的 `rmtree`。

    這是三分部署的**可測代理**：那裡的 clone 是 `0700 <job 帳號>`，Manager 連
    `stat` 標記檔都會拿到 `PermissionError`。`chmod 000` 在同一個 UID 上重現的正是
    「進不去這棵樹」這個形狀（#653／#659 的不變式測試用的是同一招）。

    **它證得了什麼、證不了什麼**：證得了本函式在讀不進去時的**處置**（不刪、具名
    理由、採信不受影響）；證不了「別的帳號擁有的 `0700` 樹刪不掉」——那條見下一條
    測試的 skip 理由。
    """

    if os.geteuid() == 0:
        pytest.skip("以 root 執行時 DAC 不生效，`chmod 000` 攔不住任何東西")

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))
    job = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    clone = Path(str(job["worktree"]))
    candidate = _commit_in(clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace, clone=clone, branch=str(job["branch"]),
        spool_key=str(job["job_id"]), coordinator_root=coordinator_root,
    )

    clone.chmod(0o000)
    try:
        target, refusal = manager._trusted_build_workspace_target(
            job, run=run, candidate=candidate
        )
        assert manager._reclaim_trusted_build_workspace(
            job, run=run, candidate=candidate
        ) is None
    finally:
        clone.chmod(0o700)

    assert target is None
    assert refusal == "workspace-not-a-job-clone"
    assert clone.is_dir(), "讀不進去就不動它——半刪比留著更糟"


def test_multi_uid_ownership_semantics_are_not_reachable_from_a_test_process() -> None:
    """三分／四分部署下「誰回收得掉那棵樹」——**任何**測試進程都驗不到，明確 skip。

    #658 的核心答案是：**回收身分 ＝ 採信身分，不新增任何身分**。抵達即時回收必須
    先走完 `_verify_exact_candidate()`，而它以 Manager 身分對**同一棵樹**跑
    `git -C <worktree> rev-parse HEAD`；因此回收要的授權面是採信路徑已經在用的
    那一份，本票沒有、也不需要擴張任何 ACL。

    要在測試裡驗證這條，必須同時成立三件事：

    1. 真的存在 `cortex-manager` 與 `cortex-builder` 兩個帳號；
    2. 工作區真的是 `0700 cortex-builder`（要 `chown` 給別的 owner 需要 root，
       而「cortex 任何元件永不具 root」是既有裁決 ⇒ 產品程式碼永遠做不到這一步）；
    3. 執行回收的進程真的是 `cortex-manager`（＝**不是**跑測試的這個 UID）。

    第 2、3 條在單一測試進程裡結構性不可能：pytest 只有一個 UID，而它若真的是
    root，第 (1)(2) 條的斷言又全部失去意義（DAC 對 root 不生效）。任何「用
    `chmod 000` 模擬」的寫法驗到的是 owner 的例外語意，不是跨帳號語意——上一條
    測試明講了它證得了什麼、證不了什麼。

    比照 #638／#657 的教訓：**明確 skip 並說清楚缺什麼**，不寫一條看起來綠、
    實際上與目標部署無關的斷言。實機稽核步驟寫在
    `docs/superpowers/runbooks/trust-root-phase2b-setup.md`（第 2 步稽核 5c）。
    """

    pytest.skip(
        "跨帳號擁有權語意需要 root 建置 ＋ 以另一個 UID 執行回收，"
        "單一 pytest 進程結構性做不到（見本函式 docstring 的三條）；"
        f"本機 euid={os.geteuid()}、"
        f"cortex-builder 存在={job_runner._account_exists('cortex-builder')}、"
        f"cortex-manager 存在={job_runner._account_exists('cortex-manager')}。"
        "回收邏輯本身已由本檔其餘測試在真 git repo ＋ 真 per-job clone 上端到端覆蓋"
    )
