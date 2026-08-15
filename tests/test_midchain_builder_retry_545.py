"""#545：中段 builder 卡採信失敗後沒有契約內的重派路徑。

現場（run ``workflow-084f75e2178cf7547476``，#540 的殘留項）：builder 交付的
RED commit 合格、ledger 已由 ``regenerate-gates``（#540／PR #541）重生成正確，
但**舊 job 的 terminal envelope 是模型輸出**——自報 gate 名
``'focused pytest RED expectation'``，契約內不可竄改，``resume`` 重新採信仍
必敗於 ``gate-evidence-unknown-gate``。#541 已把 canonical gate 名機械注入
prompt，因此**新的** tdd-red job 會產出正確 envelope；缺的只是「重派中段
builder 卡」這條路：

- ``retry-build`` 只受理最後一張 builder 卡（tdd-red 是中段卡），而且它是
  candidate 修復語意——會把該卡的 ``action`` 覆寫成 repair 文案，中段卡走那條
  路等於把卡片自己的指示（「寫一個 RED regression test」）抹掉。
- ``recover-pre-candidate`` 要求 null candidate（worktree-isolation 早已錨定
  candidate）。
- ``abandon`` 會連合格的 RED commit 與一個世代一起燒掉。

本檔釘住新增的 ``retry-card`` work action：以 exact WorkflowRun CAS 加卡名定
錨，原子清掉 ``needs_human`` 並讓 manager 以**原卡片契約**重派一個新 job；舊
job 與舊 envelope 一個位元組都不動，已採信的 evidence 一律拒絕重派。
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex.control import contract as control_contract
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator import manager, manager_daemon, work_actions
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)
from paulsha_cortex.porcelain import recover as porcelain_recover

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "d" * 40
REPO = "acme/demo"
WORK_ID = "demo"


def _manifest_steps():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "midchain retry", change="midchain-retry")
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
    """build phase 停在 ``card``：它之前的 builder 卡全 passed、它自己 pending。"""

    build_cards = [step.card for step in _manifest_steps() if step.phase == "build"]
    stop = build_cards.index(card)
    passed = set(build_cards[:stop])
    return tuple(
        replace(step, gate_result="passed")
        if step.phase == "build" and step.card in passed
        else step
        for step in _manifest_steps()
    )


def _plan_file(tmp_path: Path) -> None:
    plan = tmp_path / "docs" / "superpowers" / "plans" / "midchain-retry.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# midchain retry plan\n", encoding="utf-8")


def _stuck_run(tmp_path: Path, *, stopped_at: str = "tdd-red"):
    """重建現場：needs_human 的 build phase run，停在一張**中段** builder 卡，
    該卡已有一顆終止但 evidence 未綁定的 job（採信失敗，envelope 不可用）。"""

    _plan_file(tmp_path)
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
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_steps_stopped_at(stopped_at),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=HEAD,
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="failed",
        needs_human_reason=fixture_needs_human_reason(),
    )
    # worktree-isolation 已錨定 candidate（現場即如此，所以 recover-pre-candidate
    # 也走不通）。
    anchor = registry.create_job(
        task="wf-anchor",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        subject_head=HEAD,
        workflow_run_id=run.run_id,
        workflow_card="worktree-isolation",
        workflow_phase="build",
    )
    registry.update_headless_result(anchor["job_id"], status="exited", exit_code=0)
    # 中段卡自己的 job：乾淨終止，但 envelope 不可用，因此 evidence 未綁定。
    log = tmp_path / "logs" / "workflow" / f"{stopped_at}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("{}\n", encoding="utf-8")
    stuck = registry.create_job(
        task=f"wf-{stopped_at}",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card=stopped_at,
        workflow_phase="build",
        workflow_test_policy="red-required",
    )
    registry.attach_launch_handle(stuck["job_id"], log_path=str(log))
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
# 段 1：中段卡重派（work action 層）
# ==========================================================================


def test_retry_card_reopens_the_midchain_builder_card(tmp_path: Path) -> None:
    """RED（修復前）：`retry-card` 不存在，中段卡無任何契約內重派路徑。"""

    snapshot, registry, run, job_id = _stuck_run(tmp_path)
    before = registry.get_job(job_id)

    result = _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
    )["result"]

    assert result["action"] == "retry-card"
    assert result["reason"] == "builder-card-redispatched"
    assert result["card_id"] == "tdd-red"
    assert result["superseded_job_ids"] == [job_id]

    persisted = registry.get_workflow_run(run.run_id)
    # facet 清除 = 這張卡重新可派。
    assert "needs_human" not in persisted.facets
    assert persisted.gate_status == "running"
    assert persisted.attempts["build"] == 2
    assert persisted.current_phase == "build"
    target = next(step for step in persisted.steps if step.card == "tdd-red")
    assert target.gate_result == "pending"
    # 舊 job 與舊 envelope 是稽核紀錄，一個位元組都不動。
    assert registry.get_job(job_id) == before


def test_retry_card_preserves_the_card_contract(tmp_path: Path) -> None:
    """重派的必須是**原卡**：`action`／`test_policy`／inputs 全數保留。

    這正是不放寬 `retry-build` 的理由——它會把該卡的 `action` 覆寫成 candidate
    repair 文案，中段卡走那條路等於把「寫一個 RED regression test」抹掉。
    """

    snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    original = next(step for step in run.steps if step.card == "tdd-red")

    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
    )

    target = next(
        step for step in registry.get_workflow_run(run.run_id).steps if step.card == "tdd-red"
    )
    assert target.action == original.action
    assert "RED regression test" in (target.action or "")
    assert target.test_policy == "red-required"
    assert target.inputs == original.inputs
    assert target.outputs == original.outputs
    # 下游卡片完全沒被動到。
    assert [
        (step.card, step.gate_result)
        for step in registry.get_workflow_run(run.run_id).steps
    ] == [(step.card, step.gate_result) for step in run.steps]


def test_retry_build_still_refuses_the_midchain_card(tmp_path: Path) -> None:
    """回歸樁：`retry-build` 的「只受理最後一張 builder 卡」語意不得被放寬。"""

    snapshot, registry, run, _job_id = _stuck_run(tmp_path)

    with pytest.raises(ValueError, match="only the final builder card pending"):
        work_actions.execute_work_action(
            args={
                "action": "retry-build",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": 12,
                "actor": "operator",
                "expected_candidate": HEAD,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_card_rejects_a_card_that_is_not_the_next_one(tmp_path: Path) -> None:
    """指名一張更後面的卡 = 想跳過中段卡；fail closed。"""

    snapshot, registry, run, _job_id = _stuck_run(tmp_path)

    with pytest.raises(RuntimeError, match="expected card mismatch"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="subagent-build",
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_card_refuses_a_card_with_accepted_evidence(tmp_path: Path) -> None:
    """已採信的 evidence immutable：不得以「重派」名義覆寫。"""

    snapshot, registry, run, job_id = _stuck_run(tmp_path)
    registry.bind_workflow_evidence(
        job_id,
        locator={"kind": "workflow-build-result", "path": "evidence/tdd-red.json", "hash": "e" * 64},
        subject_head=HEAD,
    )

    with pytest.raises(RuntimeError, match="accepted evidence"):
        _retry_card(
            tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets
    assert registry.get_job(job_id)["workflow_evidence"]["hash"] == "e" * 64


def test_retry_card_requires_exact_run_cas(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    before = registry.get_workflow_run(run.run_id).to_dict()

    with pytest.raises(RuntimeError, match="CAS mismatch"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id="workflow-" + "0" * 20,
            card="tdd-red",
        )
    # fail closed：拒絕時不得留下任何 side effect。
    assert registry.get_workflow_run(run.run_id).to_dict() == before


def test_retry_card_requires_needs_human(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=())

    with pytest.raises(RuntimeError, match="requires needs_human workflow"):
        _retry_card(
            tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
        )


def test_retry_card_requires_a_terminal_job_for_the_card(tmp_path: Path) -> None:
    """從未派過的卡屬 `resume` 的職責，不是本動作的。"""

    snapshot, registry, run, job_id = _stuck_run(tmp_path)
    registry._jobs[:] = [job for job in registry._jobs if job["job_id"] != job_id]
    registry._persist()

    with pytest.raises(RuntimeError, match="terminal job for the card"):
        _retry_card(
            tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
        )


def test_retry_card_refuses_an_active_workflow_job(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    registry.create_job(
        task="wf-inflight",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card="tdd-red",
        workflow_phase="build",
    )

    with pytest.raises(RuntimeError, match="terminal job for the card"):
        _retry_card(
            tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_retry_card_rejects_caller_supplied_evidence(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_run(tmp_path)

    with pytest.raises(ValueError, match="rejects caller evidence/input"):
        _retry_card(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            card="tdd-red",
            expected_candidate=HEAD,
        )


def test_retry_card_requires_exact_card_id(tmp_path: Path) -> None:
    snapshot, registry, run, _job_id = _stuck_run(tmp_path)

    with pytest.raises(ValueError, match="requires exact card id"):
        _retry_card(tmp_path, snapshot, registry, expected_run_id=run.run_id)


def test_retry_card_refuses_a_build_card_once_the_run_left_build(tmp_path: Path) -> None:
    """重派永遠落在**當前 phase 的當前卡**上。

    #569 把 verify／review 的 reviewer 卡納入 `retry-card` 的受理範圍，因此這裡
    不再是「非 build phase 一律拒絕」；但指名一張已經被留在身後的 build 卡仍必須
    fail closed——重派的標的只能是 `manager._current_workflow_step` 會派的那一張。
    """

    snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    registry._manager_update_workflow_run(
        run.run_id,
        steps=tuple(
            replace(step, gate_result="passed") if step.phase == "build" else step
            for step in run.steps
        ),
        current_phase="verify",
    )

    with pytest.raises(RuntimeError, match="expected card mismatch"):
        _retry_card(
            tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


# ==========================================================================
# 段 2：dispatch 真的產生新 job，且 prompt 走現行 `_workflow_job_prompt`
# ==========================================================================


class _Launcher:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._sink.append(prompt)
        return LaunchHandle(
            executor="codex",
            model_id="gpt-primary",
            session_name=slice_id,
            pid=100,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )


def test_forced_retry_dispatches_a_new_job_for_the_midchain_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重派產生的必須是**新** job；舊 job 原樣保留，prompt 是原卡的指示。"""

    snapshot, registry, run, old_job_id = _stuck_run(tmp_path)
    # #541：canonical gate 名由宣告機械導出並注入 prompt——這正是重派能產出
    # 可採信 envelope 的原因。
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -m pytest -q")
    _retry_card(
        tmp_path, snapshot, registry, expected_run_id=run.run_id, card="tdd-red"
    )
    prompts: list[str] = []

    replacement = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=registry.get_workflow_run(run.run_id),
        identities=_identities(),
        launcher_factory=lambda _: _Launcher(prompts),
        coordinator_root=tmp_path / "coordinator",
        force_new_card=True,
    )

    assert replacement is not None
    assert replacement["job_id"] != old_job_id
    assert replacement["workflow_card"] == "tdd-red"
    assert registry.get_job(old_job_id)["status"] == "exited"
    # prompt 是原卡的 RED 指示，不是 candidate repair 文案。
    assert len(prompts) == 1
    assert "RED regression test" in prompts[0]
    assert "Repair the exact Candidate" not in prompts[0]
    # #541 的 allowed_names 注入必須出現在這條（唯一的）prompt 組裝路徑上。
    assert '"allowed_names"' in prompts[0]
    assert "pytest" in prompts[0]


def test_retry_build_final_card_semantics_do_not_regress(tmp_path: Path) -> None:
    """既有「最後一張卡」語意的呼叫端不得因本 issue 改動而破。"""

    _plan_file(tmp_path)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    steps = tuple(
        replace(
            step,
            gate_result="passed"
            if step.phase == "build" and step.card != "subagent-build"
            else step.gate_result,
        )
        for step in _manifest_steps()
    )
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=(),
        openspec_refs=(),
        candidate_head=HEAD,
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="failed",
        needs_human_reason=fixture_needs_human_reason(),
    )
    unbound = registry.create_job(
        task="wf-subagent-build",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card="subagent-build",
        workflow_phase="build",
    )
    registry.update_headless_result(unbound["job_id"], status="exited", exit_code=0)

    updated = registry._manager_reset_workflow_for_retry_build(
        run.run_id,
        expected_candidate=HEAD,
        repair_action="Repair the exact Candidate after a builder terminalization failure.",
    )

    assert "needs_human" not in updated.facets
    target = next(step for step in updated.steps if step.card == "subagent-build")
    assert target.action.startswith("Repair the exact Candidate")


def test_retry_card_reset_refuses_the_final_card_after_state_drift(tmp_path: Path) -> None:
    """registry 層的原子重驗：work action 通過後狀態若漂移，reset 仍 fail closed。"""

    _snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    # 模擬「另一條路徑先把 tdd-red 標成 passed」的競態。
    registry._manager_update_workflow_run(
        run.run_id,
        steps=tuple(
            replace(step, gate_result="passed")
            if step.card == "tdd-red"
            else step
            for step in run.steps
        ),
    )

    with pytest.raises(ValueError, match="earliest un-accepted builder card"):
        registry._manager_reset_workflow_for_retry_card(
            run.run_id, expected_run_id=run.run_id, card="tdd-red"
        )
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


# ==========================================================================
# 段 3：daemon wiring 與 facet 原子性
# ==========================================================================


def _daemon_executor(tmp_path: Path, registry: JobRegistry, run, **kwargs):
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
        **kwargs,
    )


def _retry_card_request(run):
    return build_request(
        req_type="work-action",
        args={
            "action": "retry-card",
            "repo": run.repo,
            "work_id": run.work_id,
            "expected_run_id": run.run_id,
            "card": "tdd-red",
        },
        requested_by="operator",
    )


def test_public_work_retry_card_forces_one_new_manager_dispatched_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    calls: list[bool] = []

    def forced_dispatch(*args, **kwargs):
        calls.append(kwargs.get("force_new_card"))
        return {"job_id": "replacement-builder"}

    monkeypatch.setattr(manager, "dispatch_workflow_card", forced_dispatch)
    executor = _daemon_executor(tmp_path, registry, run)

    result = executor(_retry_card_request(run))

    assert calls == [True]
    assert result["result"]["job_id"] == "replacement-builder"


def test_public_work_retry_card_restores_needs_human_when_dispatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不得出現「facet 清了但沒派出去」的中間態。"""

    _snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=(), gate_status="running")
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("launcher down")),
    )
    executor = _daemon_executor(tmp_path, registry, run)

    with pytest.raises(RuntimeError, match="launcher down"):
        executor(_retry_card_request(run))

    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_public_work_retry_card_fails_when_dispatch_produces_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, registry, run, _job_id = _stuck_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=(), gate_status="running")
    monkeypatch.setattr(manager, "dispatch_workflow_card", lambda *a, **k: None)
    executor = _daemon_executor(tmp_path, registry, run)

    with pytest.raises(RuntimeError, match="retry-card produced no builder Job"):
        executor(_retry_card_request(run))

    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


# ==========================================================================
# 段 4：contract／CLI 登錄（比照 #540 的 regenerate-gates 註冊模式）
# ==========================================================================


def test_control_contract_accepts_retry_card_with_run_cas_and_card() -> None:
    assert "retry-card" in control_contract.WORK_ACTIONS

    base = {
        "schema_version": control_contract.constants.SCHEMA_VERSION,
        "type": "work-action",
        "req_id": "r1",
        "requested_by": "operator",
        "created_at": control_contract.utcnow(),
    }
    ok = dict(
        base,
        args={
            "action": "retry-card",
            "repo": REPO,
            "work_id": WORK_ID,
            "expected_run_id": "workflow-" + "a" * 20,
            "card": "tdd-red",
        },
    )
    control_contract.validate_request(ok)

    with pytest.raises(ValueError, match="requires exact expected_run_id"):
        control_contract.validate_request(
            dict(
                base,
                args={
                    "action": "retry-card",
                    "repo": REPO,
                    "work_id": WORK_ID,
                    "card": "tdd-red",
                },
            )
        )
    with pytest.raises(ValueError, match="requires exact card id"):
        control_contract.validate_request(
            dict(
                base,
                args={
                    "action": "retry-card",
                    "repo": REPO,
                    "work_id": WORK_ID,
                    "expected_run_id": "workflow-" + "a" * 20,
                },
            )
        )
    with pytest.raises(ValueError, match="requires exact card id"):
        control_contract.validate_request(
            dict(
                base,
                args={
                    "action": "retry-card",
                    "repo": REPO,
                    "work_id": WORK_ID,
                    "expected_run_id": "workflow-" + "a" * 20,
                    "card": "TDD Red",
                },
            )
        )


def test_every_entrypoint_registers_retry_card() -> None:
    parser = coordinator_cli._build_parser()
    args = parser.parse_args(
        [
            "work", "retry-card", WORK_ID, "--repo", REPO, "--actor", "operator",
            "--expected-run-id", "workflow-" + "a" * 20, "--card", "tdd-red",
        ]
    )
    assert args.action == "retry-card"
    assert args.card == "tdd-red"

    porcelain_parser = porcelain_recover._build_parser()
    recover_args = porcelain_parser.parse_args(
        [
            "work", WORK_ID, "retry-card", "--repo", REPO, "--actor", "operator",
            "--expected-run-id", "workflow-" + "a" * 20, "--card", "tdd-red",
        ]
    )
    assert porcelain_recover._work_args(recover_args)["card"] == "tdd-red"

    assert "retry-card" in umbrella_cli._WORK_HELP


def test_coordinator_cli_forwards_card_to_the_control_request() -> None:
    submitted: list[tuple[str, dict, str]] = []

    rc = coordinator_cli.main(
        [
            "work", "retry-card", WORK_ID, "--repo", REPO, "--actor", "operator",
            "--expected-run-id", "workflow-" + "a" * 20, "--card", "tdd-red",
        ],
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda kind, args, actor: submitted.append(
            (kind, args, actor)
        )
        or "req-retry-card",
        control_poll_done=lambda *_args, **_kwargs: {"status": "ok", "result": {}},
    )

    assert rc == 0
    assert submitted[0][0] == "work-action"
    assert submitted[0][1] == {
        "action": "retry-card",
        "repo": REPO,
        "work_id": WORK_ID,
        "actor": "operator",
        "expected_run_id": "workflow-" + "a" * 20,
        "card": "tdd-red",
    }


# ==========================================================================
# 段 5：#546（部分）——needs_human 的 next_actions 曝光面
# ==========================================================================


def test_build_recovery_actions_surface_only_admissible_actions(tmp_path: Path) -> None:
    _snapshot_path, registry, run, job_id = _stuck_run(tmp_path)

    exposed = work_actions._phase_recovery_actions(
        registry.get_workflow_run(run.run_id), registry
    )

    assert "retry-card" in exposed
    assert "regenerate-gates" in exposed

    # 已採信的卡不得再宣告 retry-card——宣告一個保證失敗的動作比不宣告更糟。
    registry.bind_workflow_evidence(
        job_id,
        locator={"kind": "workflow-build-result", "path": "evidence/tdd-red.json", "hash": "e" * 64},
        subject_head=HEAD,
    )
    assert "retry-card" not in work_actions._phase_recovery_actions(
        registry.get_workflow_run(run.run_id), registry
    )


def test_build_recovery_actions_stay_empty_without_needs_human(tmp_path: Path) -> None:
    _snapshot_path, registry, run, _job_id = _stuck_run(tmp_path)
    registry._manager_update_workflow_run(run.run_id, facets=())

    assert (
        work_actions._phase_recovery_actions(
            registry.get_workflow_run(run.run_id), registry
        )
        == ()
    )
