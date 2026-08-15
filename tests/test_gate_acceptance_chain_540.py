"""#540：tdd-red terminal 採信三段連鎖。

現場 run ``workflow-084f75e2178cf7547476``：builder 交付了合格的 RED commit，
terminal 採信卻連續撞上三個獨立缺陷，run 停在 needs_human，正確的工作成果無法
被採信。本檔逐段釘住修復：

1. **gate 宣告缺漏事前無診斷。** manager env 漏 ``PSC_GATE_CMD_PYTEST`` 時，job
   結束寫出的 ledger 是 ``gates: []``，帶 ``test_policy`` 的 build 卡必然以
   ``gate-ledger-missing-expected-gate`` fail closed（正確的反自證行為），但
   operator 只能在 builder 跑完之後從 ``manager.log`` 發現。``cortex doctor``
   的 ``gate-declarations`` probe 把它改成開工前的 required fail。
2. **ledger 凍結後無官方重驗路徑。** ledger 是 job 結束當下依當時 env 生成的
   檔案；env 修好之後 ``resume`` 只重讀舊 ledger 再拒一次，``retry-build`` 只
   受理最後一張 builder 卡（tdd-red 是中段卡），``recover-pre-candidate`` 要求
   null candidate。新增 ``regenerate-gates`` work action：依當前宣告重跑 gate、
   重寫 ledger，然後就結束——不改判、不重派 builder、不動任何 run 狀態。
3. **【主修】dispatch prompt 從未告訴模型 canonical gate 名稱。** 採信要求
   envelope 自報的 gate 名 ⊆ ledger 的 gate 名（由 ``PSC_GATE_CMD_<NAME>`` 導
   出），prompt 卻只寫 "gate name"，模型自由發揮寫了
   ``'focused pytest RED expectation'``。比照 #521：prompt 內的可用值由判準機械
   生成，不手寫。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex import doctor
from paulsha_cortex.control import contract as control_contract
from paulsha_cortex.coordinator import gate_ledger, manager, work_actions
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


# ==========================================================================
# 段 1：doctor 前置驗證 gate 宣告存在且非空
# ==========================================================================


def test_gate_declaration_probe_fails_when_required_gate_undeclared() -> None:
    """RED（修復前）：完全沒有 ``PSC_GATE_CMD_*`` 時 doctor 一聲不吭。

    deck 的 tdd-red／subagent-build 卡宣告了 ``test_policy``，harvest 端因此會
    要求 ledger 有 ``pytest``；宣告缺席就是保證所有 build 卡 fail closed。
    """

    probe = doctor._gate_declaration_probe({"HOME": "/home/example"})

    assert probe.status == "fail"
    assert probe.required is True
    assert "pytest" in probe.detail
    # 訊息必須直接可操作：缺哪個 gate、變數名怎麼寫。
    assert "PSC_GATE_CMD_PYTEST" in probe.detail
    assert "gate-ledger-missing-expected-gate" in probe.detail


def test_gate_declaration_probe_passes_when_declaration_covers_deck() -> None:
    probe = doctor._gate_declaration_probe(
        {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}
    )

    assert probe.status == "pass"
    assert "pytest" in probe.detail


def test_gate_declaration_probe_fails_on_invalid_declaration() -> None:
    """宣告不合法時 ledger 退化成單一 failed 項，同樣必須事前擋下。"""

    probe = doctor._gate_declaration_probe(
        {"PSC_GATE_CMD_PYTEST": "bash -c 'python3 -m pytest -q'"}
    )

    assert probe.status == "fail"
    assert probe.required is True
    assert "invalid" in probe.detail


def test_deck_required_gate_names_uses_same_judge_as_harvest() -> None:
    """doctor 的應驗 gate 集合必須與 harvest 端同一個判準導出，不得另立一套。"""

    required = doctor._deck_required_gate_names()

    assert required == frozenset({tc.RED_REQUIRED_TEST_GATE_NAME})
    assert required == tc.expected_gate_names_for_test_policy("red-required")
    # manager 端的既有入口仍指向同一個實作。
    assert manager._expected_gate_names_for_test_policy("focused") == required


# ==========================================================================
# 段 3（主修）：dispatch prompt 機械生成 canonical gate 名稱
# ==========================================================================


_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


def _step(
    phase: str,
    card: str,
    *,
    gate_result: str = "pending",
    test_policy: str | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
        test_policy=test_policy,
    )


def _build_run(registry: JobRegistry, tmp_path: Path, *, test_policy: str):
    return registry._manager_create_workflow_run(
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=(_step("build", "tdd-red", test_policy=test_policy),),
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        facets=(),
        gate_status="running",
    )


def _terminal_schema(prompt: str) -> dict:
    return json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])["terminal_schema"]


def test_dispatch_prompt_declares_canonical_gate_names(tmp_path: Path) -> None:
    """RED（修復前）：prompt 只寫 "gate name"，canonical 集合從未進 prompt。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _build_run(registry, tmp_path, test_policy="red-required")
    step = run.steps[0]

    prompt = manager._workflow_job_prompt(
        run,
        step,
        builder_job_id=None,
        coordinator_root=tmp_path,
        env={
            "PSC_GATE_CMD_PYTEST": "python3 -m pytest -q",
            "PSC_GATE_CMD_OPENSPEC": "openspec validate --strict",
        },
    )
    schema = _terminal_schema(prompt)

    assert schema["gate_evidence"]["allowed_names"] == ["openspec", "pytest"]
    description = schema["gate_evidence"]["description"]
    assert '"pytest"' in description and '"openspec"' in description
    # 現場實際被拒的那個自由發揮名稱，必須在 prompt 內被明確標為反例。
    assert "focused pytest RED expectation" in description


def test_dispatch_prompt_gate_names_track_the_declaration(tmp_path: Path) -> None:
    """機械生成而非手寫：宣告改動自動同步進 prompt（#521 原則）。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _build_run(registry, tmp_path, test_policy="focused")
    step = run.steps[0]

    env = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q", "PSC_GATE_CMD_POLICY": "python3 -m policy_check"}
    schema = _terminal_schema(
        manager._workflow_job_prompt(
            run, step, builder_job_id=None, coordinator_root=tmp_path, env=env
        )
    )
    assert schema["gate_evidence"]["allowed_names"] == ["policy", "pytest"]
    assert list(gate_ledger.declared_gate_names(env)) == ["policy", "pytest"]

    empty = _terminal_schema(
        manager._workflow_job_prompt(
            run, step, builder_job_id=None, coordinator_root=tmp_path, env={}
        )
    )
    assert empty["gate_evidence"]["allowed_names"] == []
    assert "must be exactly []" in empty["gate_evidence"]["description"]


def test_prompt_gate_names_are_exactly_what_authorize_terminal_accepts(
    tmp_path: Path,
) -> None:
    """把 prompt 的 enum 與採信判準綁在一起：照 prompt 寫就過，自由發揮就被拒。

    這是本票的核心——過去兩者之間沒有任何機械連結。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _build_run(registry, tmp_path, test_policy="red-required")
    step = run.steps[0]
    env = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}
    schema = _terminal_schema(
        manager._workflow_job_prompt(
            run, step, builder_job_id=None, coordinator_root=tmp_path, env=env
        )
    )
    allowed = schema["gate_evidence"]["allowed_names"]

    log = tmp_path / "job.jsonl"
    log.write_text("", encoding="utf-8")
    ledger = tc.gate_ledger_path(log)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                "kind": tc.GATE_LEDGER_KIND,
                "slice_id": "s",
                # tdd-red 的合格 RED：pytest 如預期失敗（exit code 1）。
                "gates": [
                    {"name": "pytest", "status": "failed", "exit_code": 1, "detail": ""}
                ],
            }
        ),
        encoding="utf-8",
    )

    def _envelope(gate_name: str):
        return tc.validate_envelope(
            {
                "schema_version": tc.TERMINAL_SCHEMA_VERSION,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": step.card,
                "candidate": "a" * 40,
                "outputs": [],
                "diagnostics": {},
                "gate_evidence": [{"name": gate_name, "status": "failed"}],
            }
        )

    authorized = tc.authorize_terminal(
        _envelope(allowed[0]),
        ledger_path=ledger,
        require_ledger=True,
        test_policy="red-required",
        expected_gate_names=tc.expected_gate_names_for_test_policy("red-required"),
    )
    assert authorized.authorized is True

    with pytest.raises(tc.TerminalContractError) as excinfo:
        tc.authorize_terminal(
            _envelope("focused pytest RED expectation"),
            ledger_path=ledger,
            require_ledger=True,
            test_policy="red-required",
            expected_gate_names=tc.expected_gate_names_for_test_policy("red-required"),
        )
    assert excinfo.value.reason == "gate-evidence-unknown-gate"


def test_red_required_card_prompt_states_the_inverted_semantics(tmp_path: Path) -> None:
    """red-required 卡：泛用 status_policy 字面上要求回 failed，與採信規則相反。

    #307 的反轉判準過去只存在於 manager 側；模型看到的 prompt 沒有任何線索。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _build_run(registry, tmp_path, test_policy="red-required")
    env = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}

    red = _terminal_schema(
        manager._workflow_job_prompt(
            run, run.steps[0], builder_job_id=None, coordinator_root=tmp_path, env=env
        )
    )
    assert "red_required_policy" in red
    assert tc.RED_REQUIRED_TEST_GATE_NAME in red["red_required_policy"]
    assert str(tc.PYTEST_EXIT_TESTS_FAILED) in red["red_required_policy"]

    # 一般卡完全不受影響。
    plain_run = _build_run(
        JobRegistry(state_path=tmp_path / "registry2.json"), tmp_path, test_policy="focused"
    )
    plain = _terminal_schema(
        manager._workflow_job_prompt(
            plain_run,
            plain_run.steps[0],
            builder_job_id=None,
            coordinator_root=tmp_path,
            env=env,
        )
    )
    assert "red_required_policy" not in plain


# ==========================================================================
# 段 2：regenerate-gates recovery action
# ==========================================================================


HEAD = "b" * 40


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
                        "work_id": "demo",
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


def _stuck_run(tmp_path: Path):
    """重建現場：needs_human 的 build phase run + 一個已終止的 builder job，
    ledger 是漏宣告時代留下的空 ledger。"""

    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo", work_id="demo", snapshot_path=snapshot
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
        steps=(
            _step("build", "worktree-isolation", gate_result="passed", test_policy="none"),
            _step("build", "tdd-red", test_policy="red-required"),
        ),
        issue_refs=tuple(f"{authority.repo}#{n}" for n in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        candidate_head=HEAD,
        facets=("needs_human",),
        gate_status="failed",
        needs_human_reason=fixture_needs_human_reason(),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log = tmp_path / "logs" / "workflow" / "demo-1.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("{}\n", encoding="utf-8")
    job = registry.create_job(
        task="demo",
        persona="builder",
        branch="feature/demo",
        pane="",
        worktree=str(worktree),
        workflow_run_id=run.run_id,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_test_policy="red-required",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    ledger = tc.gate_ledger_path(log)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                "kind": tc.GATE_LEDGER_KIND,
                "slice_id": "",
                # 漏宣告時代的空 ledger——builder 的成果因此無法被採信。
                "gates": [],
            }
        ),
        encoding="utf-8",
    )
    return snapshot, registry, run, ledger


def _regenerate(tmp_path: Path, snapshot: Path, registry: JobRegistry, **overrides):
    args = {
        "action": "regenerate-gates",
        "repo": "acme/demo",
        "work_id": "demo",
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


def test_regenerate_gates_rewrites_ledger_from_current_declaration(
    tmp_path: Path, monkeypatch
) -> None:
    """RED（修復前）：空 ledger 一旦生成即凍結，契約內沒有任何重生成路徑。"""

    snapshot, registry, run, ledger = _stuck_run(tmp_path)
    assert json.loads(ledger.read_text(encoding="utf-8"))["gates"] == []
    # operator 補上宣告（現場的實際修法）；gate 本身在此以確定性命令代表 RED。
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c raise\\ SystemExit(1)")

    result = _regenerate(tmp_path, snapshot, registry, expected_run_id=run.run_id)["result"]

    assert result["action"] == "regenerate-gates"
    assert result["reason"] == "gate-ledger-regenerated"
    assert result["card_id"] == "tdd-red"
    assert [row["name"] for row in result["gates"]] == ["pytest"]
    assert result["next_actions"] == ["resume"]

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert [row["name"] for row in payload["gates"]] == ["pytest"]
    assert payload["gates"][0]["exit_code"] == 1
    assert result["ledger_digest"] == tc.gate_ledger_digest(payload)


def test_regenerate_gates_does_not_change_the_verdict(tmp_path: Path, monkeypatch) -> None:
    """只重生成獨立證據，採信仍走既有流程——run 狀態一律不動。"""

    snapshot, registry, run, _ledger = _stuck_run(tmp_path)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")
    before = registry.get_workflow_run(run.run_id).to_dict()

    _regenerate(tmp_path, snapshot, registry, expected_run_id=run.run_id)

    after = registry.get_workflow_run(run.run_id).to_dict()
    assert after == before
    assert "needs_human" in registry.get_workflow_run(run.run_id).facets


def test_regenerate_gates_requires_needs_human(tmp_path: Path, monkeypatch) -> None:
    snapshot, registry, run, _ledger = _stuck_run(tmp_path)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")
    registry._manager_update_workflow_run(run.run_id, facets=())

    with pytest.raises(RuntimeError, match="requires needs_human workflow"):
        _regenerate(tmp_path, snapshot, registry, expected_run_id=run.run_id)


def test_regenerate_gates_requires_exact_run_cas(tmp_path: Path, monkeypatch) -> None:
    snapshot, registry, _run, ledger = _stuck_run(tmp_path)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")

    with pytest.raises(RuntimeError, match="CAS mismatch"):
        _regenerate(
            tmp_path, snapshot, registry, expected_run_id="workflow-" + "0" * 20
        )
    # fail closed：拒絕時不得留下任何 side effect。
    assert json.loads(ledger.read_text(encoding="utf-8"))["gates"] == []


def test_regenerate_gates_requires_an_existing_job_log(tmp_path: Path, monkeypatch) -> None:
    snapshot, registry, run, _ledger = _stuck_run(tmp_path)
    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -c pass")
    job = next(
        item for item in registry.list_jobs() if item.get("workflow_run_id") == run.run_id
    )
    Path(job["log_path"]).unlink()

    with pytest.raises(RuntimeError, match="terminal builder job log"):
        _regenerate(tmp_path, snapshot, registry, expected_run_id=run.run_id)


def test_regenerate_gates_rejects_caller_supplied_evidence(tmp_path: Path) -> None:
    snapshot, registry, run, _ledger = _stuck_run(tmp_path)

    with pytest.raises(ValueError, match="rejects caller evidence/input"):
        _regenerate(
            tmp_path,
            snapshot,
            registry,
            expected_run_id=run.run_id,
            expected_candidate=HEAD,
        )


def test_control_contract_accepts_regenerate_gates_with_exact_run_cas() -> None:
    """控制佇列是所有入口的收斂點：新動作必須在此被承認且 fail-closed 驗參。"""

    assert "regenerate-gates" in control_contract.WORK_ACTIONS

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
            "action": "regenerate-gates",
            "repo": "acme/demo",
            "work_id": "demo",
            "expected_run_id": "workflow-" + "a" * 20,
        },
    )
    control_contract.validate_request(ok)

    missing = dict(
        base,
        args={"action": "regenerate-gates", "repo": "acme/demo", "work_id": "demo"},
    )
    with pytest.raises(ValueError, match="requires exact expected_run_id"):
        control_contract.validate_request(missing)
