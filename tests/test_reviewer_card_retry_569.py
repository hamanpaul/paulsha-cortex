"""#569：reviewer 卡的 `retry-verify` 只重置不重派，形成 #545 同型 catch-22。

現場（0815，run ``workflow-084f75e2178cf7547476`` verify 階段，與 #545 同一個
run）：

1. verification job ``wf-865ecb7f70-verification-484``（agy，#568 的權限剖面
   缺陷）exit 0，但 log 一行 JSON envelope 都沒有 → harvest 撞
   ``workflow terminal log has no JSON evidence`` → needs_human。
2. operator 下 ``retry-verify``，回應 ``verification-rerun-dispatched`` 但
   **``job: None``**——它只重置卡片與 facet，沒有在同一個 action 內派新 job，
   也沒有 supersede 舊 job。
3. 之後每一個 tick 的 resume 都重讀同一顆壞 job（dispatch 看到「這張卡已經有
   job」就把舊的原樣回傳），run 對 tick 實測隱形四小時。
4. 21:24 needs_human 原地回鍋，淨效果＝四小時＋回到原點。

根因與 #545 同型：卡片最新的終止 job 輸出損壞時，harvest 永遠贏過 dispatch。
builder 卡已由 PR #552 的 ``retry-card`` 解決（重置＋原子清 facet＋**同一個
action 內直接重派**＋舊 job 保留稽核），reviewer 卡當時沒有等價物。

本檔釘住 ``retry-card`` 對 reviewer persona 卡（verification／code-review／
adversarial-review）的放寬：同一組 exact WorkflowRun CAS ＋卡名定錨、同一組
evidence immutable 規則、同一條 facet 原子性補償，並且**新 job 的身分由
identity registry 在 dispatch 當下重新解析**——#568 的 reviewer fail-over 正依賴
這一點，複製舊 job 的 executor／model 等於把壞掉的身分再派一次。
"""

from __future__ import annotations

from dataclasses import replace
import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon, work_actions
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)

from diagnostic_fixtures import fixture_needs_human_reason


REPO = "acme/demo"
WORK_ID = "demo"
BUILDER_DOMAIN = "openai"
PLAN_REF = "docs/superpowers/plans/reviewer-retry.md"
PLAN_TEXT = "# reviewer retry plan\n"


def _manifest_steps():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "reviewer retry", change="reviewer-retry")
    assert result.workflow_manifest is not None
    return result.workflow_manifest.steps


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
                        "repo": REPO,
                        "work_id": WORK_ID,
                        "mapped_issues": [12],
                        "mapped_prs": [],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _steps_stopped_at(card: str):
    """``card`` 之前的所有卡全 passed（build 卡帶 builder domain），它自己 pending。

    build 卡的 ``domain`` 必須落地：reviewer 的 independence domain 過濾讀的正是
    「已 passed 的 build step 的 domain」（見
    ``manager._identity_candidates_for_persona``）。
    """

    steps = _manifest_steps()
    order = [step.card for step in steps]
    stop = order.index(card)
    passed = set(order[:stop])
    return tuple(
        replace(
            step,
            gate_result="passed",
            executor="codex" if step.phase == "build" else step.executor,
            model="gpt-primary" if step.phase == "build" else step.model,
            domain=BUILDER_DOMAIN if step.phase == "build" else step.domain,
        )
        if step.card in passed
        else step
        for step in steps
    )


def _init_candidate_repo(repo: Path) -> str:
    """真實 git repo：reviewer sandbox 是 `git clone` + `checkout <candidate>`。

    #650 之後這棵樹是 **run.workspace_root（來源樹）**，不再是 builder 的工作區：
    candidate 由 `_harvest_build_candidate()` 搬進來源樹，verify／review 卡的
    candidate 樹是 Manager 自己從那裡 clone 出來的。
    """

    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "canary@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Canary"], check=True
    )
    (repo / PLAN_REF).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REF).write_text(PLAN_TEXT, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "candidate"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _planning_authority() -> tuple[PlanningArtifactAuthority, ...]:
    import hashlib

    return (
        PlanningArtifactAuthority(
            ref=PLAN_REF,
            kind="plan",
            work_id=WORK_ID,
            baseline_sha256=hashlib.sha256(PLAN_TEXT.encode()).hexdigest(),
        ),
    )


def _stuck_reviewer_run(
    tmp_path: Path,
    *,
    card: str = "verification",
    phase: str = "verify",
    reviewer_executor: str = "agy",
    reviewer_model: str = "gemini-3.7-flash-high",
    with_git: bool = False,
):
    """重建現場：needs_human 的 reviewer phase run，停在一張 reviewer 卡，該卡
    已有一顆乾淨終止（exit 0）但 log 無 JSON envelope、evidence 未綁定的 job。"""

    workspace = tmp_path / "workspace"
    builder_worktree = tmp_path / "builder-worktree"
    builder_worktree.mkdir(parents=True, exist_ok=True)
    if with_git:
        # #650：candidate 在**來源樹**裡（harvest 之後的實況），reviewer 的 candidate
        # 樹由 Manager 從這裡 clone。builder 的工作區留成一個普通目錄——本票的重點
        # 就是 verify／review 派工不再讀它。
        candidate = _init_candidate_repo(workspace)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / PLAN_REF).parent.mkdir(parents=True, exist_ok=True)
        (workspace / PLAN_REF).write_text(PLAN_TEXT, encoding="utf-8")
        candidate = "d" * 40

    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo=REPO, work_id=WORK_ID, snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=work_actions._expected_claim_key(authority),
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase=phase,
        steps=_steps_stopped_at(card),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=candidate,
        attempts={"build": 1, phase: 1},
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(
            "workflow-terminal-log-has-no-json-evidence",
            "verification job exit 0 但 log 無 JSON envelope（#568 權限剖面缺陷）",
        ),
        gate_status="failed",
        planning_authority=_planning_authority(),
    )
    # builder 的 candidate 產出：reviewer dispatch 以它的 worktree 為 candidate root。
    builder = registry.create_job(
        task="wf-subagent-build",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(builder_worktree),
        dispatch_head="b" * 40,
        subject_head=candidate,
        executor="codex",
        model_id="gpt-primary",
        independence_domain=BUILDER_DOMAIN,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    # 壞掉的 reviewer job：exit 0，但 log 沒有任何 JSON envelope（現場實測）。
    log = tmp_path / "logs" / "workflow" / f"{card}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        'jetski: no output produced — a tool required the "unsandboxed" permission\n',
        encoding="utf-8",
    )
    stuck = registry.create_job(
        task=f"wf-{card}",
        persona="reviewer",
        kind="review",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path / "reviewer-sandbox"),
        subject_head=candidate,
        executor=reviewer_executor,
        model_id=reviewer_model,
        independence_domain="google",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=card,
        workflow_phase=phase,
        workflow_builder_job_id=str(builder["job_id"]),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(
        stuck["job_id"],
        executor=reviewer_executor,
        model_id=reviewer_model,
        log_path=str(log),
    )
    registry.update_headless_result(stuck["job_id"], status="exited", exit_code=0)
    return snapshot, registry, run, stuck["job_id"]


def _retry_card(tmp_path: Path, snapshot: Path, registry: JobRegistry, **overrides):
    args = {
        "action": "retry-card",
        "repo": REPO,
        "work_id": WORK_ID,
        "issue": 12,
        "actor": "operator",
    }
    args.update(overrides)
    return work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )


# ==========================================================================
# 段 1：reviewer 卡重派（work action 層）
# ==========================================================================


def test_retry_card_reopens_the_stuck_verification_card(tmp_path: Path) -> None:
    """RED（修復前）：`retry-card` 明文 `requires a builder card`，reviewer 卡被拒。"""

    snapshot, registry, run, job_id = _stuck_reviewer_run(tmp_path)
    before = registry.get_job(job_id)

    result = _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )["result"]

    assert result["action"] == "retry-card"
    assert result["reason"] == "reviewer-card-redispatched"
    assert result["card_id"] == "verification"
    assert result["superseded_job_ids"] == [job_id]

    persisted = registry.get_workflow_run(run.run_id)
    # facet 清除 = 這張卡重新可派（真正的重派由 daemon 在同一個 action 內完成）。
    assert "needs_human" not in persisted.facets
    assert persisted.needs_human_reason is None
    assert persisted.gate_status == "running"
    assert persisted.current_phase == "verify"
    assert persisted.attempts["verify"] == 2
    assert persisted.attempts["build"] == 1  # build phase 一步都不動
    target = next(step for step in persisted.steps if step.card == "verification")
    assert target.gate_result == "pending"
    # 身分解析結果被清掉，下一次 dispatch 重新解析（#568 的 fail-over 依賴這點）。
    assert (target.executor, target.model, target.domain) == (None, None, None)
    # 舊 job 是稽核紀錄，一個位元組都不動。
    assert registry.get_job(job_id) == before
    # candidate 不變的 reviewer 重跑不是模型修復。
    assert persisted.retry_classification == "orchestrator_retry"


def test_retry_card_preserves_the_reviewer_card_contract(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    original = next(step for step in run.steps if step.card == "verification")

    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )

    persisted = registry.get_workflow_run(run.run_id)
    target = next(step for step in persisted.steps if step.card == "verification")
    assert target.action == original.action
    assert target.inputs == original.inputs
    assert target.outputs == original.outputs
    assert target.persona == "reviewer"
    # 其他卡片的 gate_result 一張都沒被動到。
    assert [(step.card, step.gate_result) for step in persisted.steps] == [
        (step.card, "pending" if step.card == "verification" else step.gate_result)
        for step in run.steps
    ]


def test_retry_card_reopens_a_stuck_review_phase_card(tmp_path: Path) -> None:
    """review phase 的 code-review 卡走同一條路，attempts 記在 review 上。"""

    snapshot, registry, run, job_id = _stuck_reviewer_run(
        tmp_path, card="code-review", phase="review"
    )

    result = _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="code-review"
    )["result"]

    assert result["reason"] == "reviewer-card-redispatched"
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.current_phase == "review"
    assert persisted.attempts["review"] == 2
    assert "needs_human" not in persisted.facets
    assert registry.get_job(job_id)["status"] == "exited"
    # review 重跑分類比照 retry-review。
    assert persisted.retry_classification == "review_handoff_failure"


def test_retry_card_refuses_a_reviewer_card_with_accepted_evidence(
    tmp_path: Path,
) -> None:
    """已採信的 evidence immutable：不得以「重派」名義覆寫。"""

    snapshot, registry, run, job_id = _stuck_reviewer_run(tmp_path)
    registry.bind_workflow_evidence(
        job_id,
        locator={
            "kind": "workflow-verification-result",
            "path": "evidence/verification.json",
            "hash": "e" * 64,
        },
        subject_head=run.candidate_head,
    )

    with pytest.raises(RuntimeError, match="accepted evidence"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="verification",
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets
    assert registry.get_job(job_id)["workflow_evidence"]["hash"] == "e" * 64


def test_retry_card_ignores_evidence_bound_to_a_superseded_candidate(
    tmp_path: Path,
) -> None:
    """evidence 的 immutable 判斷以**現在這個 candidate** 為錨。

    上一代 candidate 的 reviewer job（retry-build 換過 candidate 之前的紀錄）帶著
    evidence 是正常的稽核事實；把它算進來會讓「換過 candidate 之後 reviewer 卡再
    次卡住」變成無解——那就是再造一次本 issue 的 catch-22。
    """

    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    stale = registry.create_job(
        task="wf-verification-old",
        persona="reviewer",
        kind="review",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path / "old-sandbox"),
        subject_head="a" * 40,  # 上一代 candidate
        workflow_run_id=run.run_id,
        workflow_card="verification",
        workflow_phase="verify",
    )
    registry.update_headless_result(stale["job_id"], status="exited", exit_code=0)
    registry.bind_workflow_evidence(
        stale["job_id"],
        locator={
            "kind": "workflow-verification-result",
            "path": "evidence/old.json",
            "hash": "f" * 64,
        },
        subject_head="a" * 40,
    )

    result = _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )["result"]

    assert result["reason"] == "reviewer-card-redispatched"
    # 舊 candidate 的 evidence 原樣保留。
    assert registry.get_job(stale["job_id"])["workflow_evidence"]["hash"] == "f" * 64


def test_retry_card_requires_exact_run_cas_for_reviewer_cards(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    before = registry.get_workflow_run(run.run_id).to_dict()

    with pytest.raises(RuntimeError, match="CAS mismatch"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id="workflow-" + "0" * 20,
            card="verification",
        )
    assert registry.get_workflow_run(run.run_id).to_dict() == before


def test_retry_card_rejects_a_reviewer_card_that_is_not_the_next_one(
    tmp_path: Path,
) -> None:
    """指名 review phase 的第二張卡 = 想跳過 code-review；fail closed。"""

    snapshot, registry, run, _job_id = _stuck_reviewer_run(
        tmp_path, card="code-review", phase="review"
    )

    with pytest.raises(RuntimeError, match="expected card mismatch"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="adversarial-review",
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_card_requires_needs_human_for_reviewer_cards(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=())

    with pytest.raises(RuntimeError, match="requires needs_human workflow"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="verification",
        )


def test_retry_card_requires_a_terminal_reviewer_job(tmp_path: Path) -> None:
    """從未派過的卡屬 `resume` 的職責，不是本動作的。"""

    snapshot, registry, run, job_id = _stuck_reviewer_run(tmp_path)
    registry._jobs[:] = [job for job in registry._jobs if job["job_id"] != job_id]
    registry._persist()

    with pytest.raises(RuntimeError, match="terminal job for the card"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="verification",
        )


def test_retry_card_refuses_an_active_reviewer_job(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    registry.create_job(
        task="wf-verification-inflight",
        persona="reviewer",
        kind="review",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path / "inflight"),
        subject_head=run.candidate_head,
        workflow_run_id=run.run_id,
        workflow_card="verification",
        workflow_phase="verify",
    )

    with pytest.raises(RuntimeError, match="terminal job for the card"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="verification",
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_card_reset_refuses_the_reviewer_card_after_state_drift(
    tmp_path: Path,
) -> None:
    """registry 層的原子重驗：work action 通過後狀態若漂移，reset 仍 fail closed。"""

    _snapshot_path, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    registry._manager_update_workflow_run(
        run.run_id,
        steps=tuple(
            replace(step, gate_result="passed")
            if step.card == "verification"
            else step
            for step in run.steps
        ),
    )

    with pytest.raises(ValueError, match="earliest un-accepted reviewer card"):
        registry._manager_reset_workflow_for_retry_card(
            run.run_id, expected_run_id=run.run_id, card="verification"
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_verify_call_site_still_works(tmp_path: Path) -> None:
    """既有呼叫端不破：`retry-verify` 的 CAS 與 admission 一字未改。

    （`docs/superpowers/specs/fix-repair-commit-recovery-spec.md` R4 明文鎖定。）
    """

    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)

    result = work_actions.execute_work_action(
        args={
            "action": "retry-verify",
            "repo": REPO,
            "work_id": WORK_ID,
            "issue": 12,
            "actor": "operator",
            "expected_candidate": run.candidate_head,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )["result"]

    assert result["action"] == "retry-verify"
    assert result["reason"] == "verification-rerun-dispatched"
    assert "needs_human" not in registry.get_workflow_run(run.run_id).facets


# ==========================================================================
# 段 2：dispatch 真的產生新 job，身分重新解析，sandbox 原子回收
# ==========================================================================


class _ReviewLauncher:
    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink
        self.executor = "claude"
        self.model_id = "sonnet-primary"

    def as_review_only(self, *, terminal_kind: str):
        self._terminal_kind = terminal_kind
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._sink.append((prompt, worktree))
        return LaunchHandle(
            executor=self.executor,
            model_id=self.model_id,
            session_name=slice_id,
            pid=101,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _reviewer_identities() -> IdentityRegistry:
    """registry 當下只有 claude/anthropic 具 review capability。

    舊 job 記的是 agy/gemini（#568 的失效身分）——新 job 必須解析成 claude，
    證明身分是**重新解析**而不是從舊 job 複製。
    """

    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": BUILDER_DOMAIN,
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


def _dispatch(tmp_path: Path, registry: JobRegistry, run_id: str, sink: list):
    return manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=registry.get_workflow_run(run_id),
        identities=_reviewer_identities(),
        launcher_factory=lambda _identity: _ReviewLauncher(sink),
        coordinator_root=tmp_path / "coordinator",
        force_new_card=True,
    )


def test_forced_retry_dispatches_a_new_reviewer_job_with_a_re_resolved_identity(
    tmp_path: Path,
) -> None:
    snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )
    sink: list[tuple[str, str]] = []

    replacement = _dispatch(tmp_path, registry, run.run_id, sink)

    assert replacement is not None
    assert replacement["job_id"] != old_job_id
    assert replacement["workflow_card"] == "verification"
    assert replacement["workflow_phase"] == "verify"
    assert replacement["persona"] == "reviewer"
    # 身分重新解析：舊 job 的 agy/gemini 沒有被複製。
    assert (replacement["executor"], replacement["model_id"]) == (
        "claude",
        "sonnet-primary",
    )
    assert replacement["independence_domain"] == "anthropic"
    assert registry.get_job(old_job_id)["executor"] == "agy"
    # 舊 job 原樣保留供稽核。
    assert registry.get_job(old_job_id)["status"] == "exited"
    assert registry.get_job(old_job_id)["workflow_evidence"] is None
    # prompt 走既有的 `_workflow_job_prompt`，沒有第二條組裝路徑。
    assert len(sink) == 1
    assert "verification" in sink[0][0]


def test_forced_retry_recycles_the_superseded_reviewer_sandbox(tmp_path: Path) -> None:
    """sandbox 目錄名是 `sha256(run_id:card:candidate)`——重派同一張卡＋同一個
    candidate 必然撞名，舊 sandbox 沒回收就永遠派不出去。"""

    snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    coordinator_root = tmp_path / "coordinator"
    # 讓舊 job 的 worktree 指向「真的存在」的 sandbox 路徑（現場即如此）。
    old_sandbox = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=tmp_path / "builder-worktree",
    ) / __import__("hashlib").sha256(
        f"{run.run_id}:verification:{run.candidate_head}".encode()
    ).hexdigest()[:32]
    old_sandbox.mkdir(parents=True)
    (old_sandbox / "stale-marker").write_text("x", encoding="utf-8")
    from paulsha_cortex.coordinator import planning_runtime

    registry._find_job(old_job_id).update(
        {
            "worktree": str(old_sandbox),
            "workflow_repo_root": str((tmp_path / "builder-worktree").resolve()),
            "workflow_input_root": str(old_sandbox),
            "workflow_sandbox_hash": planning_runtime._tree_snapshot(
                (tmp_path / "builder-worktree").resolve()
            ),
        }
    )
    registry._persist()

    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )
    replacement = _dispatch(tmp_path, registry, run.run_id, [])

    assert replacement is not None
    # 同一個路徑被回收後重建：陳舊內容不得留給新 reviewer 看。
    assert not (old_sandbox / "stale-marker").exists()
    assert Path(replacement["worktree"]).is_dir()


def test_forced_retry_fails_closed_when_the_reviewer_modified_the_candidate(
    tmp_path: Path,
) -> None:
    """重派不得成為「蓋掉 reviewer 動過 candidate」這個事實的名義。"""

    snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    coordinator_root = tmp_path / "coordinator"
    old_sandbox = manager._reviewer_sandbox_parent(
        coordinator_root=coordinator_root,
        candidate_root=tmp_path / "builder-worktree",
    ) / __import__("hashlib").sha256(
        f"{run.run_id}:verification:{run.candidate_head}".encode()
    ).hexdigest()[:32]
    old_sandbox.mkdir(parents=True)
    registry._find_job(old_job_id).update(
        {
            "worktree": str(old_sandbox),
            "workflow_repo_root": str((tmp_path / "builder-worktree").resolve()),
            "workflow_input_root": str(old_sandbox),
            # candidate checkout 的 baseline 對不上 = reviewer 動過 candidate。
            "workflow_sandbox_hash": "0" * 64,
        }
    )
    registry._persist()

    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="verification"
    )

    with pytest.raises(ValueError, match="modified Candidate checkout"):
        _dispatch(tmp_path, registry, run.run_id, [])


def test_force_new_card_rejects_a_card_that_is_neither_builder_nor_reviewer(
    tmp_path: Path,
) -> None:
    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    # plan phase 的 planner 卡：強制重派不受理（那是 recover-planning 的職責）。
    registry._manager_create_workflow_run(
        work_id="other",
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path / "workspace"),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest_steps(),
        gate_status="running",
    )
    planner_run = [
        item for item in registry.list_workflow_runs() if item.work_id == "other"
    ][0]

    with pytest.raises(ValueError, match="builder or reviewer card"):
        manager.dispatch_workflow_card(
            type("D", (), {"_registry": registry, "_git_runner": None})(),
            run=planner_run,
            identities=_reviewer_identities(),
            launcher_factory=lambda _identity: _ReviewLauncher([]),
            coordinator_root=tmp_path / "coordinator",
            force_new_card=True,
        )
    assert snapshot.is_file()


# ==========================================================================
# 段 3：daemon wiring 與 facet 原子性
#
# #569 的實測中間態就是「facet 清了、job 沒派出去」——它撐了四個小時。
# ==========================================================================


def _daemon_executor(tmp_path: Path, registry: JobRegistry, run):
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    return manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "retry-card", "run": run.to_dict()},
        },
    )


def _retry_card_request(run, card: str = "verification"):
    return build_request(
        req_type="work-action",
        args={
            "action": "retry-card",
            "repo": run.repo,
            "work_id": run.work_id,
            "expected_run_id": run.run_id,
            "card": card,
        },
        requested_by="operator",
    )


def test_public_work_retry_card_forces_one_new_manager_dispatched_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    calls: list[bool] = []

    def forced_dispatch(*args, **kwargs):
        calls.append(kwargs.get("force_new_card"))
        return {"job_id": "replacement-reviewer"}

    monkeypatch.setattr(manager, "dispatch_workflow_card", forced_dispatch)
    executor = _daemon_executor(tmp_path, registry, run)

    result = executor(_retry_card_request(run))

    assert calls == [True]
    assert result["result"]["job_id"] == "replacement-reviewer"


def test_public_work_retry_card_restores_needs_human_when_reviewer_dispatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不得出現「facet 清了但沒派出去」的中間態——#569 的四小時就是它。"""

    _snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=(), gate_status="running")
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reviewer down")),
    )
    executor = _daemon_executor(tmp_path, registry, run)

    with pytest.raises(RuntimeError, match="reviewer down"):
        executor(_retry_card_request(run))

    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    # 診斷 invariant（#527）：補回 facet 的同時必須落結構化理由。
    assert persisted.needs_human_reason["reason"] == "forced-card-retry-failed"
    assert persisted.needs_human_reason["context"]["phase"] == "verify"


def test_public_work_retry_card_fails_when_reviewer_dispatch_produces_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=(), gate_status="running")
    monkeypatch.setattr(manager, "dispatch_workflow_card", lambda *a, **k: None)
    executor = _daemon_executor(tmp_path, registry, run)

    with pytest.raises(RuntimeError, match="retry-card produced no reviewer Job"):
        executor(_retry_card_request(run))

    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_reviewer_retry_never_leaves_a_cleared_facet_without_a_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子性總樁：無論 dispatch 以哪一種方式失敗，run 都不得停在
    「ongoing／無 needs_human／無 active job」——那正是對 tick 隱形的狀態。"""

    for failure in (
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("launcher down")),
        lambda *a, **k: None,
    ):
        registry_dir = tmp_path / f"case-{id(failure)}"
        registry_dir.mkdir()
        _snapshot, registry, run, _job_id = _stuck_reviewer_run(registry_dir)
        monkeypatch.setattr(manager, "dispatch_workflow_card", failure)
        executor = _daemon_executor(registry_dir, registry, run)

        with pytest.raises(RuntimeError):
            executor(_retry_card_request(run))

        persisted = registry.get_workflow_run(run.run_id)
        active = [
            job
            for job in registry.list_jobs()
            if job.get("workflow_run_id") == run.run_id
            and job.get("status") in {"dispatched", "running"}
        ]
        assert persisted.status == "ongoing"
        assert not active
        assert "needs_human" in persisted.facets


# ==========================================================================
# 段 4：曝光面——resume／status 必須說得出 retry-card
# ==========================================================================


def test_phase_recovery_actions_expose_retry_card_for_a_stuck_reviewer_card(
    tmp_path: Path,
) -> None:
    _snapshot, registry, run, job_id = _stuck_reviewer_run(tmp_path)

    exposed = work_actions._phase_recovery_actions(
        registry.get_workflow_run(run.run_id), registry
    )

    assert "retry-card" in exposed

    # 已採信的卡不得再宣告 retry-card——宣告一個保證失敗的動作比不宣告更糟。
    registry.bind_workflow_evidence(
        job_id,
        locator={
            "kind": "workflow-verification-result",
            "path": "evidence/verification.json",
            "hash": "e" * 64,
        },
        subject_head=run.candidate_head,
    )
    assert "retry-card" not in work_actions._phase_recovery_actions(
        registry.get_workflow_run(run.run_id), registry
    )


def test_resume_response_exposes_retry_card_instead_of_only_abandon(
    tmp_path: Path,
) -> None:
    """#569 的 operator 之所以改用 retry-verify，是因為 resume 只說得出 abandon。"""

    snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": REPO, "work_id": WORK_ID, "issue": 12},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )["result"]

    assert result["action"] == "needs_human"
    assert "abandon" in result["next_actions"]
    assert "retry-card" in result["next_actions"]


def test_status_attention_entry_exposes_retry_card_for_a_stuck_reviewer_card(
    tmp_path: Path,
) -> None:
    """#527 的 attention 條目（`cortex status`）同步看得到重派出口。"""

    _snapshot, registry, run, _job_id = _stuck_reviewer_run(tmp_path)

    entry = manager.workflow_status_entry(
        registry, registry.get_workflow_run(run.run_id)
    )

    assert entry["kind"] == "workflow_run"
    assert "retry-card" in entry["next_actions"]
