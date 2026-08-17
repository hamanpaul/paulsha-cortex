"""issue #648：canonical（workflow）lane 的工作區改為 **per-job**。

#645／#646 把「工作區目錄名 ＝ systemd 模板 instance 名」收斂成單一推導點
（`job_workspace.job_segment()`），但同時發現 canonical lane **結構上**滿足不了它：
那條 lane 的工作區是 per-run 的（build 卡 provision 之後，同一個 run 後續的卡沿用
`builder_jobs[-1]["worktree"]`），一個工作區對多個 `job_id`。模板 unit 的
`ReadWritePaths=<pool>/%i` 因此對第二張卡起必然指向不存在的路徑 → `226/NAMESPACE`
→ 起不來。#646 的程式碼裡明文寫著這條邊界，本票把它拿掉。

本檔的核心是三條不變式：

1. **命名**：canonical lane 每一張 build 卡的工作區目錄名 == 該卡的
   `job_runner.template_instance_id(job_id)`（兩側都跑真實推導函式，不對常數斷言）。
2. **交接**：卡與卡之間的交接走 #637 的 bundle ＋ append-only spool，**不依賴磁碟
   殘留**——把前一張卡的工作區整個刪掉，後續卡仍能拿到正確的 base。
3. **零回歸**：branch 名、spool key 推導、`direct` 模式的既有行為逐字不變。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex.coordinator import job_runner, job_workspace, manager, seams
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


_REPO = "hamanpaul/paulsha-cortex"
_WORK_ID = "648-canonical-per-job-workspace"


# ---------------------------------------------------------------------------
# fixtures
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
        test_policy="focused",
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
    """記下每次 launch 的 `slice_id`／`worktree`——不變式要驗的正是這兩個的關係。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self) -> "_RecordingLauncher":
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "worktree": worktree})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _run_with_cards(
    registry: JobRegistry,
    *,
    workspace: Path,
    cards: tuple[str, ...],
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
        issue_refs=(f"{_REPO}#648",),
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
) -> tuple[dict[str, object], _RecordingLauncher]:
    """跑**正式** dispatch 路徑：真的 `ScriptWorktreeCreator`、真的 git repo。"""

    creator = seams.ScriptWorktreeCreator(repo=workspace, wt_root=pool, base="main")
    dispatcher = type(
        "D",
        (),
        {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    launcher = _RecordingLauncher()
    dispatched = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=_identities(),
        launcher_factory=lambda _identity: launcher,
        coordinator_root=coordinator_root,
        force_new_card=force_new_card,
    )
    assert dispatched is not None
    return registry.get_job(str(dispatched["job_id"])), launcher


def _accept_card(
    registry: JobRegistry,
    *,
    run,
    job: dict[str, object],
    card: str,
    candidate: str,
):
    """把「這張卡已被採信」這件事寫進 registry（harvest 之後的狀態）。

    `subject_head` 由 `bind_workflow_evidence()` 寫入——那正是正式路徑（見
    `manager._read_job_workflow_evidence`），而 `builder_jobs` 的過濾條件就是
    `subject_head == run.candidate_head`。手動塞 `subject_head` 會繞過那條綁定。
    """

    registry.update_headless_result(str(job["job_id"]), status="exited", exit_code=0)
    registry.bind_workflow_evidence(
        str(job["job_id"]),
        locator={"kind": "build", "path": "evidence/workflow/fake.json", "hash": "a" * 64},
        subject_head=candidate,
    )
    steps = tuple(
        _build_step(step.card, gate_result="passed") if step.card == card else step
        for step in run.steps
    )
    return registry._manager_update_workflow_run(
        run.run_id, steps=steps, candidate_head=candidate
    )


def _harvest_through_the_spool(
    *,
    workspace: Path,
    clone: Path,
    branch: str,
    spool_key: str,
    coordinator_root: Path,
) -> str:
    """完整跑一次 #637 的交接：builder 產 bundle → Manager 從那個檔 fetch。

    刻意**不**手動 `git push`／`fetch <clone>`：本票的整個論點就是「交接走一條顯式
    通道」，測試若自己抄捷徑把 commit 搬過去，被驗的就不是那條通道。
    """

    bundle = job_workspace.prepare_commit_spool(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    command = job_workspace.build_bundle_command(workspace=clone, bundle=bundle)
    produced = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert produced.returncode == 0, produced.stderr
    return job_workspace.harvest_branch(
        source_repo=workspace, bundle=bundle, branch=branch
    )


def _commit_in(clone: Path, *, filename: str) -> str:
    (clone / filename).write_text("candidate\n", encoding="utf-8")
    _git(clone, "add", filename)
    _git(clone, "commit", "-qm", f"add {filename}")
    return _git(clone, "rev-parse", "HEAD").lower()


# ---------------------------------------------------------------------------
# 不變式 1：目錄名 ＝ instance 名（#646 明文排除 canonical lane，本票拿掉那個排除）
# ---------------------------------------------------------------------------


def test_every_canonical_build_card_workspace_name_equals_its_instance_name(
    tmp_path: Path,
) -> None:
    """canonical lane 的**每一張** build 卡，工作區目錄名 == 該卡的 instance 名。

    左邊是真的在真 git repo 上 provision 出來的目錄，右邊是
    `job_runner.template_instance_id()`（`prepare_systemd_template()` 內部唯一的
    instance 來源）對 `launcher.launch(slice_id=…)` 收到的那個 id 的輸出。#646 之前
    canonical lane 傳的是 run 層級的 build 身分，第一張卡就對不上，第二張卡起更是
    「一個目錄對多個 instance」——結構上不可能相等。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one, launcher_one = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    candidate = _commit_in(first_clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace,
        clone=first_clone,
        branch=str(job_one["branch"]),
        spool_key=str(job_one["job_id"]),
        coordinator_root=coordinator_root,
    )
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )
    job_two, launcher_two = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    second_clone = Path(str(job_two["worktree"]))

    for job, launcher, clone in (
        (job_one, launcher_one, first_clone),
        (job_two, launcher_two, second_clone),
    ):
        launched_id = launcher.calls[0]["slice_id"]
        # 接線：交給 provisioning 的 id，就是 `launch(slice_id=…)` 之後交給
        # `prepare_systemd_template(job_id=…)` 的那一個。
        assert launched_id == str(job["job_id"])
        assert launcher.calls[0]["worktree"] == str(clone)
        assert clone.name == job_runner.template_instance_id(launched_id), (
            f"card={job['workflow_card']}：工作區目錄名 {clone.name!r} 與模板 instance 名 "
            f"{job_runner.template_instance_id(launched_id)!r} 不相等——模板 unit 的 "
            "ReadWritePaths=<pool>/%i 會指向不存在的路徑（#645 的 226/NAMESPACE）"
        )
        # unit 的 RWP 是 `<pool>/%i`：目錄的父層也必須就是 pool 本身。
        assert clone.parent == pool
        assert clone.is_dir()

    # 兩張卡不得共用同一個工作區——那正是 #648 要消滅的一對多。
    assert first_clone != second_clone


def test_canonical_lane_never_produces_the_pre_648_per_run_directory(
    tmp_path: Path,
) -> None:
    """突變守衛：#648 之前的目錄名（run 層級的 build 身分）不得再出現在 pool 裡。

    沒有這一條，上面的不變式可能因為「兩個名字剛好都被改成 run 身分」而假通過。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))

    job, _launcher = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=tmp_path / "coordinator",
    )

    # #646 的形狀：`job_segment("<issue>-<work_id>")`（run 層級，全 run 一份）
    legacy = pool / job_workspace.job_segment(f"648-{_WORK_ID}")
    assert not legacy.exists()
    # #645 之前的形狀：branch slug
    assert not (pool / job_workspace.legacy_branch_slug(str(job["branch"]))).exists()
    assert sorted(path.name for path in pool.iterdir()) == [
        job_runner.template_instance_id(str(job["job_id"]))
    ]


# ---------------------------------------------------------------------------
# 不變式 2：交接顯式化——不依賴磁碟殘留
# ---------------------------------------------------------------------------


def test_next_card_gets_its_base_after_the_previous_workspace_is_deleted(
    tmp_path: Path,
) -> None:
    """把前一張卡的工作區**整個刪掉**，後續卡仍拿得到它需要的 base。

    這是本票的全部價值：per-run 工作區隱含「前一張卡的產出留在磁碟上給下一張用」，
    per-job 之後那個交接必須走一條顯式通道。這裡跑的就是 #637 落地的那一條——
    builder 在自己的 clone 產出 bundle → 寫進 Manager-owned 的 append-only spool →
    Manager 從**那個檔案** fetch 進來源樹的 `refs/heads/<branch>`。下一張卡從來源樹
    clone，因此前一張卡的目錄可以在它派工之前就被回收掉。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    branch = str(job_one["branch"])
    candidate = _commit_in(first_clone, filename="one.txt")
    harvested = _harvest_through_the_spool(
        workspace=workspace, clone=first_clone, branch=branch,
        spool_key=str(job_one["job_id"]), coordinator_root=coordinator_root,
    )
    assert harvested == candidate
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )

    # 前一張卡的工作區被回收：磁碟上再也沒有那棵樹。
    shutil.rmtree(first_clone)
    assert not first_clone.exists()

    job_two, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    second_clone = Path(str(job_two["worktree"]))

    # 後續卡的工作區確實帶著前一張卡的成果。
    assert _git(second_clone, "rev-parse", "HEAD").lower() == candidate
    assert (second_clone / "one.txt").is_file()
    assert _git(second_clone, "branch", "--show-current") == branch
    # base pin 也錨在被採信的 candidate 上——下一輪 bundle 因此只帶這張卡的增量。
    assert _git(second_clone, "rev-parse", job_workspace.BASE_REF).lower() == candidate


def test_build_handoff_fails_closed_when_the_candidate_never_reached_the_source_tree(
    tmp_path: Path,
) -> None:
    """交接沒走完時 fail-closed，**不得**退回 run 的原始 base 重新 provision。

    退回原始 base 的後果不是「跑得比較慢」，而是 `branch -f` 把整個 run 已採信的
    commit 從 branch 上抹掉——成果只剩在已被回收的 clone 裡。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    candidate = _commit_in(first_clone, filename="one.txt")
    # 刻意**不** harvest：candidate 只存在於 builder 的 clone 裡。
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )

    with pytest.raises(ValueError, match="git worktree base invalid"):
        _dispatch(
            registry, run=run, workspace=workspace, pool=pool,
            coordinator_root=coordinator_root,
        )


# ---------------------------------------------------------------------------
# 不變式 3：多卡 run 的 base 推導，含中段卡重派（#545）
# ---------------------------------------------------------------------------


def test_midchain_card_redispatch_starts_from_the_accepted_candidate(
    tmp_path: Path,
) -> None:
    """中段卡重派：新工作區、新目錄，base 仍是**最後一張被採信**的 candidate。

    `_manager_reset_workflow_for_retry_card()`（#545）不動 `candidate_head`，只把那
    一張卡打回 pending。因此重派拿到的 base 不是 run 的原始 base（會丟掉前面幾張卡
    的成果），也不是那次失敗嘗試留在磁碟上的東西（那是另一個 job_id、另一個目錄）。

    附帶：#601 的生產現場是「重派撞 `worktree target already exists`」。per-job 命名
    之後兩次嘗試的目錄名不同，那個撞名在這條 lane 上**結構性消失**——殘留目錄的
    回收仍是 #601 的範圍，但它不再擋住重派。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    # 卡 1 成功並交接回來源樹。
    job_one, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    candidate = _commit_in(first_clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace, clone=first_clone, branch=str(job_one["branch"]),
        spool_key=str(job_one["job_id"]), coordinator_root=coordinator_root,
    )
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )

    # 卡 2 首派：失敗（terminal 壞掉、evidence 綁不上），工作區留在磁碟上。
    job_two, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    failed_clone = Path(str(job_two["worktree"]))
    registry.update_headless_result(str(job_two["job_id"]), status="exited", exit_code=1)
    assert failed_clone.is_dir()

    # 卡 2 重派：走**真的** `retry-card` 原子重置——它刻意不動 candidate_head。
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    run = registry._manager_reset_workflow_for_retry_card(
        run.run_id, expected_run_id=run.run_id, card="tdd-red"
    )
    assert run.candidate_head == candidate, "retry-card 不得動 candidate_head"
    job_retry, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root, force_new_card=True,
    )
    retry_clone = Path(str(job_retry["worktree"]))

    assert job_retry["job_id"] != job_two["job_id"]
    assert retry_clone != failed_clone, "#601：重派必須拿到自己的目錄，不得撞名"
    assert retry_clone.name == job_runner.template_instance_id(str(job_retry["job_id"]))
    # base 是最後一張**被採信**的 candidate，不是 run 的原始 base。
    assert _git(retry_clone, "rev-parse", "HEAD").lower() == candidate
    assert (retry_clone / "one.txt").is_file()
    # 失敗那次的殘留一個位元組都沒被動過（回收是 #601 的範圍，不是 provision 的）。
    assert failed_clone.is_dir()


def test_dispatch_head_stays_run_level_across_the_card_chain(tmp_path: Path) -> None:
    """`dispatch_head` 仍是 **run 層級**的 base——per-job 只改工作區，不改記帳基準。

    `dispatch_head` 是 persona scope diff／required-artifact diff 的比較基準
    （`verification`），語意是「這個 run 從哪裡長出來」。把它改成「上一張卡的
    candidate」會讓 scope 檢查只看得到最後一張卡的 diff。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )
    run_base = _git(workspace, "rev-parse", "HEAD").lower()

    job_one, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    candidate = _commit_in(first_clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace, clone=first_clone, branch=str(job_one["branch"]),
        spool_key=str(job_one["job_id"]), coordinator_root=coordinator_root,
    )
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )
    job_two, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )

    assert job_one["dispatch_head"] == run_base
    assert job_two["dispatch_head"] == run_base != candidate


# ---------------------------------------------------------------------------
# direct 模式零回歸
# ---------------------------------------------------------------------------


def test_direct_mode_branch_and_spool_surfaces_are_unchanged(tmp_path: Path) -> None:
    """只有磁碟上的目錄名改：branch 名、來源樹的 ref、spool key 推導全部不變。

    `direct` 模式（與 `gc`／harvest）完全不看目錄名——它們讀來源 repo 的
    `refs/heads/<branch>`、讀工作區自己 checked-out 的 branch、以及 `log_path` 推導
    的 spool key。這條把那三個面一次釘住。
    """

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )
    base = _git(workspace, "rev-parse", "HEAD").lower()

    job_one, launcher_one = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    branch = str(job_one["branch"])
    clone = Path(str(job_one["worktree"]))

    # 1. branch 名由 run 的 primary issue ＋ work_id 導出，與 #648 之前逐字相同。
    assert branch == f"feature/648-{_WORK_ID}"
    # 2. 來源 repo 的 branch 錨在 dispatch base（gc／dispatch baseline 都直接讀它）
    assert job_workspace.source_branch_head(workspace, branch) == base
    # 3. 工作區 checked-out 的仍是同一條 branch（gc 由此取 branch，不看目錄名）
    assert job_workspace.workspace_branch(clone) == branch
    assert (job_workspace.read_marker(clone) or {}).get("branch") == branch
    # 4. spool key 仍由 log_path 推導（#637），與目錄名無關
    assert job_workspace.spool_key_for_job(job_one) == str(job_one["job_id"])
    assert job_workspace.spool_key_for_job(job_one) == launcher_one.calls[0]["slice_id"]

    # 後續卡同樣不改 branch——per-job 的是工作區，不是 branch。
    candidate = _commit_in(clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace, clone=clone, branch=branch,
        spool_key=str(job_one["job_id"]), coordinator_root=coordinator_root,
    )
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )
    job_two, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    assert job_two["branch"] == branch
    assert job_workspace.source_branch_head(workspace, branch) == candidate


def test_reserved_job_id_is_the_id_the_job_actually_gets(tmp_path: Path) -> None:
    """`reserve_job_id()` 配發的 id 就是 `create_job()` 寫進 registry 的那一個。

    per-job 工作區的目錄名由這個 id 導出，而它必須在 `create_job()` **之前**就定案
    （工作區要先存在，`workflow_input_snapshot` 才算得出來）。配發與寫入若各自產生
    一次 id，目錄名與 registry 身分就會漂開——那正是 #645 的形狀。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    reserved = registry.reserve_job_id("wf-abc-tdd-red")
    job = registry.create_job(
        task="wf-abc-tdd-red",
        job_id=reserved,
        persona="builder",
        branch="feature/x",
        pane="",
        worktree=str(tmp_path),
    )
    # 讓它離開 active 狀態，底下兩條才驗得到 job_id 守衛本身（而不是先撞上
    # 「同一個 task 已有 active builder」那條既有守衛）。
    registry.update_headless_result(reserved, status="exited", exit_code=0)

    assert job["job_id"] == reserved
    # 配發即消耗：同一個 id 不可能被第二個 job 拿到。
    assert registry.reserve_job_id("wf-abc-tdd-red") != reserved
    with pytest.raises(ValueError, match="已被使用"):
        registry.create_job(
            task="wf-abc-tdd-red",
            job_id=reserved,
            persona="builder",
            branch="feature/x",
            pane="",
            worktree=str(tmp_path),
        )
    # 未經配發的 id 一律拒絕——放寬等於讓呼叫端自己造 registry 身分。
    with pytest.raises(ValueError, match="未經配發"):
        registry.create_job(
            task="wf-abc-tdd-red",
            job_id="wf-abc-tdd-red-9999",
            persona="builder",
            branch="feature/x",
            pane="",
            worktree=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# 回收：gc／worktree_reclaim 對「一個 run 一個工作區」的假設
# ---------------------------------------------------------------------------


def test_gc_and_reclaim_handle_several_workspaces_on_one_branch(tmp_path: Path) -> None:
    """同一條 branch 上有多個 per-job 工作區時，`gc` 與 `worktree_reclaim` 都要正確。

    #648 之前 canonical lane 一個 run 只有一個工作區，因此「工作區 ↔ branch」是
    一對一。改 per-job 之後同一條 branch 上會同時掛著 N 個工作區——這條把兩件事
    釘住：

    - `gc.scan()` 的判準是**形狀**（標記檔／`.git` 檔）與 branch 的 merge 狀態，
      不看目錄名也不假設一對一，因此 N 個都掃得到；只要還有任何一個沒被回收，
      那條 branch 就仍受保護（不會在 job 還活著時被刪掉）。
    - `worktree_reclaim` 收的是**呼叫端給的路徑**。per-job 之後每一列 job 記錄的
      `worktree` 都是自己那一個，回收一張卡不會波及同 run 的其他卡——per-run 時代
      那是同一條路徑，回收任一張卡等於把兄弟卡的樹一起刪掉。
    """

    from paulsha_cortex.coordinator import gc, worktree_reclaim

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(
        registry, workspace=workspace, cards=("worktree-isolation", "tdd-red")
    )

    job_one, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    first_clone = Path(str(job_one["worktree"]))
    branch = str(job_one["branch"])
    candidate = _commit_in(first_clone, filename="one.txt")
    _harvest_through_the_spool(
        workspace=workspace, clone=first_clone, branch=branch,
        spool_key=str(job_one["job_id"]), coordinator_root=coordinator_root,
    )
    run = _accept_card(
        registry, run=run, job=job_one, card="worktree-isolation", candidate=candidate
    )
    job_two, _ = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=coordinator_root,
    )
    second_clone = Path(str(job_two["worktree"]))

    artifacts = gc.scan(workspace, worktree_root=pool)
    scanned = {
        item.identifier for item in artifacts if item.kind == "worktree"
    }
    assert scanned == {str(first_clone.resolve()), str(second_clone.resolve())}
    # branch 未 merge ⇒ 兩個工作區都 keep，branch 因此受保護。
    branch_rows = [
        item for item in artifacts if item.kind == "branch" and item.branch == branch
    ]
    assert branch_rows and all(item.action == gc.ACTION_KEEP for item in branch_rows)

    # 回收第一張卡的工作區，不得波及第二張卡的。
    result = worktree_reclaim.reclaim_recorded_or_derived(
        recorded_path=first_clone,
        pool_root=pool,
        job_id=str(job_one["job_id"]),
        branch=branch,
        repo_root=workspace,
        preserve_root=tmp_path / "evidence",
    )
    assert result is not None and result.ok, result.detail if result else None
    assert not first_clone.exists()
    assert second_clone.is_dir()
    assert _git(second_clone, "rev-parse", "HEAD").lower() == candidate


# ---------------------------------------------------------------------------
# 完整 preflight（需 Phase 2b 部署；單 UID 的開發機／CI 明確 skip，見 #638）
# ---------------------------------------------------------------------------


def test_prepare_systemd_template_agrees_with_canonical_provisioning(
    tmp_path: Path,
) -> None:
    """canonical lane 的完整 `prepare_systemd_template()` 與 provisioning 對齊。

    上面的不變式跑的是 `template_instance_id()`——它正是
    `prepare_systemd_template()` 內部唯一的 instance 來源，因此在任何機器上都測得到。
    這一條再往外包一層，連 preflight（帳號／unit 檔／shim／spec spool）一起跑，證明
    **正式派工路徑**上算出來的 instance 名也是同一個。

    這些前置物是 OS 層的（真的存在 `cortex-builder` 帳號、真的裝了模板 unit），
    單 UID 的開發機與 CI 都沒有，任何單 UID 的模擬都測不出 mount namespace 的語意。
    比照 #638 的教訓：**明確 skip 並逐項說明缺什麼**，不得靜默通過。
    """

    missing: list[str] = []
    account = job_runner.resolve_builder_account({})
    group = job_runner.resolve_builder_group({})
    template = job_runner.resolve_template_unit({})
    shim = job_runner.resolve_job_shim({})
    spool = job_runner.resolve_job_spec_spool({})
    if shutil.which("systemctl") is None:
        missing.append("PATH 上沒有 systemctl")
    if not job_runner._systemd_booted():
        missing.append("/run/systemd/system 不存在（本機未以 systemd 開機）")
    if not job_runner._account_exists(account):
        missing.append(f"builder 帳號 {account} 不存在")
    if not job_runner._group_exists(group):
        missing.append(f"builder group {group} 不存在")
    for profile in sorted(job_runner.TEMPLATE_UNIT_SUFFIX_BY_PROFILE):
        unit = job_runner.template_unit_for_profile(template, profile)
        if not job_runner._unit_file_installed(unit):
            missing.append(f"模板 unit {unit}（剖面 {profile}）未安裝")
    if not job_runner._is_executable(shim):
        missing.append(f"降權 shim {shim} 不存在或不可執行")
    # #657：`Path.is_dir()` 在 EACCES 時**會 raise**（只吞 ENOENT／ENOTDIR 一類），
    # 而 spool 的父層對非 Manager 帳號本來就是 0700——用 `os.path.isdir()`（吞掉所有
    # OSError）才是這個 skip 判斷該有的語意。
    if not os.path.isdir(spool):
        missing.append(f"job spec spool {spool} 不存在（#657 起每個角色一格）")
    else:
        # 「存在」不等於「那個身分讀得到」——那正是 #657。前置物清單一併涵蓋它，
        # 否則本條會在一台 spool 存在但 ACL 未套用的機器上以真實派工失敗收場。
        ok, why = job_runner._spool_readable_by(spool, account)
        if not ok:
            missing.append(f"builder 讀不到自己的 spec spool（{why}）")
    if missing:
        pytest.skip(
            "本機沒有 trust-root Phase 2b 的降權前置物（"
            + "；".join(missing)
            + "）。#648 的不變式在 `template_instance_id()` 那條測試裡已被完整覆蓋，"
            "本條只多驗 preflight 這一層——刻意 skip 而非空過"
        )

    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run_with_cards(registry, workspace=workspace, cards=("worktree-isolation",))
    job, _launcher = _dispatch(
        registry, run=run, workspace=workspace, pool=pool,
        coordinator_root=tmp_path / "coordinator",
    )
    clone = Path(str(job["worktree"]))

    for executor, stem in (("claude", "cortex-job"), ("codex", "cortex-job-jit")):
        plan = job_runner.prepare_systemd_template(
            {},
            job_id=str(job["job_id"]),
            executor=executor,
            unit_active=lambda _binary, _unit: False,
        )
        assert clone.name == plan.instance, executor
        assert plan.unit == f"{stem}@{clone.name}.service", executor
