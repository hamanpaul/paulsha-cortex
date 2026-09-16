"""#830：派工結果的非 Job 決策契約——真 producer 的合法非 Job 決策不得在消費端炸 KeyError。

`manager._dispatch_workflow_card` 對 plan 完成且 sizing_band=red 的 run 合法回傳
`{run_id, current_phase, reason: "needs-decomposition"}`（#223），不建立 Job；daemon 的
start／work-action 消費端與 manager 的 resume／provider-retry 消費端過去只驗 `is not None`
就讀 `["job_id"]`。本檔以 #223 的真 red producer fixture 接到各正式消費端重現，並鎖住
分類契約：真 Job（registry 綁定相符）／合法 decision／確定性 transition／None／malformed。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

WORK_ID = "dispatch-decision-830"
REPO = "hamanpaul/paulsha-cortex"


# --- 真 red producer fixture（沿 tests/test_dispatch_needs_decomposition_223.py） ---


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "dispatch decision", change=WORK_ID)
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _write_planning_artifacts(root: Path) -> tuple[PlanningArtifactAuthority, ...]:
    proposal = root / f"openspec/changes/{WORK_ID}/proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    authority: list[PlanningArtifactAuthority] = []
    for kind, body in bodies.items():
        ref = f"docs/{kind}.md"
        path = root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        authority.append(
            PlanningArtifactAuthority(ref=ref, kind=kind, work_id=WORK_ID, baseline_sha256=digest)
        )
    return tuple(authority)


def _red_run(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo)
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=(f"{REPO}#830",),
        openspec_refs=(WORK_ID,),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
        sizing_score=8,
        sizing_band="red",
        decomposition_depth=0,
    )
    return registry, run


def _dispatcher(registry: JobRegistry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def _must_not_launch(_identity):
    raise AssertionError("red band 必須在 build 前攔下，不得啟動任何 launcher")


def _daemon_executor(tmp_path: Path, dispatcher, **kwargs):
    return manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        launcher=_must_not_launch,
        **kwargs,
    )


def _decision(run, reason: str = "needs-decomposition") -> dict:
    return {"run_id": run.run_id, "current_phase": run.current_phase, "reason": reason}


# --- 真 producer → daemon workflow-action start 消費端（manager_daemon 的第一個 job_id 讀取點） ---


def test_daemon_start_consumes_real_red_decision_without_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, run = _red_run(tmp_path)
    monkeypatch.setattr(
        manager,
        "apply_workflow_action",
        lambda *a, **k: {"run_id": run.run_id, "current_phase": "plan"},
    )
    executor = _daemon_executor(tmp_path, _dispatcher(registry))
    request = build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator")

    result = executor(request)

    assert "job_id" not in result
    assert result["dispatch"] == {
        "kind": "decision",
        "run_id": run.run_id,
        "current_phase": "plan",
        "reason": "needs-decomposition",
    }
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_decomposition",)
    assert "needs_human" not in persisted.facets
    assert registry.list_jobs() == []

    # R5：同一 start 重送不得新增 run／Job，也不得改寫 decision。
    again = executor(request)
    assert again["dispatch"]["reason"] == "needs-decomposition"
    assert registry.list_jobs() == []
    assert len(registry.list_workflow_runs()) == 1
    assert registry.get_workflow_run(run.run_id).facets == ("needs_decomposition",)


# --- 真 producer → manager.resume_workflow_run（explicit resume 與 periodic 共用） ---


def test_resume_returns_real_red_decision_instead_of_job_failed(tmp_path: Path) -> None:
    registry, run = _red_run(tmp_path)

    result = manager.resume_workflow_run(
        _dispatcher(registry),
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "plan",
        "reason": "needs-decomposition",
    }
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in persisted.facets
    assert persisted.needs_human_reason is None
    assert persisted.facets == ("needs_decomposition",)
    assert registry.list_jobs() == []


# --- daemon work-action 消費端（start／intake／resume 同步續推的第二個 job_id 讀取點） ---


def _work_action_run(registry: JobRegistry, tmp_path: Path, *, phase: str = "claim"):
    if phase == "claim":
        step = WorkflowStep(
            phase="claim",
            persona="manager",
            card="claim",
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            inputs=(),
            outputs=(),
            gate_result="passed",
        )
    else:
        step = WorkflowStep(
            phase=phase,
            persona="builder",
            card="tdd-red",
            executor="codex",
            model="gpt-5",
            domain="openai",
            inputs=(),
            outputs=(),
        )
    return registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase=phase,
        steps=(step,),
        issue_refs=(f"{REPO}#830",),
        gate_status="running",
    )


def _work_action_request(run, action: str, **extra) -> dict:
    return build_request(
        req_type="work-action",
        args={"action": action, "repo": run.repo, "work_id": run.work_id, **extra},
        requested_by="operator",
    )


def test_work_action_start_projects_decision_and_never_fabricates_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path)
    calls: list[dict] = []

    def fake_dispatch(*args, **kwargs):
        calls.append(kwargs)
        return _decision(run)

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)
    executor = _daemon_executor(
        tmp_path,
        _dispatcher(registry),
        work_action_fn=lambda **_: {"result": {"action": "claim", "run": run.to_dict()}},
    )

    result = executor(_work_action_request(run, "start"))

    assert len(calls) == 1
    assert "job_id" not in result["result"]
    assert result["result"]["dispatch"]["kind"] == "decision"
    assert result["result"]["dispatch"]["reason"] == "needs-decomposition"
    assert result["result"]["run"]["run_id"] == run.run_id
    assert "needs_human" not in registry.get_workflow_run(run.run_id).facets


def test_work_action_real_job_still_returns_exact_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path)

    def fake_dispatch(*args, **kwargs):
        return registry.create_job(
            task=WORK_ID,
            persona="builder",
            branch="feature/830",
            pane="",
            worktree=str(tmp_path / "wt"),
            workflow_run_id=run.run_id,
            workflow_card="subagent-build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)
    executor = _daemon_executor(
        tmp_path,
        _dispatcher(registry),
        work_action_fn=lambda **_: {"result": {"action": "claim", "run": run.to_dict()}},
    )

    result = executor(_work_action_request(run, "start"))

    job_id = result["result"]["job_id"]
    assert registry.get_job(job_id)["workflow_run_id"] == run.run_id
    assert result["result"]["dispatch"] == {"kind": "job", "job_id": job_id, "run_id": run.run_id}


def test_work_action_none_without_transition_keeps_not_dispatched_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path)
    monkeypatch.setattr(manager, "dispatch_workflow_card", lambda *a, **k: None)
    executor = _daemon_executor(
        tmp_path,
        _dispatcher(registry),
        work_action_fn=lambda **_: {"result": {"action": "claim", "run": run.to_dict()}},
    )

    result = executor(_work_action_request(run, "start"))

    assert "job_id" not in result["result"]
    assert result["result"]["dispatch"] == {"kind": "none", "run_id": run.run_id, "current_phase": "claim"}


def test_work_action_reports_deterministic_transition_when_phase_advanced_without_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path)

    def fake_deterministic_dispatch(*args, **kwargs):
        # 模擬 producer 已持久化推進 phase 但沒有派任何模型 Job。
        registry._manager_update_workflow_run(run.run_id, current_phase="define")
        return None

    monkeypatch.setattr(manager, "dispatch_workflow_card", fake_deterministic_dispatch)
    executor = _daemon_executor(
        tmp_path,
        _dispatcher(registry),
        work_action_fn=lambda **_: {"result": {"action": "claim", "run": run.to_dict()}},
    )

    result = executor(_work_action_request(run, "start"))

    assert "job_id" not in result["result"]
    assert result["result"]["dispatch"] == {
        "kind": "transition",
        "run_id": run.run_id,
        "from_phase": "claim",
        "current_phase": "define",
    }
    assert result["result"]["run"]["current_phase"] == "define"


# --- forced retry（retry-build／retry-card）：合法 decision 仍不滿足「新 replacement Job」後置條件 ---


def test_forced_retry_with_decision_keeps_fail_closed_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path, phase="build")
    monkeypatch.setattr(manager, "dispatch_workflow_card", lambda *a, **k: _decision(run, "runtime-preflight-refused"))
    executor = _daemon_executor(
        tmp_path,
        _dispatcher(registry),
        work_action_fn=lambda **_: {"result": {"action": "retry-card", "run": run.to_dict()}},
    )

    with pytest.raises(RuntimeError, match="runtime-preflight-refused"):
        executor(_work_action_request(run, "retry-card", expected_run_id=run.run_id, card="tdd-red"))

    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    assert persisted.needs_human_reason["reason"] == "forced-card-retry-failed"
    assert "runtime-preflight-refused" in persisted.needs_human_reason["detail"]
    assert registry.list_jobs() == []


# --- 分類契約本體 ---


def test_classify_dispatch_result_matrix(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _work_action_run(registry, tmp_path)
    classify = manager.classify_dispatch_result

    assert classify(None, registry=registry, run_id=run.run_id, before_phase="claim") == {
        "kind": "none",
        "run_id": run.run_id,
        "current_phase": "claim",
    }
    assert classify(_decision(run), registry=registry, run_id=run.run_id, before_phase="claim") == {
        "kind": "decision",
        "run_id": run.run_id,
        "current_phase": "claim",
        "reason": "needs-decomposition",
    }
    bound = registry.create_job(
        task=WORK_ID, persona="builder", branch="feature/830", pane="", worktree=str(tmp_path / "wt"),
        workflow_run_id=run.run_id, workflow_card="subagent-build",
    )
    assert classify(bound, registry=registry, run_id=run.run_id, before_phase="claim") == {
        "kind": "job",
        "job_id": bound["job_id"],
        "run_id": run.run_id,
    }

    # forged／錯 run／malformed 一律 fail-closed，不得取得 dispatch authority。
    with pytest.raises(ValueError, match="job_id"):
        classify({"job_id": "forged-job"}, registry=registry, run_id=run.run_id, before_phase="claim")
    other = registry.create_job(
        task="other", persona="builder", branch="feature/other", pane="", worktree=str(tmp_path / "wt2"),
        workflow_run_id="workflow-someone-else", workflow_card="subagent-build",
    )
    with pytest.raises(ValueError, match="run"):
        classify(other, registry=registry, run_id=run.run_id, before_phase="claim")
    with pytest.raises(ValueError, match="run"):
        classify(
            {"run_id": "workflow-someone-else", "current_phase": "plan", "reason": "needs-decomposition"},
            registry=registry, run_id=run.run_id, before_phase="claim",
        )
    for malformed in ({}, {"reason": "x"}, {"run_id": run.run_id}, {"job_id": ""}, "job-1", 3):
        with pytest.raises(ValueError):
            classify(malformed, registry=registry, run_id=run.run_id, before_phase="claim")
