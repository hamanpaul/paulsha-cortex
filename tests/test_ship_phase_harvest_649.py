"""#649：ship phase 的成果回收——`openspec-archive` 的 commit 必須進來源樹。

本檔全部跑**正式路徑**：真的 git repo、真的 per-job clone、真的
`work_bridge._commit_archive_and_require_reverification()`、真的
`seams.ScriptWorktreeCreator`。不手動 `git push`／不自己捏 registry 狀態。

查證結論（見 PR body）：`manager._validated_ship_steps()` 的 `matches_candidate()`
容許 `openspec-archive` 的 `subject_head` 是 final candidate 的**祖先**，而那條
ancestry 檢查跑在 `run.workspace_root`（來源樹）上。#623 把工作區從 `git worktree`
（共用 object store）換成 per-job 完整 clone 之後，archive commit 只存在於做 commit
的那棵 clone 裡，來源樹沒有它 ⇒ `git merge-base --is-ancestor` 回 **128**
（`Not a valid commit name`）⇒ `matches_candidate()` 回 False ⇒ 整個 ship audit
fail-closed。既有測試看不到這件事，是因為它的 fixture 把兩個 commit 都直接做在來源樹
裡（#623 之前的形狀）。本檔的 `test_ship_audit_ancestry_needs_harvested_archive_commit`
與 `..._fails_closed_without_harvest` 一正一反把它釘住。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import (
    job_workspace,
    manager,
    seams,
    work_bridge,
)
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, WorkflowStep

from git_fixtures import make_job_clone

BRANCH = "feature/14-ship-harvest"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(root: Path) -> tuple[Path, str]:
    """來源樹：預設 branch 上有一個 commit，delivery branch 指向它但**不 checkout**。

    這是 build harvest 跑完之後來源樹的實況——`refs/heads/<branch>` ==
    `run.candidate_head`，而 Manager 自己停在預設 branch。
    """

    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "remote", "add", "origin", "git@github.com:acme/demo.git")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("## [Unreleased]\n\n- work\n", encoding="utf-8")
    change = root / "openspec" / "changes" / "work"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (change / "tasks.md").write_text("- [x] ready\n", encoding="utf-8")
    todo = root / "docs" / "todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text("- [x] ready\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    _git(root, "branch", BRANCH)
    return root, _git(root, "rev-parse", "HEAD")


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
                            "todo:acme/demo:docs/todo.md@todo-1",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _steps() -> tuple[WorkflowStep, ...]:
    personas = {
        "claim": "manager",
        "define": "planner",
        "plan": "planner",
        "build": "builder",
        "verify": "reviewer",
        "review": "reviewer",
    }
    steps = [
        WorkflowStep(
            phase=phase,
            persona=persona,
            card=f"{phase}-card",
            executor="codex" if phase == "build" else "claude",
            model="gpt" if phase == "build" else "sonnet",
            domain="openai" if phase == "build" else "anthropic",
            inputs=(),
            outputs=(),
            gate_result="passed",
        )
        for phase, persona in personas.items()
    ]
    steps.append(
        WorkflowStep("ship", "manager", "openspec-archive", None, None, None, (), ())
    )
    steps.append(
        WorkflowStep("ship", "manager", "policy-commit", None, None, None, (), ())
    )
    return tuple(steps)


def _run(registry: JobRegistry, *, repo: Path, candidate: str, authority):
    return registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=_steps(),
        issue_refs=("acme/demo#14",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"verify": 1, "review": 1},
        gate_refs=(GateEvidenceRef("foreign-review", "review.json", "1" * 64),),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
    )


def _apply_archive(worktree: Path) -> None:
    """把 `openspec archive` 的檔案效果做出來（allowlist 內的異動）。"""

    archived = worktree / "openspec" / "changes" / "archive"
    archived.mkdir(parents=True, exist_ok=True)
    (worktree / "openspec" / "changes" / "work").rename(archived / "work")
    with (worktree / "CHANGELOG.md").open("a", encoding="utf-8") as handle:
        handle.write("- Archive work.\n")


def _archive_fixture(tmp_path: Path) -> SimpleNamespace:
    """跑到「archive commit 已回收」為止的共用場景。"""

    repo, candidate = _source_repo(tmp_path / "repo")
    worktree = make_job_clone(repo, tmp_path / "job-worktree", branch=BRANCH)
    authority = load_work_authority(
        repo="acme/demo", work_id="work", snapshot_path=_snapshot(tmp_path / "snap.json")
    )
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    run = _run(registry, repo=repo, candidate=candidate, authority=authority)
    _apply_archive(worktree)
    reset = work_bridge._commit_archive_and_require_reverification(
        registry=registry,
        state_root=state_root,
        run=run,
        authority=authority,
        worktree=worktree,
        branch=BRANCH,
        candidate=candidate,
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    return SimpleNamespace(
        repo=repo,
        worktree=worktree,
        authority=authority,
        state_root=state_root,
        registry=registry,
        candidate=candidate,
        reset=reset,
    )


# ---------------------------------------------------------------------------
# 1. 回收本身
# ---------------------------------------------------------------------------

def test_archive_commit_is_harvested_into_source_tree(tmp_path: Path) -> None:
    """archive commit 進來源樹，且 `refs/heads/<branch>` == 新 candidate。

    這正是 build phase 在 `_harvest_build_candidate()` 之後成立、而 ship phase 在
    本票之前不成立的那條不變式。
    """

    fixture = _archive_fixture(tmp_path)
    archive_head = fixture.reset.candidate_head

    assert archive_head is not None and archive_head != fixture.candidate
    assert job_workspace.commit_present(fixture.repo, archive_head)
    assert job_workspace.source_branch_head(fixture.repo, BRANCH) == archive_head
    # 採信值與來源樹實況同步：#651 的 `_workflow_build_handoff_base()` 以
    # `run.candidate_head` 當 clone base，兩者一分岔就會在 creator 的守衛上炸開。
    assert _git(fixture.repo, "rev-parse", f"refs/heads/{BRANCH}") == archive_head


def test_archive_handoff_survives_worktree_removal(tmp_path: Path) -> None:
    """交接不依賴磁碟殘留：把做 commit 的那棵工作區整個刪掉，成果仍在。

    #651 對 build 卡釘的是同一個形狀；ship 卡在本票之前做不到——archive commit
    只存在於那棵 clone 的 object store 裡，`rmtree` 一下就沒了。
    """

    fixture = _archive_fixture(tmp_path)
    archive_head = fixture.reset.candidate_head
    shutil.rmtree(fixture.worktree)

    assert not fixture.worktree.exists()
    assert job_workspace.commit_present(fixture.repo, archive_head)
    # 成果也真的還讀得出來（不只是 object 還在）。
    assert "archive/work" in _git(
        fixture.repo, "ls-tree", "-r", "--name-only", str(archive_head)
    )


def test_archive_harvest_runs_before_candidate_head_advances(tmp_path: Path) -> None:
    """回收失敗時 fail-closed，且 `candidate_head` **不得**先被推進。

    推進之後才發現來源樹沒有那個 commit，錯誤會在很遠的地方（下一次 provision、
    或 ship audit）以看不懂的訊息出現——那正是本票要消除的失敗形態。
    """

    repo, candidate = _source_repo(tmp_path / "repo")
    worktree = make_job_clone(repo, tmp_path / "job-worktree", branch=BRANCH)
    authority = load_work_authority(
        repo="acme/demo", work_id="work", snapshot_path=_snapshot(tmp_path / "snap.json")
    )
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    run = _run(registry, repo=repo, candidate=candidate, authority=authority)
    _apply_archive(worktree)
    # 工作區把 commit 做在 detached HEAD 上（branch 不動）——回收的 refspec 是
    # `refs/heads/<branch>`，此時 bundle 帶的就不是那個 commit。
    _git(worktree, "checkout", "-q", "--detach")

    with pytest.raises(RuntimeError, match="not on the recorded branch"):
        work_bridge._commit_archive_and_require_reverification(
            registry=registry,
            state_root=state_root,
            run=run,
            authority=authority,
            worktree=worktree,
            branch=BRANCH,
            candidate=candidate,
            runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.candidate_head == candidate
    assert unchanged.current_phase == "review"
    assert job_workspace.source_branch_head(repo, BRANCH) == candidate


# ---------------------------------------------------------------------------
# 2. `matches_candidate()` 的 ancestry 檢查（本票的查證題）
# ---------------------------------------------------------------------------

def _repair_descendant(fixture: SimpleNamespace, pool: Path) -> str:
    """post-archive repair：以 archive commit 為 base 重新 provision 並產 descendant。

    這一段同時是 **#651 的回歸守衛**：`_workflow_build_handoff_base()` 會把
    `run.candidate_head`（＝ archive commit）交給 `ScriptWorktreeCreator.create()`
    當 base，而它的第一道守衛就是在**來源樹**上 `rev-parse --verify <base>`。回收
    沒做的話這裡就是 `git worktree base invalid`，post-archive 的 `retry-build`
    直接起不來。
    """

    creator = seams.ScriptWorktreeCreator(repo=fixture.repo, wt_root=pool, base="main")
    repair = Path(
        creator.create(
            BRANCH, job_id="wf-repair-1", base_sha=str(fixture.reset.candidate_head)
        )
    )
    (repair / "repair.txt").write_text("repair\n", encoding="utf-8")
    _git(repair, "add", "repair.txt")
    _git(repair, "commit", "-qm", "fix review finding")
    final = _git(repair, "rev-parse", "HEAD")
    bundle = job_workspace.prepare_commit_spool(
        spool_key="wf-repair-1", coordinator_root=fixture.state_root
    )
    job_workspace.publish_commit_bundle(
        workspace=repair,
        bundle=bundle,
        branch=BRANCH,
        exclude=str(fixture.reset.candidate_head),
    )
    assert (
        job_workspace.harvest_branch(
            source_repo=str(fixture.repo), bundle=bundle, branch=BRANCH
        )
        == final
    )
    return final


def _record_policy_commit(fixture: SimpleNamespace, *, run, candidate: str) -> None:
    work_bridge._record_manager_ship_job(
        registry=fixture.registry,
        state_root=fixture.state_root,
        run=run,
        worktree=fixture.worktree,
        branch=BRANCH,
        card="policy-commit",
        old_head=candidate,
        new_head=candidate,
    )


def test_ship_audit_ancestry_needs_harvested_archive_commit(tmp_path: Path) -> None:
    """回收之後，post-archive repair 的 ship audit 走得通（ancestry 分支成立）。

    這條同時證明兩件事：`ScriptWorktreeCreator` 拿 archive commit 當 base 能
    provision（#651 的 post-archive retry-build），以及 final candidate 是 archive
    commit 的真祖先關係在**來源樹**上驗得出來。
    """

    fixture = _archive_fixture(tmp_path)
    archive_head = str(fixture.reset.candidate_head)
    final = _repair_descendant(fixture, tmp_path / "pool")

    assert final != archive_head
    assert (
        subprocess.run(
            [
                "git", "-C", str(fixture.repo), "merge-base", "--is-ancestor",
                archive_head, final,
            ],
            capture_output=True,
        ).returncode
        == 0
    )

    run = fixture.registry._manager_update_workflow_run(
        fixture.reset.run_id, candidate_head=final, verified_head=final
    )
    _record_policy_commit(fixture, run=run, candidate=final)

    audited = manager._validated_ship_steps(
        fixture.registry,
        run=run,
        candidate=final,
        coordinator_root=fixture.state_root,
    )
    assert all(step.gate_result == "passed" for step in audited if step.phase == "ship")


def test_ship_audit_ancestry_fails_closed_without_harvest(tmp_path: Path) -> None:
    """突變守衛：archive commit 沒進來源樹時，ancestry 檢查 fail-closed。

    這就是 #623 之後、本票之前的實況——`merge-base --is-ancestor` 對來源樹沒有的
    commit 回 128，`matches_candidate()` 回 False，整張卡被濾掉。**刻意不對錯誤
    訊息以外的行為做斷言**：重點是「它不會靜默放行」。
    """

    repo, candidate = _source_repo(tmp_path / "repo")
    worktree = make_job_clone(repo, tmp_path / "job-worktree", branch=BRANCH)
    authority = load_work_authority(
        repo="acme/demo", work_id="work", snapshot_path=_snapshot(tmp_path / "snap.json")
    )
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    run = _run(registry, repo=repo, candidate=candidate, authority=authority)

    # 只在 clone 裡做 archive commit（不回收）——本票之前的形狀。
    _apply_archive(worktree)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "chore(openspec): archive work")
    orphan_archive = _git(worktree, "rev-parse", "HEAD")
    (worktree / "repair.txt").write_text("repair\n", encoding="utf-8")
    _git(worktree, "add", "repair.txt")
    _git(worktree, "commit", "-qm", "fix")
    final = _git(worktree, "rev-parse", "HEAD")
    assert not job_workspace.commit_present(repo, orphan_archive)

    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=worktree,
        branch=BRANCH,
        card="openspec-archive",
        old_head=candidate,
        new_head=orphan_archive,
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        candidate_head=final,
        verified_head=final,
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
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=worktree,
        branch=BRANCH,
        card="policy-commit",
        old_head=final,
        new_head=final,
    )

    with pytest.raises(ValueError, match="openspec-archive"):
        manager._validated_ship_steps(
            registry, run=run, candidate=final, coordinator_root=state_root
        )


# ---------------------------------------------------------------------------
# 3. `openspec-archive` → `policy-commit` 的接續
# ---------------------------------------------------------------------------

def test_archive_to_policy_commit_handoff_binds_the_archive_job(tmp_path: Path) -> None:
    """archive 之後的每一段都指向同一棵樹與同一個 candidate。

    `_builder_binding()` 以「`subject_head == candidate` 的 job」決定 ship 段後續
    要用哪個工作區與 branch；archive 之後那個 job 就是 archive 卡自己。這條把
    「archive 卡的記錄 → 後續 ship 段」這條接線釘住，也是本票最容易回歸的一條。
    """

    fixture = _archive_fixture(tmp_path)
    archive_head = str(fixture.reset.candidate_head)
    run = fixture.registry.get_workflow_run(fixture.reset.run_id)

    archive_jobs = [
        job
        for job in fixture.registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == "ship"
        and job.get("workflow_card") == "openspec-archive"
    ]
    assert len(archive_jobs) == 1
    archive_job = archive_jobs[0]
    assert archive_job["subject_head"] == archive_head
    assert archive_job["branch"] == BRANCH
    assert archive_job["persona"] == "manager"
    assert archive_job["executor"] == "cortex-manager"

    # `_builder_binding()` 會挑到它，且拿到的 worktree／branch 就是 archive 卡的。
    run = fixture.registry._manager_update_workflow_run(
        run.run_id, verified_head=archive_head
    )
    _record_policy_commit(fixture, run=run, candidate=archive_head)
    audited = manager._validated_ship_steps(
        fixture.registry,
        run=run,
        candidate=archive_head,
        coordinator_root=fixture.state_root,
    )
    assert all(step.gate_result == "passed" for step in audited if step.phase == "ship")

    # `policy-commit` 不產生 commit：來源樹的 branch 一個位元組沒動。
    assert job_workspace.source_branch_head(fixture.repo, BRANCH) == archive_head


def test_ship_job_id_is_reserved_and_addresses_its_own_spool(tmp_path: Path) -> None:
    """spool key ＝ 那張卡的 job_id（`reserve_job_id()` 配發的那一個）。

    與 #651 的 build 卡同一條順序：先配 id → 用它定址 → 再建 job。兩處的 task 字串
    由 `_manager_ship_job_task()` 單一推導，registry 因此驗得過。
    """

    fixture = _archive_fixture(tmp_path)
    run = fixture.registry.get_workflow_run(fixture.reset.run_id)
    archive_job = next(
        job
        for job in fixture.registry.list_jobs()
        if job.get("workflow_card") == "openspec-archive"
    )
    expected_task = work_bridge._manager_ship_job_task(run=run, card="openspec-archive")

    assert archive_job["task"] == expected_task
    assert str(archive_job["job_id"]).startswith(f"{expected_task}-")
    slot = job_workspace.commit_spool_dir(
        spool_key=str(archive_job["job_id"]), coordinator_root=fixture.state_root
    )
    assert slot.is_dir()
    assert (slot / job_workspace.COMMIT_BUNDLE_FILENAME).is_file()


def test_ship_harvest_has_no_runner_mode_branch(tmp_path: Path, monkeypatch) -> None:
    """`direct` 零回歸：本 PR 沒有引入任何依 `PSC_JOB_RUNNER` 的分支。

    #634 的原則——以形狀判斷，不依旗標分支。兩種 runner mode 下走的是同一條路徑，
    結果逐字相同。
    """

    observed = {}
    for index, mode in enumerate(("direct", "systemd-template")):
        monkeypatch.setenv("PSC_JOB_RUNNER", mode)
        fixture = _archive_fixture(tmp_path / f"case-{index}")
        archive_head = str(fixture.reset.candidate_head)
        observed[mode] = {
            # commit 的 SHA 兩邊本來就不同（不同的 tmp repo、不同的時間戳），
            # 因此比的是**結構性事實**而不是字面值。
            "harvested": job_workspace.source_branch_head(fixture.repo, BRANCH)
            == archive_head,
            "advanced": archive_head != fixture.candidate,
            "phase": fixture.reset.current_phase,
            "spool_sealed": job_workspace.commit_spool_dir(
                spool_key=str(
                    next(
                        job
                        for job in fixture.registry.list_jobs()
                        if job.get("workflow_card") == "openspec-archive"
                    )["job_id"]
                ),
                coordinator_root=fixture.state_root,
            ).stat().st_mode
            & 0o200
            == 0,
        }

    assert observed["direct"] == observed["systemd-template"]
    assert observed["direct"] == {
        "harvested": True,
        "advanced": True,
        "phase": "verify",
        "spool_sealed": True,
    }


def test_commit_spool_seal_enforcement_is_not_single_uid_testable(tmp_path: Path) -> None:
    """封口的**強制力**單 UID 測不出來——明確 skip，不靜默通過（#638 的教訓）。

    `seal_commit_spool()` 收掉那一格目錄的 `w`，同時把 POSIX ACL 的 mask 收成
    `---`，producer 具名條目的授權因此一併失效。但 producer 進不去那一格的前提是
    「唯一路徑是具名 ACL 條目」——同 UID 下 producer 就是目錄 owner，`chmod` 回去
    即可，任何單 UID 的模擬都測不到真正的語意（`spool_slot` 模組 docstring 的
    「誠實邊界」段已寫明）。這裡只驗**可測的那一半**：封口確實發生。
    """

    fixture = _archive_fixture(tmp_path)
    archive_job = next(
        job
        for job in fixture.registry.list_jobs()
        if job.get("workflow_card") == "openspec-archive"
    )
    slot = job_workspace.commit_spool_dir(
        spool_key=str(archive_job["job_id"]), coordinator_root=fixture.state_root
    )
    assert slot.stat().st_mode & 0o200 == 0, "封口沒發生：那一格仍可寫"

    pytest.skip(
        "封口對 producer 的**強制力**需要 producer 與 consumer 是不同 UID"
        "（三分部署的 `cortex-builder` / `cortex-manager`），且那一格帶 per-account "
        "POSIX ACL。單 UID 的開發機與 CI 兩者皆無：此處 Manager 同時是 producer 與 "
        "目錄 owner，`chmod` 回去就繞過了，模擬出來的結果與真部署無關。"
        "可測的那一半（封口真的發生）已在 skip 之前斷言"
    )
