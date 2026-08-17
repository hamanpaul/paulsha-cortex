"""#650：verify／review 卡的 candidate 樹搬出 builder 的 clone。

在此之前 verify／review 卡以 `builder_jobs[-1]["worktree"]`（前一張 build 卡的工作
區）為 candidate 樹，六個用途全掛在它身上。#648 把 build phase 的工作區改成 per-job
之後，一個 run 會累積 N 棵這種樹（每棵約 35MB），而 `_harvest_build_candidate()` 落地
之後**被採信的卡的工作區已經沒有任何獨佔資訊**——bundle 已封存、commit 已在來源樹
裡。唯一還讓它回收不掉的就是這條引用。

本檔全部跑**正式路徑**：真 git repo、真 `seams.ScriptWorktreeCreator` provision 的
per-job clone、真 `manager.dispatch_workflow_card()`、真 #637 bundle ＋ append-only
spool 交接、真 `_create_reviewer_sandbox()`、真
`_WorkflowReportPublicationTransaction`。不手動 `git push`、不自己捏工作區。

三條核心不變式：

1. **解耦**：把前一張 build 卡的工作區刪掉（或 `chmod 000`），verify／review 卡仍
   正確派得出去。形狀沿用 #653 的
   `test_ship_phase_completes_while_the_builder_clone_is_unreadable`。
2. **語意不變**：`workflow_sandbox_hash`／input snapshot（含 #310 checkbox 容忍）／
   output baseline 在新模型下與舊模型逐位元相同——這三個進 evidence，改了會動到採信
   判斷。
3. **卡與卡的交接**：`code-review` 發佈在 candidate 樹裡的 canonical report 是
   `adversarial-review` 的宣告輸入（`requires: reports/review/*<task-slug>*.md`）。
   同一個 candidate 的 review 卡因此必須共用同一棵樹；candidate 前進才換一棵。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.config.paths import worktree_root_for
from paulsha_cortex.coordinator import (
    job_workspace,
    manager,
    planning_runtime,
    seams,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


_REPO = "hamanpaul/paulsha-cortex"
_WORK_ID = "650-reviewer-candidate-tree"
_BRANCH = "feature/650-650-reviewer-candidate-tree"

TASKS_REF = "openspec/changes/demo-650/tasks.md"
PROPOSAL_REF = "openspec/changes/demo-650/proposal.md"

TASKS_BASELINE = """---
status: accepted
work_item: demo-650
---

# Tasks

- [ ] 1.1 RED：新增測試。
- [ ] 1.2 GREEN：實作。
"""
TASKS_TICKED = TASKS_BASELINE.replace("- [ ] 1.1", "- [x] 1.1")
PROPOSAL_BASELINE = "# Proposal\n\n把 candidate 樹搬出 builder 的 clone。\n"

REPORT_REF = "reports/review/demo-650.md"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(root: Path) -> Path:
    """來源樹（`run.workspace_root`）：Manager 擁有、可寫，停在 `main`。"""

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    _git(root, "config", "user.email", "manager@example.invalid")
    _git(root, "config", "user.name", "Cortex Manager")
    (root / TASKS_REF).parent.mkdir(parents=True, exist_ok=True)
    (root / TASKS_REF).write_text(TASKS_BASELINE, encoding="utf-8")
    (root / PROPOSAL_REF).write_text(PROPOSAL_BASELINE, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _planning_authority() -> tuple[PlanningArtifactAuthority, ...]:
    return (
        PlanningArtifactAuthority(
            ref=TASKS_REF,
            kind="plan",
            work_id=_WORK_ID,
            baseline_sha256=hashlib.sha256(TASKS_BASELINE.encode()).hexdigest(),
        ),
        PlanningArtifactAuthority(
            ref=PROPOSAL_REF,
            kind="spec",
            work_id=_WORK_ID,
            baseline_sha256=hashlib.sha256(PROPOSAL_BASELINE.encode()).hexdigest(),
        ),
    )


def _steps(*, build_passed: bool, review_passed: tuple[str, ...] = ()) -> tuple[WorkflowStep, ...]:
    """build 一張 ＋ review 兩張（`code-review` → `adversarial-review`）。

    兩張 review 卡的 `inputs`／`outputs` 沿用 deck 的形狀：第二張的 `requires` 就是
    第一張的 `produces`（見 `deck/data/cards.yaml`），那正是本票必須維持的交接。
    """

    return (
        WorkflowStep(
            phase="build",
            persona="builder",
            card="subagent-build",
            executor="codex" if build_passed else None,
            model="gpt-primary" if build_passed else None,
            domain="openai" if build_passed else None,
            inputs=(),
            outputs=(),
            commit_policy="required",
            test_policy="focused",
            gate_result="passed" if build_passed else "pending",
        ),
        WorkflowStep(
            phase="review",
            persona="reviewer",
            card="code-review",
            executor=None,
            model=None,
            domain=None,
            inputs=(),
            outputs=("reports/review/*.md",),
            gate_result="passed" if "code-review" in review_passed else "pending",
        ),
        WorkflowStep(
            phase="review",
            persona="reviewer",
            card="adversarial-review",
            executor=None,
            model=None,
            domain=None,
            inputs=("reports/review/*.md",),
            outputs=("reports/review/*.md",),
            gate_result="passed" if "adversarial-review" in review_passed else "pending",
        ),
    )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "sonnet-primary",
                "independence_domain": "anthropic",
                "capabilities": ["build", "review"],
            },
        ]
    )


class _RecordingLauncher:
    """記下每次 launch 的 `slice_id`／`worktree`（reviewer 的 worktree ＝ sandbox）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self) -> "_RecordingLauncher":
        return self

    def as_review_only(self, *, terminal_kind: str) -> "_RecordingLauncher":
        self.terminal_kind = terminal_kind
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "worktree": worktree, "prompt": prompt})
        return LaunchHandle(
            executor="claude",
            model_id="sonnet-primary",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.source = _source_repo(tmp_path / "source")
        self.pool = worktree_root_for(self.source)
        self.coordinator_root = tmp_path / "coordinator"
        self.registry = JobRegistry(state_path=self.coordinator_root / "jobs.json")
        self.run = self.registry._manager_create_workflow_run(
            work_id=_WORK_ID,
            repo=_REPO,
            claim_key="claim:v1:" + "1" * 64,
            source_revision="2" * 64,
            workspace_root=str(self.source),
            combo="feature-oneshot",
            current_phase="build",
            steps=_steps(build_passed=False),
            issue_refs=(f"{_REPO}#650",),
            openspec_refs=(),
            pr_refs=(),
            attempts={"build": 1},
            gate_status="running",
            planning_authority=_planning_authority(),
        )
        self.builder_clone: Path | None = None

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, *, force_new_card: bool = False) -> tuple[dict[str, object], _RecordingLauncher]:
        creator = seams.ScriptWorktreeCreator(
            repo=self.source, wt_root=self.pool, base="main"
        )
        dispatcher = type(
            "D",
            (),
            {"_registry": self.registry, "_worktree_creator": creator, "_git_runner": None},
        )()
        launcher = _RecordingLauncher()
        dispatched = manager.dispatch_workflow_card(
            dispatcher,
            run=self.registry.get_workflow_run(self.run.run_id),
            identities=_identities(),
            launcher_factory=lambda _identity: launcher,
            coordinator_root=self.coordinator_root,
            force_new_card=force_new_card,
        )
        assert dispatched is not None
        return self.registry.get_job(str(dispatched["job_id"])), launcher

    # -- build phase --------------------------------------------------------

    def land_a_candidate(
        self, *, tasks_text: str = TASKS_TICKED, force_new_card: bool = False
    ) -> str:
        """跑完一張 build 卡：provision → commit → #637 交接 → 採信。"""

        job, _launcher = self.dispatch(force_new_card=force_new_card)
        clone = Path(str(job["worktree"]))
        self.builder_clone = clone
        (clone / TASKS_REF).write_text(tasks_text, encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "build: tick a checkbox")
        candidate = _git(clone, "rev-parse", "HEAD").lower()

        spool_key = job_workspace.spool_key_for_job(job) or str(job["job_id"])
        bundle = job_workspace.prepare_commit_spool(
            spool_key=spool_key, coordinator_root=self.coordinator_root
        )
        produced = subprocess.run(
            ["bash", "-c", job_workspace.build_bundle_command(workspace=clone, bundle=bundle)],
            capture_output=True,
            text=True,
        )
        assert produced.returncode == 0, produced.stderr
        harvested = job_workspace.harvest_branch(
            source_repo=self.source, bundle=bundle, branch=str(job["branch"])
        )
        assert harvested.lower() == candidate

        self.registry.update_headless_result(
            str(job["job_id"]), status="exited", exit_code=0
        )
        self.registry.bind_workflow_evidence(
            str(job["job_id"]),
            locator={"kind": "build", "path": "evidence/workflow/fake.json", "hash": "a" * 64},
            subject_head=candidate,
        )
        self.run = self.registry._manager_update_workflow_run(
            self.run.run_id,
            steps=_steps(build_passed=True),
            candidate_head=candidate,
        )
        return candidate

    def advance_to_review(self) -> None:
        """phase 只能一格一格往前（`validate_workflow_phase_transition`）。"""

        for phase in ("verify", "review"):
            self.run = self.registry._manager_update_workflow_run(
                self.run.run_id, current_phase=phase
            )

    def reopen_build_for_retry(self, *, candidate: str) -> None:
        """走**真的** `retry-build` 原子重置，把最後一張 build 卡打回 pending。"""

        self.registry._manager_update_workflow_run(
            self.run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=fixture_needs_human_reason(),
        )
        self.run = self.registry._manager_reset_workflow_for_retry_build(
            self.run.run_id,
            expected_candidate=candidate,
            repair_action="review 提出阻擋性 findings，重跑 build 卡",
        )

    # -- review phase -------------------------------------------------------

    def accept_review_card(self, job: dict[str, object], card: str) -> None:
        self.registry.update_headless_result(
            str(job["job_id"]), status="exited", exit_code=0
        )
        self.run = self.registry._manager_update_workflow_run(
            self.run.run_id, steps=_steps(build_passed=True, review_passed=(card,))
        )

    def publish_report(self, job: dict[str, object], *, body: str) -> str:
        """走**正式**的 canonical report 發佈通道，把 report 落進 `workflow_repo_root`。

        production 路徑是 `terminalize_workflow_job()` 在 evidence 綁定前後包一層這個
        transaction；這裡直接用同一個 publisher，不另抄一份寫檔。
        """

        transaction = manager._WorkflowReportPublicationTransaction(
            repo_root=Path(str(job["workflow_repo_root"])),
            coordinator_root=self.coordinator_root,
            job_id=str(job["job_id"]),
        )
        transaction.publish(
            ((REPORT_REF, body),),
            job=job,
            candidate=str(job["subject_head"]),
        )
        transaction.commit()
        return REPORT_REF

    # -- derived paths ------------------------------------------------------

    def candidate_tree(self, candidate: str) -> Path:
        """這一輪 verify／review 實際使用的 candidate 樹（由產品的推導點算出來）。"""

        return job_workspace.workspace_path(
            self.pool,
            manager._reviewer_candidate_workspace_id(
                self.registry.get_workflow_run(self.run.run_id), candidate
            ),
        )


# ---------------------------------------------------------------------------
# 1. 本票的全部價值：verify／review 不再讀前一張 build 卡的工作區
# ---------------------------------------------------------------------------


def test_review_dispatch_works_after_the_builder_clone_is_deleted(tmp_path: Path) -> None:
    """把前一張 build 卡的工作區整棵刪掉，review 卡仍派得出去。

    這是「卡被採信之後即時回收它的工作區」的機械前提：只要 verify／review 還讀那棵樹，
    就回收不掉。刪除版對任何 UID 都成立，因此是本檔的主測試。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    assert harness.builder_clone is not None
    shutil.rmtree(harness.builder_clone)
    assert not harness.builder_clone.exists()

    job, launcher = harness.dispatch()

    assert job["persona"] == "reviewer"
    assert job["workflow_card"] == "code-review"
    assert job["subject_head"] == candidate
    # candidate 樹是 Manager 自己 clone 的那一棵，不是（已不存在的）builder clone。
    assert Path(str(job["workflow_repo_root"])) == harness.candidate_tree(candidate)
    assert len(launcher.calls) == 1


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root 不受目錄權限限制，`chmod 000` 這條不變式驗不到"
)
def test_review_dispatch_works_while_the_builder_clone_is_unreadable(
    tmp_path: Path,
) -> None:
    """把 builder 的 clone `chmod 000`——實機上 `0700 cortex-builder` 對 Manager 的樣子。

    本票之前這裡必炸，而且不只是「讀」：`_workflow_input_snapshot()` 會往那棵樹
    `mkdir`＋`mkstemp` seed 缺席的 planning authority 檔，`planning_runtime._tree_snapshot()`
    會遞迴走完整棵樹。#641 收掉 Manager 對 job 工作樹的唯讀 ACL 之後，兩步都是
    `Permission denied`。形狀沿用 #653 的
    `test_ship_phase_completes_while_the_builder_clone_is_unreadable`。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    assert harness.builder_clone is not None
    os.chmod(harness.builder_clone, 0o000)
    try:
        # 前提成立：這棵樹現在真的進不去。
        assert (
            subprocess.run(
                ["git", "-C", str(harness.builder_clone), "status"], capture_output=True
            ).returncode
            != 0
        )
        job, _launcher = harness.dispatch()
    finally:
        os.chmod(harness.builder_clone, 0o700)

    assert job["persona"] == "reviewer"
    assert Path(str(job["workflow_repo_root"])) == harness.candidate_tree(candidate)
    # 那棵 clone 的 HEAD 與工作區內容一個位元組沒被動過。
    assert _git(harness.builder_clone, "rev-parse", "HEAD").lower() == candidate
    assert _git(harness.builder_clone, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# 2. candidate 樹的身分
# ---------------------------------------------------------------------------


def test_the_candidate_tree_is_a_manager_owned_clone_at_the_accepted_candidate(
    tmp_path: Path,
) -> None:
    """身分：pool 底下、真 clone（`.git` 是目錄）、標記檔對得上、HEAD 恰為 candidate。"""

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    job, _launcher = harness.dispatch()

    tree = Path(str(job["workflow_repo_root"]))
    assert tree == harness.candidate_tree(candidate)
    assert tree.parent == harness.pool
    assert tree != harness.builder_clone
    assert job_workspace.is_job_clone(tree)
    # clone 模型（#623）：`.git` 是目錄，不得退回共用 object store 的 linked worktree。
    assert (tree / ".git").is_dir()
    assert not job_workspace.is_linked_worktree(tree)
    marker = job_workspace.read_marker(tree)
    assert isinstance(marker, dict)
    assert marker["branch"] == str(job["branch"])
    assert str(marker["base"]).lower() == candidate
    assert _git(tree, "rev-parse", "HEAD").lower() == candidate
    assert _git(tree, "symbolic-ref", "--short", "HEAD") == str(job["branch"])
    # 來源樹沒有被 clone 弄髒，仍停在 main。
    assert _git(harness.source, "symbolic-ref", "--short", "HEAD") == "main"


def test_the_candidate_tree_is_not_the_job_worktree(tmp_path: Path) -> None:
    """`direct` 零回歸 ＋ #648 邊界：reviewer job 的欄位形狀一格未改。

    `worktree` 仍是 reviewer sandbox、`workflow_input_root` 仍是 sandbox 裡的
    checkout、`workflow_repo_root` 仍是 candidate 樹。candidate 樹**不是** job 的
    工作區，因此 #648 的 `ReadWritePaths=<pool>/%i` 不變式本來就不落在它身上——這條
    是票上「不是 blocking，是耦合」那句話的機械形式。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    job, launcher = harness.dispatch()

    sandbox = Path(str(job["worktree"]))
    checkout = Path(str(job["workflow_input_root"]))
    tree = Path(str(job["workflow_repo_root"]))

    assert launcher.calls[0]["worktree"] == str(sandbox)
    assert sandbox.parent == harness.coordinator_root.resolve() / "review-sandboxes"
    assert checkout == sandbox / "candidate"
    assert _git(checkout, "rev-parse", "HEAD").lower() == candidate
    assert tree not in {sandbox, checkout}
    # 目錄名由 (run, candidate) 導出，**不是** job_id ⇒ 與模板 unit 的 `%i` 無關。
    assert tree.name != job_workspace.job_segment(str(job["job_id"]))
    workspace_id = manager._reviewer_candidate_workspace_id(
        harness.registry.get_workflow_run(harness.run.run_id), candidate
    )
    assert workspace_id.endswith(f"-review-{candidate[:12]}")
    assert tree.name == job_workspace.job_segment(workspace_id)


def test_dispatch_fails_closed_when_the_candidate_never_reached_the_source_tree(
    tmp_path: Path,
) -> None:
    """交接沒走完（來源樹沒有這個 commit）時 provision 不起來，而不是靜默走下去。

    守衛用的是 `ScriptWorktreeCreator` 既有的 `rev-parse --verify <base>^{commit}`，
    訊息逐字沿用——與 #648 的 build 交接同一條。
    """

    harness = _Harness(tmp_path)
    job, _launcher = harness.dispatch()
    clone = Path(str(job["worktree"]))
    (clone / TASKS_REF).write_text(TASKS_TICKED, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "build: never harvested")
    candidate = _git(clone, "rev-parse", "HEAD").lower()
    harness.registry.update_headless_result(
        str(job["job_id"]), status="exited", exit_code=0
    )
    harness.registry.bind_workflow_evidence(
        str(job["job_id"]),
        locator={"kind": "build", "path": "evidence/workflow/fake.json", "hash": "a" * 64},
        subject_head=candidate,
    )
    harness.run = harness.registry._manager_update_workflow_run(
        harness.run.run_id,
        steps=_steps(build_passed=True),
        candidate_head=candidate,
    )
    harness.advance_to_review()

    with pytest.raises(ValueError, match="git worktree base invalid"):
        harness.dispatch()


# ---------------------------------------------------------------------------
# 3. 語意不變（這三個進 evidence）
# ---------------------------------------------------------------------------


def test_sandbox_hash_input_snapshot_and_output_baseline_keep_their_meaning(
    tmp_path: Path,
) -> None:
    """三個進 evidence 的量在新模型下與舊模型逐位元相同。

    - `workflow_sandbox_hash`：舊模型是 `_tree_snapshot(builder 的 clone)`。兩棵樹是
      **同一個 commit 的兩份乾淨 checkout**，`_tree_snapshot` 排除 `.git`，因此雜湊
      必須相等——這條斷言就是「換了一棵樹但看到的內容一模一樣」的機械證明。
    - **input snapshot**：#310 的 checkbox 容忍仍以**候選實際內容**為 pinned 期望值
      （builder 勾過的 `tasks.md`），不是 claim 當下的 baseline。容忍若失效，
      `foreign_review.verify_authority_in_input_snapshot()` 會在 dispatch 當場炸開。
    - **output baseline**：candidate 樹上還沒有 review report ⇒ 空，與舊模型相同。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    assert harness.builder_clone is not None
    builder_snapshot = planning_runtime._tree_snapshot(harness.builder_clone)

    job, _launcher = harness.dispatch()

    assert job["workflow_sandbox_hash"] == builder_snapshot
    assert job["workflow_sandbox_hash"] == planning_runtime._tree_snapshot(
        Path(str(job["workflow_repo_root"]))
    )

    rows = {row["path"]: row for row in job["workflow_input_snapshot"]}
    assert set(rows) == {TASKS_REF, PROPOSAL_REF}
    # 勾過的 tasks.md：pinned 期望值 ＝ 候選的實際 hash（#310），不是 baseline。
    assert rows[TASKS_REF]["sha256"] == hashlib.sha256(TASKS_TICKED.encode()).hexdigest()
    assert rows[TASKS_REF]["sha256"] != hashlib.sha256(TASKS_BASELINE.encode()).hexdigest()
    assert rows[TASKS_REF]["authority"] == "planning-authority"
    # 沒被動過的 proposal.md：仍是 baseline。
    assert rows[PROPOSAL_REF]["sha256"] == hashlib.sha256(PROPOSAL_BASELINE.encode()).hexdigest()

    assert job["workflow_output_baseline"] == []

    # 採信時 `_verify_exact_candidate()` 對 reviewer 讀的就是 `workflow_repo_root`。
    assert manager._verify_exact_candidate(job) == candidate


# ---------------------------------------------------------------------------
# 4. 卡與卡的交接：canonical report 是下一張 review 卡的宣告輸入
# ---------------------------------------------------------------------------


def test_both_review_cards_of_one_candidate_share_the_tree_and_its_report(
    tmp_path: Path,
) -> None:
    """`code-review` 發佈的 report 必須被 `adversarial-review` 的 input snapshot 看到。

    這條是「candidate 樹的識別穩定於 (run, candidate) 而**不是** per-job」的理由：
    `adversarial-review` 的 `requires` 就是 `code-review` 的 `produces`，而 canonical
    report 是 Manager 發佈在 `workflow_repo_root` 裡的**未追蹤檔**（#653 之後不再被
    ship 段清掉）。每張卡各自 clone 一棵乾淨的樹，那個 glob 會落空 ⇒
    `workflow declared input missing`。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()

    first, _ = harness.dispatch()
    tree = Path(str(first["workflow_repo_root"]))
    created_at = job_workspace.read_marker(tree)["created_at"]
    body = "---\ncandidate: " + candidate + "\n---\n\n# Review\n\n找不到阻擋性問題。\n"
    harness.publish_report(first, body=body)
    assert (tree / REPORT_REF).is_file()
    harness.accept_review_card(first, "code-review")

    second, _ = harness.dispatch()

    assert second["workflow_card"] == "adversarial-review"
    # 同一棵樹（標記檔的 created_at 沒變 ⇒ 沒有重新 clone），report 原地還在。
    assert Path(str(second["workflow_repo_root"])) == tree
    assert job_workspace.read_marker(tree)["created_at"] == created_at
    assert (tree / REPORT_REF).is_file()

    rows = {row["path"]: row for row in second["workflow_input_snapshot"]}
    assert REPORT_REF in rows
    assert rows[REPORT_REF]["sha256"] == hashlib.sha256(
        (tree / REPORT_REF).read_bytes()
    ).hexdigest()
    assert rows[REPORT_REF]["authority"] == "worktree"
    # 前一張卡的產出也進了 output baseline，artifact 校驗因此看得到「本來就有的東西」。
    baseline = {row["path"]: row["sha256"] for row in second["workflow_output_baseline"]}
    assert baseline[REPORT_REF] == rows[REPORT_REF]["sha256"]
    # reviewer 真的在 sandbox 裡看得到那份 report（seed 自 evidence store，不是 clone）。
    assert (Path(str(second["workflow_input_root"])) / REPORT_REF).is_file()


def test_a_new_candidate_gets_a_new_candidate_tree(tmp_path: Path) -> None:
    """candidate 前進（retry-build／post-archive 重驗）就換一棵樹，前一棵原地留著。

    這是對的：新 candidate 的 review phase 從頭跑起，舊 candidate 的 report 不該被它
    讀到。舊那一棵交給 `cortex work gc`，與 build／ship 卡的 clone 同一套。
    """

    harness = _Harness(tmp_path)
    first_candidate = harness.land_a_candidate()
    harness.advance_to_review()
    first, _ = harness.dispatch()
    first_tree = Path(str(first["workflow_repo_root"]))
    harness.publish_report(first, body="---\nx: 1\n---\n\n# Review\n")

    # 走**真的** `retry-build`：review 提出阻擋性 findings → 最後一張 build 卡重開。
    harness.registry.update_headless_result(
        str(first["job_id"]), status="exited", exit_code=1
    )
    harness.reopen_build_for_retry(candidate=first_candidate)
    second_candidate = harness.land_a_candidate(
        tasks_text=TASKS_TICKED.replace("- [ ] 1.2", "- [x] 1.2"),
        force_new_card=True,
    )
    assert second_candidate != first_candidate
    harness.advance_to_review()

    second, _ = harness.dispatch(force_new_card=True)
    second_tree = Path(str(second["workflow_repo_root"]))

    assert second_tree != first_tree
    assert second_tree == harness.candidate_tree(second_candidate)
    assert _git(second_tree, "rev-parse", "HEAD").lower() == second_candidate
    # 前一棵原地留著（回收是 `cortex work gc` 的事），但新的那一棵沒有舊 report。
    assert first_tree.is_dir()
    assert (first_tree / REPORT_REF).is_file()
    assert not (second_tree / REPORT_REF).exists()


# ---------------------------------------------------------------------------
# 5. 重用的守衛
# ---------------------------------------------------------------------------


def test_reusing_the_candidate_tree_fails_closed_when_its_head_moved(
    tmp_path: Path,
) -> None:
    """這棵樹只有 Manager 寫。HEAD 被第三方動過就 fail-closed，不自癒、不刪。

    自癒（整棵重建）會把前一張 review 卡的 report 一起丟掉，讓下一張卡以
    `workflow declared input missing` 這種與成因無關的訊息失敗。
    """

    harness = _Harness(tmp_path)
    harness.land_a_candidate()
    harness.advance_to_review()
    first, _ = harness.dispatch()
    tree = Path(str(first["workflow_repo_root"]))
    harness.accept_review_card(first, "code-review")
    (tree / "intruder.txt").write_text("x\n", encoding="utf-8")
    _git(tree, "add", "intruder.txt")
    _git(tree, "commit", "-qm", "third party commit")

    with pytest.raises(ValueError, match="candidate workspace head mismatch"):
        harness.dispatch()


@pytest.mark.skip(
    reason="需要真的三分 UID：CI 以單一 UID 跑，`chown cortex-builder` 與 `getfacl` "
    "的具名條目都造不出來，`chmod 000` 只是同一件事的近似。稽核步驟寫在 "
    "docs/superpowers/runbooks/trust-root-phase2b-setup.md（`wf-*-review-*` 那段）"
    "——#638／#657 的教訓：測不到就明確 skip 並說明，不得靜默通過。"
)
def test_candidate_tree_is_owned_by_cortex_manager_with_no_named_acl() -> None:
    """實機稽核項（單 UID 驗不到）：

        stat -c '%U %a' /var/lib/cortex/worktree/wf-*-review-*   → cortex-manager 700
        getfacl -p /var/lib/cortex/worktree/wf-*-review-* | grep -c '^user:'  → 0

    第二條是 #641／#644 的紅線在這條 lane 上的形式：candidate 樹是 Manager 自己
    clone 的，不該有任何指向 job 工作樹的具名 ACL 條目，runbook 稽核 5b 的「零
    `setfacl`」因此仍然成立。
    """


def test_untracked_reports_do_not_count_as_drift(tmp_path: Path) -> None:
    """未追蹤檔刻意放行——canonical report 就是未追蹤檔，也正是交接的載體。

    ship 段那條「完全乾淨」（`work_bridge._require_pristine_ship_workspace`）在這裡
    會讓第二張 review 卡永遠開不了工，因此判準是 `--untracked-files=no`。
    """

    harness = _Harness(tmp_path)
    candidate = harness.land_a_candidate()
    harness.advance_to_review()
    first, _ = harness.dispatch()
    tree = Path(str(first["workflow_repo_root"]))

    manager._require_reviewer_candidate_workspace(
        tree, branch=str(first["branch"]), candidate=candidate
    )
    (tree / REPORT_REF).parent.mkdir(parents=True, exist_ok=True)
    (tree / REPORT_REF).write_text("# untracked report\n", encoding="utf-8")
    manager._require_reviewer_candidate_workspace(
        tree, branch=str(first["branch"]), candidate=candidate
    )

    # 追蹤檔被改動則不放行。
    (tree / TASKS_REF).write_text(TASKS_BASELINE, encoding="utf-8")
    with pytest.raises(ValueError, match="candidate workspace has tracked drift"):
        manager._require_reviewer_candidate_workspace(
            tree, branch=str(first["branch"]), candidate=candidate
        )
