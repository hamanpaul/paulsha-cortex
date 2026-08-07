"""#275：canonical engineering outcome contract 測試。

涵蓋：(i) schema fail-closed 驗證、(ii) OutcomeStore.append 的 outcome_id
idempotency、(iii) ``_ship_action`` 端到端產生 ``shipped`` record、
(iv) ``_abandon_action`` 端到端產生 ``abandoned`` record 且重入分支不重複
append、(v) Hippo 未安裝時模組行為不受影響、以及 CLI ``outcome`` 唯讀 surface。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator import engineering_outcome, work_actions
from paulsha_cortex.coordinator.github_delivery import (
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    MergeStatus,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, WorkflowRun


HEAD = "a" * 40
TREE = "b" * 40


# ---------------------------------------------------------------------------
# 沿用 tests/test_work_actions.py 的 fixture pattern（獨立複本，避免跨測試檔
# import——tests/ 不是 package，跨檔 import 依賴 sys.path 插入順序不可靠）。
# ---------------------------------------------------------------------------


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )
    return root


def _pr_metadata(path: Path, *, title="fix(work): 修正工作流程", body="Closes #12") -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["enhancement"]}),
        encoding="utf-8",
    )
    return path


def _snapshot(
    path: Path,
    *,
    issues=(12,),
    source_revisions=("issue:12@open", "openspec:demo@1"),
    provider_revision="gh-1",
    auto_label=True,
    prs=(8,),
    changes=("demo",),
    todo_paths=("docs/todo.md",),
) -> Path:
    _init_repo(path.parent)
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
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": auto_label,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _sample_run(*, run_id: str = "workflow-" + "1" * 20, status: str = "ongoing") -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:" + "c" * 64,
        source_revision="issue:12@open",
        workspace_root="/tmp/does-not-exist/worktree",
        combo="combo-a",
        current_phase="build",
        steps=(),
        issue_refs=("acme/demo#12",),
        openspec_refs=("demo",),
        pr_refs=(),
        attempts={},
        evidence_refs=(),
        gate_refs=(),
        brainstorm_required=False,
        primary_domain=None,
        candidate_head=HEAD,
        verified_head=HEAD,
        facets=(),
        gate_status="pending",
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:10:00+00:00",
        status=status,
    )


def _sample_authority() -> SimpleNamespace:
    return SimpleNamespace(repo="acme/demo", work_id="demo")


def _sample_jobs(run_id: str) -> list[dict]:
    """單一 run 底下的 job record（已是呼叫端過濾後應傳入 build_outcome_record
    的形狀——build_outcome_record 本身不做 run_id 過濾，那是 emit_outcome 的
    責任，見 :func:`_sample_jobs_with_foreign_run`）。"""

    return [
        {
            "job_id": "demo-1",
            "task": "demo",
            "workflow_run_id": run_id,
            "workflow_card": "build-card",
            "persona": "builder",
            "workflow_phase": "build",
            "session_name": "demo",
        },
        {
            "job_id": "demo-2",
            "task": "demo",
            "workflow_run_id": run_id,
            "workflow_card": "review-card",
            "persona": "reviewer",
            "workflow_phase": "review",
            "session_name": "demo",
        },
    ]


def _sample_jobs_with_foreign_run(run_id: str) -> list[dict]:
    """混入另一個 run 的 job——用來驗證 ``emit_outcome`` 會依 workflow_run_id
    過濾掉不屬於本次終局轉換的 job，而不是把 registry 裡全部 job 都收進去。"""

    return _sample_jobs(run_id) + [
        {
            "job_id": "other-1",
            "task": "other",
            "workflow_run_id": "workflow-" + "9" * 20,
            "workflow_card": "build-card",
            "persona": "builder",
            "workflow_phase": "build",
            "session_name": "other",
        },
    ]


def _build_sample_record(**overrides) -> dict:
    run = _sample_run()
    authority = _sample_authority()
    jobs = _sample_jobs(run.run_id)
    kwargs = dict(
        run=run,
        authority=authority,
        jobs=jobs,
        outcome="shipped",
        attempt_digest="digest-a",
        candidate={"sha": HEAD},
    )
    kwargs.update(overrides)
    return engineering_outcome.build_outcome_record(**kwargs)


# ---------------------------------------------------------------------------
# (i) schema fail-closed 驗證
# ---------------------------------------------------------------------------


def test_emitted_outcome_statuses_is_a_documented_subset_of_schema_statuses() -> None:
    # v1 只有 shipped／abandoned 有 emitter（見 _ship_action／_abandon_action）；
    # rejected／failed／rolled_back 是 schema 保留值，尚無呼叫端會產生。
    assert set(engineering_outcome.EMITTED_OUTCOME_STATUSES) == {"shipped", "abandoned"}
    assert set(engineering_outcome.EMITTED_OUTCOME_STATUSES) <= set(
        engineering_outcome.OUTCOME_STATUSES
    )


def test_build_outcome_record_projects_jobs_and_derives_slice_id() -> None:
    record = _build_sample_record()
    assert record["schema"] == engineering_outcome.ENGINEERING_OUTCOME_KIND
    assert record["schema_version"] == engineering_outcome.ENGINEERING_OUTCOME_SCHEMA_VERSION
    assert record["repo"] == "acme/demo"
    assert record["work_id"] == "demo"
    assert record["outcome"] == "shipped"
    assert record["slice_id"] == "demo"
    assert [job["job_id"] for job in record["jobs"]] == ["demo-1", "demo-2"]
    assert record["jobs"][0] == {
        "job_id": "demo-1",
        "card": "build-card",
        "persona": "builder",
        "workflow_phase": "build",
    }
    assert record["execution_provenance"]["correlation_confidence"] == "weak"
    assert record["execution_provenance"]["session_refs"] == ["demo"]
    assert record["outcome_id"] == engineering_outcome.outcome_id(
        run_id=record["workflow_run_id"], outcome="shipped", attempt_digest="digest-a"
    )


def test_emit_outcome_filters_jobs_by_workflow_run_id(tmp_path: Path) -> None:
    """``emit_outcome`` 收到整份 ``workflow_registry.list_jobs()`` 時，只把
    ``workflow_run_id`` 等於本次 run 的 job 收進 record，不屬於本次終局轉換的
    job（例如另一個 work item 的 job）不得混入。"""

    run = _sample_run()
    store = engineering_outcome.OutcomeStore(tmp_path / "outcomes" / "acme-demo.jsonl")
    record = engineering_outcome.emit_outcome(
        store,
        run=run,
        authority=_sample_authority(),
        jobs=_sample_jobs_with_foreign_run(run.run_id),
        outcome="shipped",
        attempt_digest="digest-filter",
    )
    assert [job["job_id"] for job in record["jobs"]] == ["demo-1", "demo-2"]


def test_validate_outcome_record_rejects_unknown_outcome_status() -> None:
    payload = dict(_build_sample_record())
    payload["outcome"] = "cancelled"  # 不在 OUTCOME_STATUSES 白名單
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.validate_outcome_record(payload)
    assert exc.value.reason == "outcome-status-invalid"
    assert exc.value.validation_path == "$.outcome"


def test_validate_outcome_record_rejects_missing_required_field() -> None:
    payload = dict(_build_sample_record())
    del payload["repo"]
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.validate_outcome_record(payload)
    assert exc.value.reason == "repo-missing"


def test_validate_outcome_record_rejects_malformed_outcome_id() -> None:
    payload = dict(_build_sample_record())
    payload["outcome_id"] = "not-an-outcome-id"
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.validate_outcome_record(payload)
    assert exc.value.reason == "outcome-id-invalid"


def test_validate_outcome_record_rejects_malformed_run_id() -> None:
    payload = dict(_build_sample_record())
    payload["workflow_run_id"] = "not-a-run-id"
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.validate_outcome_record(payload)
    assert exc.value.reason == "workflow-run-id-invalid"


def test_validate_outcome_record_rejects_non_object_jobs_entry() -> None:
    payload = dict(_build_sample_record())
    payload["jobs"] = [{"job_id": "x", "card": "c", "persona": "p", "workflow_phase": 7}]
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.validate_outcome_record(payload)
    assert exc.value.reason == "jobs-invalid"


def test_outcome_id_rejects_illegal_outcome_status() -> None:
    with pytest.raises(engineering_outcome.EngineeringOutcomeError) as exc:
        engineering_outcome.outcome_id(
            run_id="workflow-" + "1" * 20, outcome="cancelled", attempt_digest="d"
        )
    assert exc.value.reason == "outcome-status-invalid"


# ---------------------------------------------------------------------------
# (ii) OutcomeStore.append idempotency
# ---------------------------------------------------------------------------


def test_outcome_id_is_deterministic_per_attempt_and_diverges_across_attempts() -> None:
    run_id = "workflow-" + "2" * 20
    first = engineering_outcome.outcome_id(run_id=run_id, outcome="shipped", attempt_digest="d1")
    again = engineering_outcome.outcome_id(run_id=run_id, outcome="shipped", attempt_digest="d1")
    different_attempt = engineering_outcome.outcome_id(
        run_id=run_id, outcome="shipped", attempt_digest="d2"
    )
    assert first == again
    assert first != different_attempt


def test_outcome_store_append_is_idempotent_by_outcome_id(tmp_path: Path) -> None:
    store = engineering_outcome.OutcomeStore(tmp_path / "outcomes" / "acme-demo.jsonl")
    first_record = _build_sample_record(attempt_digest="stable-digest", candidate={"sha": HEAD})
    stored_first = store.append(first_record)
    assert list(store.list_outcomes()) == [stored_first]

    # 同一個 outcome_id（同 run_id/outcome/attempt_digest）但內容不同的第二次
    # append：必須被視為同一次終局轉換的重複 tick，不得產生第二筆，且回傳的是
    # 第一次寫入的內容（不是被新內容覆寫）。
    second_record = _build_sample_record(
        attempt_digest="stable-digest", candidate={"sha": "different-sha-should-be-ignored"}
    )
    assert second_record["outcome_id"] == first_record["outcome_id"]
    stored_second = store.append(second_record)
    assert stored_second == stored_first
    all_records = list(store.list_outcomes())
    assert len(all_records) == 1
    assert all_records[0]["candidate"] == {"sha": HEAD}


def test_outcome_store_list_show_replay_filter_correctly(tmp_path: Path) -> None:
    store = engineering_outcome.OutcomeStore(tmp_path / "outcomes" / "acme-demo.jsonl")
    shipped = store.append(
        _build_sample_record(outcome="shipped", attempt_digest="ship-digest")
    )
    other_run = _sample_run(run_id="workflow-" + "3" * 20)
    abandoned = store.append(
        engineering_outcome.build_outcome_record(
            run=other_run,
            authority=_sample_authority(),
            jobs=_sample_jobs(other_run.run_id),
            outcome="abandoned",
            attempt_digest="abandon-digest",
            reason_code="superseded by newer authority",
        )
    )
    assert {row["outcome_id"] for row in store.list_outcomes()} == {
        shipped["outcome_id"],
        abandoned["outcome_id"],
    }
    assert store.show_outcome(shipped["outcome_id"]) == shipped
    assert store.show_outcome("outcome-" + "0" * 20) is None
    replayed = list(store.replay_outcomes(since=shipped["emitted_at"]))
    assert shipped in replayed


# ---------------------------------------------------------------------------
# (v) Hippo 未安裝時本模組一切行為不受影響
# ---------------------------------------------------------------------------


def test_engineering_outcome_module_imports_without_hippo_and_still_works(tmp_path: Path) -> None:
    import inspect

    import sys

    assert "paulsha_hippo" not in sys.modules
    import paulsha_cortex.coordinator.engineering_outcome as module

    assert "paulsha_hippo" not in inspect.getsource(module)
    # 端到端一次真的跑過 build → validate → append → list，證明「未裝 Hippo」
    # 不是靠沒被呼叫到才不出錯。
    store = module.OutcomeStore(tmp_path / "hippo-absent" / "acme-demo.jsonl")
    record = store.append(_build_sample_record())
    assert list(store.list_outcomes())[0]["outcome_id"] == record["outcome_id"]


# ---------------------------------------------------------------------------
# (iii) _ship_action 端到端：outcome store 產生一筆 shipped record
# ---------------------------------------------------------------------------


def test_ship_action_emits_shipped_outcome_before_terminal_transition(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    # _ship_action 的 status="done"／emit_outcome 只在 canonical_run.current_phase
    # == "ship" 時觸發（見 work_actions._ship_action）；"start" 只建到 "define"，
    # 這裡直接沿 WORKFLOW_PHASES 一步步推到 "ship"（phase transition 只允許
    # +1 step），並補上 "ship" phase 的 post_init 硬性要求
    # （gate_status="passed" 需附 foreign-review gate evidence）。
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    # "ship" phase 的 post_init 也要求：verify/review/ship steps 全部
    # gate_result=="passed"；build 與 verify/review 的 independence domain
    # 必須不相交（reviewer 與 builder 分離）。
    def _advance_step(step):
        if step.phase == "build":
            return replace(step, domain="openai")
        if step.phase in {"verify", "review"}:
            return replace(step, domain="google", gate_result="passed")
        if step.phase == "ship":
            return replace(step, gate_result="passed")
        return step

    passed_steps = tuple(
        _advance_step(step) for step in registry.get_workflow_run(run_id).steps
    )
    registry._manager_update_workflow_run(
        run_id,
        current_phase="ship",
        steps=passed_steps,
        gate_status="passed",
        gate_refs=(
            GateEvidenceRef(kind="foreign-review", ref="evidence-ref-1"),
            GateEvidenceRef(kind="maintainer-review", ref="evidence-ref-2"),
        ),
        candidate_head=HEAD,
        verified_head=HEAD,
    )
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    completion = tmp_path / "completion.json"
    completion.write_text("{}", encoding="utf-8")
    review_available = {"value": False}
    merged_state = {"value": False}

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            reviews = ()
            if review_available["value"]:
                from paulsha_cortex.coordinator.github_delivery import COPILOT_REVIEWER_LOGIN

                reviews = (
                    CopilotReview(
                        review_id=9,
                        commit_id=HEAD,
                        state="COMMENTED",
                        body="ok",
                        author=COPILOT_REVIEWER_LOGIN,
                        submitted_at_epoch=205,
                    ),
                )
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=(GitHubCheck("pytest", "completed", "success"),),
                copilot_reviews=reviews,
                review_threads=(),
                closing_issues=(12,),
                active_openspec_absent=True,
                archive_present=True,
            )

        def request_copilot(self, **kwargs):
            pass

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(
                merged=merged_state["value"],
                pr_head=HEAD,
                merge_commit="c" * 40 if merged_state["value"] else None,
            )

    class Orchestrator:
        def __init__(self, *, github, now):
            pass

        def merge_if_ready(self, **kwargs):
            merged_state["value"] = True
            return SimpleNamespace(expected_head=HEAD, expected_tree_hash=TREE)

        def verify_remote_closure(self, **kwargs):
            return SimpleNamespace(
                facts=SimpleNamespace(merge_commit="c" * 40),
                completion_record={"path": "/evidence/completion.json", "hash": "d" * 64},
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    foreign_normalized = {"state": "passed", "candidate": HEAD}
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: foreign_normalized,
    )
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
    base = {
        "action": "ship",
        "repo": "acme/demo",
        "work_id": "demo",
        "repo_root": str(tmp_path),
        "pr_number": 8,
        "change": "demo",
        "todo_paths": ["docs/todo.md"],
        "foreign_review_path": str(foreign),
        "foreign_review_hash": work_actions.verification.canonical_json_hash(foreign_normalized),
        "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
    }
    first = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    assert first["result"]["action"] == "awaiting-copilot"

    outcome_store = engineering_outcome.OutcomeStore(
        engineering_outcome.outcome_store_path(state, repo="acme/demo")
    )
    # 尚未 merge：不得提早 emit「shipped」outcome。
    assert list(outcome_store.list_outcomes()) == []

    review_available["value"] = True
    second = work_actions.execute_work_action(
        args=base,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )
    assert second["result"]["action"] == "merged-awaiting-closure"
    assert list(outcome_store.list_outcomes()) == []

    third = work_actions.execute_work_action(
        args={**base, "completion_record_path": str(completion)},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 220,
        workflow_registry=registry,
    )
    assert third["result"]["action"] == "done"
    assert registry.get_workflow_run(run_id).status == "done"

    records = list(outcome_store.list_outcomes(repo="acme/demo", work_id="demo"))
    assert len(records) == 1
    record = records[0]
    assert record["outcome"] == "shipped"
    assert record["candidate"]["sha"] == HEAD
    assert record["candidate"]["merge_commit"] == "c" * 40
    assert record["candidate"]["pr_number"] == 8
    assert record["review"]["merge_authorization_hash"]
    # 本 e2e 只跑 start → ship，未派工 build/verify/review job，因此這個 run 底
    # 下沒有 job record；用空陣列證明過濾邏輯正確地不把其他 run 的 job 混入。
    assert record["jobs"] == []


# ---------------------------------------------------------------------------
# (iv) _abandon_action 端到端：abandoned record，且 superseded 重入不重複 append
# ---------------------------------------------------------------------------


def test_abandon_action_emits_abandoned_outcome_and_reentry_is_idempotent(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", prs=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    run = registry.get_workflow_run(run_id)
    job = registry.create_job(
        task="wf-abandon-outcome",
        persona="planner",
        kind="build",
        branch="feature/demo",
        pane="",
        worktree=run.workspace_root,
        executor="codex",
        model_id="gpt",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="define-card",
        workflow_phase="define",
        workflow_repo_root=run.workspace_root,
        source_revision=run.source_revision,
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=1)

    args = {
        "action": "abandon",
        "repo": "acme/demo",
        "work_id": "demo",
        "issue": 12,
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "Superseded by the clean terminal canary.",
    }
    outcome_store = engineering_outcome.OutcomeStore(
        engineering_outcome.outcome_store_path(state, repo="acme/demo")
    )
    assert list(outcome_store.list_outcomes()) == []

    first = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    records_after_first = list(outcome_store.list_outcomes())
    assert len(records_after_first) == 1
    assert records_after_first[0]["outcome"] == "abandoned"
    assert records_after_first[0]["workflow_run_id"] == run_id
    assert records_after_first[0]["reason_code"] == args["reason"]
    assert [job_row["job_id"] for job_row in records_after_first[0]["jobs"]] == [job["job_id"]]

    # 重入：run 已是 superseded，_abandon_action 會走 reentry 分支再次呼叫
    # emit_outcome；attempt_digest 是既有 evidence 的內容 digest，因此必須被
    # OutcomeStore 去重，不得產生第二筆。
    second = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        workflow_registry=registry,
    )
    assert first == second
    records_after_second = list(outcome_store.list_outcomes())
    assert len(records_after_second) == 1
    assert records_after_second[0]["outcome_id"] == records_after_first[0]["outcome_id"]


# ---------------------------------------------------------------------------
# CLI 唯讀 surface（Phase 3）
# ---------------------------------------------------------------------------


def test_cli_outcome_list_show_replay(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "agents"))
    root = engineering_outcome.default_outcomes_root()
    store = engineering_outcome.OutcomeStore(
        engineering_outcome.outcome_store_path_for_repo(root, "acme/demo")
    )
    record = store.append(_build_sample_record())

    assert coordinator_cli.main(["outcome", "list", "--repo", "acme/demo"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == [record]

    assert coordinator_cli.main(["outcome", "show", record["outcome_id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == record

    assert coordinator_cli.main(["outcome", "show", "outcome-" + "0" * 20]) == 1
    assert "查無" in capsys.readouterr().err

    assert coordinator_cli.main(["outcome", "replay", "--repo", "acme/demo"]) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed == [record]
