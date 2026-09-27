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


def _step(card: str, *, phase: str = "build", persona: str = "builder") -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor="planned-executor",
        model="planned-model",
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
    run = _run(
        registry, tmp_path, work_id="consistency-840",
        steps=(_step("build-card"),), facets=("needs_human",),
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
    run = _run(registry, tmp_path, work_id="bytes-840", steps=(_step("build-card"),))
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(decision_id="adm:v1:" + "0" * 64, run_id=run.run_id, card_id="build-card")
    store.record(decision)
    registry._manager_update_workflow_run(run.run_id, quota_admission=_quota_admission_pointer(decision))
    run = registry.get_workflow_run(run.run_id)

    decisions_before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    registry_before = hashlib.sha256(state.read_bytes()).hexdigest()

    manager.workflow_status_entry(registry, run, quota_decision_store=store)
    WorkflowRegistryProvider(REPO, state_path=state).scan()

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
    run = _run(registry, tmp_path, work_id="e2e-work-show-840", steps=(_step("build-card"),))
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
