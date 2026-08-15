from __future__ import annotations

from dataclasses import replace
import json
import hashlib
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import (
    manager, manager_daemon, planning_runtime, registry as registry_module, review,
    terminal_contract, verification, work_actions, work_bridge,
)
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import (
    AGY_LIVE_PROBE,
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
)
from paulsha_cortex.coordinator.planning import BrainstormResult, PlanningGateRefs
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowManifest,
    WorkflowRun,
    WorkflowStep,
)
from paulsha_cortex.deck.compile import compile_combo, emit
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _gate_ledger_passed(log_path, *, gates: list[dict[str, object]] | None = None) -> None:
    """#261：模擬 manager wrapper 在模型行程結束後寫下的 gate ledger。

    真實流程中這份檔案由 `launcher` 產生的 wrapper script 呼叫
    `paulsha_cortex.coordinator.gate_ledger` 寫出，模型碰不到；沒有它的話
    build／verify 的 `passed` 會（正確地）因為缺乏獨立 gate 證據而 fail closed。

    #379：預設仍是空 gate 清單（維持既有多數呼叫端模擬「operator 未宣告
    PSC_GATE_CMD_*」的情境）；呼叫端如果在模擬一張 test_policy 非
    none／null 的卡片（例如 tdd-red／subagent-build），必須顯式傳入
    ``gates`` 帶上對應的 pytest 條目——否則 manager 現在會（正確地）因為
    plan 宣告的應驗 gate 沒出現在 ledger 而 fail closed，而不是像修復前那樣
    vacuous pass。
    """

    path = terminal_contract.gate_ledger_path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
                "kind": "workflow-gate-ledger",
                "slice_id": Path(log_path).stem,
                "gates": gates if gates is not None else [],
            }
        ),
        encoding="utf-8",
    )


def _manifest() -> WorkflowManifest:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "production wiring", change="production-wiring")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _assert_planning_failure_evidence_recoverable(
    *,
    coordinator_root: Path,
    persisted: WorkflowRun,
    classification: str,
    reason: str,
) -> None:
    """issue #393 回歸樁：define needs_human 靜默失敗必須留下
    `cortex-planning-failure/v1` evidence，且該 evidence 要能被
    `work_actions._read_planning_failure_record`／`_planning_failure_hint`
    讀到——這正是 `recover-planning` 的前置成立條件。過去全庫只有 reader、
    沒有任何 producer，這條斷言在修復前必然 FAIL（RuntimeError: recover-
    planning requires planning failure evidence／`_planning_failure_hint`
    回 None）。
    """

    evidence_dir = coordinator_root / "evidence" / "planning-recovery"
    evidence_paths = sorted(evidence_dir.glob(f"{persisted.run_id}-*.json"))
    assert evidence_paths, f"未見 planning-recovery evidence，目錄內容：{list(evidence_dir.glob('*'))}"
    assert len(evidence_paths) == 1
    evidence_path = evidence_paths[0]
    body = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-planning-failure/v1"
    assert body["run_id"] == persisted.run_id
    assert body["classification"] == classification
    assert body["reason"] == reason
    assert isinstance(body.get("created_at"), str) and body["created_at"]
    assert str(evidence_path) in persisted.evidence_refs

    record = work_actions._read_planning_failure_record(run=persisted, run_id=persisted.run_id)
    assert record["classification"] == classification
    assert record["reason"] == reason
    assert record["evidence_ref"] == str(evidence_path)

    hint = work_actions._planning_failure_hint(persisted)
    assert hint is not None
    assert hint["classification"] == classification


def _done_ship_run(registry: JobRegistry, root: Path) -> WorkflowRun:
    candidate = "a" * 40
    bindings = {
        "manager": ("cortex-manager", "deterministic", "cortex"),
        "planner": ("agy", "planner", "google"),
        "builder": ("codex", "builder", "openai"),
        "reviewer": ("claude", "reviewer", "anthropic"),
    }
    steps = tuple(
        replace(
            step,
            executor=bindings[step.persona][0],
            model=bindings[step.persona][1],
            domain=bindings[step.persona][2],
            gate_result="passed",
        )
        for step in _manifest().steps
    )
    run = registry._manager_create_workflow_run(
        work_id="terminal-refresh",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(root),
        combo="feature-oneshot",
        current_phase="ship",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#31",),
        openspec_refs=("terminal-refresh",),
        pr_refs=("hamanpaul/paulsha-cortex#54",),
        attempts={"ship": 1},
        gate_refs=(
            GateEvidenceRef("foreign-review", "evidence/foreign.json", "3" * 64),
            GateEvidenceRef("copilot", "github:copilot/54", "4" * 64),
        ),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="passed",
    )
    for card in ("openspec-archive", "policy-commit"):
        work_bridge._record_manager_ship_job(
            registry=registry,
            state_root=root,
            run=run,
            worktree=root,
            branch="feature/terminal-refresh",
            card=card,
            old_head=candidate,
            new_head=candidate,
        )
    run = registry._manager_update_workflow_run(
        run.run_id,
        status="done",
        completion_record_path=str(root / "evidence/completion-old.json"),
        completion_record_hash="5" * 64,
        completion_record_revision=candidate,
        completion_source_revisions={"github_pr:repo#54": "state:open"},
        pr_candidate=candidate,
        merge_revision="6" * 40,
    )
    (root / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "ship": {
                            "phase": "done",
                            "head": candidate,
                            "merge_commit": "6" * 40,
                            "merge_authorization": {
                                "path": "/evidence/authorization.json",
                                "hash": "7" * 64,
                                "payload": {
                                    "run_id": run.run_id,
                                    "repo": run.repo,
                                    "work_id": run.work_id,
                                    "head": candidate,
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return registry.get_workflow_run(run.run_id)


def test_feature_oneshot_manifest_has_monotonic_complete_spine_and_foreign_review_before_ship() -> None:
    manifest = _manifest()
    phases = [step.phase for step in manifest.steps]
    order = {phase: index for index, phase in enumerate(("claim", "define", "plan", "build", "verify", "review", "ship"))}

    assert phases[0] == "claim"
    assert set(phases) == set(order)
    assert [order[phase] for phase in phases] == sorted(order[phase] for phase in phases)
    first_ship = phases.index("ship")
    assert any(step.phase == "review" and step.persona == "reviewer" for step in manifest.steps[:first_ship])

    without_reviewer = WorkflowManifest(
        combo=manifest.combo,
        task_slug=manifest.task_slug,
        steps=tuple(step for step in manifest.steps if step.phase != "review"),
    )
    with pytest.raises(ValueError, match="完整 phase spine|reviewer"):
        without_reviewer.validate_manager_spine()


def test_deck_emit_persists_round_trippable_workflow_manifest(tmp_path: Path) -> None:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "durable manifest", change="durable-manifest")

    written = emit(result, tmp_path)
    manifest_path = tmp_path / "durable-manifest.workflow.json"

    assert manifest_path in written
    assert WorkflowManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8"))) == result.workflow_manifest


def _workflow_args(manifest_path: Path, artifact_root: Path) -> dict[str, object]:
    return {
        "action": "start",
        "manifest_path": str(manifest_path),
        "work_id": "production-wiring",
        "repo": "hamanpaul/paulsha-cortex",
        "claim_key": "hamanpaul/paulsha-cortex/production-wiring/rev-a",
        "source_revision": "rev-a",
        "artifact_root": str(artifact_root),
        "planning_artifacts": [],
        "primary_executor": "codex",
        "primary_model": "gpt-primary",
        "evidence_dir": str(artifact_root / "evidence"),
    }


def _write_planning_artifacts(root: Path, *, missing: set[str] | None = None) -> tuple[PlanningArtifactAuthority, ...]:
    missing = missing or set()
    proposal = root / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    authority: list[PlanningArtifactAuthority] = []
    for kind, body in bodies.items():
        # #414：plan 的 ref 必須落在 writing-plans 卡宣告的 canonical outputs
        # glob（`docs/superpowers/plans/*<task-slug>*.md`）內，否則
        # deterministic pass 前的 declared-outputs 驗證會判定缺席、觸發
        # materialize fallback，讓 `planner.outputs` 多出一筆——這裡的測試
        # 目的是驗證「outputs 已存在」的既有正路，故 ref 需真的匹配宣告。
        ref = (
            "docs/superpowers/plans/production-wiring.md"
            if kind == "plan"
            else f"docs/{kind}.md"
        )
        path = root / ref
        if kind not in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        else:
            digest = "0" * 64
        authority.append(
            PlanningArtifactAuthority(
                ref=ref,
                kind=kind,
                work_id="production-wiring",
                baseline_sha256=digest,
            )
        )
    return tuple(authority)


def test_control_queue_workflow_action_is_the_production_mutation_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    with caplog.at_level(logging.ERROR, logger="paulsha_cortex.coordinator.manager"):
        result = executor(build_request(req_type="workflow-action", args=_workflow_args(manifest_path, tmp_path), requested_by="operator"))

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.current_phase == "define"
    assert persisted.facets == ("needs_human",)
    assert result["reason"] == "planning-runtime-unavailable"
    assert not hasattr(registry, "create_workflow_run")
    assert not hasattr(registry, "update_workflow_run")
    # #391：daemon periodic tick 觸發時沒人消費回傳值，reason 過去只活在
    # return dict 裡就蒸發；needs_human 落地時必須同時留一筆可查的結構化 log
    # （run_id + reason），不能只靠呼叫端讀 return 值。
    assert any(
        "planning-runtime-unavailable" in record.message and persisted.run_id in record.message
        for record in caplog.records
    )
    # issue #393：這條 needs_human 出口過去沒有任何 producer 寫
    # `cortex-planning-failure/v1` evidence，recover-planning 對它結構性
    # 不可用；修復後必須留下可被 reader 消費的 evidence。
    _assert_planning_failure_evidence_recoverable(
        coordinator_root=tmp_path,
        persisted=persisted,
        classification="environment",
        reason="planning-runtime-unavailable",
    )


def test_planning_runtime_initialization_failure_logs_reason_and_records_needs_human(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """issue #391：runtime_factory 本身 raise（例如 sandbox 建立失敗）時，
    manager.apply_workflow_action 過去只把 exception 整個吞掉、reason 只塞進
    回傳值——daemon periodic tick 觸發時沒有呼叫端讀這個回傳值，reason 蒸發，
    只留下一個查不出原因的 needs_human facet。修復後必須落一筆含 run_id、
    reason、底層 exception 型別與訊息的結構化 log。
    """
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)

    # #507：factory 另收 `evidence_root`／`run_id`（drift 報告的 run-scoped 落點）。
    def failing_runtime_factory(*, primary, worktree, **_):
        raise RuntimeError("sandbox worktree creation refused")

    with caplog.at_level(logging.ERROR, logger="paulsha_cortex.coordinator.manager"):
        result = manager.apply_workflow_action(
            registry,
            args=args,
            runtime_factory=failing_runtime_factory,
            coordinator_root=tmp_path,
        )

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.current_phase == "define"
    assert persisted.facets == ("needs_human",)
    assert result["reason"] == "planning-runtime-initialization-failed"
    matching = [
        record
        for record in caplog.records
        if "planning-runtime-initialization-failed" in record.message
        and persisted.run_id in record.message
    ]
    assert matching, f"未見結構化 log，實際紀錄：{[r.message for r in caplog.records]}"
    assert "RuntimeError" in matching[0].message
    assert "sandbox worktree creation refused" in matching[0].message
    # issue #393：runtime_factory 例外路徑同樣要留 evidence；reason 併上
    # #392 已組好的字串與例外摘要。
    _assert_planning_failure_evidence_recoverable(
        coordinator_root=tmp_path,
        persisted=persisted,
        classification="environment",
        reason="planning-runtime-initialization-failed: RuntimeError: sandbox worktree creation refused",
    )


def test_brainstorm_not_ready_logs_reason_before_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """issue #391：run_heterogeneous_brainstorm 沒收斂到 ready 狀態時的
    needs_human 分支，比照另外兩條 runtime 缺失路徑，reason 也不能只活在
    回傳值裡——必須留一筆含 run_id／state／reason 的結構化 log。
    """
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "gpt-primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            }
        ]
    )
    monkeypatch.setattr(
        manager,
        "run_heterogeneous_brainstorm",
        lambda **_: BrainstormResult(
            state="needs_human",
            reason="no-heterogeneous-planner",
            secondary_domain=None,
            gate_refs=PlanningGateRefs(),
        ),
    )

    with caplog.at_level(logging.ERROR, logger="paulsha_cortex.coordinator.manager"):
        result = manager.apply_workflow_action(
            registry,
            args=args,
            identity_registry=identities,
            primary_questioner=lambda *a, **k: None,
            secondary_planner=lambda *a, **k: None,
            primary_integrator=lambda *a, **k: None,
            coordinator_root=tmp_path,
        )

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.current_phase == "define"
    assert persisted.facets == ("needs_human",)
    assert result["reason"] == "no-heterogeneous-planner"
    assert any(
        "no-heterogeneous-planner" in record.message and persisted.run_id in record.message
        for record in caplog.records
    )
    # issue #393：brainstorm 未 ready 歸類 content（非 environment）——
    # `_resume_decision` 對 content 一律不浮現 recover-planning，
    # 與 fail-closed 意圖一致（見 test_planning_claim_recovery 的
    # test_resume_hides_recovery_for_content_failure）。
    _assert_planning_failure_evidence_recoverable(
        coordinator_root=tmp_path,
        persisted=persisted,
        classification="content",
        reason="no-heterogeneous-planner",
    )


@pytest.mark.parametrize(
    "reason",
    [
        "primary-artifact-write-rejected: ValueError: planning artifact lacks current "
        "planning authority: docs/superpowers/specs/fix-416-design.md",
        "primary-artifact-write-rejected: ValueError: planning artifact current authority "
        "drift: docs/superpowers/specs/fix-416-design.md",
    ],
)
def test_planning_authority_residue_write_rejection_is_environment_classified(reason: str) -> None:
    """issue #416（選做修法 3）：`_publish_planning_artifacts` 對「已存在但無/與
    目前 authority 不符」的檔案一律 fail-closed（見 manager.py
    `_publish_planning_artifacts` 的兩處 raise），正是 abandon 未回滾發佈殘留
    撞見下一世代重新發佈同一 destinations 的死鎖地雷特徵——屬環境／狀態殘留
    而非模型內容缺陷，`manager._is_planning_authority_residue_failure` 必須把
    它辨識出來，好讓呼叫端可以改歸 `environment`（進而讓
    recover-planning 可用）。"""

    assert manager._is_planning_authority_residue_failure(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "no-heterogeneous-planner",
        "question-pack-malformed: RuntimeError: questioner exploded",
        # #416 判準刻意窄：write-rejected 前綴存在，但底層錯誤不是 authority
        # 殘留（例如整合出的路徑逃出 governed roots），仍必須維持 content。
        "primary-artifact-write-rejected: ValueError: planning artifact path outside "
        "governed roots",
    ],
)
def test_planning_authority_residue_classifier_stays_content_for_unrelated_reasons(
    reason: str | None,
) -> None:
    """反向情境：非 authority 殘留特徵的失敗（含 write-rejected 但底層是其他
    內容型驗證錯誤）必須維持既有 `content` 分類，不擴大 #393 的分類映射。"""

    assert manager._is_planning_authority_residue_failure(reason) is False


def test_brainstorm_authority_residue_write_rejection_records_environment_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #416 端到端：`run_heterogeneous_brainstorm` 因 `_publish_planning_
    artifacts` 的 authority fail-closed 回 `primary-artifact-write-rejected:
    ValueError: planning artifact lacks current planning authority: ...` 時，
    `apply_workflow_action` 必須把 evidence 落成 `environment`（而不是 #393
    預設的 `content`），讓 `_planning_failure_hint`／`_read_planning_failure_
    record` 顯示可 recover-planning——修法前這條分支恆為 `content`，
    recover-planning 永遠不可用，只能改名重識別（issue 內文記載的短期實操）
    燒一個世代繞過。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "gpt-primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            }
        ]
    )
    residue_reason = (
        "primary-artifact-write-rejected: ValueError: planning artifact lacks current "
        "planning authority: docs/superpowers/specs/fix-416-design.md"
    )
    monkeypatch.setattr(
        manager,
        "run_heterogeneous_brainstorm",
        lambda **_: BrainstormResult(
            state="needs_human",
            reason=residue_reason,
            secondary_domain=None,
            gate_refs=PlanningGateRefs(),
        ),
    )

    result = manager.apply_workflow_action(
        registry,
        args=args,
        identity_registry=identities,
        primary_questioner=lambda *a, **k: None,
        secondary_planner=lambda *a, **k: None,
        primary_integrator=lambda *a, **k: None,
        coordinator_root=tmp_path,
    )

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.facets == ("needs_human",)
    assert result["reason"] == residue_reason
    _assert_planning_failure_evidence_recoverable(
        coordinator_root=tmp_path,
        persisted=persisted,
        classification="environment",
        reason=residue_reason,
    )


def test_public_work_resume_routes_through_phase_aware_poll_terminalize_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls: list[tuple[str, bool]] = []

    def phase_aware_resume(*args, **kwargs):
        calls.append((kwargs["run_id"], kwargs["operator_resume"]))
        return {
            "run_id": kwargs["run_id"],
            "current_phase": "build",
            "job_id": "new-build-job",
            "reason": "advanced",
        }

    monkeypatch.setattr(manager, "resume_workflow_run", phase_aware_resume)
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("public resume must not dispatch without polling terminal state")
        ),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "resume", "run": run.to_dict()},
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": run.repo, "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert calls == [(run.run_id, True)]
    assert result["result"]["current_phase"] == "build"
    assert result["result"]["job_id"] == "new-build-job"


def test_public_work_resume_preserves_define_retry_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="define",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"define": 1},
        facets=("needs_human",),
        brainstorm_required=True,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    monkeypatch.setattr(
        manager,
        "resume_workflow_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("define retry is completed by the canonical work starter")
        ),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {
                "action": "needs_human",
                "reason": "planning-runtime-initialization-failed",
                "run": run.to_dict(),
            },
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": run.repo, "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert result["result"]["reason"] == "planning-runtime-initialization-failed"
    assert result["result"]["run"]["current_phase"] == "define"
    assert result["result"]["run"]["facets"] == ["needs_human"]


def test_public_work_retry_build_forces_one_new_manager_dispatched_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls: list[bool] = []

    def forced_dispatch(*args, **kwargs):
        calls.append(kwargs.get("force_new_build"))
        return {"job_id": "repair-builder"}

    monkeypatch.setattr(manager, "dispatch_workflow_card", forced_dispatch)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "retry-build", "run": run.to_dict()},
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={
                "action": "retry-build",
                "repo": run.repo,
                "work_id": run.work_id,
                "expected_candidate": "a" * 40,
            },
            requested_by="operator",
        )
    )

    assert calls == [True]
    assert result["result"]["job_id"] == "repair-builder"


def test_plan_dispatch_passes_complete_planner_card_without_launch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo)
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
    )

    result = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    planner = next(step for step in persisted.steps if step.card == "writing-plans")

    assert result is None
    assert persisted.current_phase == "build"
    assert persisted.attempts == {"plan": 1, "build": 1}
    assert planner.gate_result == "passed"
    assert (planner.executor, planner.model, planner.domain) == (
        "cortex-manager",
        "deterministic",
        "cortex",
    )
    assert planner.outputs == tuple(item.ref for item in authority)
    assert registry.list_jobs() == []


def test_plan_dispatch_launches_planner_when_artifacts_are_incomplete(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo, missing={"plan"})
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
    )
    launched: list[str] = []

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(slice_id)
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    job = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }]
        ),
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)

    assert job is not None
    assert job["workflow_card"] == "writing-plans"
    assert launched == [job["job_id"]]
    assert persisted.current_phase == "plan"
    assert next(step for step in persisted.steps if step.card == "writing-plans").gate_result == "pending"
    assert len(registry.list_jobs()) == 1


def test_forced_retry_build_dispatches_new_job_after_prior_success(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "docs/superpowers/plans/production-wiring.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Repair plan\n", encoding="utf-8")
    steps = tuple(
        replace(
            step,
            gate_result=(
                "pending"
                if step.phase == "build" and step.card == "subagent-build"
                else "passed" if step.phase == "build" else step.gate_result
            ),
            action=(
                "Repair exact Candidate and commit a tested descendant."
                if step.phase == "build" and step.card == "subagent-build"
                else step.action
            ),
        )
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    old = registry.create_job(
        task="wf-old-subagent-build",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head="a" * 40,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(old["job_id"], status="exited", exit_code=0)
    launched: list[str] = []

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(prompt)
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    replacement = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }]
        ),
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        force_new_build=True,
    )

    assert replacement["job_id"] != old["job_id"]
    assert replacement["dispatch_head"] == old["dispatch_head"]
    assert "Repair exact Candidate" in launched[0]


def test_public_work_retry_build_restores_needs_human_when_dispatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 2},
        candidate_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "retry-build", "run": run.to_dict()},
        },
    )

    with pytest.raises(RuntimeError, match="launch failed"):
        executor(
            build_request(
                req_type="work-action",
                args={
                    "action": "retry-build",
                    "repo": run.repo,
                    "work_id": run.work_id,
                    "expected_candidate": "a" * 40,
                },
                requested_by="operator",
            )
        )

    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)


def test_periodic_resume_does_not_clear_needs_human_or_retry(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path,
    )

    assert result["reason"] == "operator-resume-required"
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.list_jobs() == []


def test_ship_validator_failure_persists_needs_human_on_review_complete_run(
    tmp_path: Path,
) -> None:
    steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase != "ship"
        else step
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"review": 1},
        candidate_head="a" * 40,
        verified_head="a" * 40,
        gate_status="running",
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(RuntimeError, match="preflight failed"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda _: None,
            coordinator_root=tmp_path,
            operator_resume=True,
            ship_validator=lambda **_: (_ for _ in ()).throw(
                RuntimeError("preflight failed")
            ),
        )

    stopped = registry.get_workflow_run(run.run_id)
    assert stopped.current_phase == "review"
    assert stopped.facets == ("needs_human",)
    assert stopped.gate_status == "failed"


def _rate_limited_ship_run(tmp_path: Path) -> tuple[JobRegistry, WorkflowRun]:
    steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase != "ship"
        else step
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"review": 1},
        candidate_head="a" * 40,
        verified_head="a" * 40,
        gate_status="running",
    )
    return registry, run


def test_ship_validator_rate_limit_does_not_raise_or_require_operator(tmp_path: Path) -> None:
    """#370: a canonical GitHub provider AuthorityValidationError classified
    as rate-limited (REASON_PROVIDER_RATE_LIMITED_CANONICAL, raised by
    `load_work_authority` inside a real `build_production_ship_validator`)
    must not behave like an arbitrary ship_validator failure -- it's a known
    transient condition that resolves itself, not something needing a human.
    Unlike `test_ship_validator_failure_persists_needs_human_on_review_complete_run`
    (an *unclassified* RuntimeError, which correctly still needs_human+raises),
    this must return a soft "provider-rate-limited" result and leave needs_human
    untouched -- so a plain periodic tick resume (no operator) naturally
    retries once the durable backoff clears, no human required."""
    from paulsha_cortex.coordinator.claim import (
        REASON_PROVIDER_RATE_LIMITED_CANONICAL,
        AuthorityValidationError,
    )

    registry, run = _rate_limited_ship_run(tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls = []

    def rate_limited_validator(**kwargs):
        calls.append(kwargs)
        raise AuthorityValidationError(
            "durable GitHub provider authority rate-limited",
            reason_code=REASON_PROVIDER_RATE_LIMITED_CANONICAL,
            repo=run.repo,
            work_id=run.work_id,
            provider_id=f"github:{run.repo}",
            field="status",
        )

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=rate_limited_validator,
    )

    assert len(calls) == 1
    assert result["reason"] == "provider-rate-limited"
    assert result["run_id"] == run.run_id
    assert isinstance(result.get("retry_after_epoch"), float)

    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in persisted.facets
    assert persisted.current_phase == "review"

    # Durable: the backoff deadline is now on disk under coordinator_root,
    # readable independently of anything resume_workflow_run kept in memory.
    from paulsha_cortex.coordinator import provider_backoff

    active = provider_backoff.active_backoff(tmp_path, f"github:{run.repo}", now=0.0)
    assert active is not None
    assert active.deadline_epoch == result["retry_after_epoch"]


def test_resume_before_backoff_deadline_short_circuits_without_calling_ship_validator(
    tmp_path: Path,
) -> None:
    """#370 acceptance: "operator resume 在 deadline 前給明確限流中訊息，
    而非立即重撞" -- a resume attempt made *before* a previously-recorded
    backoff deadline must not invoke ship_validator (and therefore not
    touch GitHub-derived authority) again at all."""
    import time

    from paulsha_cortex.coordinator import provider_backoff

    registry, run = _rate_limited_ship_run(tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    # Real wall-clock "now" -- resume_workflow_run's short-circuit check
    # compares the durable deadline against `time.time()`, not a fake clock.
    provider_backoff.record_backoff(tmp_path, f"github:{run.repo}", now=time.time())

    def must_not_be_called(**_kwargs):
        raise AssertionError("ship_validator must not be called before backoff deadline")

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=must_not_be_called,
    )

    assert result["reason"] == "provider-rate-limited"
    assert "retry_after_epoch" in result
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in persisted.facets


@pytest.mark.parametrize("terminal_phase", ["merged", "done"])
def test_post_merge_closure_skips_active_planning_path_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_phase: str,
) -> None:
    steps = tuple(
        WorkflowStep.from_dict({**step.to_dict(), "gate_result": "passed"})
        if step.phase != "ship"
        else step
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    candidate = "a" * 40
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=("hamanpaul/paulsha-cortex#17",),
        attempts={"review": 1},
        candidate_head=candidate,
        verified_head=candidate,
        facets=("needs_human",),
        gate_status="failed",
        brainstorm_required=True,
    )
    (tmp_path / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "ship": {
                            "phase": terminal_phase,
                            "head": candidate,
                            "merge_commit": "b" * 40,
                            "merge_authorization": {
                                "path": "/evidence/authorization.json",
                                "hash": "d" * 64,
                                "payload": {
                                    "run_id": run.run_id,
                                    "repo": run.repo,
                                    "work_id": run.work_id,
                                    "head": candidate,
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager,
        "_validated_brainstorm_planning_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("post-merge closure must not require active planning paths")
        ),
    )
    calls: list[str] = []
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=lambda **_: calls.append("ship")
        or {
            "trusted": True,
            "status": "pending",
            "head": candidate,
            "commit_id": candidate,
            "ref": "delivery:merged",
            "hash": "c" * 64,
        },
    )

    assert result["reason"] == "delivery-in-progress"
    assert calls == ["ship"]
    assert registry.get_workflow_run(run.run_id).facets == ()


def test_post_merge_closure_routing_rejects_incomplete_authorization(tmp_path: Path) -> None:
    run = SimpleNamespace(
        run_id="workflow-" + "1" * 20,
        repo="hamanpaul/paulsha-cortex",
        work_id="production-wiring",
        current_phase="review",
        candidate_head="a" * 40,
    )
    (tmp_path / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run.run_id: {
                        "run_id": run.run_id,
                        "repo": run.repo,
                        "work_id": run.work_id,
                        "ship": {
                            "phase": "merged",
                            "head": run.candidate_head,
                            "merge_commit": "b" * 40,
                            "merge_authorization": {
                                "path": "/evidence/authorization.json",
                                "hash": "c" * 64,
                                "payload": {
                                    "run_id": run.run_id,
                                    "repo": run.repo,
                                    "work_id": run.work_id,
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert not manager._merged_delivery_reconciliation_pending(
        run, coordinator_root=tmp_path
    )


def test_done_ship_resume_refreshes_completion_without_dispatch(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _done_ship_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    jobs_before = registry.list_jobs()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(
            AssertionError("terminal completion refresh must not launch a workflow card")
        ),
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=lambda **_: {
            "trusted": True,
            "status": "passed",
            "head": run.candidate_head,
            "commit_id": run.candidate_head,
            "ref": "evidence/maintainer-review.json",
            "hash": "8" * 64,
            "review_kind": "maintainer-review",
            "review_ref": "evidence/maintainer-review.json",
            "review_hash": "8" * 64,
            "completion": {
                "record_path": str(tmp_path / "evidence/completion-current.json"),
                "record_hash": "9" * 64,
                "record_revision": run.candidate_head,
                "source_revisions": {"github_pr:repo#54": "state:closed"},
                "pr_candidate": run.candidate_head,
                "merge_revision": "6" * 40,
            },
        },
    )

    refreshed = registry.get_workflow_run(run.run_id)
    assert result["reason"] == "completion-refreshed"
    assert refreshed.current_phase == "ship"
    assert refreshed.status == "done"
    assert refreshed.completion_record_path.endswith("completion-current.json")
    assert refreshed.completion_source_revisions == {
        "github_pr:repo#54": "state:closed"
    }
    assert {ref.kind for ref in refreshed.gate_refs} == {
        "foreign-review",
        "maintainer-review",
    }
    assert registry.list_jobs() == jobs_before


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [("pending", "delivery-in-progress"), ("needs_human", "closure-mismatch")],
)
def test_done_ship_resume_keeps_existing_completion_until_refresh_passes(
    tmp_path: Path,
    status: str,
    expected_reason: str,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _done_ship_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
        ship_validator=lambda **_: {
            "trusted": True,
            "status": status,
            "head": run.candidate_head,
            "commit_id": run.candidate_head,
            "ref": "github:copilot/54",
            "hash": "4" * 64,
            "reason": "closure-mismatch" if status == "needs_human" else None,
        },
    )

    unchanged = registry.get_workflow_run(run.run_id)
    assert result["reason"] == expected_reason
    assert unchanged.status == "done"
    assert unchanged.completion_record_path == run.completion_record_path
    assert unchanged.completion_record_hash == run.completion_record_hash


def test_done_ship_resume_rejects_malformed_completion_refresh(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _done_ship_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(ValueError, match="completion binding invalid"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda _: None,
            coordinator_root=tmp_path,
            operator_resume=True,
            ship_validator=lambda **_: {
                "trusted": True,
                "status": "passed",
                "head": run.candidate_head,
                "commit_id": run.candidate_head,
                "ref": "github:copilot/54",
                "hash": "4" * 64,
                "completion": {
                    "record_path": str(tmp_path / "completion-invalid.json"),
                    "record_hash": "too-short",
                    "record_revision": run.candidate_head,
                    "source_revisions": {"github_pr:repo#54": "state:closed"},
                    "pr_candidate": run.candidate_head,
                    "merge_revision": "6" * 40,
                },
            },
        )

    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.status == "done"
    assert unchanged.completion_record_path == run.completion_record_path
    assert unchanged.completion_record_hash == run.completion_record_hash


def test_public_work_resume_routes_done_ship_run_through_terminal_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _done_ship_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    calls: list[str] = []
    monkeypatch.setattr(
        manager,
        "resume_workflow_run",
        lambda *_args, **kwargs: calls.append(kwargs["run_id"])
        or {
            "run_id": kwargs["run_id"],
            "current_phase": "ship",
            "reason": "completion-refreshed",
        },
    )
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("done ship resume must not use normal dispatch")
        ),
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_ship_validator=lambda **_: None,
        work_action_fn=lambda **_: {
            "work_id": run.work_id,
            "repo": run.repo,
            "result": {"action": "resume", "run": run.to_dict()},
        },
    )

    result = executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": run.repo, "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert calls == [run.run_id]
    assert result["result"]["reason"] == "completion-refreshed"


def test_operator_resume_retries_bound_needs_human_terminal_without_rewriting_old_job(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "docs/superpowers/plans/production-wiring-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.card == "worktree-isolation" else step.gate_result,
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    log = tmp_path / "needs-human.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    old_job = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(old_job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(old_job["job_id"], status="exited", exit_code=0)

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    identities = IdentityRegistry.from_rows([{
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "capabilities": ["build"],
    }])
    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=tmp_path / "coordinator",
    )
    assert stopped["reason"] == "operator-resume-required"
    assert len(registry.list_jobs()) == 1

    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "wrong-run",
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    malformed = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )
    assert malformed["reason"] == "card-terminal-malformed-retry"
    assert malformed["job_id"] != old_job["job_id"]
    assert registry.get_workflow_run(run.run_id).facets == ()
    assert len(registry.list_jobs()) == 2
    assert registry.get_job(old_job["job_id"])["workflow_evidence"] is None
    retry_job = registry.get_job(malformed["job_id"])
    retry_log = Path(retry_job["log_path"])
    retry_log.parent.mkdir(parents=True, exist_ok=True)
    retry_log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": "a" * 40,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    registry.update_headless_result(retry_job["job_id"], status="exited", exit_code=0)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        gate_status="running",
    )

    result = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert result["reason"] == "in-flight"
    assert result["job_id"] != old_job["job_id"]
    assert registry.get_job(old_job["job_id"])["status"] == "exited"
    assert registry.get_job(old_job["job_id"])["workflow_evidence"] is None
    assert registry.get_workflow_run(run.run_id).facets == ()


def test_nonpassing_terminal_retry_authority_requires_exact_schema_and_binding(
    tmp_path: Path,
) -> None:
    log = tmp_path / "terminal.jsonl"
    payload = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    job = {
        "workflow_evidence": None,
        "status": "exited",
        "exit_code": 0,
        "workflow_phase": "build",
        "workflow_run_id": "run",
        "workflow_card": "card",
        "log_path": str(log),
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal(job) is True

    for key, value in (
        ("schema_version", True),
        ("status", "passed"),
        ("run_id", "other-run"),
        ("card_id", "other-card"),
        ("candidate", "not-a-sha"),
        ("outputs", "not-a-list"),
    ):
        invalid = {**payload, key: value}
        log.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
        assert manager._retryable_nonpassing_workflow_terminal(job) is False

    log.write_text("not-json\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal(job) is False

    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert manager._retryable_nonpassing_workflow_terminal({**job, "exit_code": False}) is False


def test_malformed_workflow_card_terminal_detects_unterminalizable_card_results(
    tmp_path: Path,
) -> None:
    log = tmp_path / "terminal.jsonl"
    job = {
        "workflow_evidence": None,
        "status": "exited",
        "exit_code": 0,
        "workflow_phase": "build",
        "workflow_run_id": "run",
        "workflow_card": "card",
        "log_path": str(log),
    }
    good = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }

    log.write_text(json.dumps({**good, "candidate": None}) + "\n", encoding="utf-8")
    assert manager._malformed_workflow_card_terminal(job) is True

    for status in ("failed", "needs_human"):
        log.write_text(json.dumps({**good, "status": status}) + "\n", encoding="utf-8")
        assert manager._malformed_workflow_card_terminal(job) is False

    for status in ("failed", "needs_human"):
        log.write_text(
            json.dumps({**good, "status": status, "candidate": None}) + "\n",
            encoding="utf-8",
        )
        assert manager._malformed_workflow_card_terminal(job) is True

    log.write_text(json.dumps({**good, "status": "done"}) + "\n", encoding="utf-8")
    assert manager._malformed_workflow_card_terminal(job) is True

    log.write_text(json.dumps(good) + "\n", encoding="utf-8")
    assert manager._malformed_workflow_card_terminal(job) is False


def test_operator_resume_retries_malformed_build_terminal_without_advancing(
    tmp_path: Path,
) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    coordinator_root = tmp_path / "coordinator"
    operator_root.mkdir()
    builder_root.mkdir()
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nstatus: accepted\n---\n# Plan\n## Steps\n- Reproduce\n", encoding="utf-8")
    registry = JobRegistry(state_path=coordinator_root / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(operator_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="production-wiring",
                baseline_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
            ),
        ),
    )
    step = manager._current_workflow_step(run)
    assert step is not None
    assert step.phase == "build"
    log = tmp_path / "malformed-build.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": step.card,
                "candidate": None,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy = registry.create_job(
        task="wf-malformed-build",
        persona="builder",
        kind="build",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(builder_root),
        dispatch_head="a" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=step.card,
        workflow_phase=step.phase,
        workflow_repo_root=str(builder_root),
        workflow_input_root=str(builder_root),
        workflow_inputs=manager._effective_workflow_inputs(run, step),
        workflow_outputs=step.outputs,
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(
        legacy["job_id"],
        executor="codex",
        model_id="gpt-primary",
        session_name="wf-malformed-build",
        log_path=str(log),
    )
    registry.update_headless_result(legacy["job_id"], status="exited", exit_code=0)
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "codex",
            "model_id": "gpt-primary",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    result = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=coordinator_root,
        operator_resume=True,
    )

    assert result["reason"] == "card-terminal-malformed-retry"
    assert result["current_phase"] == "build"
    assert result["job_id"] != legacy["job_id"]
    assert registry.get_job(legacy["job_id"])["workflow_evidence"] is None
    assert registry.get_workflow_run(run.run_id).current_phase == "build"


def test_operator_resume_recovers_only_exact_legacy_agy_reviewer_terminal(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Canary"], check=True)
    (tmp_path / "README.md").write_text("legacy recovery\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.phase in {"claim", "define", "plan", "build"} else "pending",
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="verify",
        steps=steps,
        candidate_head=candidate,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"verify": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    builder = registry.create_job(
        task="wf-builder",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    legacy_log = tmp_path / "legacy-agy.jsonl"
    legacy_log.write_text(
        "```json\n"
        + json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": "verification",
                "candidate": candidate,
                "outputs": ["reports/verify/production-wiring.md"],
            },
            indent=2,
        )
        + "\n```\n",
        encoding="utf-8",
    )
    legacy = registry.create_job(
        task="wf-verification",
        persona="reviewer",
        kind="review",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(tmp_path),
        executor="agy",
        model_id=AGY_MODEL_ID,
        independence_domain="google",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="verification",
        workflow_phase="verify",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        workflow_outputs=("reports/verify/*production-wiring*.md",),
        source_revision=run.source_revision,
        workflow_output_baseline=(),
    )
    registry.attach_launch_handle(
        legacy["job_id"],
        executor="agy",
        model_id=AGY_MODEL_ID,
        session_name=legacy["job_id"],
        log_path=str(legacy_log),
    )
    registry.update_headless_result(legacy["job_id"], status="exited", exit_code=0)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": AGY_LIVE_PROBE,
            },
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )
    launched: list[str] = []

    class Launcher:
        def as_review_only(self, *, terminal_kind):
            assert terminal_kind == "workflow-verification-result"
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(slice_id)
            return LaunchHandle(
                executor="claude",
                model_id="sonnet",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    verify_step = next(step for step in run.steps if step.card == "verification")
    assert manager._is_exact_legacy_agy_recovery(
        registry.get_job(legacy["job_id"]),
        run=run,
        step=verify_step,
        identities=identities,
    ), (registry.get_job(legacy["job_id"]), run.to_dict(), verify_step.to_dict())
    assert not manager._is_exact_legacy_agy_recovery(
        {
            **registry.get_job(legacy["job_id"]),
            "workflow_outputs": ["reports/verify/other.md"],
        },
        run=run,
        step=verify_step,
        identities=identities,
    )

    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=tmp_path / "coordinator",
    )
    assert stopped["reason"] == "operator-resume-required"
    assert launched == []

    resumed = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )

    assert resumed["reason"] == "in-flight"
    assert resumed["job_id"] != legacy["job_id"]
    assert registry.get_job(legacy["job_id"])["workflow_evidence"] is None
    replacement = registry.get_job(resumed["job_id"])
    assert replacement["executor"] == "claude"
    assert replacement["workflow_builder_job_id"] == builder["job_id"]
    assert launched == [replacement["job_id"]]


def test_build_card_advances_candidate_only_to_exact_descendant_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "canary@example.invalid")
    git("config", "user.name", "Canary")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "tests").mkdir()
    (repo / "tests/red.py").write_text("assert False\n", encoding="utf-8")
    git("add", "tests/red.py")
    git("commit", "-qm", "red")
    candidate = git("rev-parse", "HEAD")

    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.card == "worktree-isolation" else step.gate_result,
        })
        for step in _manifest().steps
    )
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        candidate_head=base,
    )
    log = tmp_path / "tdd-red.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": "tdd-red",
        "candidate": candidate,
        "outputs": [],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(repo),
        dispatch_head=base,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(
        job["job_id"],
        executor="codex",
        model_id="gpt-primary",
        session_name="wf-tdd-red",
        log_path=str(log),
    )
    # #379：tdd-red 卡 test_policy=red-required，manager 現在會要求 ledger 裡
    # 出現 pytest 這個 gate；exit_code=1（RED 如預期失敗）才是 #307 語意反轉
    # 認可的合格證據，對稱既有 test_manager_harvest_authorizes_tdd_red_card_
    # with_expected_red_pytest 的寫法。
    _gate_ledger_passed(
        log,
        gates=[{"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"}],
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    terminal = manager.terminalize_workflow_job(
        registry,
        job_id=str(job["job_id"]),
        coordinator_root=tmp_path / "coordinator",
    )
    assert manager._verify_build_candidate_transition(
        terminal,
        previous_candidate=None,
    ) == candidate
    with pytest.raises(ValueError, match="baseline missing"):
        manager._verify_build_candidate_transition(
            {**terminal, "dispatch_head": None},
            previous_candidate=None,
        )
    identities = IdentityRegistry.from_rows([{
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "capabilities": [],
    }])

    result = manager.apply_workflow_action(
        registry,
        args={
            "action": "advance",
            "run_id": run.run_id,
            "card_id": "tdd-red",
            "job_id": terminal["job_id"],
            "current_phase": "build",
        },
        identity_registry=identities,
        coordinator_root=tmp_path / "coordinator",
        trusted_terminal=True,
    )

    updated = registry.get_workflow_run(run.run_id)
    assert result["current_phase"] == "build"
    assert updated.candidate_head == candidate
    assert next(step for step in updated.steps if step.card == "tdd-red").gate_result == "passed"

    git("checkout", "-q", "--detach", base)
    (repo / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    git("add", "sibling.txt")
    git("commit", "-qm", "sibling")
    sibling = git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="not a descendant"):
        manager._verify_build_candidate_transition(
            {**terminal, "subject_head": sibling},
            previous_candidate=candidate,
        )


def test_operator_resume_reconciles_brainstorm_artifact_authority_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    rows = {
        "spec": "docs/superpowers/specs/production-wiring-spec.md",
        "design": "docs/superpowers/specs/production-wiring-design.md",
        "plan": "docs/superpowers/plans/production-wiring-plan.md",
    }
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Tasks\n- Build.\n",
    }
    artifact_rows = []
    for kind, ref in rows.items():
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bodies[kind], encoding="utf-8")
        artifact_rows.append(
            {"kind": kind, "ref": ref, "sha256": manager._sha256_path(path)}
        )
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": "hamanpaul/paulsha-cortex",
                    "work_id": "production-wiring",
                    "source_revision": "2" * 64,
                },
                "artifacts": artifact_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    state_path = coordinator_root / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_refs=(
            GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),
        ),
        gate_status="running",
    )
    legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
    legacy_state["workflows"][0].pop("planning_source_revision")
    state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
    registry = JobRegistry(state_path=state_path)
    run = registry.get_workflow_run(run.run_id)
    assert run.planning_source_revision is None
    seen: list[tuple[PlanningArtifactAuthority, ...]] = []

    def no_dispatch(_dispatcher, *, run, **_kwargs):
        seen.append(run.planning_authority)
        return None

    monkeypatch.setattr(manager, "dispatch_workflow_card", no_dispatch)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=coordinator_root,
        operator_resume=True,
    )

    assert result["reason"] == "not-dispatchable"
    assert {item.ref for item in seen[0]} == set(rows.values())
    reconciled = registry.get_workflow_run(run.run_id)
    assert reconciled.planning_authority == seen[0]
    assert reconciled.planning_source_revision == "2" * 64

    rebased = registry._manager_update_workflow_run(
        run.run_id,
        source_revision="3" * 64,
    )
    assert rebased.planning_source_revision == "2" * 64
    periodic = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=coordinator_root,
    )
    assert periodic["reason"] == "not-dispatchable"
    assert registry.get_workflow_run(run.run_id).facets == ()

    evidence.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence hash drift"):
        manager._validated_brainstorm_planning_authority(
            registry.get_workflow_run(run.run_id),
            coordinator_root=coordinator_root,
        )


def test_brainstorm_required_without_evidence_stays_stopped_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human",),
        gate_status="running",
        brainstorm_required=True,
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        manager,
        "dispatch_workflow_card",
        lambda *_args, **_kwargs: dispatched.append("called"),
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    result = manager.resume_workflow_run(
        dispatcher,
        run_id=run.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=tmp_path,
        operator_resume=True,
    )

    assert result["reason"] == "planning-authority-reconciliation-failed"
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert dispatched == []


def test_brainstorm_authority_resolves_exact_manager_archive_after_active_path_moves(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    ref = "openspec/changes/production-wiring/proposal.md"
    body = "---\nstatus: accepted\n---\n# Proposal\n## Requirements\nBound.\n"
    active = workspace / ref
    active.parent.mkdir(parents=True)
    active.write_text(body, encoding="utf-8")
    digest = manager._sha256_path(active)
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": "hamanpaul/paulsha-cortex",
                    "work_id": "production-wiring",
                    "source_revision": "2" * 64,
                },
                "artifacts": [{"kind": "spec", "ref": ref, "sha256": digest}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    archive = (
        workspace
        / "openspec"
        / "changes"
        / "archive"
        / "2026-07-19-production-wiring"
        / "proposal.md"
    )
    archive.parent.mkdir(parents=True)
    active.replace(archive)
    archive_step = replace(
        next(step for step in _manifest().steps if step.card == "openspec-archive"),
        executor="cortex-manager",
        model="deterministic",
        domain="cortex",
        gate_result="passed",
    )
    run = SimpleNamespace(
        repo="hamanpaul/paulsha-cortex",
        work_id="production-wiring",
        workspace_root=str(workspace),
        steps=(archive_step,),
        openspec_refs=("production-wiring",),
        brainstorm_required=True,
        planning_source_revision="2" * 64,
        planning_authority=(
            PlanningArtifactAuthority(
                ref=ref,
                kind="spec",
                work_id="production-wiring",
                baseline_sha256=digest,
            ),
        ),
        gate_refs=(
            GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),
        ),
    )

    authority, source_revision = manager._validated_brainstorm_planning_authority(
        run,
        coordinator_root=coordinator_root,
    )

    assert authority == run.planning_authority
    assert source_revision == "2" * 64

    untrusted = SimpleNamespace(
        **{
            **run.__dict__,
            "steps": (replace(archive_step, executor="operator"),),
        }
    )
    with pytest.raises(ValueError, match="artifact hash drift"):
        manager._validated_brainstorm_planning_authority(
            untrusted,
            coordinator_root=coordinator_root,
        )


def _materialize_authority_fixture(
    tmp_path: Path,
    *,
    extra_digest_matches_todo: bool = True,
    extra_ref_matches_plan_pattern: bool = True,
) -> tuple[SimpleNamespace, Path, str]:
    """issue #418 樁：重現 #414 deterministic plan pass materialize 出的
    canonical plan 檔（與 brainstorm 實際發佈的
    `docs/superpowers/workstreams/<slug>/todo.md` 是同一份內容的
    byte-copy，只是路徑不同，為了對齊 build 端 declared input pattern）與
    brainstorm evidence 對帳。回傳 ``(run, coordinator_root, canonical_ref)``。
    """

    slug = "materialize-authority-repro"
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    plan_body = "---\nstatus: accepted\n---\n# Plan\n## Tasks\n- Ship.\n"
    other_plan_body = "---\nstatus: accepted\n---\n# Plan\n## Tasks\n- Different.\n"
    spec_body = "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n"
    design_body = "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n"

    todo_ref = f"docs/superpowers/workstreams/{slug}/todo.md"
    spec_ref = f"docs/superpowers/specs/{slug}-spec.md"
    design_ref = f"docs/superpowers/specs/{slug}-design.md"
    canonical_ref = (
        f"docs/superpowers/plans/{slug}-plan.md"
        if extra_ref_matches_plan_pattern
        else f"docs/superpowers/plans-archive/{slug}-plan.md"
    )

    def _write(ref: str, body: str) -> Path:
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    todo_path = _write(todo_ref, plan_body)
    spec_path = _write(spec_ref, spec_body)
    design_path = _write(design_ref, design_body)
    canonical_path = _write(
        canonical_ref, plan_body if extra_digest_matches_todo else other_plan_body
    )

    artifact_rows = [
        {"kind": "plan", "ref": todo_ref, "sha256": manager._sha256_path(todo_path)},
        {"kind": "spec", "ref": spec_ref, "sha256": manager._sha256_path(spec_path)},
        {"kind": "design", "ref": design_ref, "sha256": manager._sha256_path(design_path)},
    ]
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": "hamanpaul/paulsha-cortex",
                    "work_id": slug,
                    "source_revision": "2" * 64,
                },
                "artifacts": artifact_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    define_step = WorkflowStep(
        phase="define",
        persona="planner",
        card="brainstorming",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(
            f"docs/superpowers/specs/*{slug}*-spec.md",
            f"docs/superpowers/specs/*{slug}*-design.md",
        ),
        gate_result="passed",
    )
    plan_step = WorkflowStep(
        phase="plan",
        persona="planner",
        card="writing-plans",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(f"docs/superpowers/plans/*{slug}*.md",),
        gate_result="passed",
    )

    planning_authority = (
        PlanningArtifactAuthority(
            ref=spec_ref,
            kind="spec",
            work_id=slug,
            baseline_sha256=manager._sha256_path(spec_path),
        ),
        PlanningArtifactAuthority(
            ref=design_ref,
            kind="design",
            work_id=slug,
            baseline_sha256=manager._sha256_path(design_path),
        ),
        PlanningArtifactAuthority(
            ref=todo_ref,
            kind="plan",
            work_id=slug,
            baseline_sha256=manager._sha256_path(todo_path),
        ),
        # #414 materialize 出的 canonical plan 副本：不在 brainstorm evidence
        # 的 artifacts 列表中（brainstorm 只發佈了 todo_ref），但已隨
        # deterministic plan pass 併入 `run.planning_authority`。
        PlanningArtifactAuthority(
            ref=canonical_ref,
            kind="plan",
            work_id=slug,
            baseline_sha256=manager._sha256_path(canonical_path),
        ),
    )

    run = SimpleNamespace(
        repo="hamanpaul/paulsha-cortex",
        work_id=slug,
        workspace_root=str(workspace),
        steps=(define_step, plan_step),
        openspec_refs=(slug,),
        brainstorm_required=True,
        planning_source_revision="2" * 64,
        planning_authority=planning_authority,
        gate_refs=(
            GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),
        ),
    )
    return run, coordinator_root, canonical_ref


def test_brainstorm_authority_accepts_materialized_plan_byte_copy(tmp_path: Path) -> None:
    """issue #418：materialized canonical plan（`docs/superpowers/plans/<slug>.md`）
    是 brainstorm 發佈的 `todo.md` 的 byte-copy（同 digest、同 kind=plan、同
    work_id），且 ref 落在 plan phase 宣告的 output pattern 內——這是合法副本，
    不應被判定為 evidence omission；回傳的 authority 仍要保留這筆副本，讓
    build worktree 能繼續透過 authority_refs fallback seed 到它。修正前
    （單純 `set(persisted) - set(scanned)` 非空即 raise）本測試必 RED。"""

    run, coordinator_root, canonical_ref = _materialize_authority_fixture(tmp_path)

    authority, source_revision = manager._validated_brainstorm_planning_authority(
        run,
        coordinator_root=coordinator_root,
    )

    assert authority == run.planning_authority
    assert source_revision == "2" * 64
    assert canonical_ref in {item.ref for item in authority}


def test_brainstorm_authority_rejects_materialized_plan_with_different_digest(
    tmp_path: Path,
) -> None:
    """負向樁：canonical plan 副本內容與任何已驗證的 brainstorm kind=plan
    entry 皆不同（非 byte-copy）——這是真正的 omission，必須維持 raise，
    不可被 #418 的例外路徑誤放行。"""

    run, coordinator_root, _canonical_ref = _materialize_authority_fixture(
        tmp_path, extra_digest_matches_todo=False,
    )

    with pytest.raises(ValueError, match="omits persisted authority"):
        manager._validated_brainstorm_planning_authority(
            run,
            coordinator_root=coordinator_root,
        )


def test_brainstorm_authority_rejects_persisted_ref_outside_plan_output_pattern(
    tmp_path: Path,
) -> None:
    """負向樁：即使 digest 對得上，若多出的 persisted ref 不落在 plan phase
    宣告的 output pattern 內，就不是 `_materialize_plan_card_output` 會產生
    的路徑形狀——必須維持 raise，避免例外路徑被濫用成任意 ref 的 omission
    漏洞。"""

    run, coordinator_root, _canonical_ref = _materialize_authority_fixture(
        tmp_path, extra_ref_matches_plan_pattern=False,
    )

    with pytest.raises(ValueError, match="omits persisted authority"):
        manager._validated_brainstorm_planning_authority(
            run,
            coordinator_root=coordinator_root,
        )


def test_operator_resume_dispatch_error_restores_needs_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        facets=("needs_human", "degraded"),
        gate_status="running",
    )
    monkeypatch.setattr(
        manager,
        "_dispatch_workflow_card",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("input gate failed")),
    )
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    with pytest.raises(ValueError, match="input gate failed"):
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda _: None,
            coordinator_root=tmp_path,
            operator_resume=True,
        )

    assert registry.get_workflow_run(run.run_id).facets == ("degraded", "needs_human")


def test_build_input_snapshot_seeds_hash_bound_plan_and_versions_prompt(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan_bytes = b"# Accepted plan\n\nBuild the contract.\n"
    plan.write_bytes(plan_bytes)
    builder_root.mkdir()
    digest = hashlib.sha256(plan_bytes).hexdigest()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(operator_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref,
                kind="plan",
                work_id="production-wiring",
                baseline_sha256=digest,
            ),
        ),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    patterns = manager._effective_workflow_inputs(run, red)
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=builder_root,
        patterns=patterns,
        coordinator_root=tmp_path / "coordinator",
    )
    payload = json.loads(
        manager._workflow_job_prompt(
            run,
            red,
            builder_job_id=None,
            coordinator_root=tmp_path / "coordinator",
            input_snapshot=snapshot,
        ).split("Contract: ", 1)[1]
    )

    assert patterns == ("docs/superpowers/plans/*production-wiring*.md",)
    assert (builder_root / plan_ref).read_bytes() == plan_bytes
    assert snapshot == (
        {
            "pattern": patterns[0],
            "path": plan_ref,
            "sha256": digest,
            "authority": "planning-authority",
            "content_ref": snapshot[0]["content_ref"],
        },
    )
    assert payload["kind"] == "workflow-card-prompt"
    assert payload["schema_version"] == 1
    assert payload["skill_ref"] == "superpowers:test-driven-development"
    assert payload["source_material"][0]["content"] == plan_bytes.decode()
    assert payload["terminal_schema"]["required"]
    assert payload["terminal_schema"]["fixed"]["run_id"] == run.run_id
    assert payload["terminal_schema"]["fixed"]["card_id"] == red.card
    assert payload["terminal_schema"]["fixed"]["outputs"] == []
    assert payload["terminal_schema"]["outputs"]["descriptive_objects_forbidden"] is True
    assert Path(snapshot[0]["content_ref"]).stat().st_mode & 0o222 == 0
    prompt = manager._workflow_job_prompt(
        run,
        red,
        builder_job_id=None,
        coordinator_root=tmp_path / "coordinator",
        input_snapshot=snapshot,
    )
    assert "40-hex" in prompt
    assert "rev-parse HEAD" in prompt
    assert "must never be null" in prompt
    assert "must be null" in prompt


def test_input_content_tamper_is_rejected_by_prompt_and_terminal_validation(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Accepted\n", encoding="utf-8")
    builder_root.mkdir()
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256=digest,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")
    coordinator_root = tmp_path / "coordinator"
    snapshot = manager._workflow_input_snapshot(
        run=run,
        repo_root=builder_root,
        patterns=manager._effective_workflow_inputs(run, red),
        coordinator_root=coordinator_root,
    )
    content_ref = Path(snapshot[0]["content_ref"])
    envelope = json.loads(content_ref.read_text(encoding="utf-8"))
    envelope["content"] = "# Tampered\n"
    content_ref.chmod(0o600)
    content_ref.write_text(json.dumps(envelope), encoding="utf-8")
    content_ref.chmod(0o444)

    with pytest.raises(ValueError, match="locator drift"):
        manager._workflow_job_prompt(
            run,
            red,
            builder_job_id=None,
            coordinator_root=coordinator_root,
            input_snapshot=snapshot,
        )
    with pytest.raises(ValueError, match="locator drift"):
        manager._validate_workflow_input_snapshot(
            builder_root,
            list(snapshot),
            coordinator_root=coordinator_root,
        )


def test_build_input_snapshot_rejects_mutable_operator_drift(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("changed after acceptance\n", encoding="utf-8")
    builder_root.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256="0" * 64,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    with pytest.raises(ValueError, match="planning input drift"):
        manager._workflow_input_snapshot(
            run=run,
            repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
    assert list(builder_root.rglob("*")) == []


def test_build_input_seed_rejects_symlinked_parent_without_outside_write(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    builder_root = tmp_path / "builder"
    outside = tmp_path / "outside"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    plan = operator_root / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Accepted\n", encoding="utf-8")
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    builder_root.mkdir()
    outside.mkdir()
    (builder_root / "docs").symlink_to(outside, target_is_directory=True)
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
        steps=_manifest().steps, issue_refs=(), openspec_refs=("production-wiring",), pr_refs=(),
        attempts={"build": 1}, gate_status="running",
        planning_authority=(PlanningArtifactAuthority(
            ref=plan_ref, kind="plan", work_id="production-wiring", baseline_sha256=digest,
        ),),
    )
    red = next(step for step in run.steps if step.card == "tdd-red")

    with pytest.raises(ValueError, match="symlink"):
        manager._workflow_input_snapshot(
            run=run, repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
    assert not (outside / "superpowers/plans/production-wiring-plan.md").exists()


def test_same_input_content_isolated_across_workflow_runs(tmp_path: Path) -> None:
    refs: list[str] = []
    for index in (1, 2):
        operator_root = tmp_path / f"operator-{index}"
        builder_root = tmp_path / f"builder-{index}"
        plan_ref = f"docs/superpowers/plans/work-{index}-plan.md"
        plan = operator_root / plan_ref
        plan.parent.mkdir(parents=True)
        plan.write_text("# Identical accepted content\n", encoding="utf-8")
        builder_root.mkdir()
        digest = hashlib.sha256(plan.read_bytes()).hexdigest()
        registry = JobRegistry(state_path=tmp_path / f"registry-{index}.json")
        manifest = compile_combo(
            load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", load_cards(DEFAULT_CARDS_PATH)),
            load_cards(DEFAULT_CARDS_PATH), f"work {index}", change=f"work-{index}",
        ).workflow_manifest
        run = registry._manager_create_workflow_run(
            work_id=f"work-{index}", repo="hamanpaul/paulsha-cortex",
            claim_key=f"claim:v1:{index}" + "1" * 63, source_revision=str(index) * 64,
            workspace_root=str(operator_root), combo="feature-oneshot", current_phase="build",
            steps=manifest.steps, issue_refs=(), openspec_refs=(f"work-{index}",), pr_refs=(),
            attempts={"build": 1}, gate_status="running",
            planning_authority=(PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id=f"work-{index}", baseline_sha256=digest,
            ),),
        )
        red = next(step for step in run.steps if step.card == "tdd-red")
        snapshot = manager._workflow_input_snapshot(
            run=run, repo_root=builder_root,
            patterns=manager._effective_workflow_inputs(run, red),
            coordinator_root=tmp_path / "coordinator",
        )
        refs.append(snapshot[0]["content_ref"])

    assert refs[0] != refs[1]
    assert all(Path(ref).is_file() for ref in refs)


def test_control_queue_manager_executes_heterogeneous_brainstorm_before_plan(tmp_path: Path) -> None:
    coordinator_dir = tmp_path.parent / f".{tmp_path.name}-coordinator"
    state_path = coordinator_dir / "registry.json"
    registry = JobRegistry(state_path=state_path)
    proposal = tmp_path / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Canary"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "openspec"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def git_runner(argv, **kwargs):
        if "cat-file" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout=candidate + "\n", stderr="")
        raise AssertionError(argv)

    created_branches: list[str] = []

    class WorktreeCreator:
        def create(self, branch, base_sha=None):
            created_branches.append(branch)
            return str(tmp_path)

    dispatcher = Dispatcher(
        registry, pane_sender=None, worktree_creator=WorktreeCreator(), git_runner=git_runner
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning", "build"],
            },
            {
                "executor": "claude",
                "model_id": "claude-secondary",
                "independence_domain": "anthropic",
                "capabilities": ["planning", "review"],
            },
        ]
    )
    calls: list[str] = []
    plan_launch_roots: list[Path] = []
    commit_capability_requests: list[str] = []
    review_capability_requests: list[str] = []
    adversarial_launches: list[str] = []

    class WorkflowLauncher:
        def as_read_only(self):
            return self

        def as_commit_required(self):
            commit_capability_requests.append("required")
            return self

        def as_review_only(self, *, terminal_kind):
            review_capability_requests.append(terminal_kind)
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            contract_payload = json.loads(prompt.split("Contract: ", 1)[1])
            job = registry.get_job(slice_id)
            phase = contract_payload["phase"]
            card = contract_payload["card_id"]
            if phase == "plan":
                plan_launch_roots.append(Path(worktree))
                evidence = {
                    "schema_version": 1, "kind": "workflow-card", "status": "passed",
                    "run_id": contract_payload["run_id"], "card_id": card,
                    "candidate": None,
                    "outputs": ["docs/superpowers/plans/production-wiring-plan.md"],
                }
            elif phase == "build":
                evidence = {
                    "schema_version": 1, "kind": "workflow-card", "status": "passed",
                    "run_id": contract_payload["run_id"], "card_id": card,
                    "candidate": candidate, "outputs": [],
                }
            elif phase == "verify":
                evidence = {
                    "schema_version": 1, "kind": "workflow-verification-result",
                    "status": "verified", "summary": "ok",
                    "details": {"card": card},
                    "reports": [{
                        "path": "reports/verify/production-wiring.md",
                        "body": "# Verification\n\nPassed.",
                    }],
                }
            else:
                suffix = "-adversarial" if card == "adversarial-review" else ""
                report_ref = f"reports/review/production-wiring{suffix}.md"
                findings = []
                if card == "adversarial-review":
                    adversarial_launches.append(slice_id)
                    if len(adversarial_launches) == 1:
                        findings = [{
                            "category": "correctness",
                            "severity": "minor",
                            "summary": "prior report omitted one sandbox-only failure file",
                            "evidence": [{
                                "path": "reports/review/production-wiring.md",
                                "line": None,
                                "detail": "the Candidate verdict is unchanged",
                            }],
                            "recommendation": "correct the enumeration in a fresh report",
                        }]
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-review-result",
                    "reason": "blocking findings" if findings else "accepted",
                    "findings": findings,
                    "reports": [{"path": report_ref, "body": "# Review\n\nPassed."}],
                }
                authority_hashes = (
                    contract_payload.get("terminal_schema", {}).get("authority_hashes", {}).get("expected")
                )
                if authority_hashes:
                    evidence["authority_hashes"] = authority_hashes
            log_path = Path(log_dir) / f"{slice_id}.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            log_path.with_suffix(".exit").write_text("0", encoding="utf-8")
            # #379：build phase 卡片若宣告 test_policy（tdd-red=red-required／
            # subagent-build=focused），manager 現在要求 ledger 出現對應的
            # pytest gate，否則 fail closed；其餘卡片（含 worktree-isolation
            # 與 plan/verify/review phase）維持既有空 gate 清單。
            build_gate_rows = None
            if phase == "build" and card == "tdd-red":
                build_gate_rows = [
                    {"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"}
                ]
            elif phase == "build" and card == "subagent-build":
                build_gate_rows = [{"name": "pytest", "status": "passed", "exit_code": 0}]
            _gate_ledger_passed(log_path, gates=build_gate_rows)
            return LaunchHandle(
                executor=str(job["executor"]), model_id=str(job["model_id"]),
                session_name=slice_id, pid=100, log_path=str(log_path),
            )

    workflow_launcher = WorkflowLauncher()

    def questioner(report):
        calls.append("questioner")
        from paulsha_cortex.coordinator.planning import assess_planning_completeness

        return assess_planning_completeness([]).default_question_pack.to_dict()

    def secondary(pack, identity):
        calls.append(f"secondary:{identity.independence_domain}")
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "evidence": [
                {"question_id": row["question_id"], "claims": ["missing"], "source_refs": ["index:1"]}
                for row in pack["questions"]
            ],
        }

    def integrator(pack, evidence):
        calls.append("integrator")
        bodies = {
            "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
            "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
            "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        }
        resolutions = []
        artifacts = []
        for row in pack["questions"]:
            kind = row["kind"].removeprefix("missing-")
            ref = (
                "docs/superpowers/plans/production-wiring-plan.md"
                if kind == "plan"
                else f"docs/superpowers/specs/production-wiring-{kind}.md"
            )
            resolutions.append(
                {"question_id": row["question_id"], "decision": "accepted", "artifact_kind": kind, "artifact_refs": [ref]}
            )
            artifacts.append({"kind": kind, "path": ref, "content": bodies[kind]})
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": resolutions,
            "artifacts": artifacts,
        }

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        workflow_probes={
            ("claude", "claude-secondary"): CapabilityProbe.ready_for(
                "claude", "claude-secondary", "anthropic"
            )
        },
        workflow_primary_questioner=questioner,
        workflow_secondary_planner=secondary,
        workflow_primary_integrator=integrator,
        launcher=workflow_launcher,
    )

    workflow_args = _workflow_args(manifest_path, tmp_path)
    workflow_args["evidence_dir"] = str(coordinator_dir / "evidence")
    result = executor(build_request(
        req_type="workflow-action", args=workflow_args, requested_by="operator"
    ))
    run = registry.get_workflow_run(result["run_id"])

    assert calls == ["questioner", "secondary:anthropic", "integrator"]
    assert commit_capability_requests == []
    assert review_capability_requests == []
    assert plan_launch_roots == []
    assert run.current_phase == "build"
    assert [ref.kind for ref in run.gate_refs] == ["brainstorm"]
    assert Path(run.gate_refs[0].ref).is_file()

    with pytest.raises(ValueError, match="rejects caller evidence"):
        executor(build_request(
            req_type="workflow-action",
            args={
                "action": "resume", "run_id": run.run_id,
                "verification_ref": {"path": "/tmp/forged", "hash": "0" * 64},
            },
            requested_by="operator",
        ))

    # Simulate daemon restart: only durable registry + job log/sentinel survive.
    registry = JobRegistry(state_path=state_path)
    dispatcher = Dispatcher(
        registry, pane_sender=None, worktree_creator=WorktreeCreator(), git_runner=git_runner
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        launcher=workflow_launcher,
    )

    periodic = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=workflow_launcher,
        workflow_identity_registry=identities,
        scan_specs_fn=lambda _: [],
        run_tick_fn=lambda *args, **kwargs: {"dispatch_skipped": False},
        auto_claim_fn=lambda: [],
    )
    periodic()
    assert registry.get_workflow_run(run.run_id).current_phase == "build"

    seen_phases = ["build"]
    for _ in range(6):
        result = executor(build_request(
            req_type="workflow-action",
            args={"action": "resume", "run_id": run.run_id},
            requested_by="operator",
        ))
        seen_phases.append(result["current_phase"])
        if result["reason"] == "blocking-findings":
            break
    assert seen_phases == ["build", "build", "verify", "review", "review", "review"]
    assert commit_capability_requests == ["required", "required"]
    assert review_capability_requests == [
        "workflow-verification-result",
        "workflow-review-result",
        "workflow-review-result",
    ]
    assert result["reason"] == "blocking-findings"
    blocked = registry.get_workflow_run(run.run_id)
    assert blocked.facets == ("needs_human",)
    assert blocked.gate_status == "failed"
    assert next(
        step for step in blocked.steps if step.card == "adversarial-review"
    ).gate_result == "needs_human"
    rejected_job = next(
        job
        for job in reversed(registry.list_jobs())
        if job.get("workflow_card") == "adversarial-review"
    )
    forged_job = dict(rejected_job)
    forged_job["workflow_evidence"] = {
        **rejected_job["workflow_evidence"],
        "hash": "0" * 64,
    }
    assert not manager._is_rejected_workflow_review_evidence(
        forged_job, run=blocked, coordinator_root=coordinator_dir
    )
    stale_job = {**rejected_job, "subject_head": "f" * 40}
    assert not manager._is_rejected_workflow_review_evidence(
        stale_job, run=blocked, coordinator_root=coordinator_dir
    )
    rejected_index = next(
        index
        for index, job in enumerate(registry._jobs)
        if job.get("job_id") == rejected_job["job_id"]
    )
    jobs_before_mismatch = len(registry.list_jobs())
    rejected_subject = registry._jobs[rejected_index]["subject_head"]
    registry._jobs[rejected_index]["subject_head"] = "f" * 40
    registry._persist()
    mismatch = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert mismatch["reason"] == "rejected-review-recovery-mismatch"
    assert len(registry.list_jobs()) == jobs_before_mismatch
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.get_workflow_run(run.run_id).gate_status == "failed"
    registry._jobs[rejected_index]["subject_head"] = rejected_subject
    registry._persist()
    rejected_hash = registry._jobs[rejected_index]["workflow_evidence"]["hash"]
    registry._jobs[rejected_index]["workflow_evidence"]["hash"] = "0" * 64
    registry._persist()
    mismatch = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert mismatch["reason"] == "rejected-review-recovery-mismatch"
    assert len(registry.list_jobs()) == jobs_before_mismatch
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)
    assert registry.get_workflow_run(run.run_id).gate_status == "failed"
    registry._jobs[rejected_index]["workflow_evidence"]["hash"] = rejected_hash
    registry._persist()
    jobs_before_periodic = len(registry.list_jobs())
    periodic()
    assert len(registry.list_jobs()) == jobs_before_periodic
    assert registry.get_workflow_run(run.run_id).facets == ("needs_human",)

    result = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert result["reason"] == "ship-validator-unavailable"
    assert len(adversarial_launches) == 2
    assert review_capability_requests == [
        "workflow-verification-result",
        "workflow-review-result",
        "workflow-review-result",
        "workflow-review-result",
    ]

    passed = registry.get_workflow_run(run.run_id)
    replay_steps = tuple(
        replace(step, gate_result="pending")
        if step.card == "adversarial-review"
        else step
        for step in passed.steps
    )
    registry._manager_update_workflow_run(
        run.run_id,
        steps=replay_steps,
        facets=("needs_human",),
        gate_status="running",
    )
    jobs_before_replay = len(registry.list_jobs())
    replayed = executor(build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    ))
    assert replayed["reason"] == "ship-validator-unavailable"
    assert len(registry.list_jobs()) == jobs_before_replay
    assert next(
        step
        for step in registry.get_workflow_run(run.run_id).steps
        if step.card == "adversarial-review"
    ).gate_result == "passed"

    fake_ship = build_request(
        req_type="workflow-action",
        args={
            "action": "advance", "run_id": run.run_id, "card_id": "adversarial-review",
            "current_phase": "ship", "gate_refs": [{"kind": "copilot", "ref": "fake"}],
        },
        requested_by="operator",
    )
    with pytest.raises(ValueError, match="internal"):
        executor(fake_ship)
    current = registry.get_workflow_run(run.run_id)
    for card in ("openspec-archive", "policy-commit"):
        work_bridge._record_manager_ship_job(
            registry=registry,
            state_root=coordinator_dir,
            run=current,
            worktree=tmp_path,
            branch="feature/production-wiring",
            card=card,
            old_head=candidate,
            new_head=candidate,
        )
    trusted_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=identities,
        launcher=workflow_launcher,
        workflow_ship_validator=lambda **_: {
            "trusted": True, "status": "passed", "head": candidate, "commit_id": candidate,
            "ref": "github:copilot/current-head", "hash": "f" * 64,
            "completion": {
                "record_path": str(tmp_path / "evidence/completion.json"),
                "record_hash": "e" * 64,
                "record_revision": candidate,
                "source_revisions": {"openspec:production-wiring": "rev-a"},
                "pr_candidate": candidate,
                "merge_revision": "d" * 40,
            },
        },
    )
    trusted_ship = build_request(
        req_type="workflow-action",
        args={"action": "resume", "run_id": run.run_id},
        requested_by="operator",
    )
    assert trusted_executor(trusted_ship)["current_phase"] == "ship"

    shipped = registry.get_workflow_run(run.run_id)
    assert shipped.status == "done"
    assert shipped.completion_record_revision == candidate
    assert shipped.merge_revision == "d" * 40
    assert shipped.verified_head == shipped.candidate_head == candidate
    assert {ref.kind for ref in shipped.gate_refs} == {"brainstorm", "foreign-review", "copilot"}
    assert all(
        step.executor is not None and step.domain is not None and step.gate_result == "passed"
        for step in shipped.steps if step.phase in {"claim", "define", "plan", "build", "verify", "review"}
    )
    workflow_jobs = [job for job in registry.list_jobs() if job.get("workflow_run_id") == run.run_id]
    assert len(workflow_jobs) == 9
    assert {
        job.get("workflow_card")
        for job in workflow_jobs
        if job.get("workflow_phase") == "ship"
    } == {"openspec-archive", "policy-commit"}
    assert created_branches == ["feature/production-wiring"]
    assert all(job.get("workflow_evidence") for job in workflow_jobs)
    assert all(job.get("workflow_claim_key") == run.claim_key for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_inputs"), list) for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_outputs"), list) for job in workflow_jobs)
    assert all(isinstance(job.get("workflow_output_baseline"), list) for job in workflow_jobs)
    adversarial_jobs = [
        job for job in workflow_jobs if job.get("workflow_card") == "adversarial-review"
    ]
    assert len(adversarial_jobs) == 2
    assert any(
        row["path"] == "reports/review/production-wiring.md"
        for row in adversarial_jobs[0]["workflow_output_baseline"]
    )
    assert any(
        row["path"] == "reports/review/production-wiring-adversarial.md"
        for row in adversarial_jobs[1]["workflow_output_baseline"]
    )
    assert all(not Path(job["workflow_evidence"]["path"]).is_absolute() for job in workflow_jobs)
    assert all(
        (coordinator_dir / job["workflow_evidence"]["path"]).is_file()
        for job in workflow_jobs
    )


def test_workflow_candidate_must_exist_at_exact_worktree_head(tmp_path: Path) -> None:
    candidate = "a" * 40
    job = {"subject_head": candidate, "worktree": str(tmp_path)}

    def missing_runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    with pytest.raises(ValueError, match="does not exist"):
        manager._verify_exact_candidate(job, git_runner=missing_runner)


def test_ship_audit_accepts_manager_archive_ancestor_after_retry_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    coordinator = tmp_path / "coordinator"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "archive.txt").write_text("archived\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "archive.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "archive"], check=True)
    archive_candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    registry = JobRegistry(state_path=coordinator / "jobs.json")
    steps = tuple(
        replace(
            step,
            executor="cortex-manager",
            model="deterministic",
            domain="cortex",
            gate_result="passed",
        )
        if step.phase == "ship" and step.card == "openspec-archive"
        else step
        for step in _manifest().steps
    )
    run = registry._manager_create_workflow_run(
        work_id="archive-repair",
        repo="owner/repo",
        claim_key="claim:v1:" + "a" * 64,
        source_revision="b" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        candidate_head=final_candidate,
        verified_head=final_candidate,
        gate_status="running",
    )
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        worktree=repo,
        branch="feature/archive-repair",
        card="openspec-archive",
        old_head=archive_candidate,
        new_head=archive_candidate,
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        source_revision="e" * 64,
    )
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        worktree=repo,
        branch="feature/archive-repair",
        card="policy-commit",
        old_head=final_candidate,
        new_head=final_candidate,
    )

    audited = manager._validated_ship_steps(
        registry,
        run=run,
        candidate=final_candidate,
        coordinator_root=coordinator,
    )
    assert all(
        step.gate_result == "passed"
        for step in audited
        if step.phase == "ship"
    )

    archive_tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{archive_candidate}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    sibling_candidate = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", archive_tree, "-p", archive_candidate],
        check=True,
        capture_output=True,
        text=True,
        input="unrelated sibling\n",
    ).stdout.strip()
    unrelated_root = tmp_path / "unrelated-coordinator"
    unrelated_registry = JobRegistry(state_path=unrelated_root / "jobs.json")
    unrelated_run = unrelated_registry._manager_create_workflow_run(
        work_id="unrelated-archive",
        repo="owner/repo",
        claim_key="claim:v1:" + "c" * 64,
        source_revision="d" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=steps,
        candidate_head=final_candidate,
        verified_head=final_candidate,
        gate_status="running",
    )
    work_bridge._record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        worktree=repo,
        branch="feature/unrelated-archive",
        card="openspec-archive",
        old_head=archive_candidate,
        new_head=sibling_candidate,
    )
    work_bridge._record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        worktree=repo,
        branch="feature/unrelated-archive",
        card="policy-commit",
        old_head=final_candidate,
        new_head=final_candidate,
    )
    with pytest.raises(ValueError, match="openspec-archive"):
        manager._validated_ship_steps(
            unrelated_registry,
            run=unrelated_run,
            candidate=final_candidate,
            coordinator_root=unrelated_root,
        )


def test_manager_rejects_same_domain_reviewer_before_dispatch(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    candidate = "b" * 40
    steps = tuple(
        WorkflowStep(
            phase=step.phase,
            persona=step.persona,
            card=step.card,
            executor="codex" if step.phase == "build" else step.executor,
            model="builder" if step.phase == "build" else step.model,
            domain="openai" if step.phase == "build" else step.domain,
            inputs=step.inputs,
            outputs=step.outputs,
            gate_result="passed" if step.phase == "build" else step.gate_result,
        )
        for step in _manifest().steps
    )
    run = registry._manager_create_workflow_run(
        work_id="same-domain", repo="owner/repo", claim_key="owner/repo/same-domain/rev-a",
        source_revision="rev-a", workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="review",
        steps=steps, candidate_head=candidate, verified_head=candidate, gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "builder", "independence_domain": "openai", "capabilities": []},
            {"executor": "claude", "model_id": "reviewer", "independence_domain": "openai", "capabilities": ["review"]},
        ]
    )
    review_step = next(step for step in run.steps if step.phase == "review")
    with pytest.raises(ValueError, match="no configured identity"):
        manager._select_workflow_identity(run, review_step, identities)


def _workflow_identity_run(*, primary_domain: str | None, build_domain: str | None = None) -> SimpleNamespace:
    steps = []
    if build_domain is not None:
        steps.append(SimpleNamespace(phase="build", gate_result="passed", domain=build_domain))
    return SimpleNamespace(primary_domain=primary_domain, steps=steps)


def test_planner_identity_selection_ignores_primary_domain_preference() -> None:
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "builder-openai",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "planner-claude",
                "independence_domain": "anthropic",
                "capabilities": ["planning", "review"],
            },
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": AGY_LIVE_PROBE,
            },
        ]
    )

    selected = manager._select_workflow_identity(
        _workflow_identity_run(primary_domain="google"),
        SimpleNamespace(persona="planner"),
        identities,
    )

    assert (selected.executor, selected.model_id) == ("claude", "planner-claude")


def test_builder_identity_selection_requires_build_capability_before_domain_preference() -> None:
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "builder-openai",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "planner-claude",
                "independence_domain": "anthropic",
                "capabilities": ["planning", "review"],
            },
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": AGY_LIVE_PROBE,
            },
        ]
    )

    selected = manager._select_workflow_identity(
        _workflow_identity_run(primary_domain="google"),
        SimpleNamespace(persona="builder"),
        identities,
    )

    assert (selected.executor, selected.model_id) == ("copilot", "builder-openai")


def test_builder_identity_selection_still_prefers_primary_domain_after_build_filter() -> None:
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "builder-openai",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "planner-claude",
                "independence_domain": "anthropic",
                "capabilities": ["planning", "review"],
            },
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": AGY_LIVE_PROBE,
            },
            {
                "executor": "gemini",
                "model_id": "builder-google",
                "independence_domain": "google",
                "capabilities": ["build"],
            },
        ]
    )

    selected = manager._select_workflow_identity(
        _workflow_identity_run(primary_domain="google"),
        SimpleNamespace(persona="builder"),
        identities,
    )

    assert (selected.executor, selected.model_id) == ("gemini", "builder-google")


def test_reviewer_identity_selection_excludes_builder_domains() -> None:
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "claude",
                "model_id": "review-google",
                "independence_domain": "google",
                "capabilities": ["review"],
            },
            {
                "executor": "claude",
                "model_id": "review-anthropic",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )

    selected = manager._select_workflow_identity(
        _workflow_identity_run(primary_domain="google", build_domain="google"),
        SimpleNamespace(persona="reviewer"),
        identities,
    )

    assert (selected.executor, selected.model_id) == ("claude", "review-anthropic")


def test_manager_rejects_planner_artifacts_outside_governed_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [{"kind": "plan", "path": "README.md", "content": "not allowed"}],
            work_id="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


def test_planning_artifact_publish_is_scoped_cas_and_transactional(tmp_path: Path) -> None:
    plan = {
        "kind": "plan",
        "path": "docs/superpowers/plans/production-wiring-plan.md",
        "content": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    spec = {
        "kind": "spec",
        "path": "docs/superpowers/specs/production-wiring-spec.md",
        "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
    }
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), [plan, spec], work_id="production-wiring",
        allowed_refs=(
            "docs/superpowers/plans/*production-wiring*.md",
            "docs/superpowers/specs/*production-wiring*-spec.md",
        ),
    )
    assert (tmp_path / plan["path"]).is_file()
    assert (tmp_path / spec["path"]).is_file()
    rollback()
    assert not (tmp_path / plan["path"]).exists()
    assert not (tmp_path / spec["path"]).exists()

    conflict = tmp_path / spec["path"]
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("owned by another transaction\n", encoding="utf-8")
    with pytest.raises(ValueError, match="current planning authority"):
        manager._publish_planning_artifacts(
            str(tmp_path), [plan, spec], work_id="production-wiring",
            allowed_refs=(
                "docs/superpowers/plans/*production-wiring*.md",
                "docs/superpowers/specs/*production-wiring*-spec.md",
            ),
        )
    assert not (tmp_path / plan["path"]).exists()
    assert conflict.read_text(encoding="utf-8") == "owned by another transaction\n"

    other_work = dict(plan, path="docs/superpowers/plans/other-work-plan.md")
    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path), [other_work], work_id="production-wiring",
            allowed_refs=("docs/superpowers/plans/*production-wiring*.md",),
        )


def test_planning_artifact_publish_replaces_only_exact_baseline_and_rolls_back_group(
    tmp_path: Path,
) -> None:
    spec_ref = "docs/superpowers/specs/production-wiring-spec.md"
    plan_ref = "docs/superpowers/plans/production-wiring-plan.md"
    old_spec = "---\nstatus: draft\n---\n# Spec\n## Requirements\nTBD\n"
    spec_path = tmp_path / spec_ref
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(old_spec, encoding="utf-8")
    baseline = manager._sha256_path(spec_path)
    rows = [
        {
            "kind": "spec", "path": spec_ref,
            "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        },
        {
            "kind": "plan", "path": plan_ref,
            "content": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        },
    ]
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), rows, work_id="production-wiring",
        allowed_refs=(
            "docs/superpowers/specs/*production-wiring*-spec.md",
            "docs/superpowers/plans/*production-wiring*.md",
        ),
        authorities=(PlanningArtifactAuthority(
            ref=spec_ref, kind="spec", work_id="production-wiring",
            baseline_sha256=baseline,
        ),),
    )
    assert "Bound." in spec_path.read_text(encoding="utf-8")
    assert (tmp_path / plan_ref).is_file()
    rollback()
    assert spec_path.read_text(encoding="utf-8") == old_spec
    assert not (tmp_path / plan_ref).exists()

    spec_path.write_text("operator changed after scan\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authority drift"):
        manager._publish_planning_artifacts(
            str(tmp_path), rows, work_id="production-wiring",
            allowed_refs=(
                "docs/superpowers/specs/*production-wiring*-spec.md",
                "docs/superpowers/plans/*production-wiring*.md",
            ),
            authorities=(PlanningArtifactAuthority(
                ref=spec_ref, kind="spec", work_id="production-wiring",
                baseline_sha256=baseline,
            ),),
        )
    assert spec_path.read_text(encoding="utf-8") == "operator changed after scan\n"
    assert not (tmp_path / plan_ref).exists()


# --- issue #511：planning artifact 拒收的診斷面 -------------------------------
#
# 修法前 `_publish_planning_artifacts` 只用 `assess_planning_artifact(...).accepted`
# 的布林值，`reasons`／`blocking_markers` 全被丟棄，被拒的內容又只活在 planning
# launcher 的 `TemporaryDirectory` 裡（`planning_runtime.py` 的 `last.json`），
# context 結束即刪除——operator 只看得到 `planning artifact is not accepted: <path>`，
# 無從得知是哪一條驗收條件不過、planner 到底寫了什麼，只能盲目重試（實測
# abandon→重新 claim→同樣失敗，四次全同）。下列測試鎖住兩件事：
#   A. 拒收原因（與 blocking markers 的行號／文字）進錯誤訊息
#   B. 被拒 artifact 的完整內容落 `cortex-planning-artifact-rejection/v1` evidence
_REJECTION_SPEC_REF = "docs/superpowers/specs/production-wiring-spec.md"
_REJECTION_ALLOWED_REFS = ("docs/superpowers/specs/*production-wiring*-spec.md",)


def _publish_rejected_spec(root: Path, content: str, *, coordinator_root: Path | None = None):
    return manager._publish_planning_artifacts(
        str(root),
        [{"kind": "spec", "path": _REJECTION_SPEC_REF, "content": content}],
        work_id="production-wiring",
        allowed_refs=_REJECTION_ALLOWED_REFS,
        coordinator_root=coordinator_root,
    )


@pytest.mark.parametrize(
    ("content", "expected_reason"),
    [
        # frontmatter 沒有 `status: accepted`
        (
            "---\nstatus: draft\n---\n# Spec\n## Requirements\nBound.\n",
            "status-not-accepted",
        ),
        # 有 accepted 但缺 spec 必備英文標題（Requirements／Problem／Goals）
        (
            "---\nstatus: accepted\n---\n# Spec\n## Notes\nBound.\n",
            "required-section-missing",
        ),
        # Open Questions 章節下的清單項目 → blocking marker
        (
            "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n"
            "## Open Questions\n- 這條要不要收斂到 v2？\n",
            "blocking-decision",
        ),
    ],
)
def test_planning_artifact_rejection_message_carries_assessment_reasons(
    tmp_path: Path, content: str, expected_reason: str
) -> None:
    """issue #511 A：三種 `ArtifactAssessment.reasons` 都必須出現在錯誤訊息裡。"""

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(tmp_path, content)

    message = str(excinfo.value)
    assert message.startswith(f"planning artifact is not accepted: {_REJECTION_SPEC_REF}")
    assert f"reasons={expected_reason}" in message


def test_planning_artifact_rejection_message_carries_blocking_marker_lines(
    tmp_path: Path,
) -> None:
    """issue #511 A：`blocking-decision` 必須附上 markers 的行號與文字，
    operator 才知道 planner 卡在哪一條未決問題。"""

    content = (
        "---\n"           # L1
        "status: accepted\n"  # L2
        "---\n"           # L3
        "# Spec\n"        # L4
        "## Requirements\n"   # L5
        "Bound.\n"        # L6
        "## Open Questions\n"  # L7
        "- 誰負責 schema 遷移？\n"  # L8
        "- 是否沿用既有 evidence 目錄？\n"  # L9
    )

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(tmp_path, content)

    message = str(excinfo.value)
    assert "markers=" in message
    assert "L8:誰負責 schema 遷移？" in message
    assert "L9:是否沿用既有 evidence 目錄？" in message


def test_planning_artifact_rejection_message_stays_single_line_and_bounded(
    tmp_path: Path,
) -> None:
    """issue #511 A：訊息會被 `run_heterogeneous_brainstorm` 包進 needs_human 的
    reason、再原樣落進 `cortex-planning-failure/v1` evidence 的 `reason` 欄位，
    而 `work_actions`／`control.contract` 對 `failure_reason` 都拒收換行；長度
    也必須有上限保護（比照 `manager_daemon.TICK_ERROR_REASON_MAX_LENGTH`）。"""

    questions = "".join(f"- 第 {index} 條未決問題：{'長' * 120}\n" for index in range(40))
    content = (
        "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n"
        f"## Open Questions\n{questions}"
    )

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(tmp_path, content)

    message = str(excinfo.value)
    assert "\n" not in message and "\r" not in message
    assert len(message) <= manager.PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH
    # 上限保護不得吃掉最關鍵的診斷欄位：reasons 必須在截斷後仍看得到。
    assert "reasons=blocking-decision" in message


def test_planning_artifact_rejection_records_full_content_evidence(tmp_path: Path) -> None:
    """issue #511 B：被拒 artifact 的 kind／path／完整 content／reasons／markers
    必須落 evidence，operator 才能直接開檔看 planner 寫了什麼——修法前這份內容
    只存在於 planning launcher 的 TemporaryDirectory，沒有任何副本留存。"""

    coordinator_root = tmp_path / "coordinator"
    artifact_root = tmp_path / "worktree"
    content = (
        "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n"
        "## Open Questions\n- 要不要換 schema？\n"
    )

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(artifact_root, content, coordinator_root=coordinator_root)

    evidence_dir = coordinator_root / "evidence" / "planning-artifacts"
    evidence_paths = sorted(evidence_dir.glob("*.json"))
    assert len(evidence_paths) == 1, f"目錄內容：{list(evidence_dir.glob('*'))}"
    body = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-planning-artifact-rejection/v1"
    assert body["work_id"] == "production-wiring"
    assert body["kind"] == "spec"
    assert body["path"] == _REJECTION_SPEC_REF
    assert body["content"] == content
    assert body["content_length"] == len(content)
    assert body["truncated"] is False
    assert body["reasons"] == ["blocking-decision"]
    assert body["markers"] == [
        {"kind": "open-question", "line": 8, "text": "要不要換 schema？"}
    ]
    assert isinstance(body.get("created_at"), str) and body["created_at"]
    # evidence 落在 coordinator_root 底下，不得污染被監控的 artifact worktree。
    assert not (artifact_root / "evidence").exists()
    # 錯誤訊息要指向該檔，operator 不必自己猜路徑。
    assert f"evidence={evidence_paths[0]}" in str(excinfo.value)


def test_planning_artifact_rejection_evidence_truncates_oversized_content(
    tmp_path: Path,
) -> None:
    """issue #511 B：artifact 可能很大，evidence 內容須設上限；超過時截斷並標記
    `truncated: true` 與原始長度，避免 evidence 檔爆量。"""

    coordinator_root = tmp_path / "coordinator"
    limit = manager.PLANNING_ARTIFACT_REJECTION_CONTENT_MAX_CHARS
    filler = "x" * (limit + 4096)
    content = f"---\nstatus: draft\n---\n# Spec\n## Requirements\n{filler}\n"

    with pytest.raises(ValueError):
        _publish_rejected_spec(tmp_path / "worktree", content, coordinator_root=coordinator_root)

    body = json.loads(
        sorted((coordinator_root / "evidence" / "planning-artifacts").glob("*.json"))[0]
        .read_text(encoding="utf-8")
    )
    assert body["truncated"] is True
    assert body["content_length"] == len(content)
    assert len(body["content"]) == limit
    assert body["content"] == content[:limit]


def test_planning_artifact_rejection_without_coordinator_root_still_reports_reasons(
    tmp_path: Path,
) -> None:
    """未帶 `coordinator_root`（既有直呼叫端與測試）時不得落檔、也不得爆炸：
    仍照舊 raise，只是訊息不帶 `evidence=`。"""

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(tmp_path, "---\nstatus: draft\n---\n# Spec\n## Requirements\nBound.\n")

    message = str(excinfo.value)
    assert "reasons=status-not-accepted" in message
    assert "evidence=" not in message
    assert not (tmp_path / "evidence").exists()


def test_planning_artifact_rejection_evidence_write_failure_does_not_mask_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """evidence 記錄本身 fail-open（比照 `_record_planning_failure_evidence`）：
    落檔失敗只留 log，拒收本身仍照舊 raise，不得把真正的拒收原因換成 IO 錯誤。"""

    def exploding_writer(**_kwargs):
        raise OSError("evidence volume full")

    monkeypatch.setattr(manager, "_write_planning_artifact_rejection_evidence", exploding_writer)

    with pytest.raises(ValueError) as excinfo:
        _publish_rejected_spec(
            tmp_path / "worktree",
            "---\nstatus: draft\n---\n# Spec\n## Requirements\nBound.\n",
            coordinator_root=tmp_path / "coordinator",
        )

    assert "reasons=status-not-accepted" in str(excinfo.value)
    assert "evidence volume full" not in str(excinfo.value)


def test_planning_artifact_publish_accepts_valid_artifact_without_rejection_evidence(
    tmp_path: Path,
) -> None:
    """回歸樁：成功路徑不得因為 #511 的診斷落檔而多寫任何 evidence。"""

    coordinator_root = tmp_path / "coordinator"
    artifact_root = tmp_path / "worktree"
    rollback = _publish_rejected_spec(
        artifact_root,
        "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        coordinator_root=coordinator_root,
    )

    assert (artifact_root / _REJECTION_SPEC_REF).is_file()
    assert not (coordinator_root / "evidence" / "planning-artifacts").exists()
    rollback()


def test_define_wires_coordinator_root_into_planning_artifact_rejection_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #511 端到端接線：`apply_workflow_action` 的 define 路徑把
    `artifact_writer` 綁在被監控的 worktree 上，evidence 必須改落
    `transaction_root`（coordinator_root）——否則 #507 的 planning 失敗清樹會
    連同這份診斷一起抹掉，等於白記。"""

    coordinator_root = tmp_path / "coordinator"
    coordinator_root.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "gpt-primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            }
        ]
    )
    rejected_content = "---\nstatus: draft\n---\n# Plan\n## Task 1\nBuild.\n"

    def brainstorm_through_writer(**kwargs):
        try:
            kwargs["artifact_writer"](
                [
                    {
                        "kind": "plan",
                        "path": "docs/superpowers/plans/production-wiring.md",
                        "content": rejected_content,
                    }
                ]
            )
        except ValueError as exc:
            return BrainstormResult(
                state="needs_human",
                reason=f"primary-artifact-write-rejected: ValueError: {str(exc)[:160]}",
                secondary_domain=None,
                gate_refs=PlanningGateRefs(),
            )
        raise AssertionError("artifact_writer 應該拒收未 accepted 的 plan")

    monkeypatch.setattr(manager, "run_heterogeneous_brainstorm", brainstorm_through_writer)

    result = manager.apply_workflow_action(
        registry,
        args=args,
        identity_registry=identities,
        primary_questioner=lambda *a, **k: None,
        secondary_planner=lambda *a, **k: None,
        primary_integrator=lambda *a, **k: None,
        coordinator_root=coordinator_root,
    )

    persisted = registry.get_workflow_run(result["run_id"])
    assert persisted.facets == ("needs_human",)
    assert "reasons=status-not-accepted" in result["reason"]
    evidence_paths = sorted(
        (coordinator_root / "evidence" / "planning-artifacts").glob(f"{persisted.run_id}-*.json")
    )
    assert len(evidence_paths) == 1
    body = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    assert body["run_id"] == persisted.run_id
    assert body["content"] == rejected_content
    assert not (tmp_path / "evidence" / "planning-artifacts").exists()


def test_verify_terminal_evidence_cannot_substitute_for_declared_report(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    log = tmp_path / "verify.jsonl"
    payload = {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="card", workflow_phase="verify", workflow_repo_root=str(tmp_path),
        workflow_outputs=("reports/verify/work.md",), source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="non-empty list"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert not (tmp_path / "evidence/workflow").exists()


def test_planner_terminalization_rejects_disposable_sandbox_pollution(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_ref = "docs/superpowers/plans/work-plan.md"
    plan = repo / plan_ref
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    sandbox = tmp_path / "planning-sandboxes" / ("a" * 32)
    sandbox.parent.mkdir()
    planning_runtime._copy_planning_sandbox(repo, sandbox)
    sandbox_hash = planning_runtime._tree_snapshot(sandbox)
    log = tmp_path / "plan.jsonl"
    payload = {
        "schema_version": 1, "kind": "workflow-card", "status": "passed",
        "run_id": "run", "card_id": "card", "candidate": None,
        "outputs": [plan_ref],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="plan", persona="planner", branch="feature/work", pane="",
        worktree=str(sandbox), executor="codex", model_id="planner",
        independence_domain="openai", workflow_run_id="run",
        workflow_claim_key="claim", workflow_repo="owner/repo", workflow_card="card",
        workflow_phase="plan", workflow_repo_root=str(repo), workflow_outputs=(plan_ref,),
        source_revision="rev", workflow_sandbox_hash=sandbox_hash,
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    (sandbox / "empty-pollution").mkdir()

    with pytest.raises(ValueError, match="modified disposable read-only sandbox"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert not sandbox.exists()
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None


def test_terminal_json_uses_codex_final_agent_message_not_turn_envelope(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": None,
        "outputs": ["docs/superpowers/plans/work.md"],
    }
    log = tmp_path / "codex.jsonl"
    log.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "aggregated_output": json.dumps({"status": "fake"}),
                    },
                }),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(evidence)},
                }),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 10}}),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert manager._extract_terminal_json(str(log)) == evidence


def test_terminal_json_reads_copilot_assistant_message_data_content(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log = tmp_path / "copilot.jsonl"
    log.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant.message",
                    "data": {"content": json.dumps(evidence)},
                }),
                json.dumps({"type": "result", "exitCode": 0}),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert manager._extract_terminal_json(str(log)) == evidence


def test_terminal_json_rejects_copilot_non_message_data_content(tmp_path: Path) -> None:
    fake = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log = tmp_path / "copilot-tool.jsonl"
    log.write_text(
        json.dumps({
            "type": "tool.execution_complete",
            "data": {"content": json.dumps(fake)},
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no JSON evidence"):
        manager._extract_terminal_json(str(log))


def test_failed_planner_retry_replaces_only_its_disposable_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.md").write_text("source\n", encoding="utf-8")
    proposal = repo / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "codex",
            "model_id": "gpt-primary",
            "independence_domain": "openai",
            "capabilities": ["planning"],
        }]
    )

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    first = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
    )
    assert first["workflow_input_root"] == first["worktree"]
    first_sandbox = Path(first["worktree"])
    (first_sandbox / "failed-attempt-marker").write_text("stale\n", encoding="utf-8")
    registry.update_headless_result(first["job_id"], status="failed", exit_code=1)

    retried = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
        retry_failed=True,
    )

    assert retried["job_id"] != first["job_id"]
    assert Path(retried["worktree"]) == first_sandbox
    assert not (first_sandbox / "failed-attempt-marker").exists()
    assert (first_sandbox / "source.md").read_text(encoding="utf-8") == "source\n"


def test_malformed_planner_terminal_retry_reuses_cleaned_disposable_sandbox(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.md").write_text("source\n", encoding="utf-8")
    proposal = repo / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "codex",
            "model_id": "gpt-primary",
            "independence_domain": "openai",
            "capabilities": ["planning"],
        }]
    )

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    first = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
    )
    first_sandbox = Path(first["worktree"])
    (first_sandbox / "failed-attempt-marker").write_text("stale\n", encoding="utf-8")
    first_log = Path(first["log_path"])
    first_log.parent.mkdir(parents=True, exist_ok=True)
    first_log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "done",
                "run_id": run.run_id,
                "card_id": first["workflow_card"],
                "candidate": None,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.update_headless_result(first["job_id"], status="exited", exit_code=0)

    retried = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=coordinator_root,
        retry_failed=True,
    )

    assert retried["job_id"] != first["job_id"]
    assert Path(retried["worktree"]) == first_sandbox
    assert not (first_sandbox / "failed-attempt-marker").exists()
    assert (first_sandbox / "source.md").read_text(encoding="utf-8") == "source\n"


def _run(
    *, phase: str, status: str, refs: tuple[GateEvidenceRef, ...],
    brainstorm_required: bool = True,
) -> WorkflowRun:
    now = "2026-07-17T00:00:00+00:00"
    steps = _manifest().steps
    if phase == "ship":
        steps = tuple(
            WorkflowStep(
                phase=step.phase,
                persona=step.persona,
                card=step.card,
                executor="test" if step.phase in {"build", "verify", "review"} else step.executor,
                model="test-model" if step.phase in {"build", "verify", "review"} else step.model,
                domain=(
                    "openai" if step.phase == "build"
                    else "anthropic" if step.phase in {"verify", "review"}
                    else step.domain
                ),
                inputs=step.inputs,
                outputs=step.outputs,
                gate_result=(
                    "passed" if step.phase in {"verify", "review", "ship"}
                    else step.gate_result
                ),
            )
            for step in steps
        )
    return WorkflowRun(
        run_id="workflow-1",
        work_id="work-1",
        repo="owner/repo",
        claim_key="owner/repo/work-1/rev-a",
        source_revision="rev-a",
        workspace_root="/tmp/work-1",
        combo="feature-oneshot",
        current_phase=phase,
        steps=steps,
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        evidence_refs=(),
        gate_refs=refs,
        brainstorm_required=brainstorm_required,
        primary_domain="openai",
        candidate_head="a" * 40 if phase == "ship" else None,
        verified_head="a" * 40 if phase == "ship" else None,
        facets=(),
        gate_status=status,
        created_at=now,
        updated_at=now,
    )


def test_workflow_gate_refs_are_typed_distinct_and_ship_requires_all_three() -> None:
    brainstorm = GateEvidenceRef("brainstorm", "evidence/brainstorm.json")
    foreign = GateEvidenceRef("foreign-review", "evidence/foreign.json")
    copilot = GateEvidenceRef("copilot", "evidence/copilot.json")
    maintainer = GateEvidenceRef("maintainer-review", "evidence/maintainer.json")

    assert _run(phase="review", status="passed", refs=(brainstorm, foreign)).gate_status == "passed"
    assert _run(phase="ship", status="passed", refs=(brainstorm, foreign, copilot)).current_phase == "ship"
    assert _run(phase="ship", status="passed", refs=(brainstorm, foreign, maintainer)).current_phase == "ship"
    with pytest.raises(ValueError, match="foreign-review"):
        _run(phase="review", status="passed", refs=(brainstorm,))
    with pytest.raises(ValueError, match="delivery review"):
        _run(phase="ship", status="passed", refs=(brainstorm, foreign))
    with pytest.raises(ValueError, match="gate_status.*passed"):
        _run(phase="ship", status="running", refs=(brainstorm, foreign, copilot))
    with pytest.raises(ValueError, match="distinct"):
        _run(
            phase="ship",
            status="passed",
            refs=(brainstorm, GateEvidenceRef("foreign-review", brainstorm.ref), copilot),
        )
    no_brainstorm = _run(
        phase="review", status="passed", refs=(foreign,), brainstorm_required=False
    )
    assert no_brainstorm.gate_status == "passed"


def test_restart_reconcile_keeps_publication_when_registry_gate_is_committed(
    tmp_path: Path,
) -> None:
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(
        artifact, b"# Plan\n", baseline_hash=None, kind="artifact"
    )
    evidence = tmp_path / "evidence/brainstorm.json"
    transaction.write_evidence(
        evidence, {"schema_version": 1, "kind": "brainstorm-peer"}
    )
    digest = manager._sha256_path(evidence)
    run = _run(
        phase="plan",
        status="running",
        refs=(GateEvidenceRef("brainstorm", str(evidence), digest),),
    )

    manager._PlanningPublicationTransaction.reconcile(
        root=tmp_path, journal_root=tmp_path, run=run
    )

    assert artifact.is_file()
    assert evidence.is_file()
    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_idempotent_existing_evidence_records_expected_gate_before_registry_commit(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence/brainstorm.json"
    evidence.parent.mkdir()
    evidence_payload = {"schema_version": 1, "kind": "brainstorm-peer"}
    evidence.write_text(
        json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(artifact, b"# Plan\n", baseline_hash=None, kind="artifact")
    transaction.write_evidence(evidence, evidence_payload)
    journal = json.loads(
        (tmp_path / "planning-transactions/workflow-1.json").read_text(encoding="utf-8")
    )
    expected = {
        "kind": "brainstorm",
        "ref": str(evidence),
        "sha256": manager._sha256_path(evidence),
    }
    assert journal["expected_gate_ref"] == expected
    assert [row["kind"] for row in journal["operations"]] == ["artifact", "evidence"]

    run = _run(
        phase="plan", status="running",
        refs=(GateEvidenceRef("brainstorm", expected["ref"], expected["sha256"]),),
    )
    manager._PlanningPublicationTransaction.reconcile(
        root=tmp_path, journal_root=tmp_path, run=run
    )
    assert artifact.is_file()
    assert evidence.is_file()
    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_idempotent_existing_evidence_rejects_noncanonical_mode(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence/brainstorm.json"
    evidence.parent.mkdir()
    payload = {"schema_version": 1, "kind": "brainstorm-peer"}
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    evidence.chmod(0o644)
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )

    with pytest.raises(ValueError, match="immutable evidence mode conflict"):
        transaction.write_evidence(evidence, payload)

    assert not (tmp_path / "planning-transactions/workflow-1.json").exists()


def test_committed_reconcile_detects_artifact_drift_and_preserves_intent(
    tmp_path: Path,
) -> None:
    transaction = manager._PlanningPublicationTransaction(
        root=tmp_path, run_id="workflow-1", journal_root=tmp_path
    )
    artifact = tmp_path / "docs/superpowers/plans/work-1.md"
    transaction.publish(artifact, b"# Plan\n", baseline_hash=None, kind="artifact")
    evidence = tmp_path / "evidence/brainstorm.json"
    transaction.write_evidence(
        evidence, {"schema_version": 1, "kind": "brainstorm-peer"}
    )
    run = _run(
        phase="plan", status="running",
        refs=(GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),),
    )
    artifact.write_text("operator drift\n", encoding="utf-8")

    with pytest.raises(manager.PlanningPublicationDrift, match="drift"):
        manager._PlanningPublicationTransaction.reconcile(
            root=tmp_path, journal_root=tmp_path, run=run
        )

    assert artifact.read_text(encoding="utf-8") == "operator drift\n"
    assert (tmp_path / "planning-transactions/workflow-1.json").is_file()


def test_existing_report_requires_baseline_change_and_embedded_workflow_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    report_ref = "reports/verify/work.md"
    report = tmp_path / report_ref
    report.parent.mkdir(parents=True)
    stale = (
        "---\nworkflow_run_id: run\nworkflow_card_id: card\n"
        f"candidate: {'a' * 40}\n---\n# Verification\n\nPassed.\n"
    )
    report.write_text(stale, encoding="utf-8")
    baseline = manager._sha256_path(report)
    log = tmp_path / "verify.jsonl"
    payload = {
        "schema_version": 1, "kind": "workflow-verification-result",
        "status": "verified", "summary": "ok", "details": {},
        "reports": [{"path": report_ref, "body": "# Verification\n\nPassed after this job."}],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="card", workflow_phase="verify", workflow_repo_root=str(tmp_path),
        workflow_outputs=(report_ref,), source_revision="rev",
        workflow_output_baseline=({"path": report_ref, "sha256": baseline},),
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    report.write_text(stale.replace("Passed.", "Operator drift."), encoding="utf-8")
    with pytest.raises(ValueError, match="baseline CAS conflict"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )

    report.write_text(stale, encoding="utf-8")
    terminal = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=tmp_path
    )
    assert terminal["workflow_evidence"] is not None
    binding = manager._report_binding(report.read_bytes())
    assert binding == {
        "workflow_run_id": "run",
        "workflow_card_id": "card",
        "workflow_job_id": job["job_id"],
        "candidate": "a" * 40,
    }
    assert "Passed after this job." in report.read_text(encoding="utf-8")

    run = SimpleNamespace(
        run_id="run",
        claim_key="claim",
        repo="owner/repo",
        source_revision="rev",
        candidate_head="a" * 40,
    )
    report_bytes = report.read_bytes()
    report_hash = manager._sha256_path(report)
    report.unlink()
    with pytest.raises(ValueError, match="artifact drift"):
        manager._read_job_workflow_evidence(
            terminal,
            run=run,
            coordinator_root=tmp_path,
        )

    work_bridge._write_json_evidence(
        tmp_path,
        "report-cleanup",
        {
            "schema": "cortex-workflow-report-cleanup/v1",
            "run_id": run.run_id,
            "candidate": run.candidate_head,
            "reports": [{"path": report_ref, "sha256": report_hash}],
        },
    )
    payload, outputs, _path, _digest = manager._read_job_workflow_evidence(
        terminal,
        run=run,
        coordinator_root=tmp_path,
    )
    assert payload["status"] == "verified"
    assert outputs == (report_ref,)

    report.write_bytes(report_bytes)
    original_read_bytes = Path.read_bytes
    raced = False

    def disappear_before_read(path: Path) -> bytes:
        nonlocal raced
        if path == report and not raced:
            raced = True
            report.unlink()
            raise FileNotFoundError(report)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", disappear_before_read)
    payload, outputs, _path, _digest = manager._read_job_workflow_evidence(
        terminal,
        run=run,
        coordinator_root=tmp_path,
    )
    assert payload["status"] == "verified"
    assert outputs == (report_ref,)


def test_report_cleanup_evidence_enumeration_is_bounded_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "evidence" / "report-cleanup"
    directory.mkdir(parents=True)
    run = SimpleNamespace(run_id="run", candidate_head="a" * 40)
    original_iterdir = Path.iterdir

    def too_many_markers(path: Path):
        if path == directory:
            for index in range(2049):
                yield directory / f"{index:064x}.json"
            return
        yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", too_many_markers)
    assert manager._workflow_report_cleanup_allows_missing(
        coordinator_root=tmp_path,
        run=run,
        ref="reports/verify/work.md",
        expected_hash="b" * 64,
    ) is False

    def failed_enumeration(path: Path):
        if path == directory:
            raise OSError("directory enumeration failed")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failed_enumeration)
    assert manager._workflow_report_cleanup_allows_missing(
        coordinator_root=tmp_path,
        run=run,
        ref="reports/verify/work.md",
        expected_hash="b" * 64,
    ) is False


def test_terminal_report_manifest_cannot_authorize_arbitrary_markdown_overwrite(
    tmp_path: Path,
) -> None:
    registry = JobRegistry(state_path=tmp_path / "state.json")
    readme = tmp_path / "README.md"
    readme.write_text("operator content\n", encoding="utf-8")
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [{"path": "README.md", "body": "replaced"}],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-wide", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=("**/*.md",),
        source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="manifest root invalid"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path / "coordinator"
        )
    assert readme.read_text(encoding="utf-8") == "operator content\n"


def test_report_publication_rolls_back_when_registry_bind_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    report_ref = "reports/verify/work.md"
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [{"path": report_ref, "body": "# Verification\n\nPassed."}],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-bind-fault", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=(report_ref,), source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    monkeypatch.setattr(
        registry,
        "bind_workflow_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("save fault")),
    )

    with pytest.raises(OSError, match="save fault"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=coordinator
        )
    assert not (tmp_path / report_ref).exists()
    assert not list((coordinator / "workflow-report-transactions").glob("*.json"))
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None


def test_multi_report_partial_write_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    refs = ("reports/verify/work-a.md", "reports/verify/work-b.md")
    log = tmp_path / "verify.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "ok",
        "details": {},
        "reports": [
            {"path": refs[0], "body": "# A"},
            {"path": refs[1], "body": "# B"},
        ],
    }) + "\n", encoding="utf-8")
    job = registry.create_job(
        task="verify-partial", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(tmp_path), workflow_outputs=("reports/verify/*.md",),
        source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    original = manager._PlanningPublicationTransaction._write_atomic
    failed = False

    def flaky(path, content, mode, **kwargs):
        nonlocal failed
        if path.name == "work-b.md" and not failed:
            failed = True
            raise OSError("second write fault")
        return original(path, content, mode, **kwargs)

    monkeypatch.setattr(
        manager._PlanningPublicationTransaction,
        "_write_atomic",
        staticmethod(flaky),
    )
    with pytest.raises(OSError, match="second write fault"):
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=coordinator
        )
    assert all(not (tmp_path / ref).exists() for ref in refs)
    assert not list((coordinator / "workflow-report-transactions").glob("*.json"))


def test_forged_report_journal_traversal_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    job = registry.create_job(
        task="verify-journal", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(repo), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(repo), workflow_outputs=("reports/verify/*.md",),
        source_revision="rev",
    )
    transaction = manager._WorkflowReportPublicationTransaction(
        repo_root=repo,
        coordinator_root=coordinator,
        job_id=job["job_id"],
    )
    transaction.publish(
        (("reports/verify/work.md", "# Verification"),),
        job=job,
        candidate="a" * 40,
    )
    payload = json.loads(transaction.journal_path.read_text(encoding="utf-8"))
    payload["operations"][0]["path"] = str(
        repo / "reports" / "verify" / ".." / ".." / ".." / "outside.md"
    )
    transaction.journal_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(manager.WorkflowReportPublicationDrift, match="operation invalid"):
        manager._WorkflowReportPublicationTransaction.reconcile(
            registry=registry,
            job=job,
            coordinator_root=coordinator,
        )
    assert not (tmp_path / "outside.md").exists()
    assert transaction.journal_path.is_file()


def test_reviewer_disposable_checkout_detects_candidate_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "canary@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Canary"], check=True)
    readme = repo / "README.md"
    readme.write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    run = SimpleNamespace(run_id="workflow-review", candidate_head=candidate)
    step = SimpleNamespace(card="verification")
    coordinator = tmp_path / "coordinator"
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run,
        step=step,
        executor="claude",
        candidate_root=repo,
        coordinator_root=coordinator,
        input_snapshot=(),
    )
    assert sandbox != repo
    assert subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == candidate
    assert subprocess.run(
        ["git", "-C", str(checkout), "remote"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == ""
    assert all((sandbox / ref).is_file() for ref in manager._CLAUDE_REVIEW_PROTECTED_FILES)
    assert all((sandbox / ref).is_dir() for ref in manager._CLAUDE_REVIEW_PROTECTED_DIRS)
    assert subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"], check=True,
        capture_output=True, text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/forbidden"],
        capture_output=True, text=True, check=False,
    ).returncode != 0
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    job = registry.create_job(
        task="review-mutation", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(sandbox), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head=candidate,
        workflow_run_id="workflow-review", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="verification", workflow_phase="verify",
        workflow_repo_root=str(repo), workflow_input_root=str(checkout),
        workflow_sandbox_hash=manager.planning_runtime._tree_snapshot(repo),
        source_revision="rev",
    )
    readme.write_text("reviewer mutation\n", encoding="utf-8")

    with pytest.raises(ValueError, match="modified Candidate"):
        manager._discard_reviewer_sandbox(
            job,
            coordinator_root=coordinator,
            require_candidate_unchanged=True,
        )
    assert not sandbox.exists()


def test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "canary@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Canary"], check=True
    )
    (repo / "README.md").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    steps = tuple(
        WorkflowStep.from_dict(
            {
                **step.to_dict(),
                "gate_result": (
                    "passed"
                    if step.phase in {"claim", "define", "plan", "build"}
                    else "pending"
                ),
            }
        )
        for step in _manifest().steps
    )
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="verify",
        steps=steps,
        candidate_head=candidate,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"verify": 1},
        facets=("needs_human",),
        gate_status="running",
    )
    builder = registry.create_job(
        task="wf-builder",
        persona="builder",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(repo),
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        workflow_input_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    verify_step = next(step for step in run.steps if step.card == "verification")
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run,
        step=verify_step,
        executor="codex",
        candidate_root=repo,
        coordinator_root=coordinator,
        input_snapshot=(),
    )
    log_root = coordinator / "logs" / "workflow"
    log_root.mkdir(parents=True)
    legacy = registry.create_job(
        task="wf-verification",
        persona="reviewer",
        kind="review",
        branch="feature/14-production-wiring",
        pane="",
        worktree=str(sandbox),
        executor="claude",
        model_id="sonnet",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=verify_step.card,
        workflow_phase=verify_step.phase,
        workflow_repo_root=str(repo),
        workflow_input_root=str(checkout),
        workflow_outputs=verify_step.outputs,
        workflow_output_baseline=(),
        workflow_sandbox_hash=manager.planning_runtime._tree_snapshot(repo),
        workflow_builder_job_id=builder["job_id"],
        source_revision=run.source_revision,
    )
    log = log_root / f"{legacy['job_id']}.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Plan Mode prevented tests; no terminal JSON was produced.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.attach_launch_handle(
        legacy["job_id"],
        executor="claude",
        model_id="sonnet",
        session_name=legacy["job_id"],
        log_path=str(log),
    )
    registry.update_headless_result(legacy["job_id"], status="exited", exit_code=0)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
                "capabilities": ["review"],
            },
        ]
    )
    bound = registry.get_job(legacy["job_id"])
    assert manager._is_exact_reviewer_terminal_recovery(
        registry,
        bound,
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "subject_head": "f" * 40},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "worktree": str(sandbox.with_name("0" * 32))},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "workflow_input_root": str(repo)},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )
    assert not manager._is_exact_reviewer_terminal_recovery(
        registry,
        {**bound, "workflow_repo_root": str(workspace)},
        run=run,
        step=verify_step,
        identities=identities,
        coordinator_root=coordinator,
    )

    launched: list[tuple[str, str, str]] = []

    class Launcher:
        def as_review_only(self, *, terminal_kind):
            assert terminal_kind == "workflow-verification-result"
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append((slice_id, prompt, worktree))
            return LaunchHandle(
                executor="claude",
                model_id="sonnet",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    stopped = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=coordinator,
    )
    assert stopped["reason"] == "operator-resume-required"
    assert launched == []

    resumed = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=coordinator,
        operator_resume=True,
    )
    assert resumed["reason"] == "in-flight"
    assert resumed["job_id"] != legacy["job_id"]
    assert [row[0] for row in launched] == [resumed["job_id"]]
    assert '"candidate_checkout": "candidate"' in launched[0][1]
    assert launched[0][2] == str(sandbox)
    replacement = registry.get_job(resumed["job_id"])
    assert Path(replacement["worktree"]) == sandbox
    assert Path(replacement["workflow_input_root"]) == sandbox / "candidate"
    assert (sandbox / "candidate").is_dir()
    assert registry.get_job(legacy["job_id"])["workflow_evidence"] is None


def test_review_terminal_rejects_non_builder_job_binding_before_publication(
    tmp_path: Path,
) -> None:
    candidate = "a" * 40
    steps = tuple(
        WorkflowStep.from_dict({
            **step.to_dict(),
            "gate_result": "passed" if step.phase in {"claim", "define", "plan", "build", "verify"} else "pending",
        })
        for step in _manifest().steps
    )
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="production-wiring", repo="owner/repo",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase="review",
        steps=steps, issue_refs=(), openspec_refs=(), pr_refs=(),
        attempts={"review": 1}, candidate_head=candidate, verified_head=candidate,
        gate_status="running",
    )
    invalid_builder = registry.create_job(
        task="invalid-builder", persona="manager", kind="build", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="codex", model_id="builder",
        independence_domain="openai", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="subagent-build", workflow_phase="build",
        workflow_repo_root=str(tmp_path), source_revision=run.source_revision,
    )
    registry.update_headless_result(invalid_builder["job_id"], status="exited", exit_code=0)
    report_ref = "reports/review/production-wiring.md"
    log = tmp_path / "review.jsonl"
    log.write_text(json.dumps({
        "schema_version": 1, "kind": "workflow-review-result", "reason": "accepted",
        "findings": [], "reports": [{"path": report_ref, "body": "# Review"}],
    }) + "\n", encoding="utf-8")
    review_job = registry.create_job(
        task="review-invalid-builder", persona="reviewer", kind="review",
        branch="feature/work", pane="", worktree=str(tmp_path), executor="claude",
        model_id="reviewer", independence_domain="anthropic", subject_head=candidate,
        workflow_run_id=run.run_id, workflow_claim_key=run.claim_key,
        workflow_repo=run.repo, workflow_card="code-review", workflow_phase="review",
        workflow_repo_root=str(tmp_path), workflow_outputs=(report_ref,),
        workflow_builder_job_id=invalid_builder["job_id"], source_revision=run.source_revision,
    )
    registry.attach_launch_handle(review_job["job_id"], log_path=str(log))
    _gate_ledger_passed(log)
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError, match="builder binding mismatch: persona"):
        manager.terminalize_workflow_job(
            registry, job_id=review_job["job_id"], coordinator_root=coordinator
        )
    assert not (tmp_path / report_ref).exists()


def test_planning_replacement_requires_persisted_authority_not_caller_hash(
    tmp_path: Path,
) -> None:
    ref = "docs/superpowers/specs/production-wiring-spec.md"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nstatus: draft\n---\n# Spec\n## Requirements\nTBD\n",
        encoding="utf-8",
    )
    authority = PlanningArtifactAuthority(
        ref=ref, kind="spec", work_id="production-wiring",
        baseline_sha256=manager._sha256_path(path),
    )
    replacement = {
        "kind": "spec", "path": ref,
        "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
    }
    rollback = manager._publish_planning_artifacts(
        str(tmp_path), [replacement], work_id="production-wiring",
        allowed_refs=("docs/superpowers/specs/*production-wiring*-spec.md",),
        authorities=(authority,),
    )
    rollback()

    forged = PlanningArtifactAuthority(
        ref=ref, kind="design", work_id="production-wiring",
        baseline_sha256=authority.baseline_sha256,
    )
    with pytest.raises(ValueError, match="current planning authority"):
        manager._publish_planning_artifacts(
            str(tmp_path), [replacement], work_id="production-wiring",
            allowed_refs=("docs/superpowers/specs/*production-wiring*-spec.md",),
            authorities=(forged,),
        )


def test_complete_plan_does_not_require_or_launch_brainstorm(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    proposal = tmp_path / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    rows = []
    for kind, body in bodies.items():
        # #414：同上——plan 的 ref 需落在 writing-plans 卡宣告的 canonical
        # outputs glob 內，否則 deterministic pass 前的驗證判定缺席，觸發
        # materialize fallback，多出一筆 planning_authority。
        ref = (
            "docs/superpowers/plans/production-wiring.md"
            if kind == "plan"
            else f"docs/{kind}.md"
        )
        path = tmp_path / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        rows.append({"kind": kind, "ref": ref})
    args = _workflow_args(manifest_path, tmp_path)
    args["planning_artifacts"] = rows
    args["primary_domain"] = "openai"
    launched: list[str] = []

    class Launcher:
        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            launched.append(slice_id)
            return LaunchHandle(
                executor="test",
                model_id="test",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=Launcher(),
        workflow_runtime_factory=lambda **_: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    result = executor(build_request(req_type="workflow-action", args=args, requested_by="operator"))
    run = registry.get_workflow_run(result["run_id"])
    assert result["reason"] == "planning-complete"
    assert launched == []
    assert run.brainstorm_required is False
    assert run.current_phase == "build"
    assert run.gate_refs == ()
    assert {
        (authority.ref, authority.kind, authority.work_id, authority.baseline_sha256)
        for authority in run.planning_authority
    } == {
        (row["ref"], row["kind"], "production-wiring", manager._sha256_path(tmp_path / row["ref"]))
        for row in rows
    }


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_brainstorm_publication_reconciles_registry_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_before_error: bool,
) -> None:
    state_path = tmp_path / "registry.json"
    registry = JobRegistry(state_path=state_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            },
            {
                "executor": "claude", "model_id": "secondary",
                "independence_domain": "anthropic", "capabilities": ["planning"],
            },
        ]
    )

    def questioner(report):
        from paulsha_cortex.coordinator.planning import assess_planning_completeness

        return assess_planning_completeness([]).default_question_pack.to_dict()

    def secondary(pack, identity):
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "evidence": [
                {"question_id": row["question_id"], "claims": ["missing"], "source_refs": ["scan:1"]}
                for row in pack["questions"]
            ],
        }

    def integrator(pack, evidence):
        bodies = {
            "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
            "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n",
            "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
        }
        refs = {
            "spec": "docs/superpowers/specs/production-wiring-spec.md",
            "design": "docs/superpowers/specs/production-wiring-design.md",
            "plan": "docs/superpowers/plans/production-wiring-plan.md",
        }
        resolutions = []
        artifacts = []
        for row in pack["questions"]:
            kind = row["kind"].removeprefix("missing-")
            resolutions.append(
                {
                    "question_id": row["question_id"], "decision": "accepted",
                    "artifact_kind": kind, "artifact_refs": [refs[kind]],
                }
            )
            artifacts.append({"kind": kind, "path": refs[kind], "content": bodies[kind]})
        return {
            "schema_version": 1, "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": resolutions, "artifacts": artifacts,
        }

    args = _workflow_args(manifest_path, tmp_path)
    args.update({"primary_model": "primary"})
    real_write = registry._write_payload_atomically
    failed = False

    def fail_plan_transition(payload):
        nonlocal failed
        if not failed and any(
            row.get("current_phase") == "plan" and row.get("gate_refs")
            for row in payload.get("workflows", [])
        ):
            failed = True
            if commit_before_error:
                real_write(payload)
            raise OSError("registry save fault")
        real_write(payload)

    monkeypatch.setattr(registry, "_write_payload_atomically", fail_plan_transition)
    with pytest.raises(OSError, match="registry save fault"):
        manager.apply_workflow_action(
            registry, args=args, identity_registry=identities,
            probes={
                ("claude", "secondary"): CapabilityProbe.ready_for(
                    "claude", "secondary", "anthropic"
                )
            },
            primary_questioner=questioner, secondary_planner=secondary,
            primary_integrator=integrator, coordinator_root=tmp_path,
        )

    if commit_before_error:
        assert registry.list_workflow_runs()[0].current_phase == "plan"
        assert (tmp_path / "docs/superpowers").is_dir()
        assert list((tmp_path / "evidence").glob("brainstorm-*.json"))
        assert not list((tmp_path / "planning-transactions").glob("*.json"))
    else:
        assert registry.list_workflow_runs()[0].current_phase == "define"
        assert not (tmp_path / "docs/superpowers").exists()
        assert not list((tmp_path / "evidence").glob("brainstorm-*.json"))

    restarted = JobRegistry(state_path=state_path)
    result = manager.apply_workflow_action(
        restarted, args=args, identity_registry=identities,
        probes={
            ("claude", "secondary"): CapabilityProbe.ready_for(
                "claude", "secondary", "anthropic"
            )
        },
        primary_questioner=questioner, secondary_planner=secondary,
        primary_integrator=integrator, coordinator_root=tmp_path,
    )
    assert result["reason"] == (
        "already-claimed" if commit_before_error else "brainstorm-complete"
    )
    assert restarted.get_workflow_run(result["run_id"]).current_phase == "plan"
    assert {
        authority.ref
        for authority in restarted.get_workflow_run(result["run_id"]).planning_authority
    } == {
        "docs/superpowers/specs/production-wiring-spec.md",
        "docs/superpowers/specs/production-wiring-design.md",
        "docs/superpowers/plans/production-wiring-plan.md",
    }
    assert not list((tmp_path / "planning-transactions").glob("*.json"))


def test_manager_rejects_forged_persona_spine(tmp_path: Path) -> None:
    manifest = _manifest()
    bad_steps = tuple(
        WorkflowStep(
            phase=step.phase,
            persona="builder" if step.phase == "review" else step.persona,
            card=step.card,
            executor=step.executor,
            model=step.model,
            domain=step.domain,
            inputs=step.inputs,
            outputs=step.outputs,
            gate_result=step.gate_result,
        )
        for step in manifest.steps
    )
    forged = WorkflowManifest(combo=manifest.combo, task_slug=manifest.task_slug, steps=bad_steps)
    with pytest.raises(ValueError, match="review.*reviewer"):
        forged.validate_manager_spine()


def test_run_loop_workflow_request_calls_production_runtime_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control_root = tmp_path / "control"
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(control_root))
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    args = _workflow_args(manifest_path, tmp_path)
    request = build_request(req_type="workflow-action", args=args, requested_by="operator")
    contract.atomic_write_json(constants.requests_dir() / f"{request['req_id']}.json", request)
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "gpt-primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            }
        ]
    )
    calls: list[tuple[tuple[str, str], Path]] = []
    drift_wiring: list[tuple[Path, str]] = []

    # #507：factory 另收 `evidence_root`／`run_id`——operator worktree drift 的
    # 備份與報告要落在 run-scoped evidence 底下才找得到，這裡一併釘住 wiring。
    def factory(*, primary, worktree, evidence_root, run_id):
        calls.append((primary, Path(worktree)))
        drift_wiring.append((Path(evidence_root), run_id))
        return planning_runtime.ProductionPlanningRuntime(
            identities,
            {},
            lambda report: {},
            lambda pack, identity: {},
            lambda pack, evidence: {},
        )

    monkeypatch.setattr(planning_runtime, "build_production_planning_runtime", factory)
    started = manager_daemon.run_loop(
        poll_interval=0,
        tick_interval=300,
        monotonic_fn=lambda: 0,
        sleep_fn=lambda _: None,
        max_rounds=1,
        registry=registry,
        dispatcher=dispatcher,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
    )
    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")

    assert started is True
    assert calls == [(('codex', 'gpt-primary'), tmp_path)]
    assert done and done["status"] == "ok"
    assert done["result"]["reason"] == "no-heterogeneous-planner"
    assert len(drift_wiring) == 1
    evidence_root, run_id = drift_wiring[0]
    # `evidence_dir` 是 `<artifact_root>/evidence`，transaction_root 取其 parent。
    assert evidence_root == tmp_path.resolve()
    assert run_id == done["result"]["run_id"]


def test_registry_restores_file_and_memory_when_directory_fsync_fails_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "registry.json"
    registry = JobRegistry(state_path=state)
    registry.create_job(task="baseline", persona="builder", branch="feature/base", pane="%0", worktree="/wt/base")
    original = state.read_bytes()
    calls = 0
    real_fsync = registry_module._fsync_directory

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync fault")
        real_fsync(path)

    monkeypatch.setattr(registry_module, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="directory fsync fault"):
        registry.create_job(task="new", persona="builder", branch="feature/new", pane="%1", worktree="/wt/new")

    assert state.read_bytes() == original
    assert [job["task"] for job in registry.list_jobs()] == ["baseline"]
    assert calls >= 2
