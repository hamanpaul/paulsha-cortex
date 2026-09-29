"""#1141：Manager ship lane 自動 merge 交付 PR 後，run 不得因 Monitor 反映 merge 而失聯。

現場（run ``workflow-08ca9b35fc7955942a21``，PR #1140）：ship lane 開 PR、自動 merge、
``Closes #N`` 關票後，Monitor 把該 PR 以 ``state:closed`` 關聯進 WorkAuthority、issue 也
轉成 ``state:closed``。下一次 periodic tick 續跑同一 run 時，``_canonical_workflow_run``
以 claim-era 比對找不到任何 active run，擲出
``RuntimeError: delivery WorkflowRun does not match current WorkAuthority``，run 停在
review／``resume-workflow-failed``，``next_actions`` 只剩一個必被拒的 ``abandon``。

本檔以既有 ship lane 的正式函式（``_ship_action``、``_authority_with_manager_pr``、
``_rebase_delivery_journal_authority``、``manager.resume_workflow_run``）重現整段：

1. claim 時 authority 只有 open issue 與 todo（沒有 PR）；
2. Manager 建 PR（run.pr_refs），Monitor 看見 open PR，ship lane 採信 Copilot 並 merge；
3. Monitor 反映 merge：PR 轉 terminal、issue 轉 closed；
4. tick 以 ``resume_workflow_run`` 續跑同一 run → 必須走 completion（CompletionRecord／done）。

以及兩條 fail-closed：authority 出現**其他** PR、或 PR 尚未由本 run merge。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import (
    claim as claim_module,
    engineering_outcome,
    manager,
    work_actions,
    work_bridge,
)
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    MergeStatus,
)
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef


HEAD = "a" * 40
# git tree 是 40-hex SHA-1；live 的 merge authorization 就是這個形狀。
TREE = "b" * 40
MERGE_COMMIT = "c" * 40
REPO = "acme/demo"
WORK_ID = "demo"
ISSUE = 12
PR = 8
FOREIGN_PR = 9
TODO_PATH = "docs/todo.md"
TODO_REVISION = f"todo:{REPO}:{TODO_PATH}@identity:{TODO_PATH}"


def _issue_revision(state: str) -> str:
    return f"github_issue:{REPO}#{ISSUE}@identity:{REPO}#{ISSUE};state:{state}"


def _pr_revision(number: int, state: str) -> str:
    return f"github_pr:{REPO}#{number}@identity:{REPO}#{number};state:{state}"


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{REPO}.git"],
            check=True,
        )


def _write_snapshot(
    path: Path,
    *,
    prs: tuple[int, ...],
    source_revisions: tuple[str, ...],
    provider_revision: str,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": REPO,
                        "work_id": WORK_ID,
                        "mapped_issues": [ISSUE],
                        "mapped_prs": list(prs),
                        "mapped_openspec": [],
                        "mapped_todo_paths": [TODO_PATH],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _claim_era_snapshot(path: Path) -> Path:
    return _write_snapshot(
        path,
        prs=(),
        source_revisions=(_issue_revision("open"), TODO_REVISION),
        provider_revision="gh-1",
    )


def _open_pr_snapshot(path: Path) -> Path:
    """Monitor 已看見 Manager 建立的 open PR（merge 前）。"""

    return _write_snapshot(
        path,
        prs=(PR,),
        source_revisions=(_issue_revision("open"), _pr_revision(PR, "open"), TODO_REVISION),
        provider_revision="gh-2",
    )


def _post_merge_snapshot(
    path: Path,
    *,
    pr_state: str = "closed",
    extra_prs: tuple[int, ...] = (),
) -> Path:
    """Monitor 反映 merge：PR 轉 terminal（live 是 ``state:closed``）、``Closes`` 關掉 issue。"""

    return _write_snapshot(
        path,
        prs=(PR, *extra_prs),
        source_revisions=(
            _issue_revision("closed"),
            _pr_revision(PR, pr_state),
            *(_pr_revision(number, "open") for number in extra_prs),
            TODO_REVISION,
        ),
        provider_revision="gh-3",
    )


class _GitHub:
    def __init__(self) -> None:
        self.reviews: tuple[CopilotReview, ...] = (
            CopilotReview(
                review_id=42,
                commit_id=HEAD,
                state="COMMENTED",
                body="LGTM",
                author=COPILOT_REVIEWER_LOGIN,
                submitted_at_epoch=1000.0,
            ),
        )
        self.merged = False

    def ensure_pr_metadata(self, **kwargs) -> None:
        pass

    def request_copilot(self, **kwargs) -> None:
        pass

    def fetch_delivery_facts(self, **kwargs) -> DeliveryFacts:
        return DeliveryFacts(
            head=HEAD,
            mergeable=True,
            mergeable_state="clean",
            checks=(GitHubCheck("pytest", "completed", "success"),),
            copilot_reviews=self.reviews,
            review_threads=(),
            closing_issues=(ISSUE,),
            active_openspec_absent=True,
            archive_present=True,
            openspec_required=False,
        )

    def fetch_merge_status(self, **kwargs) -> MergeStatus:
        return MergeStatus(
            merged=self.merged,
            pr_head=HEAD,
            merge_commit=MERGE_COMMIT if self.merged else None,
        )


def _ship_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, copilot_reviewed: bool = True):
    _init_repo(tmp_path)
    snapshot = _claim_era_snapshot(tmp_path / "snapshot.json")
    # production 的 delivery journal 與 registry 同在 coordinator root。
    journal = tmp_path / "delivery-journal.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": REPO, "work_id": WORK_ID},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=journal,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    claim_era_digest = registry.get_workflow_run(run_id).source_revision

    def _advance_step(step):
        if step.phase == "build":
            return replace(step, domain="openai")
        if step.phase in {"verify", "review"}:
            return replace(step, domain="google", gate_result="passed")
        if step.phase == "ship":
            return replace(step, gate_result="passed")
        return step

    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        current_phase="review",
        steps=tuple(_advance_step(step) for step in registry.get_workflow_run(run_id).steps),
        gate_status="running",
        gate_refs=(GateEvidenceRef(kind="foreign-review", ref="evidence-ref-1"),),
        candidate_head=HEAD,
        verified_head=HEAD,
        # ship lane 建 PR 時寫入 run.pr_refs（work_bridge 的 pr-created 分支）。
        pr_refs=(f"{REPO}#{PR}",),
    )

    github = _GitHub()
    if not copilot_reviewed:
        github.reviews = ()
    closure_calls: list[dict[str, object]] = []

    class _Orchestrator:
        def __init__(self, *, github, now):
            self._github = github

        def merge_if_ready(self, **kwargs):
            self._github.merged = True
            return SimpleNamespace(
                expected_head=kwargs["expected_head"],
                expected_tree_hash=kwargs["expected_tree_hash"],
            )

        def verify_remote_closure(self, **kwargs):
            closure_calls.append(kwargs)
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit=MERGE_COMMIT),
                completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", lambda **kwargs: github)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", _Orchestrator)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=HEAD,
            tree_hash=TREE,
        ),
    )
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    monkeypatch.setattr(
        work_actions, "_validate_foreign_review", lambda *args, **kwargs: foreign_normalized
    )
    foreign = tmp_path / "foreign-review.json"
    foreign.write_text(json.dumps(foreign_normalized), encoding="utf-8")
    metadata = tmp_path / "pr.json"
    metadata.write_text(
        json.dumps(
            {"title": "fix(work): 修正工作流程", "body": f"Closes #{ISSUE}", "labels": ["enhancement"]}
        ),
        encoding="utf-8",
    )
    completion = tmp_path / "completion.json"
    completion.write_text("{}", encoding="utf-8")
    base_args = {
        "repo_root": str(tmp_path),
        "pr_number": PR,
        "change": None,
        "todo_paths": [TODO_PATH],
        "pr_metadata_path": str(metadata),
        "foreign_review_path": str(foreign),
        "foreign_review_hash": work_actions.verification.canonical_json_hash(foreign_normalized),
    }

    def ship(*, now: float, closure: bool = False) -> dict[str, object]:
        """與 ``build_production_ship_validator`` 在呼叫 ``_ship_action`` 前做的事逐步相同。"""

        authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=snapshot)
        if authority.mapped_prs not in {(), (PR,)}:
            raise RuntimeError("workflow PR differs from current WorkAuthority")
        authority = work_bridge._authority_with_manager_pr(authority, PR)
        rows = (
            json.loads(journal.read_text(encoding="utf-8"))["runs"] if journal.exists() else {}
        )
        if run_id in rows:
            work_bridge._rebase_delivery_journal_authority(
                state_root=tmp_path,
                run=registry.get_workflow_run(run_id),
                authority=authority,
            )
        args = dict(base_args)
        if closure:
            args["completion_record_path"] = str(completion)
        return work_actions._ship_action(
            args=args,
            authority=authority,
            runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
            now=lambda: now,
            state_path=journal,
            workflow_registry=registry,
        )

    def ship_validator(*, run, candidate):
        action = ship(now=3000.0, closure=True)
        status = work_bridge._delivery_adapter_status(action.get("action"))
        result: dict[str, object] = {
            "trusted": True,
            "status": status,
            "head": candidate,
            "commit_id": candidate,
            "ref": "delivery-adapter.json",
            "hash": "f" * 64,
        }
        if status == "passed":
            record = action["completion_record"]
            result["completion"] = {
                "record_path": record["path"],
                "record_hash": record["hash"],
                "record_revision": candidate,
                "source_revisions": {f"github_issue:{REPO}#{ISSUE}": "closed"},
                "pr_candidate": candidate,
                "merge_revision": action["merge_commit"],
            }
        return result

    monkeypatch.setattr(manager, "_validated_ship_steps", lambda _registry, *, run, **_: run.steps)
    return SimpleNamespace(
        tmp_path=tmp_path,
        snapshot=snapshot,
        journal=journal,
        registry=registry,
        run_id=run_id,
        claim_era_digest=claim_era_digest,
        github=github,
        closure_calls=closure_calls,
        ship=ship,
        ship_validator=ship_validator,
    )


def _merge_via_ship_lane(lane) -> None:
    _open_pr_snapshot(lane.snapshot)
    merged = lane.ship(now=2000.0)
    assert merged["action"] == "merged-awaiting-closure"
    ship_state = json.loads(lane.journal.read_text(encoding="utf-8"))["runs"][lane.run_id]["ship"]
    assert ship_state["phase"] == "merged"
    assert ship_state["pr_number"] == PR
    assert ship_state["merge_commit"] == MERGE_COMMIT


def _tick_resume(lane):
    return manager.resume_workflow_run(
        SimpleNamespace(_registry=lane.registry, _git_runner=None),
        run_id=lane.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=lane.tmp_path,
        ship_validator=lane.ship_validator,
    )


# ---------------------------------------------------------------------------
# 1. 重現：merge 後 Monitor 反映 PR／issue 終局，tick 續跑必須走 completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pr_state", ["closed", "merged"])
def test_tick_resume_completes_run_after_ship_lane_merge_is_reflected_in_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str
) -> None:
    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    _post_merge_snapshot(lane.snapshot, pr_state=pr_state)
    post_merge = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=lane.snapshot)
    # 前提：authority 已前進到 claim-era 對不上的形狀（issue closed、PR terminal）。
    assert post_merge.mapped_prs == (PR,)
    assert work_actions.work_authority_digest(post_merge) != lane.claim_era_digest
    assert not claim_module.authority_matches_claim_era(
        post_merge, lane.registry.get_workflow_run(lane.run_id)
    )

    result = _tick_resume(lane)

    run = lane.registry.get_workflow_run(lane.run_id)
    assert result["current_phase"] == "ship"
    assert run.status == "done"
    assert run.completion_record_path == "/evidence/completion.json"
    assert run.completion_record_hash == "d" * 64
    assert run.merge_revision == MERGE_COMMIT
    assert "needs_human" not in run.facets
    ship_state = json.loads(lane.journal.read_text(encoding="utf-8"))["runs"][lane.run_id]["ship"]
    assert ship_state["phase"] == "done"
    # remote closure 以 merge 後的 current authority 驗證，不是 claim-era 快照。
    assert len(lane.closure_calls) == 1
    closure_authority = lane.closure_calls[0]["authority"]
    assert closure_authority.mapped_prs == (PR,)
    assert _issue_revision("closed") in closure_authority.source_revisions
    outcomes = list(
        engineering_outcome.OutcomeStore(
            engineering_outcome.outcome_store_path(lane.journal, repo=REPO)
        ).list_outcomes(repo=REPO, work_id=WORK_ID)
    )
    assert [record["outcome"] for record in outcomes] == ["shipped"]


def test_merged_delivery_journal_bound_accepts_git_sha1_tree_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ship lane 寫下的 merge authorization 帶 40-hex git tree；不得因此判定為未交付。"""

    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    ship_state = json.loads(lane.journal.read_text(encoding="utf-8"))["runs"][lane.run_id]["ship"]
    assert ship_state["merge_authorization"]["payload"]["tree_hash"] == TREE

    assert manager._merged_delivery_journal_bound(
        lane.registry.get_workflow_run(lane.run_id), journal_path=lane.journal
    )


# ---------------------------------------------------------------------------
# 2. fail-closed：其他 PR、或尚未由本 run merge 的 PR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mapped_prs", [(PR, FOREIGN_PR), (FOREIGN_PR,)])
def test_post_merge_foreign_pr_in_authority_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped_prs: tuple[int, ...]
) -> None:
    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    _write_snapshot(
        lane.snapshot,
        prs=mapped_prs,
        source_revisions=(
            _issue_revision("closed"),
            *(
                _pr_revision(number, "closed" if number == PR else "open")
                for number in mapped_prs
            ),
            TODO_REVISION,
        ),
        provider_revision="gh-3",
    )
    authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=lane.snapshot)

    with pytest.raises(RuntimeError, match="delivery WorkflowRun does not match current WorkAuthority"):
        work_actions._canonical_workflow_run(
            workflow_registry=lane.registry,
            authority=authority,
            merged_delivery_journal=lane.journal,
        )
    with pytest.raises(RuntimeError):
        lane.ship(now=3000.0, closure=True)
    run = lane.registry.get_workflow_run(lane.run_id)
    assert run.status == "ongoing"
    assert run.completion_record_path is None


def test_unmerged_pr_is_not_treated_as_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR 在 ship lane merge 之前就轉 closed（人工關閉、未 merge），不得當成交付。"""

    lane = _ship_lane(tmp_path, monkeypatch, copilot_reviewed=False)
    _open_pr_snapshot(lane.snapshot)
    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    ship_state = json.loads(lane.journal.read_text(encoding="utf-8"))["runs"][lane.run_id]["ship"]
    assert ship_state["phase"] == "review-requested"
    _post_merge_snapshot(lane.snapshot)
    authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=lane.snapshot)
    run = lane.registry.get_workflow_run(lane.run_id)

    assert not manager._merged_delivery_journal_bound(run, journal_path=lane.journal)
    with pytest.raises(RuntimeError, match="delivery WorkflowRun does not match current WorkAuthority"):
        work_actions._canonical_workflow_run(
            workflow_registry=lane.registry,
            authority=authority,
            merged_delivery_journal=lane.journal,
        )
    with pytest.raises(RuntimeError, match="delivery WorkflowRun does not match current WorkAuthority"):
        lane.ship(now=3000.0, closure=True)
    assert lane.registry.get_workflow_run(lane.run_id).status == "ongoing"


def test_merged_delivery_tolerance_is_ship_lane_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不帶 delivery journal 的呼叫端（review-attest 等）維持原本的 claim-era 比對。"""

    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    _post_merge_snapshot(lane.snapshot)
    authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=lane.snapshot)

    with pytest.raises(RuntimeError, match="delivery WorkflowRun does not match current WorkAuthority"):
        work_actions._canonical_workflow_run(
            workflow_registry=lane.registry, authority=authority
        )
    assert (
        work_actions._canonical_workflow_run(
            workflow_registry=lane.registry,
            authority=authority,
            merged_delivery_journal=lane.journal,
        ).run_id
        == lane.run_id
    )


# ---------------------------------------------------------------------------
# 3. 已交付但 resume 失敗：next_actions／next_step_hint 指向 retire-delivered
# ---------------------------------------------------------------------------


def _mark_resume_failed(lane) -> None:
    lane.registry._manager_update_workflow_run(
        lane.run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=diagnostic_reason(
            "resume-workflow-failed",
            "periodic tick 續跑 workflow 時擲出例外：RuntimeError: remote closure blocked",
            source="manager_daemon.periodic_tick:resume-workflow",
            run_id=lane.run_id,
            work_id=WORK_ID,
            repo=REPO,
            phase="review",
        ),
    )


def test_delivered_run_resume_failure_points_to_retire_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    _mark_resume_failed(lane)
    run = lane.registry.get_workflow_run(lane.run_id)

    assert "retire-delivered" in work_actions._phase_recovery_actions(run, lane.registry)
    entry = manager.workflow_status_entry(lane.registry, run)

    # abandon 的 pre-delivery admission 對帶 pr_refs 的 run 必拒，不得再曝光。
    assert entry["next_actions"] == ["retire-delivered"]
    assert "retire-delivered" in entry["next_step_hint"]
    assert "cortex work abandon" not in entry["next_step_hint"]
    assert lane.run_id in entry["next_step_hint"]


def test_undelivered_run_resume_failure_offers_resume_instead_of_abandon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1170：PR 已開、未證實 merge 的 run 不是 retire-delivered 的對象，但也越過了
    abandon 的 pre-delivery 閘門；投影改給註冊的 `resume`，不再給必被拒的 abandon。"""

    lane = _ship_lane(tmp_path, monkeypatch, copilot_reviewed=False)
    _open_pr_snapshot(lane.snapshot)
    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    _mark_resume_failed(lane)
    run = lane.registry.get_workflow_run(lane.run_id)

    assert "retire-delivered" not in work_actions._phase_recovery_actions(run, lane.registry)
    with pytest.raises(ValueError, match="pre-delivery"):
        lane.registry._manager_validate_workflow_abandon(
            run.run_id, evidence_ref=str(tmp_path / "abandon-probe.json")
        )
    entry = manager.workflow_status_entry(lane.registry, run)
    assert entry["next_actions"] == ["resume"]
    assert "cortex work abandon" not in entry["next_step_hint"]
    assert f"cortex work resume {WORK_ID} --repo {REPO}" in entry["next_step_hint"]


def test_needs_human_base_actions_switch_to_retire_delivered_for_delivered_runs() -> None:
    assert claim_module.needs_human_next_actions(
        phase="review",
        planning_failure_classification=None,
        job_recovery_actions=("retire-delivered",),
    ) == ("retire-delivered",)
    hint = claim_module.needs_human_next_step_hint(
        phase="review",
        planning_failure_classification=None,
        work_id=WORK_ID,
        repo=REPO,
        run_id="workflow-" + "0" * 20,
        job_recovery_actions=("retire-delivered",),
    )
    assert "cortex work retire-delivered demo --repo acme/demo" in hint
    assert "cortex work abandon" not in hint
    # 未交付（沒有 retire-delivered）的基礎集合不變。
    assert claim_module.needs_human_next_actions(
        phase="review", planning_failure_classification=None
    ) == ("abandon",)


def test_monitor_work_list_projection_points_delivered_run_to_retire_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.monitor import providers

    lane = _ship_lane(tmp_path, monkeypatch)
    _merge_via_ship_lane(lane)
    _mark_resume_failed(lane)
    monkeypatch.setattr(
        work_actions, "work_authority_projection_state", lambda **_: "available"
    )
    rows = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))["workflows"]

    projected = providers._workflow_next_actions_projection(
        rows,
        repo=REPO,
        job_rows=[],
        slice_rows=[],
        state_path=tmp_path / "jobs.json",
    )

    assert projected[WORK_ID] == {"run_id": lane.run_id, "actions": ["retire-delivered"]}


# ---------------------------------------------------------------------------
# 4. merged-delivery 判準的兩個形狀缺口：git SHA-1 tree、run 自產 OpenSpec change
# ---------------------------------------------------------------------------


def _journal_run(registry: JobRegistry, *, workspace: Path, declared_change: str):
    from paulsha_cortex.coordinator.workflow import WorkflowStep

    steps = (
        WorkflowStep("claim", "manager", "workflow-claim", None, None, None, (), (), "passed"),
        WorkflowStep(
            "define",
            "planner",
            "openspec-propose",
            None,
            None,
            None,
            (),
            (f"openspec/changes/{declared_change}/proposal.md",),
            "passed",
        ),
        WorkflowStep("review", "reviewer", "code-review", None, None, None, (), (), "passed"),
        WorkflowStep("ship", "manager", "policy-commit", None, None, None, (), ()),
    )
    return registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=(f"{REPO}#{ISSUE}",),
        openspec_refs=(),
        pr_refs=(f"{REPO}#{PR}",),
        attempts={"review": 1},
        candidate_head=HEAD,
        verified_head=HEAD,
    )


def _write_merged_journal(path: Path, run, *, change: str | None, tree_hash: str) -> None:
    step_ids = [f"{run.run_id}:{step.phase}:{step.card}" for step in run.steps]
    binding = {"pr_number": PR, "change": change, "todo_paths": [TODO_PATH]}
    body = {
        "schema": "cortex-merge-authorization/v1",
        "run_id": run.run_id,
        "workflow_step_ids": step_ids,
        "repo": run.repo,
        "work_id": run.work_id,
        "authority_digest": "3" * 64,
        **binding,
        "head": HEAD,
        "tree_hash": tree_hash,
        "foreign_review_path": str(path.parent / "foreign-review.json"),
        "foreign_review_hash": "5" * 64,
        "preflight_hash": "6" * 64,
        "checks_hash": "7" * 64,
        "copilot_requested_at_epoch": 100.0,
        "copilot_review_id": 19,
        "copilot_hash": "8" * 64,
    }
    auth_hash = work_actions.verification.canonical_json_hash(body)
    auth_path = path.parent / "evidence" / f"merge-authorization-{auth_hash}.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps({"payload": body, "hash": auth_hash}), encoding="utf-8")
    auth_path.chmod(0o444)
    path.write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "workflow_step_ids": step_ids,
                        "delivery_binding": binding,
                        "ship": {
                            "phase": "merged",
                            **binding,
                            "head": HEAD,
                            "merge_commit": MERGE_COMMIT,
                            "merge_authorization": {
                                "path": str(auth_path),
                                "hash": auth_hash,
                                "payload": body,
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("change", "tree_hash", "expected"),
    [
        (None, "b" * 40, True),
        (None, "b" * 64, True),
        (None, "b" * 39, False),
        (None, "B" * 40, False),
        # run 自己 planning 宣告建立的 change（claim 時 run.openspec_refs 還是空的）
        ("demo-change", "b" * 40, True),
        # 不是 run 宣告的 change：不承認
        ("foreign-change", "b" * 40, False),
    ],
)
def test_merged_delivery_binding_shapes(
    tmp_path: Path, change: str | None, tree_hash: str, expected: bool
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _journal_run(registry, workspace=tmp_path, declared_change="demo-change")
    journal = tmp_path / "delivery-journal.json"
    _write_merged_journal(journal, run, change=change, tree_hash=tree_hash)

    binding = manager._merged_delivery_journal_binding(run, journal_path=journal)

    assert manager._merged_delivery_journal_bound(run, journal_path=journal) is expected
    if expected:
        assert binding == {"pr_number": PR, "change": change, "todo_paths": [TODO_PATH]}
    else:
        assert binding is None
