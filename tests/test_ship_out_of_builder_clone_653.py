"""#653：ship 段從 builder 的 clone 搬進 Manager-owned 的樹。

本檔全部跑**正式路徑**：真 git repo、真 per-job clone、真
`work_bridge.build_production_ship_validator()`、真 `seams.ScriptWorktreeCreator`、
真 `_run_exact_candidate_preflight()`。不手動 `git push`、不自己捏工作區。

問題（#654 查證出來的）：ship 段不是降權派工的對象——兩張 ship 卡由 Manager 自己
在 `work_bridge` 內以 `cortex-manager` 身分同步執行；但它們全程在
`_builder_binding()` 交回來的 **builder 的 clone** 裡動手，而 #641 已把 Manager 對
job 工作樹的讀取授權全部收掉 ⇒ 降權模式下 ship phase 會在**第一個 `git -C`** 就
`Permission denied`。**症狀是權限，不是 mount namespace。**

本檔的第一條測試就是這件事的機械形式：把 builder 的 clone `chmod 000`（＝實機上
`0700 cortex-builder` 對 Manager 的樣子），ship 段仍必須完整跑完。形狀沿用 #637 的
`test_manager_never_touches_the_builder_clone_while_harvesting`。

共用 harness 取自 `test_preflight_closeout_order`——那是 repo 內唯一一份「真的把
production ship validator 端到端跑起來」的 fixture，複製一份只會讓兩邊漂移。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.config.paths import worktree_root_for
from paulsha_cortex.coordinator import (
    job_workspace,
    manager,
    seams,
    work_actions,
    work_bridge,
)
from paulsha_cortex.coordinator.claim import load_work_authority

from test_preflight_closeout_order import (  # noqa: E402
    ShipHarness,
    _capture,
    _ship_harness,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _with_real_ship_cards(harness: ShipHarness):
    """把共用 harness 的佔位 `ship-card` 換成真的兩張 ship 卡。

    `_manager_archive_applied()` 與 `_validated_ship_steps()` 都以 **card id** 定址
    （`openspec-archive`／`policy-commit`），佔位卡在那兩處等於「這個 run 沒有
    ship 卡」。需要走 archive 宣告或 ship audit 的測試必須先補上。
    """

    from paulsha_cortex.coordinator.workflow import WorkflowStep

    run = harness.registry.get_workflow_run(harness.run_id)
    return harness.registry._manager_update_workflow_run(
        run.run_id,
        steps=tuple(step for step in run.steps if step.phase != "ship")
        + (
            WorkflowStep("ship", "manager", "openspec-archive", None, None, None, (), ()),
            WorkflowStep("ship", "manager", "policy-commit", None, None, None, (), ()),
        ),
    )


def _ship_workspace(harness: ShipHarness, candidate: str) -> Path:
    """這一輪 ship 段實際使用的 Manager-owned 工作區（由產品的推導點算出來）。"""

    return job_workspace.workspace_path(
        worktree_root_for(harness.repo),
        work_bridge._ship_workspace_id(harness.run, candidate),
    )


# ---------------------------------------------------------------------------
# 1. 本票的全部價值：ship 段不碰 builder 的樹
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目錄權限限制，這條不變式驗不到")
def test_ship_phase_completes_while_the_builder_clone_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把 builder 的 clone `chmod 000`，ship 段仍要跑完 local closeout。

    以 `chmod 000` 重現 operator 實機看到的形狀（clone 為 `0700 cortex-builder`，
    Manager 連 `ls` 都 `Permission denied`）。本票之前這裡必炸——ship 段的第一個
    動作就是對那棵樹 `resolve(strict=True)` 然後一路 `git -C` 下去。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    os.chmod(harness.worktree, 0o000)
    try:
        # 前提成立：這棵樹現在真的進不去（等同實機的 Permission denied）。
        assert (
            subprocess.run(
                ["git", "-C", str(harness.worktree), "status"],
                capture_output=True,
            ).returncode
            != 0
        )
        outcome = _capture(
            harness.validator, run=harness.run, candidate=harness.candidate
        )
    finally:
        os.chmod(harness.worktree, 0o700)

    updated = harness.registry.get_workflow_run(harness.run_id)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["reason"] == "archive-commit-invalidated-candidate-evidence"
    assert updated.current_phase == "verify"
    assert updated.candidate_head not in (None, harness.candidate)
    # archive commit 走 #654 的 bundle ＋ spool 回到來源樹（consumer 仍是
    # `harvest_branch()`，全 repo 唯一實作）。
    assert (
        job_workspace.source_branch_head(harness.repo, "feature/14-work")
        == updated.candidate_head
    )
    # builder 的 clone 原封不動：active change 還在，也沒有多出任何 commit。
    assert (harness.worktree / "openspec" / "changes" / "work").is_dir()
    assert _git(harness.worktree, "rev-parse", "HEAD") == harness.candidate


def test_ship_workspace_is_a_manager_owned_clone_bound_to_run_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ship 段的樹是 pool 裡一棵 Manager-owned 的 per-job clone，不是 builder 的。

    同時釘住 `openspec archive` 實際落在哪棵樹上——那是「搬家」有沒有真的發生的
    唯一直接證據（`_ship_action`／`_commit_archive_and_require_reverification`
    都拿同一個 `worktree`）。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    workspace = _ship_workspace(harness, harness.candidate)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert workspace.is_dir()
    assert workspace != harness.worktree
    assert workspace.parent == worktree_root_for(harness.repo).resolve()
    # 形狀是 per-job clone（自己的 object store ＋ 工作區標記檔），不是 linked
    # worktree、也不是來源樹本身。
    assert job_workspace.is_job_clone(workspace)
    assert (workspace / ".git").is_dir()
    marker = job_workspace.read_marker(workspace)
    assert marker is not None
    assert marker["branch"] == "feature/14-work"
    assert marker["base"] == harness.candidate
    # archive 只在那棵樹裡被套用過。
    assert harness.runner.archived_in == [workspace]
    assert not (workspace / "openspec" / "changes" / "work").exists()


def test_ship_workspace_is_reused_across_ticks_and_reset_to_pristine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一個 candidate 的多次 tick 共用同一棵樹，且每次進入都是 pristine。

    ship phase 會被 tick 很多次（等 preflight、等 PR、等 copilot、等 merge）。
    識別穩定於 (run, candidate) 因此不會每 tick 燒一份 clone；而**開工前一律打回
    原狀**，讓「上一次崩在中間」與「從沒跑過」收斂成同一個狀態——這正是
    `archive-applied-needs-commit` 重入路徑在新模型下的處置方式（票上兩個選項中的
    「在新樹裡重跑 archive」）。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    expected = _ship_workspace(harness, harness.candidate)

    first = work_bridge._manager_ship_workspace(
        run=harness.run, branch="feature/14-work", candidate=harness.candidate
    )
    assert first == expected
    marker_created_at = job_workspace.read_marker(first)["created_at"]

    # 模擬「上一個 tick 崩在中間」：工作區留下已套用但未 commit 的 archive 異動
    # ＋ 一份雜物；下一次進入必須看不到它們。
    archived = first / "openspec" / "changes" / "archive"
    archived.mkdir(parents=True, exist_ok=True)
    (first / "openspec" / "changes" / "work").rename(archived / "work")
    (first / "leftover.txt").write_text("junk\n", encoding="utf-8")
    assert _git(first, "status", "--porcelain") != ""

    second = work_bridge._manager_ship_workspace(
        run=harness.run, branch="feature/14-work", candidate=harness.candidate
    )

    assert second == first
    # 同一棵樹（沒有重新 clone——標記檔還是第一次寫的那一份）。
    assert job_workspace.read_marker(second)["created_at"] == marker_created_at
    # 但已被打回 candidate 的原狀。
    assert not (second / "leftover.txt").exists()
    assert (second / "openspec" / "changes" / "work").is_dir()
    assert _git(second, "rev-parse", "HEAD") == harness.candidate
    assert _git(second, "status", "--porcelain") == ""
    assert _git(second, "symbolic-ref", "--short", "HEAD") == "feature/14-work"


# ---------------------------------------------------------------------------
# 2. `archive-applied-needs-commit` 重入路徑
# ---------------------------------------------------------------------------

def _bind_pr_and_mark_archive_applied(harness: ShipHarness):
    """把 run 推到「archive 已宣告完成、PR 已綁定」的狀態。

    這正是 `_ship_action()` 回 `archive-applied-needs-commit` 的**唯一**入口：
    `validate()` 的早期 local-closeout 段以 `_manager_archive_applied(run)` 為守衛，
    該值為真時就會跳過那一段，改由 `_ship_action()` 在 PR 段裡發現工作區仍有
    active change、套用 archive、然後把球丟回 `work_bridge`。

    順帶把 authority／journal 一起推到 PR 已建立之後的形狀（PR 是前一個 tick 建的，
    delivery journal 那一筆因此已經存在），否則 push 段會先在
    `delivery push journal missing canonical run` 上停下來，測不到重入路徑。
    """

    from dataclasses import replace

    from paulsha_cortex.coordinator.claim import work_authority_digest

    from test_preflight_closeout_order import _snapshot

    _snapshot(harness.snapshot, mapped_prs=(17,))
    authority = work_bridge._authority_with_manager_pr(
        load_work_authority(
            repo="acme/demo", work_id="work", snapshot_path=harness.snapshot
        ),
        17,
    )
    run = _with_real_ship_cards(harness)
    run = harness.registry._manager_update_workflow_run(
        run.run_id,
        pr_refs=("acme/demo#17",),
        source_revision=work_authority_digest(authority),
        steps=tuple(
            replace(
                step,
                executor="cortex-manager",
                model="deterministic",
                domain="cortex",
                gate_result="passed",
            )
            if step.phase == "ship" and step.card == "openspec-archive"
            else step
            for step in run.steps
        ),
    )
    work_actions._load_work_run(
        state_path=harness.state_root / "delivery-journal.json",
        workflow_registry=harness.registry,
        authority=authority,
    )
    return run


def test_archive_applied_needs_commit_reentry_commits_in_the_manager_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重入路徑：`_ship_action()` 在工作區套用 archive → 同一棵樹裡被 commit。

    #653 明載這條必須一起處理：套用與 commit 若不在同一棵樹，
    `_commit_archive_and_require_reverification()` 會看到一個乾淨的樹（`changed`
    為空）而落到 `archive diff escaped strict OpenSpec/docs/changelog allowlist`。
    新模型下兩者都拿 `validate()` 開頭那一次 provisioning 的結果，因此結構上就是
    同一棵——本測試把它釘住。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    _bind_pr_and_mark_archive_applied(harness)
    workspace = _ship_workspace(harness, harness.candidate)

    outcome = _capture(
        harness.validator, run=harness.run, candidate=harness.candidate
    )
    updated = harness.registry.get_workflow_run(harness.run_id)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["reason"] == "archive-commit-invalidated-candidate-evidence"
    # 走的是 `_ship_action()` 的重入路徑：PR 段已經跑過（push 與 gh 都發生了），
    # 而不是 `validate()` 開頭那一段 local closeout。
    assert harness.runner.saw_push()
    # archive 套用在 ship 的工作區裡，commit 也在那裡——同一棵樹。
    assert harness.runner.archived_in == [workspace]
    assert updated.candidate_head not in (None, harness.candidate)
    assert _git(workspace, "rev-parse", "HEAD") == updated.candidate_head
    # 且已回收：來源樹的 delivery branch 就是新 candidate。
    assert (
        job_workspace.source_branch_head(harness.repo, "feature/14-work")
        == updated.candidate_head
    )
    # builder 的 clone 全程沒動。
    assert (harness.worktree / "openspec" / "changes" / "work").is_dir()
    assert _git(harness.worktree, "rev-parse", "HEAD") == harness.candidate


# ---------------------------------------------------------------------------
# 3. archive → policy-commit 的接續，與 `matches_candidate()` 的 ancestry 前提
# ---------------------------------------------------------------------------

def test_archive_to_policy_commit_handoff_binds_the_archive_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """archive 卡的 job 記錄就是後續 ship 段會綁到的那一筆。

    `_builder_binding()` 現在只回 branch，但**選 job 的那一段一個位元組沒改**
    ——archive 之後被指名的就是 archive 卡自己。這條是本票最容易回歸的一條
    （範本為 #654 的同名測試，改成走 validator 的正式路徑）。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    _with_real_ship_cards(harness)
    workspace = _ship_workspace(harness, harness.candidate)

    assert _capture(
        harness.validator, run=harness.run, candidate=harness.candidate
    ).exception is None
    updated = harness.registry.get_workflow_run(harness.run_id)
    archive_head = str(updated.candidate_head)

    archive_jobs = [
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == harness.run_id
        and job.get("workflow_phase") == "ship"
        and job.get("workflow_card") == "openspec-archive"
    ]
    assert len(archive_jobs) == 1
    archive_job = archive_jobs[0]
    assert archive_job["subject_head"] == archive_head
    assert archive_job["branch"] == "feature/14-work"
    assert archive_job["persona"] == "manager"
    assert archive_job["executor"] == "cortex-manager"
    # #653：記在 archive 卡上的工作區是 **Manager-owned 的 ship 樹**，不是
    # builder 的 clone。post-archive 的 verify／review 卡以
    # `builder_jobs[-1]["worktree"]` 當 candidate 樹，因此這棵樹不得在 ship 段
    # 結束時被刪掉（回收交給 `cortex work gc`，與 build 卡的 clone 同一套）。
    assert archive_job["worktree"] == str(workspace)
    assert workspace.is_dir()
    assert workspace != harness.worktree

    # policy-commit 記在同一棵樹、同一個 candidate 上，且 ship audit 兩張卡皆 passed。
    run = harness.registry._manager_update_workflow_run(
        updated.run_id, current_phase="review", verified_head=archive_head
    )
    work_bridge._record_manager_ship_job(
        registry=harness.registry,
        state_root=harness.state_root,
        run=run,
        worktree=workspace,
        branch="feature/14-work",
        card="policy-commit",
        old_head=archive_head,
        new_head=archive_head,
    )
    audited = manager._validated_ship_steps(
        harness.registry,
        run=run,
        candidate=archive_head,
        coordinator_root=harness.state_root,
    )
    assert all(step.gate_result == "passed" for step in audited if step.phase == "ship")


def test_post_archive_repair_still_provisions_and_passes_ship_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`matches_candidate()` 的 ancestry 分支在搬家之後仍然成立。

    #654 剛把這條檢查的**前提**修好（archive commit 進來源樹）。本票換掉的是
    archive commit 是在哪棵樹裡做出來的，回收通道一個位元組沒改——這條因此是
    「別弄壞它」的守衛：以 archive commit 當 base 還能 provision（#651 的
    post-archive `retry-build`），且 final candidate 對 archive commit 的祖先關係
    在**來源樹**上驗得出來，ship audit 走得通。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=True, archived_change=False
    )
    _with_real_ship_cards(harness)
    assert _capture(
        harness.validator, run=harness.run, candidate=harness.candidate
    ).exception is None
    archive_head = str(harness.registry.get_workflow_run(harness.run_id).candidate_head)

    # post-archive repair：以 archive commit 為 base 重新 provision（creator 的第一
    # 道守衛就是在來源樹 `rev-parse --verify <base>`），做出 descendant 並回收。
    creator = seams.ScriptWorktreeCreator(
        repo=harness.repo, wt_root=tmp_path / "repair-pool", base="main"
    )
    repair = Path(
        creator.create("feature/14-work", job_id="wf-repair-1", base_sha=archive_head)
    )
    (repair / "repair.txt").write_text("repair\n", encoding="utf-8")
    _git(repair, "add", "repair.txt")
    _git(repair, "commit", "-qm", "fix review finding")
    final = _git(repair, "rev-parse", "HEAD")
    bundle = job_workspace.prepare_commit_spool(
        spool_key="wf-repair-1", coordinator_root=harness.state_root
    )
    job_workspace.publish_commit_bundle(
        workspace=repair, bundle=bundle, branch="feature/14-work", exclude=archive_head
    )
    assert (
        job_workspace.harvest_branch(
            source_repo=str(harness.repo), bundle=bundle, branch="feature/14-work"
        )
        == final
    )
    assert (
        subprocess.run(
            [
                "git", "-C", str(harness.repo), "merge-base", "--is-ancestor",
                archive_head, final,
            ],
            capture_output=True,
        ).returncode
        == 0
    )

    run = harness.registry._manager_update_workflow_run(
        harness.run_id,
        current_phase="review",
        candidate_head=final,
        verified_head=final,
    )
    work_bridge._record_manager_ship_job(
        registry=harness.registry,
        state_root=harness.state_root,
        run=run,
        worktree=repair,
        branch="feature/14-work",
        card="policy-commit",
        old_head=final,
        new_head=final,
    )

    audited = manager._validated_ship_steps(
        harness.registry,
        run=run,
        candidate=final,
        coordinator_root=harness.state_root,
    )
    assert all(step.gate_result == "passed" for step in audited if step.phase == "ship")


# ---------------------------------------------------------------------------
# 4. direct 零回歸 ／ 單 UID 測不到的那一半
# ---------------------------------------------------------------------------

def test_ship_workspace_has_no_runner_mode_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`direct` 零回歸：本票沒有引入任何依 `PSC_JOB_RUNNER` 的分支。

    #634 的原則——以形狀判斷，不依旗標分支。兩種 runner mode 下走的是同一條路徑，
    結構性結果逐項相等（commit 的 SHA 兩邊本來就不同：不同的 tmp repo、不同的
    時間戳，因此比的是事實而不是字面值）。
    """

    observed = {}
    for index, mode in enumerate(("direct", "systemd-template")):
        monkeypatch.setenv("PSC_JOB_RUNNER", mode)
        (tmp_path / f"case-{index}").mkdir()
        harness = _ship_harness(
            tmp_path / f"case-{index}",
            monkeypatch,
            active_change=True,
            archived_change=False,
        )
        workspace = _ship_workspace(harness, harness.candidate)
        outcome = _capture(
            harness.validator, run=harness.run, candidate=harness.candidate
        )
        updated = harness.registry.get_workflow_run(harness.run_id)
        observed[mode] = {
            "raised": outcome.exception is not None,
            "phase": updated.current_phase,
            "advanced": updated.candidate_head != harness.candidate,
            "harvested": job_workspace.source_branch_head(
                harness.repo, "feature/14-work"
            )
            == updated.candidate_head,
            "manager_owned_workspace": job_workspace.is_job_clone(workspace),
            "archived_in_ship_tree": harness.runner.archived_in == [workspace],
            "builder_clone_untouched": (
                harness.worktree / "openspec" / "changes" / "work"
            ).is_dir(),
        }

    assert observed["direct"] == observed["systemd-template"]
    assert observed["direct"] == {
        "raised": False,
        "phase": "verify",
        "advanced": True,
        "harvested": True,
        "manager_owned_workspace": True,
        "archived_in_ship_tree": True,
        "builder_clone_untouched": True,
    }


def test_builder_clone_ownership_boundary_is_not_single_uid_testable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真正的邊界是 **OS 層**的，單 UID 測不出來——明確 skip（#638 的教訓）。

    生產上 builder 的 clone 是 `0700 cortex-builder`、ship 段以 `cortex-manager`
    執行，兩者是不同 UID；`chmod 000` 只是那個形狀的**近似**（同 UID 下 owner 隨時
    `chmod` 回去就繞過了，而且 root 完全不受限）。同理，「Manager 對 job 工作樹零
    `setfacl`」（#641 runbook 稽核 5b）是登記表與檔案系統 ACL 的性質，本機與 CI
    都沒有那些帳號，任何模擬與真部署無關。

    skip 之前先斷言**可測的那一半**：ship 段拿到的工作區在來源樹自己的 pool 底下、
    是 Manager 這個行程 clone 出來的，與 builder 的樹是兩個獨立的 object store
    ——那是「不需要任何指向 job 工作樹的授權」這件事在單 UID 下唯一驗得到的形式。
    """

    harness = _ship_harness(
        tmp_path, monkeypatch, active_change=False, archived_change=True
    )
    workspace = _ship_workspace(harness, harness.candidate)
    assert _capture(
        harness.validator, run=harness.run, candidate=harness.candidate
    ).exception is None

    assert workspace.is_dir() and workspace != harness.worktree
    assert workspace.stat().st_uid == os.geteuid(), "ship 工作區不是這個行程建的"
    assert (workspace / ".git" / "objects").is_dir(), "不是獨立 object store"
    assert not (workspace / ".git").is_file(), "不得退回 linked worktree（#623）"

    pytest.skip(
        "真正要驗的是 OS 層語意：builder 的 clone 為 `0700 cortex-builder`、ship 段"
        "以 `cortex-manager` 執行，且 `/var/lib/cortex/worktree/` 底下零 `setfacl`"
        "（#641 runbook 稽核 5b）。單 UID 的開發機與 CI 兩者皆無——此處 ship 段與"
        "builder 的樹同屬一個 uid，`chmod` 回去就繞過了，模擬出來的結果與真部署無關。"
        "可測的那一半（工作區是 Manager 自己 clone 的獨立 object store）已在 skip "
        "之前斷言；權限面的端到端驗證屬 runbook 的實機稽核"
    )
