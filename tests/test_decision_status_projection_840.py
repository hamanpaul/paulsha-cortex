"""#840（refine R10）：投影動態派工決策與額度等待來源。

驗收對應原票「最小機械驗收」六條：

- AC1：多卡／retry／跨 run-card exact-key fixture；#828 actual/planned/last
  不混用、不借同 phase 其他 job。
- AC2：negative——wait 無 Job、unknown remaining、缺 qualification/provenance、
  needs_human 語意保留、planned 不造 actual。
- AC3：各 section（`workflow_status_entry` 與
  `WorkflowRegistryProvider.scan()`）對同一份 snapshot 算出一致的
  decision/reason/freshness。
- AC4：restart/replay——store 重讀得到相同結果，last-good／stale 原因保留，
  舊 attempt receipt 不覆蓋當前。
- AC5：讀取前後 registry/decision store bytes 不變；allowlist 負面 fixture。
- AC6：producer（真實 `AdmissionDecisionStore`）→ snapshot（投影函式）→
  CLI／status 端到端接通。
"""

from __future__ import annotations

import hashlib
import json
import stat as stat_module
from pathlib import Path

import pytest

from diagnostic_fixtures import fixture_needs_human_reason

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import quota_admission as admission
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.monitor import decision_projection as dp
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider

REPO = "hamanpaul/paulsha-cortex"
_NOW = 1_900_000_000_000


# ---------------------------------------------------------------------------
# 共用 fixture 建構
# ---------------------------------------------------------------------------


def _step(
    card: str,
    *,
    phase: str = "build",
    persona: str = "builder",
    executor: str | None = "planned-executor",
    model: str | None = "planned-model",
) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor=executor,
        model=model,
        domain="test-domain",
        inputs=(),
        outputs=(),
        gate_result="pending",
    )


def _run(
    registry: JobRegistry,
    tmp_path: Path,
    *,
    work_id: str,
    steps: tuple[WorkflowStep, ...],
    facets: tuple[str, ...] = ("needs_human",),
    needs_human_reason=None,
):
    if needs_human_reason is None and "needs_human" in facets:
        needs_human_reason = fixture_needs_human_reason()
    return registry._manager_create_workflow_run(
        work_id=work_id,
        repo=REPO,
        claim_key=f"{REPO}/{work_id}/0",
        source_revision="a" * 64,
        workspace_root=str(tmp_path / "workspace" / work_id),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        attempts={"build": 1},
        facets=facets,
        gate_status="running" if "needs_human" not in facets else "failed",
        needs_human_reason=needs_human_reason,
    )


def _decision(
    *,
    decision_id: str,
    run_id: str = "run-1",
    card_id: str = "card-1",
    attempt_id: str = "attempt-0",
    profile_key: str = "epk:v1:resolved:" + "a" * 64,
    mode: str = "shadow",
    outcome: str = "admit",
    selected: dict | None = None,
    excluded: tuple = (),
    selected_observation_state: str | None = "known",
    selected_feasible: bool | None = True,
    policy_config_revision: str | None = "pools-config:v1",
    generated_at_ms: int = _NOW,
    reservation_id: str | None = None,
    demand_version: str = admission.DEMAND_FIXTURE_VERSION,
) -> admission.AdmissionDecision:
    return admission.AdmissionDecision(
        decision_id=decision_id,
        run_id=run_id,
        card_id=card_id,
        attempt_id=attempt_id,
        profile_key=profile_key,
        mode=mode,
        outcome=outcome,
        policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:" + "b" * 16,
        demand_version=demand_version,
        qualification_version="not-enforced",
        generated_at_ms=generated_at_ms,
        selected=selected if selected is not None else {"executor": "codex", "model_id": "gpt-5.3-codex"},
        reservation_id=reservation_id,
        excluded=excluded,
        selected_observation_state=selected_observation_state,
        selected_feasible=selected_feasible,
        policy_config_revision=policy_config_revision,
    )


def _quota_admission_pointer(decision: admission.AdmissionDecision) -> dict[str, dict[str, str]]:
    return {"builder": {"decision_id": decision.decision_id, "mode": decision.mode, "outcome": decision.outcome}}


# ---------------------------------------------------------------------------
# AC1：多卡／retry／跨 run-card exact-key fixture
# ---------------------------------------------------------------------------


def test_two_personas_projected_independently_not_mixed(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    planner_decision = _decision(
        decision_id="adm:v1:" + "1" * 64, card_id="plan-card", attempt_id="n0",
        selected={"executor": "claude", "model_id": "opus"},
    )
    builder_decision = _decision(
        decision_id="adm:v1:" + "2" * 64, card_id="build-card", attempt_id="n0",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(planner_decision)
    store.record(builder_decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={
            "planner": {"decision_id": planner_decision.decision_id, "mode": "shadow", "outcome": "admit"},
            "builder": {"decision_id": builder_decision.decision_id, "mode": "shadow", "outcome": "admit"},
        },
        needs_human_reason=None,
        store=store,
        now_ms=_NOW,
    )

    assert projection["personas"]["planner"]["selected"] == {"executor": "claude", "model_id": "opus"}
    assert projection["personas"]["builder"]["selected"] == {
        "executor": "codex", "model_id": "gpt-5.3-codex",
    }
    # 兩個 persona 的決策彼此獨立，不互相污染 excluded／decision_id。
    assert (
        projection["personas"]["planner"]["decision_id"]
        != projection["personas"]["builder"]["decision_id"]
    )


def test_retry_current_attempt_not_overridden_by_earlier_attempt(tmp_path: Path) -> None:
    """同一 run/card 兩個 attempt 的 receipt：run 指標指到新 attempt 時，投影
    只呈現新 attempt，舊 attempt receipt 仍完整保留在 store（不覆蓋、不刪除）。
    """
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    old_decision = _decision(
        decision_id="adm:v1:" + "3" * 64, attempt_id="n0",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    new_decision = _decision(
        decision_id="adm:v1:" + "4" * 64, attempt_id="n1",
        selected={"executor": "claude", "model_id": "sonnet"},
    )
    store.record(old_decision)
    store.record(new_decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": new_decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        store=store,
        now_ms=_NOW,
    )

    assert projection["personas"]["builder"]["decision_id"] == new_decision.decision_id
    assert projection["personas"]["builder"]["selected"] == {"executor": "claude", "model_id": "sonnet"}
    # 舊 attempt 的 receipt 沒有被覆蓋或刪除——append-only store 仍能查到。
    assert store.get(old_decision.decision_id) is not None
    assert store.get(old_decision.decision_id).selected == {"executor": "codex", "model_id": "gpt-5.3-codex"}


def test_cross_run_card_isolation_exact_key(tmp_path: Path) -> None:
    """同一個 store 裡兩個不同 run 的決策不得互相洩漏——exact-key 查找。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    run_a_decision = _decision(
        decision_id="adm:v1:" + "5" * 64, run_id="run-a", card_id="build-card",
        selected={"executor": "codex", "model_id": "model-a"},
        excluded=({"executor": "claude", "model_id": "model-x", "exclusion_reason": "insufficient-quota"},),
    )
    run_b_decision = _decision(
        decision_id="adm:v1:" + "6" * 64, run_id="run-b", card_id="build-card",
        selected={"executor": "codex", "model_id": "model-b"},
    )
    store.record(run_a_decision)
    store.record(run_b_decision)

    projection_a = dp.project_workflow_quota_admission(
        run_id="run-a",
        quota_admission={"builder": {"decision_id": run_a_decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None, store=store, now_ms=_NOW,
    )
    projection_b = dp.project_workflow_quota_admission(
        run_id="run-b",
        quota_admission={"builder": {"decision_id": run_b_decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    assert projection_a["personas"]["builder"]["selected"]["model_id"] == "model-a"
    assert len(projection_a["personas"]["builder"]["excluded"]) == 1
    assert projection_b["personas"]["builder"]["selected"]["model_id"] == "model-b"
    assert projection_b["personas"]["builder"]["excluded"] == []


# ---------------------------------------------------------------------------
# AC2：negative fixtures
# ---------------------------------------------------------------------------


def test_wait_without_job_is_not_presented_as_admitted(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    needs_human_reason = diagnostic_reason(
        "quota-admission-insufficient",
        "quota-aware admission 判定所有候選目前額度不足或未知，暫停派工：insufficient-quota",
        source="manager._dispatch_workflow_card:quota-admission",
        run_id="run-1", card="build-card", attempted_candidates="2",
    ).to_dict()

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=None, needs_human_reason=needs_human_reason,
        store=store, now_ms=_NOW,
    )

    assert projection["personas"] == {}
    assert projection["wait"]["reason"] == "quota-admission-insufficient"
    # 沒有任何候選被 admit——不得偽造出一個 job/decision。
    rendered = json.dumps(projection)
    assert '"outcome": "admit"' not in rendered
    assert '"job_id"' not in rendered


def test_unknown_remaining_selected_candidate_is_not_confirmed(tmp_path: Path) -> None:
    """shadow 模式下即使候選 unknown remaining 一樣會被 admit——投影必須忠實
    標成 unknown，不得呈現成『現在可派』的 confirmed。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 64,
        selected_observation_state="unknown",
        selected_feasible=False,
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["classification"]["observation"] == "unknown"
    assert persona["selected_feasible"] is False


def test_expired_observation_gap_marks_observation_unknown(tmp_path: Path) -> None:
    """#836 `expired-observation` 語意：候選在評估當下觀測已過期，selected
    candidate 的 observation_state 仍必須是 unknown，不得靜默視為 confirmed。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "8" * 64,
        selected_observation_state="unknown",
        selected_feasible=False,
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    assert projection["personas"]["builder"]["classification"]["observation"] == "unknown"


def test_missing_decision_in_store_reports_gap_not_confirmed(tmp_path: Path) -> None:
    """run 上有指標，但 store 從未寫過這個 decision_id（例如部署競態、或指標
    本身損毀）——必須回報缺值原因，不得憑空生出一個 confirmed 決策。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": "adm:v1:" + "9" * 64, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["available"] is False
    assert persona["stale"] is False
    assert persona["gap_reason"] == "decision-not-found"
    assert "mode" not in persona


def test_legacy_receipt_missing_provenance_fields_defaults_to_unknown(tmp_path: Path) -> None:
    """#840 之前寫的舊 receipt（缺 selected_observation_state／
    policy_config_revision）讀回後投影必須明確標 unknown，不得臆測成
    confirmed。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    legacy_row = {
        "schema_version": 1, "decision_id": "adm:v1:" + "a" * 64, "run_id": "run-1",
        "card_id": "card-1", "attempt_id": "n0", "profile_key": "epk:v1:resolved:" + "a" * 64,
        "mode": "shadow", "outcome": "admit", "policy_version": admission.ADMISSION_POLICY_VERSION,
        "observation_version": "shadow-projection:" + "c" * 16, "demand_version": admission.DEMAND_FIXTURE_VERSION,
        "qualification_version": "not-enforced", "generated_at_ms": _NOW,
        "selected": {"executor": "codex", "model_id": "gpt-5.3-codex"}, "reservation_id": None,
        "excluded": [], "reason": None, "job_id": None,
    }
    store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.path.write_text(json.dumps(legacy_row, sort_keys=True) + "\n", encoding="utf-8")
    store.path.chmod(0o600)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": legacy_row["decision_id"], "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["available"] is True
    assert persona["classification"]["observation"] == "unknown"
    assert persona["policy_config_revision"] is None


def test_needs_human_reason_preserved_verbatim_no_invented_actions(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    reason = diagnostic_reason(
        "quota-config-invalid",
        "operator quota-pools 設定檔存在但無效，opt-in enforce 已開，fail closed 暫停派工：bad-schema",
        source="manager._dispatch_workflow_card:quota-admission-config",
        next_step_hint="修正 operator quota-pools 設定檔後重試。",
        run_id="run-1", card="build-card",
    ).to_dict()

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=None, needs_human_reason=reason, store=store, now_ms=_NOW,
    )

    wait = projection["wait"]
    assert wait["reason"] == reason["reason"]
    assert wait["detail"] == reason["detail"]
    assert wait["next_step_hint"] == reason["next_step_hint"]
    assert wait["context"] == reason["context"]
    # 不得自行編造出這份理由沒有的 key（例如捏造 next_actions）。
    assert "next_actions" not in wait


def test_non_quota_wait_reason_is_not_projected_as_quota_wait(tmp_path: Path) -> None:
    """needs_human 的理由若與額度無關（例如 runtime-preflight），不得被誤判
    成 quota-wait。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    reason = diagnostic_reason(
        "runtime-preflight-capability_missing", "缺少 capability", source="manager.x:y",
    ).to_dict()

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=None, needs_human_reason=reason, store=store, now_ms=_NOW,
    )

    assert projection["wait"] is None


def test_planned_identity_does_not_fabricate_actual_and_has_no_quota_evidence(tmp_path: Path) -> None:
    """#828：run 只有 planned 執行意圖、從未真正派工——`_workflow_execution_identity`
    必須回報 planned／not-dispatched，quota_decision 完全不出現（沒有任何
    #839 證據時維持既有 attention 形狀，見 workflow_status_entry 文件字串）。"""
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(registry, tmp_path, work_id="planned-only", steps=(_step("build-card"),))

    entry = manager.workflow_status_entry(registry, run)

    assert entry["identity_source"] == "planned"
    assert entry["execution_state"] == "not-dispatched"
    assert entry["job_id"] is None
    assert "quota_decision" not in entry


# ---------------------------------------------------------------------------
# AC3：各 section 對同一份 snapshot 一致
# ---------------------------------------------------------------------------


def test_status_entry_and_work_show_row_agree_on_same_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    # 對抗審查第三輪（MAJOR）：步卡的 executor／model 必須與下面 admit
    # 決策的 `selected` 一致——`WorkflowRegistryProvider.scan()` 現在會比對
    # 兩者是否相符（見 `_current_persona_identity_from_steps`），不一致會
    # 被判成『目前 attempt 尚無決策』而不是這裡要驗證的完整投影。
    run = _run(
        registry, tmp_path, work_id="consistency-840",
        steps=(_step("build-card", executor="codex", model="gpt-5.3-codex"),),
        facets=("needs_human",),
    )
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "b" * 64, run_id=run.run_id, card_id="build-card",
    )
    store.record(decision)
    registry._manager_update_workflow_run(
        run.run_id, quota_admission=_quota_admission_pointer(decision),
    )
    run = registry.get_workflow_run(run.run_id)

    status_entry = manager.workflow_status_entry(registry, run, quota_decision_store=store)

    provider_result = WorkflowRegistryProvider(
        REPO, state_path=state, quota_decision_store=store,
    ).scan()
    row_projection = provider_result.observations["quota_decisions"]["consistency-840"]

    assert (
        status_entry["quota_decision"]["personas"]["builder"]["decision_id"]
        == row_projection["personas"]["builder"]["decision_id"]
        == decision.decision_id
    )
    assert (
        status_entry["quota_decision"]["personas"]["builder"]["classification"]
        == row_projection["personas"]["builder"]["classification"]
    )
    assert (
        status_entry["quota_decision"]["personas"]["builder"]["mode"]
        == row_projection["personas"]["builder"]["mode"]
    )


# ---------------------------------------------------------------------------
# 對抗審查第二輪 MAJOR（decision_projection.py 約 250）：wait receipt 不得
# 借用上一個 attempt 的 execution_profile_bindings。
# ---------------------------------------------------------------------------


def test_wait_receipt_after_retry_does_not_borrow_previous_attempt_profile(tmp_path: Path) -> None:
    """先成功派工（attempt n0，admit，profile A）→ retry-card → 這次 attempt
    （n1）全數候選被拒（quota-config-invalid／wait，沒有選中候選）。

    `execution_profile_bindings["builder"]` 只有 `manager._record_resolved_model_chain()`
    真的走到派工才會被覆寫（見 #835）；這次 wait 從未走到那個寫入點，run 上
    仍是 attempt n0 留下的舊值。投影不得把這份舊 binding 當成這次 wait 的
    結果——requested 必須是 ``None``、resolved 必須是 ``"unknown"``，不得
    呈現成 attempt n0 的 profile。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    admit_decision = _decision(
        decision_id="adm:v1:" + "1" * 62 + "a1", attempt_id="n0", outcome="admit",
        profile_key="epk:v1:resolved:" + "a" * 64,
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    # 比照 `manager._quota_admission_record_wait_decision()` 產出的真實 wait
    # receipt 形狀：`selected=None`、`observation_version`／`demand_version`
    # 皆為 `"not-applicable"`、`reason` 必填（`AdmissionDecision.__post_init__`
    # 對 outcome=="wait" 的既有驗證）。不能沿用 `_decision()` 輔助函式——它對
    # 未指定 `selected` 一律回退成一組假候選，無法表達『真的沒有選中任何
    # 候選』的 wait 語意。
    wait_decision = admission.AdmissionDecision(
        decision_id="adm:v1:" + "1" * 62 + "a2",
        run_id="run-1", card_id="card-1", attempt_id="n1",
        profile_key="quota-admission:no-admissible-candidate",
        mode="enforced", outcome="wait",
        policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="not-applicable", demand_version="not-applicable",
        qualification_version="not-enforced",
        generated_at_ms=_NOW,
        selected=None, reservation_id=None,
        excluded=(
            {"executor": "codex", "model_id": "gpt-5.3-codex", "exclusion_reason": "insufficient-quota"},
        ),
        reason="quota-admission-insufficient",
    )
    store.record(admit_decision)
    store.record(wait_decision)

    # 模擬『run 上還留著上一個 attempt 的 binding』這個既有事實（#835 只按
    # persona 存最近一次真正派工成功的 profile），不重造 #835 的寫入邏輯。
    stale_profile_binding = {
        "request_key": "epk:v1:requested:" + "b" * 64,
        "resolved_key": "epk:v1:resolved:" + "a" * 64,
    }

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={
            "builder": {
                "decision_id": wait_decision.decision_id, "mode": "enforced", "outcome": "wait",
            },
        },
        needs_human_reason=None,
        execution_profile_bindings={"builder": stale_profile_binding},
        store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["outcome"] == "wait"
    assert persona["requested_profile_key"] is None
    assert persona["resolved_profile_key"] == "unknown"
    # 不得洩漏 attempt n0 的舊 profile key。
    assert persona["resolved_profile_key"] != stale_profile_binding["resolved_key"]
    assert persona["requested_profile_key"] != stale_profile_binding["request_key"]
    # 被排除候選清單仍照舊帶出，不受本次修法影響。
    assert persona["excluded"] == [
        {"executor": "codex", "model_id": "gpt-5.3-codex", "exclusion_reason": "insufficient-quota"},
    ]


def test_admit_receipt_still_uses_execution_profile_binding_for_same_attempt(tmp_path: Path) -> None:
    """對照組：outcome=="admit"（真的選中候選）時，同一個 attempt 的
    `execution_profile_bindings` 仍必須疊加——本次修法只收斂 wait，不影響
    既有 admit 語意。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "2" * 64, outcome="admit",
        profile_key="epk:v1:resolved:" + "c" * 64,
    )
    store.record(decision)
    profile_binding = {
        "request_key": "epk:v1:requested:" + "d" * 64,
        "resolved_key": "epk:v1:resolved:" + "e" * 64,
    }

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, execution_profile_bindings={"builder": profile_binding},
        store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["requested_profile_key"] == profile_binding["request_key"]
    assert persona["resolved_profile_key"] == profile_binding["resolved_key"]


# ---------------------------------------------------------------------------
# 對抗審查 MAJOR（manager.py 熱路徑）：同一份 snapshot 內，`DecisionReadCache`
# 對同一個 store 檔案身分最多真的讀一次，不管有多少個 run／persona 各自查詢。
# ---------------------------------------------------------------------------


def test_shared_cache_reads_store_at_most_once_per_snapshot_across_many_runs_and_personas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修好之前，`DecisionReadCache.get()` 對每個 persona 都直接呼叫
    `AdmissionDecisionStore.get()`（全檔線性掃描一次）——即使呼叫端已經把
    `store`／`cache` 兩個實例在同一輪 snapshot 內共用（manager_daemon.py 既有
    的既有寫法），append-only 檔仍會在同一輪被重複讀 N 次（N = run 數 ×
    persona 數）。這裡用 3 個 run × 2 個 persona＝6 次查詢，驗證檔案身分
    （size／mtime／inode／mode）沒變時，底層 `AdmissionDecisionStore.all_rows()`
    只被呼叫一次。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decisions = []
    for i in range(3):
        decision = _decision(decision_id="adm:v1:" + str(i) * 64, run_id=f"run-{i}", card_id="build-card")
        store.record(decision)
        decisions.append(decision)

    read_calls: list[int] = []
    real_all_rows = admission.AdmissionDecisionStore.all_rows

    def _counting_all_rows(self):
        read_calls.append(1)
        return real_all_rows(self)

    monkeypatch.setattr(admission.AdmissionDecisionStore, "all_rows", _counting_all_rows)

    cache = dp.DecisionReadCache()
    for i, decision in enumerate(decisions):
        for persona in ("builder", "reviewer"):
            projection = dp.project_workflow_quota_admission(
                run_id=f"run-{i}",
                quota_admission={
                    persona: {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}
                },
                needs_human_reason=None, store=store, cache=cache, now_ms=_NOW,
            )
            assert projection["personas"][persona]["decision_id"] == decision.decision_id

    assert len(read_calls) == 1


# ---------------------------------------------------------------------------
# AC4：restart／replay
# ---------------------------------------------------------------------------


def test_restart_fresh_store_and_cache_reproduce_same_projection(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    store_a = admission.AdmissionDecisionStore(path)
    decision = _decision(decision_id="adm:v1:" + "c" * 64)
    store_a.record(decision)

    projection_a = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store_a, now_ms=_NOW,
    )

    # 模擬 restart：全新的 store handle 與全新的 cache，指向同一份檔案。
    store_b = admission.AdmissionDecisionStore(path)
    projection_b = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store_b, cache=dp.DecisionReadCache(), now_ms=_NOW,
    )

    assert projection_a == projection_b


def test_corrupt_store_falls_back_to_last_good_with_stale_reason(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = admission.AdmissionDecisionStore(path)
    decision = _decision(decision_id="adm:v1:" + "d" * 64)
    store.record(decision)
    cache = dp.DecisionReadCache()

    fresh = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store, cache=cache, now_ms=_NOW,
    )
    assert fresh["personas"]["builder"]["stale"] is False

    # 損毀 store（破壞群組權限位——AdmissionDecisionStore._check_file 會 fail
    # closed），模擬讀取失敗；last-good 快取必須頂上，並附精確 stale 原因。
    path.chmod(0o660)
    try:
        stale = dp.project_workflow_quota_admission(
            run_id="run-1", quota_admission=_quota_admission_pointer(decision),
            needs_human_reason=None, store=store, cache=cache, now_ms=_NOW + 1000,
        )
    finally:
        path.chmod(0o600)

    persona = stale["personas"]["builder"]
    assert persona["stale"] is True
    assert persona["stale_reason"].startswith("decision-store-read-failed:")
    assert persona["stale_since_ms"] == _NOW
    # last-good 內容仍完整保留，不得因為這次讀取失敗而消失或呈現成『現在可派』。
    assert persona["decision_id"] == decision.decision_id
    assert persona["mode"] == decision.mode


def test_daemon_wired_cache_persists_across_snapshot_ticks_for_last_good_stale(
    tmp_path: Path,
) -> None:
    """#840 對抗審查修復（MAJOR，manager_daemon.py 約 694）：
    `quota_decision_cache` 必須跨快照存活——建在
    `build_runtime_status_provider()` 這層（呼叫一次、daemon 生命週期內共用
    的閉包變數），不能放進 `provider()` 內部逐輪重建。修好之前，每輪 tick
    都建一個空 cache，上一輪成功讀到的 decision 在下一輪 store 暫時損毀／
    權限錯誤時完全遺失，退化成單純的『這次讀不到』（`mode`／`selected` 等
    欄位整個消失），不是 last-good。這裡用同一個 `provider` closure 呼叫
    兩次模擬兩輪 tick，驗證第二輪損毀時仍完整保留第一輪的 last-good
    內容＋精確 stale 原因。"""
    from paulsha_cortex.coordinator import manager_daemon

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _run(registry, tmp_path, work_id="daemon-840", steps=(_step("build-card"),))
    # 用預設路徑的 store——per-test 隔離環境（見 conftest.py 的
    # `_clear_runtime_env`）保證這與 `build_runtime_status_provider()` 內部
    # 建立的 `AdmissionDecisionStore()` 指向同一個檔案。
    store = admission.AdmissionDecisionStore()
    decision = _decision(decision_id="adm:v1:" + "9" * 64, run_id=run.run_id, card_id="build-card")
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))

    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _: [],
        ready_units_fn=lambda metas, predicate: [],
    )

    def _builder_persona(payload):
        entries = [row for row in payload["attention"] if row.get("kind") == "workflow_run"]
        assert len(entries) == 1
        return entries[0]["quota_decision"]["personas"]["builder"]

    first_tick = _builder_persona(provider())
    assert first_tick["stale"] is False
    assert first_tick["decision_id"] == decision.decision_id
    assert first_tick["mode"] == decision.mode

    store.path.chmod(0o660)
    try:
        second_tick = _builder_persona(provider())
    finally:
        store.path.chmod(0o600)

    assert second_tick["stale"] is True
    assert second_tick["stale_reason"].startswith("decision-store-read-failed:")
    # 跨 tick 存活的 last-good：不因下一輪讀不到就把已經確認過的 mode／
    # decision_id 整個丟掉。
    assert second_tick["decision_id"] == decision.decision_id
    assert second_tick["mode"] == decision.mode


def test_old_attempt_receipt_immutable_after_newer_attempt_recorded(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    old_decision = _decision(decision_id="adm:v1:" + "e" * 64, attempt_id="n0")
    store.record(old_decision)
    before = store.get(old_decision.decision_id)

    new_decision = _decision(decision_id="adm:v1:" + "f" * 64, attempt_id="n1")
    store.record(new_decision)
    after = store.get(old_decision.decision_id)

    assert before == after


# ---------------------------------------------------------------------------
# AC5：bytes 不變、allowlist 負面 fixture
# ---------------------------------------------------------------------------


def test_projection_does_not_mutate_decision_store_or_registry_bytes(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    # 步卡 executor／model 對齊下面 admit 決策的 `selected`（見
    # `_current_persona_identity_from_steps` 的比對），確保這裡驗證的是完整
    # 投影而不是「目前 attempt 尚無決策」的 mismatch 分支。
    run = _run(
        registry, tmp_path, work_id="bytes-840",
        steps=(_step("build-card", executor="codex", model="gpt-5.3-codex"),),
    )
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(decision_id="adm:v1:" + "0" * 64, run_id=run.run_id, card_id="build-card")
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))
    run = registry.get_workflow_run(run.run_id)

    decisions_before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    registry_before = hashlib.sha256(state.read_bytes()).hexdigest()

    manager.workflow_status_entry(registry, run, quota_decision_store=store)
    # 對抗審查第三輪（BLOCKER）：舊版沒有把 provider 指到上面雜湊的
    # `tmp_path/decisions.jsonl`——`WorkflowRegistryProvider` 在沒有注入
    # `quota_decision_store` 時會自建一個指向**預設路徑**（Trust Root 資產
    # 位置）的 store，跟這裡雜湊的檔案完全是兩個檔案，scan() 從未真的碰過
    # `store.path`，讓下面的 bytes-不變斷言形同空測（不管 scan 有沒有寫壞
    # store 都會通過）。這裡明確注入同一個 `store`，讓 scan() 真的讀這份
    # store，斷言才有意義；同時斷言 scan() 真的投影出這筆 decision，證明
    # provider 確實讀到了它（不是又落回『沒有任何 #839 證據』的空路徑）。
    result = WorkflowRegistryProvider(REPO, state_path=state, quota_decision_store=store).scan()
    assert (
        result.observations["quota_decisions"]["bytes-840"]["personas"]["builder"]["decision_id"]
        == decision.decision_id
    )

    decisions_after = hashlib.sha256(store.path.read_bytes()).hexdigest()
    registry_after = hashlib.sha256(state.read_bytes()).hexdigest()

    assert decisions_before == decisions_after
    assert registry_before == registry_after


def test_allowlist_strips_sensitive_fields_from_selected_and_excluded(tmp_path: Path) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "1" * 63 + "2",
        selected={
            "executor": "codex", "model_id": "gpt-5.3-codex", "independence_domain": "openai",
            "accountId": "acct-secret-123", "env": {"OPENAI_API_KEY": "sk-super-secret"},
            "raw_prompt": "system prompt leaking customer data",
        },
        excluded=(
            {
                "executor": "claude", "model_id": "sonnet", "exclusion_reason": "insufficient-quota",
                "accountId": "acct-secret-456", "raw_prompt": "another leaking prompt",
            },
        ),
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1", quota_admission=_quota_admission_pointer(decision),
        needs_human_reason=None, store=store, now_ms=_NOW,
    )

    persona = projection["personas"]["builder"]
    assert persona["selected"] == {
        "executor": "codex", "model_id": "gpt-5.3-codex", "independence_domain": "openai",
    }
    assert persona["excluded"] == [
        {"executor": "claude", "model_id": "sonnet", "exclusion_reason": "insufficient-quota"},
    ]
    rendered = json.dumps(projection)
    for leaked in ("acct-secret-123", "acct-secret-456", "sk-super-secret", "raw_prompt", "OPENAI_API_KEY"):
        assert leaked not in rendered


# ---------------------------------------------------------------------------
# AC6：producer → snapshot → CLI／status 端到端
# ---------------------------------------------------------------------------


def test_producer_to_snapshot_to_status_cli_end_to_end(tmp_path: Path, capsys) -> None:
    """真實 `AdmissionDecisionStore` 寫入 receipt → `workflow_status_entry`
    投影 → 塞進 status payload → `cortex inspect status` 的文字模式印出。"""
    import importlib
    import sys

    for module_name in ("paulsha_cortex.cli", "paulsha_cortex.porcelain", "paulsha_cortex.porcelain.inspect"):
        sys.modules.pop(module_name, None)
    cli = importlib.import_module("paulsha_cortex.cli")

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _run(registry, tmp_path, work_id="e2e-840", steps=(_step("build-card"),))
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "3" * 63 + "4", run_id=run.run_id, card_id="build-card",
        selected_observation_state="known", selected_feasible=True,
    )
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))
    run = registry.get_workflow_run(run.run_id)

    entry = manager.workflow_status_entry(registry, run, quota_decision_store=store)
    assert entry["quota_decision"]["personas"]["builder"]["decision_id"] == decision.decision_id

    payload = contract.build_status(
        ready=[], in_flight=[], recent_done=[],
        daemon={"pid": 1, "last_tick_at": "2026-09-27T00:00:00Z", "idle": True},
        updated_at="2026-09-27T00:00:00Z",
    )
    payload["attention"] = [entry]
    contract.atomic_write_json(constants.status_path(), payload)

    assert cli.main(["inspect", "status", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert (
        rendered["status"]["attention"][0]["quota_decision"]["personas"]["builder"]["decision_id"]
        == decision.decision_id
    )

    assert cli.main(["inspect", "status"]) == 0
    human = capsys.readouterr().out
    assert f"quota_decision[{run.run_id}/builder]" in human
    assert "mode=shadow" in human


def test_producer_to_snapshot_to_work_show_end_to_end(tmp_path: Path) -> None:
    """真實 `AdmissionDecisionStore` 寫入 receipt →
    `WorkflowRegistryProvider.scan()` → `cortex work show` 文字渲染函式。"""
    from paulsha_cortex.cli import _format_quota_decision

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    # 步卡 executor／model 對齊下面 admit 決策的 `selected`，理由同上（見
    # `_current_persona_identity_from_steps`）。
    run = _run(
        registry, tmp_path, work_id="e2e-work-show-840",
        steps=(_step("build-card", executor="codex", model="gpt-5.3-codex"),),
    )
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "5" * 63 + "6", run_id=run.run_id, card_id="build-card",
    )
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))

    result = WorkflowRegistryProvider(
        REPO, state_path=state, quota_decision_store=store,
    ).scan()
    row_projection = result.observations["quota_decisions"]["e2e-work-show-840"]

    lines = _format_quota_decision(row_projection)
    joined = "\n".join(lines)
    assert "quota_decision[builder]:" in joined
    assert f"decision_id: {decision.decision_id}" in joined
    assert "mode: shadow  outcome: admit" in joined


# ---------------------------------------------------------------------------
# 對抗審查第三輪
#
# 條目 1（BLOCKER，測試強度）：見上方
# `test_projection_does_not_mutate_decision_store_or_registry_bytes` 的修法——
# provider 分支已改成注入同一份 store，並斷言真的投影出這筆 decision。
#
# 條目 2（MAJOR，providers.py／decision_projection.py）：`quota_admission`
# 指標不得沿用不再對應『目前 attempt』的舊 receipt；下面補上 shared-function
# 層級（`dp.project_workflow_quota_admission` 的 `current_identity_by_persona`）
# 與 provider 端到端（`WorkflowRegistryProvider.scan()`）兩層測試，並補
# work_id 對應多個 run 時的選 run 規則。
#
# 條目 3（MAJOR，decision_projection.py）：`DecisionReadCache` 補父目錄安全
# 檢查，不再被『檔案本身沒變、父目錄權限被放寬』繞過。
# ---------------------------------------------------------------------------


def test_attempt_check_disabled_by_default_preserves_existing_behavior(tmp_path: Path) -> None:
    """`current_identity_by_persona` 完全省略（本檔既有全部呼叫方式）時，
    attempt 比對必須逐字停用——新增這個可選參數不得改變任何既有呼叫端的
    既有行為（比照 #835／#839 既有的可選欄位加法模式）。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "31", card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is True
    assert persona["selected"] == {"executor": "codex", "model_id": "gpt-5.3-codex"}


def test_admit_matches_current_step_identity_stays_available(tmp_path: Path) -> None:
    """對照組：`current_identity_by_persona` 有傳、且與這筆 admit 決策的
    ``selected``／``card_id`` 完全相符時，仍完整呈現（本次修法只收斂不相符
    的情況，不影響正常、當前的 admit 呈現）。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "32", card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        current_identity_by_persona={
            "builder": {"card": "build-card", "executor": "codex", "model": "gpt-5.3-codex"},
        },
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is True
    assert persona["outcome"] == "admit"
    assert persona["selected"] == {"executor": "codex", "model_id": "gpt-5.3-codex"}


def test_admit_after_retry_card_identity_reset_is_pending_not_stale(tmp_path: Path) -> None:
    """`retry-card` 重置卡片（`step.executor`／`step.model` 清成 ``None``，見
    `registry._manager_reset_workflow_for_retry_card`）後、新 attempt 尚未
    寫出 receipt 前，``quota_admission["builder"]`` 仍指著上一個（已經結束）
    attempt 的 admit receipt。傳入從 reset 後的 ``WorkflowRun.steps`` 推導的
    `current_identity_by_persona` 時，必須偵測到身分已被清空、跟這筆決策的
    ``selected`` 不再一致，呈現「目前 attempt 尚無決策」，不得沿用舊
    attempt 的 mode／outcome／selected。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    stale_admit = _decision(
        decision_id="adm:v1:" + "7" * 62 + "33", card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(stale_admit)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={
            "builder": {"decision_id": stale_admit.decision_id, "mode": "shadow", "outcome": "admit"},
        },
        needs_human_reason=None,
        # 模擬 retry-card 重置後的快照：同一張卡，身分已被清空。
        current_identity_by_persona={
            "builder": {"card": "build-card", "executor": None, "model": None},
        },
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is False
    assert persona["gap_reason"] == "quota-decision-attempt-superseded"
    assert persona["mismatch_reason"] == "identity-reset-since-decision"
    # 不得沿用舊 attempt 的欄位——operator 讀到的必須是『目前沒有決策』，
    # 不是上一輪的 mode／outcome／selected。
    for leaked_field in ("mode", "outcome", "selected", "excluded", "classification"):
        assert leaked_field not in persona
    # 但保留 decision_id，方便追查那筆已結束 attempt 的舊 receipt。
    assert persona["decision_id"] == stale_admit.decision_id


def test_admit_pointer_for_superseded_card_is_pending(tmp_path: Path) -> None:
    """卡片已經前進到下一張（``current_identity_by_persona`` 的 ``card``
    與決策的 ``card_id`` 不同）時，同樣視為不相符——不得把上一張卡的 admit
    誤呈現成『目前這張卡』的決策。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "34", card_id="build-card-1",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        current_identity_by_persona={
            "builder": {"card": "build-card-2", "executor": None, "model": None},
        },
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is False
    assert persona["mismatch_reason"] == "card-superseded"


def test_current_identity_missing_for_persona_is_treated_as_not_derivable(tmp_path: Path) -> None:
    """`current_identity_by_persona` 有傳，但這個 persona 底下沒有任何未通過
    的卡（例如已經全部通過、或呼叫端根本算不出來）——票面：『推導不出就視為
    不相符』，不得預設放行。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "35", card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        current_identity_by_persona={},
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is False
    assert persona["mismatch_reason"] == "current-card-not-derivable"


def test_wait_matches_current_card_stays_available(tmp_path: Path) -> None:
    """wait 決策從未寫入 step 身分（見 `manager._quota_admission_stop`
    文件字串），card 相符已是能拿到的最強訊號——相符時仍完整呈現這個 wait，
    不因為新增 attempt 比對就把正常、當前的 wait 也隱藏掉。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    wait_decision = admission.AdmissionDecision(
        decision_id="adm:v1:" + "7" * 62 + "36",
        run_id="run-1", card_id="build-card", attempt_id="n0",
        profile_key="quota-admission:no-admissible-candidate",
        mode="enforced", outcome="wait",
        policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="not-applicable", demand_version="not-applicable",
        qualification_version="not-enforced",
        generated_at_ms=_NOW, selected=None, reservation_id=None,
        excluded=(), reason="quota-admission-insufficient",
    )
    store.record(wait_decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={
            "builder": {"decision_id": wait_decision.decision_id, "mode": "enforced", "outcome": "wait"},
        },
        needs_human_reason=None,
        current_identity_by_persona={
            "builder": {"card": "build-card", "executor": None, "model": None},
        },
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is True
    assert persona["outcome"] == "wait"


def test_wait_pointer_for_superseded_card_is_pending(tmp_path: Path) -> None:
    """wait 決策的卡與目前卡不同時，同樣不相符（卡片已經前進）。"""
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    wait_decision = admission.AdmissionDecision(
        decision_id="adm:v1:" + "7" * 62 + "37",
        run_id="run-1", card_id="build-card-1", attempt_id="n0",
        profile_key="quota-admission:no-admissible-candidate",
        mode="enforced", outcome="wait",
        policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="not-applicable", demand_version="not-applicable",
        qualification_version="not-enforced",
        generated_at_ms=_NOW, selected=None, reservation_id=None,
        excluded=(), reason="quota-admission-insufficient",
    )
    store.record(wait_decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={
            "builder": {"decision_id": wait_decision.decision_id, "mode": "enforced", "outcome": "wait"},
        },
        needs_human_reason=None,
        current_identity_by_persona={
            "builder": {"card": "build-card-2", "executor": None, "model": None},
        },
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["available"] is False
    assert persona["mismatch_reason"] == "card-superseded"


def test_provider_scan_after_retry_card_reset_reports_pending_not_stale_admit(tmp_path: Path) -> None:
    """端到端（`WorkflowRegistryProvider.scan()`，`cortex work show` 路徑）：
    先寫一筆正常 admit receipt（步卡身分與 selected 相符）→ 模擬 `retry-card`
    重置這張卡（`step.executor`／`step.model` 清成 ``None``，`needs_human`
    facet／理由清空，比照 `registry._manager_reset_workflow_for_retry_card`
    的既有效果）→ 新 attempt 尚未寫出任何新 receipt 前，`cortex work show`
    不得沿用重置前那筆 admit receipt 的 mode／selected。"""
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _run(
        registry, tmp_path, work_id="retry-card-840",
        steps=(_step("build-card", executor="codex", model="gpt-5.3-codex"),),
        facets=(),
    )
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "38", run_id=run.run_id, card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))

    # 修好之前的基準：重置前，`cortex work show` 正確顯示這筆 admit。
    before_reset = WorkflowRegistryProvider(
        REPO, state_path=state, quota_decision_store=store,
    ).scan().observations["quota_decisions"]["retry-card-840"]
    assert before_reset["personas"]["builder"]["available"] is True
    assert before_reset["personas"]["builder"]["outcome"] == "admit"

    # 模擬 `retry-card`：同一張卡，身分清空、facet／理由清空（`quota_admission`
    # 指標維持不動——這正是票面描述的『新 attempt 尚未寫出 receipt 前』）。
    registry._manager_update_workflow_run(
        run.run_id,
        steps=(_step("build-card", executor=None, model=None),),
        facets=(), needs_human_reason=None,
    )

    after_reset = WorkflowRegistryProvider(
        REPO, state_path=state, quota_decision_store=store,
    ).scan().observations["quota_decisions"]["retry-card-840"]
    persona = after_reset["personas"]["builder"]
    assert persona["available"] is False
    assert persona["gap_reason"] == "quota-decision-attempt-superseded"
    # 不得沿用重置前那筆 admit 的 mode／selected。
    assert "mode" not in persona
    assert "selected" not in persona
    assert persona["decision_id"] == decision.decision_id


def test_scan_quota_decision_for_work_id_picks_active_run_not_last_processed(tmp_path: Path) -> None:
    """對抗審查第三輪（MAJOR，providers.py 約 568）：同一個 ``work_id``
    對應多個 run（例如舊 attempt 已標終局狀態、換了新 run_id 繼續派工）時，
    `scan()` 過去無條件覆寫、等於『最後一個非空投影贏』——若終局的舊 run
    恰好在迴圈裡排在仍在跑的新 run **之後**，`cortex work show` 就會顯示
    已經結束的那個 run 的額度決策。修法沿用 `lifecycle.project_work_items`
    既有的選 run 規則（排除 done／completed／failed／superseded），這裡先
    建立『仍在跑的 run』、再建立『已終局的 run』（故意讓終局 run 在
    ``rows`` 迭代順序裡排在後面，重現舊版會選錯的排序），驗證挑中的仍是
    仍在跑的那個。"""
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    work_id = "multi-run-840"
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")

    active_run = registry._manager_create_workflow_run(
        work_id=work_id, repo=REPO, claim_key=f"{REPO}/{work_id}/active",
        source_revision="a" * 64, workspace_root=str(tmp_path / "workspace" / "active"),
        combo="feature-oneshot", current_phase="build",
        steps=(_step("build-card", executor="codex", model="gpt-5.3-codex"),),
        attempts={"build": 1}, facets=(), gate_status="running",
    )
    active_decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "39", run_id=active_run.run_id, card_id="build-card",
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(active_decision)
    registry._manager_update_workflow_run(
        active_run.run_id, quota_admission=_quota_admission_pointer(active_decision),
    )

    # 建立在 active_run 之後，`rows` 迭代順序會排在它後面——舊版「最後一個
    # 非空投影贏」會誤選這個已終局的 run。
    superseded_run = registry._manager_create_workflow_run(
        work_id=work_id, repo=REPO, claim_key=f"{REPO}/{work_id}/superseded",
        source_revision="b" * 64, workspace_root=str(tmp_path / "workspace" / "superseded"),
        combo="feature-oneshot", current_phase="build",
        steps=(_step("build-card", executor="claude", model="sonnet"),),
        attempts={"build": 1}, facets=(), gate_status="running",
    )
    superseded_decision = _decision(
        decision_id="adm:v1:" + "7" * 62 + "40", run_id=superseded_run.run_id, card_id="build-card",
        selected={"executor": "claude", "model_id": "sonnet"},
    )
    store.record(superseded_decision)
    registry._manager_update_workflow_run(
        superseded_run.run_id, quota_admission=_quota_admission_pointer(superseded_decision),
    )
    registry._manager_update_workflow_run(superseded_run.run_id, status="superseded")

    result = WorkflowRegistryProvider(
        REPO, state_path=state, quota_decision_store=store,
    ).scan()
    row_projection = result.observations["quota_decisions"][work_id]

    assert row_projection["run_id"] == active_run.run_id
    assert row_projection["personas"]["builder"]["decision_id"] == active_decision.decision_id


def test_decision_read_cache_detects_parent_directory_permission_widened(tmp_path: Path) -> None:
    """對抗審查第三輪（MAJOR，decision_projection.py 約 154）：
    `DecisionReadCache` 過去只以 store 檔案本身的 (size, mtime_ns, inode,
    mode) 判斷快取命中，繞過 `AdmissionDecisionStore._check_parent()` 的父
    目錄安全檢查。第一次成功讀取後，若父目錄（`quota-admission-decisions/`）
    被改成不安全權限（例如 `0o777`）——檔案本身完全沒變——舊版仍會沿用
    快取索引、回報 `stale=False`。修法把父目錄身分併入快取鍵，權限一變就
    強迫真正重讀，落回 `AdmissionDecisionStore._check_parent()` 既有的
    fail-closed 判定。"""
    decisions_path = tmp_path / "quota-admission-decisions" / "decisions.jsonl"
    store = admission.AdmissionDecisionStore(decisions_path)
    decision = _decision(decision_id="adm:v1:" + "7" * 62 + "41")
    store.record(decision)
    cache = dp.DecisionReadCache()

    first, first_stale = cache.get(store, decision.decision_id, now_ms=_NOW)
    assert first is not None
    assert first_stale is None

    parent = decisions_path.parent
    original_mode = parent.stat().st_mode
    parent.chmod(0o777)
    try:
        second, second_stale = cache.get(store, decision.decision_id, now_ms=_NOW + 1)
    finally:
        parent.chmod(stat_module.S_IMODE(original_mode))

    # last-good 仍完整保留（檔案身分沒變，內容仍是同一筆 decision）。
    assert second is not None
    assert second.decision_id == decision.decision_id
    assert second_stale is not None
    assert second_stale["stale"] is True
    assert "parent-permissions-invalid" in second_stale["stale_reason"]
