"""#1116：quota binding 改以較穩定的 executor＋model_id identity subject涵蓋。

背景（票面）：同一個 builder 身分（例如 codex／gpt-6-luna）在不同卡片解析出
不同的 execution profile resolved key（launch contract／requirements 隨卡片
而異），`quota-pools.json` 過去只能以 resolved profile key 逐卡綁定額度池，
卡片 deck 一改、resolved key 一變，binding 就無聲失效（shadow 下變成
``unmanaged``）。

驗收對應原票三條：
- AC1：同一 executor／model 在不同卡片的派工，能被單一 binding 規則涵蓋。
- AC2：既有以 resolved key 綁定的設定仍可用（相容）；精確綁定優先於穩定
  identity 綁定，同一候選同時命中兩者時不重複計算 pool。
- AC3：operator 能從唯讀指令（`cortex quota bindings --report`）或 decision
  投影（`classification.binding`）得知哪些候選目前完全沒有任何 binding
  涵蓋（``binding-missing``）。
"""

from __future__ import annotations

import json

import pytest

from paulsha_cortex.coordinator import quota_admission as admission
from paulsha_cortex.coordinator import quota_collectors as collectors
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator import quota_sources as sources
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.monitor import decision_projection as dp
from paulsha_cortex.porcelain import quota as quota_cli

# 模擬票面現象：同一個 codex／gpt-6-luna 身分在兩張卡各自解析出不同的
# resolved profile key（tdd-red／subagent-build）。
_RESOLVED_TDD_RED = "epk:v1:resolved:" + "1" * 64
_RESOLVED_SUBAGENT_BUILD = "epk:v1:resolved:" + "2" * 64
_EXECUTOR = "codex"
_MODEL_ID = "gpt-6-luna"
_NOW = 1_800_000_000_000


# ---------------------------------------------------------------------------
# 共用 fixture（比照 test_quota_admission_839.py 既有模式）
# ---------------------------------------------------------------------------


def _profile_ref(key: str) -> dict[str, object]:
    return {"state": "known", "value": {"schema_version": 1, "key": key}}


def _pool_descriptor(
    *,
    account: str = "account-shared",
    pool: str = "pool-shared",
    unit_id: str = "token",
    windows: tuple[tuple[str, int], ...] = (("short", 300_000),),
) -> schema.PoolDescriptor:
    unit = {
        "unit_id": unit_id, "version": "1", "quantity_kind": "amount",
        "semantics_ref": "fixture:native-token/v1",
    }
    return schema.parse_pool_descriptor(
        {
            "schema_version": 1,
            "authority_id": "operator-budget-authority",
            "account_id": account,
            "pool_id": pool,
            "revision": "1",
            "authority_ref": "fixture:operator-pool-map/v1",
            "provenance_refs": ["fixture:pool-map/v1"],
            "units": [unit],
            "windows": [
                {
                    "window_id": window_id, "kind": "rolling",
                    "unit_ref": {"unit_id": unit_id, "version": "1"},
                    "duration_ms": duration_ms,
                }
                for window_id, duration_ms in windows
            ],
        }
    )


def _pool_ref(descriptor: schema.PoolDescriptor) -> dict[str, str]:
    return {
        "authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
        "pool_id": descriptor.pool_id, "revision": descriptor.revision,
    }


def _constraints(descriptors) -> list[dict[str, object]]:
    constraints = []
    for descriptor in descriptors:
        for window in descriptor.to_dict()["windows"]:
            constraints.append(
                {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window["window_id"]}}
            )
    return constraints


def _exact_binding(descriptors, profile_key: str, *, binding_id: str = "binding-exact") -> schema.ProfilePoolBinding:
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": binding_id, "revision": "1",
            "subject": {"kind": "profile", "profile_ref": _profile_ref(profile_key)},
            "constraints": _constraints(descriptors),
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=tuple(descriptors),
    )


def _identity_binding(
    descriptors, *, executor: str = _EXECUTOR, model_id: str = _MODEL_ID, binding_id: str = "binding-identity",
) -> schema.ProfilePoolBinding:
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": binding_id, "revision": "1",
            "subject": {"kind": "identity", "executor": executor, "model_id": model_id},
            "constraints": _constraints(descriptors),
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=tuple(descriptors),
    )


def _observation(
    descriptor, window_id: str, *, value: str, observed_at_ms: int = _NOW,
    profile_key: str = _RESOLVED_TDD_RED, unit_id: str = "token",
) -> schema.QuotaObservation:
    payload = {
        "schema_version": 1,
        "observation_id": f"fixture-{descriptor.pool_id}-{window_id}-{observed_at_ms}",
        "scope": {"state": "known", "value": {"pool_ref": _pool_ref(descriptor), "window_id": window_id}},
        "profile_ref": _profile_ref(profile_key),
        "unit_ref": {"state": "known", "value": {"unit_id": unit_id, "version": "1"}},
        "window_instance": {"kind": "unknown", "reason": "missing-window-instance"},
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": {"state": "observed", "amount": {"kind": "exact", "value": value}},
        },
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "known", "value": 60_000},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-provider", "source_schema": "fixture-quota-v1",
            "adapter_version": "fixture-adapter-v1", "authority_ref": "fixture:provider-contract/v1",
            "method": "provider_status", "provenance_refs": ["fixture:source-document/v1"],
            "event_identity": {"state": "unknown", "reason": "provider-has-no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }
    return schema.parse_observation(payload, descriptors=(descriptor,), unit_catalog=())


def _shadow_with(descriptor, window_id: str, value: str, *, profile_key: str = _RESOLVED_TDD_RED) -> QuotaShadowService:
    service = QuotaShadowService.in_memory()
    observation = _observation(descriptor, window_id, value=value, profile_key=profile_key)
    result = service.record_observation(observation.to_dict(), descriptors=(descriptor,), unit_catalog=())
    assert result.accepted == 1
    return service


# ---------------------------------------------------------------------------
# schema 層：`identity` subject kind（quota_observation.parse_binding）
# ---------------------------------------------------------------------------


def test_parse_binding_accepts_identity_subject_and_reports_complete() -> None:
    descriptor = _pool_descriptor()
    binding = _identity_binding((descriptor,))

    assert binding.to_dict()["subject"] == {
        "kind": "identity", "executor": _EXECUTOR, "model_id": _MODEL_ID,
    }
    assert dict(schema.binding_status(binding)) == {"state": "complete", "reasons": ()}


def test_parse_binding_rejects_identity_subject_missing_model_id() -> None:
    descriptor = schema.parse_pool_descriptor(_pool_descriptor().to_dict())
    payload = {
        "schema_version": 1, "binding_id": "binding-bad", "revision": "1",
        "subject": {"kind": "identity", "executor": _EXECUTOR},
        "constraints": _constraints((descriptor,)),
        "coverage": {"state": "complete", "gaps": []},
    }
    with pytest.raises(schema.QuotaContractError) as excinfo:
        schema.parse_binding(payload, descriptors=(descriptor,))
    assert excinfo.value.code == "invalid_shape"


def test_parse_binding_rejects_identity_subject_unknown_extra_key() -> None:
    descriptor = schema.parse_pool_descriptor(_pool_descriptor().to_dict())
    payload = {
        "schema_version": 1, "binding_id": "binding-bad", "revision": "1",
        "subject": {
            "kind": "identity", "executor": _EXECUTOR, "model_id": _MODEL_ID,
            "account_id": "sneaked-in",
        },
        "constraints": _constraints((descriptor,)),
        "coverage": {"state": "complete", "gaps": []},
    }
    with pytest.raises(schema.QuotaContractError) as excinfo:
        schema.parse_binding(payload, descriptors=(descriptor,))
    assert excinfo.value.code == "invalid_shape"


def test_parse_binding_still_rejects_unknown_subject_kind() -> None:
    """加法不能鬆動既有的『未知 kind 一律拒絕』——新增 `identity` 之後，
    第三種未列舉的 kind 仍必須 fail closed。"""
    descriptor = schema.parse_pool_descriptor(_pool_descriptor().to_dict())
    payload = {
        "schema_version": 1, "binding_id": "binding-bad", "revision": "1",
        "subject": {"kind": "account", "account_id": "whatever"},
        "constraints": _constraints((descriptor,)),
        "coverage": {"state": "complete", "gaps": []},
    }
    with pytest.raises(schema.QuotaContractError) as excinfo:
        schema.parse_binding(payload, descriptors=(descriptor,))
    assert excinfo.value.code == "invalid_identifier"
    assert excinfo.value.locator == ("subject", "kind")


# ---------------------------------------------------------------------------
# admission 層：pools_for_profile／binding_kind_for_profile 優先序
# ---------------------------------------------------------------------------


def test_pools_for_profile_single_identity_binding_covers_two_different_resolved_keys() -> None:
    """AC1：同一 executor／model 在不同卡片解析出的兩個 resolved key，都能
    被同一個 identity binding 涵蓋，不必逐卡新增 binding。"""
    descriptor = _pool_descriptor()
    binding = _identity_binding((descriptor,))

    for resolved_key in (_RESOLVED_TDD_RED, _RESOLVED_SUBAGENT_BUILD):
        pool_windows = admission.pools_for_profile(
            resolved_key, executor=_EXECUTOR, model_id=_MODEL_ID, bindings=(binding,),
        )
        assert pool_windows == ((_pool_ref(descriptor), "short"),)
        assert admission.binding_kind_for_profile(
            resolved_key, executor=_EXECUTOR, model_id=_MODEL_ID, bindings=(binding,),
        ) == "identity"


def test_pools_for_profile_without_executor_or_model_id_ignores_identity_bindings() -> None:
    """相容：既有呼叫端（#839 之前，不帶 executor／model_id）行為逐字不變
    ——沒有精確綁定時一律回空，不會被新的 identity 綁定悄悄接住。"""
    descriptor = _pool_descriptor()
    binding = _identity_binding((descriptor,))

    assert admission.pools_for_profile(_RESOLVED_TDD_RED, bindings=(binding,)) == ()
    assert admission.binding_kind_for_profile(_RESOLVED_TDD_RED, bindings=(binding,)) == "none"


def test_pools_for_profile_exact_binding_takes_precedence_and_does_not_double_count() -> None:
    """AC2：resolved key 精確綁定優先於穩定 identity 綁定；命中精確綁定時
    只採用精確綁定的 pool 集合，不與 identity 綁定的 pool 合併計算。"""
    pool_exact = _pool_descriptor(account="account-exact", pool="pool-exact")
    pool_identity = _pool_descriptor(account="account-identity", pool="pool-identity")
    exact = _exact_binding((pool_exact,), _RESOLVED_TDD_RED)
    identity = _identity_binding((pool_identity,))

    pool_windows = admission.pools_for_profile(
        _RESOLVED_TDD_RED, executor=_EXECUTOR, model_id=_MODEL_ID, bindings=(identity, exact),
    )

    assert pool_windows == ((_pool_ref(pool_exact), "short"),)
    assert admission.binding_kind_for_profile(
        _RESOLVED_TDD_RED, executor=_EXECUTOR, model_id=_MODEL_ID, bindings=(identity, exact),
    ) == "exact"


def test_pools_for_profile_falls_back_to_identity_when_no_exact_match() -> None:
    pool_exact = _pool_descriptor(account="account-exact", pool="pool-exact")
    pool_identity = _pool_descriptor(account="account-identity", pool="pool-identity")
    exact = _exact_binding((pool_exact,), _RESOLVED_TDD_RED)
    identity = _identity_binding((pool_identity,))

    # _RESOLVED_SUBAGENT_BUILD 沒有任何精確綁定，落回 identity 綁定。
    pool_windows = admission.pools_for_profile(
        _RESOLVED_SUBAGENT_BUILD, executor=_EXECUTOR, model_id=_MODEL_ID, bindings=(identity, exact),
    )
    assert pool_windows == ((_pool_ref(pool_identity), "short"),)


# ---------------------------------------------------------------------------
# admission 層：assess_candidate_quota 的 binding_kind 投影
# ---------------------------------------------------------------------------


def test_assess_candidate_quota_reports_exact_binding_kind() -> None:
    descriptor = _pool_descriptor()
    shadow = _shadow_with(descriptor, "short", "50")
    binding = _exact_binding((descriptor,), _RESOLVED_TDD_RED)

    assessment, _ = admission.assess_candidate_quota(
        executor=_EXECUTOR, model_id=_MODEL_ID, independence_domain=_EXECUTOR,
        profile_key=_RESOLVED_TDD_RED, bindings=(binding,), descriptors=(descriptor,),
        unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is True
    assert assessment.binding_kind == "exact"


def test_assess_candidate_quota_reports_identity_binding_kind_for_second_card() -> None:
    """票面重現：tdd-red 卡先用 identity binding 記過觀測，subagent-build 卡
    解析出不同的 resolved key，仍應被同一個 identity binding 涵蓋並可行。"""
    descriptor = _pool_descriptor()
    shadow = _shadow_with(descriptor, "short", "50", profile_key=_RESOLVED_TDD_RED)
    binding = _identity_binding((descriptor,))

    assessment, _ = admission.assess_candidate_quota(
        executor=_EXECUTOR, model_id=_MODEL_ID, independence_domain=_EXECUTOR,
        profile_key=_RESOLVED_SUBAGENT_BUILD, bindings=(binding,), descriptors=(descriptor,),
        unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is True
    assert assessment.binding_kind == "identity"
    assert assessment.observation_state == "known"


def test_assess_candidate_quota_reports_none_binding_kind_when_unmanaged() -> None:
    descriptor = _pool_descriptor()
    shadow = _shadow_with(descriptor, "short", "0")
    other_binding = _exact_binding((descriptor,), _RESOLVED_TDD_RED)

    assessment, _ = admission.assess_candidate_quota(
        executor="claude", model_id="sonnet", independence_domain="claude",
        profile_key=_RESOLVED_SUBAGENT_BUILD,  # 沒有任何 binding 指到這個 profile／identity
        bindings=(other_binding,), descriptors=(descriptor,), unit_catalog=(), shadow=shadow, now_ms=_NOW,
    )
    assert assessment.feasible is True
    assert assessment.observation_state == "unmanaged"
    assert assessment.binding_kind == "none"


# ---------------------------------------------------------------------------
# AdmissionDecision.selected_binding_kind：round-trip 與驗證
# ---------------------------------------------------------------------------


def _decision(*, decision_id: str, selected_binding_kind: str | None) -> admission.AdmissionDecision:
    return admission.AdmissionDecision(
        decision_id=decision_id, run_id="run-1", card_id="card-1", attempt_id="attempt-0",
        profile_key=_RESOLVED_TDD_RED, mode="shadow", outcome="admit",
        policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:" + "b" * 16,
        demand_version=admission.DEMAND_FIXTURE_VERSION,
        qualification_version="not-enforced", generated_at_ms=_NOW,
        selected={"executor": _EXECUTOR, "model_id": _MODEL_ID},
        selected_observation_state="known", selected_feasible=True,
        selected_binding_kind=selected_binding_kind,
    )


@pytest.mark.parametrize("binding_kind", ("exact", "identity", "none", None))
def test_admission_decision_round_trips_selected_binding_kind(tmp_path, binding_kind) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(decision_id="adm:v1:" + "9" * 62 + "01", selected_binding_kind=binding_kind)
    store.record(decision)

    reloaded = store.get(decision.decision_id)
    assert reloaded is not None
    assert reloaded.selected_binding_kind == binding_kind


def test_admission_decision_rejects_invalid_selected_binding_kind() -> None:
    with pytest.raises(ValueError):
        _decision(decision_id="adm:v1:" + "9" * 62 + "02", selected_binding_kind="bogus")


# ---------------------------------------------------------------------------
# decision_projection：classification.binding 可機讀原因
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("binding_kind", "expected_classification"),
    (
        ("exact", "bound-exact"),
        ("identity", "bound-identity"),
        ("none", "binding-missing"),
        (None, "unknown"),
    ),
)
def test_decision_projection_classifies_binding_kind(tmp_path, binding_kind, expected_classification) -> None:
    store = admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    decision = _decision(decision_id="adm:v1:" + "9" * 62 + "03", selected_binding_kind=binding_kind)
    store.record(decision)

    projection = dp.project_workflow_quota_admission(
        run_id="run-1",
        quota_admission={"builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"}},
        needs_human_reason=None,
        store=store, now_ms=_NOW,
    )
    persona = projection["personas"]["builder"]
    assert persona["classification"]["binding"] == expected_classification


# ---------------------------------------------------------------------------
# unbound_profile_keys_report：唯讀報表
# ---------------------------------------------------------------------------


def _admit_row(*, executor: str, model_id: str, profile_key: str, generated_at_ms: int) -> dict[str, object]:
    decision = admission.AdmissionDecision(
        decision_id=f"adm:v1:{profile_key[-16:]}:{generated_at_ms}",
        run_id="run-1", card_id="card-1", attempt_id="attempt-0", profile_key=profile_key,
        mode="shadow", outcome="admit", policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:" + "b" * 16, demand_version=admission.DEMAND_FIXTURE_VERSION,
        qualification_version="not-enforced", generated_at_ms=generated_at_ms,
        selected={"executor": executor, "model_id": model_id},
    )
    return decision.to_row()


def test_unbound_profile_keys_report_finds_missing_and_ignores_covered() -> None:
    covered_descriptor = _pool_descriptor(account="account-covered", pool="pool-covered")
    covered_binding = _exact_binding((covered_descriptor,), _RESOLVED_TDD_RED)

    rows = [
        # 有精確綁定涵蓋——不應出現在報表裡。
        _admit_row(executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_TDD_RED, generated_at_ms=_NOW),
        # 沒有任何綁定涵蓋——應出現在報表裡。
        _admit_row(
            executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_SUBAGENT_BUILD,
            generated_at_ms=_NOW + 1000,
        ),
        # 同一個缺口再度出現：彙總次數、更新 last_seen_ms。
        _admit_row(
            executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_SUBAGENT_BUILD,
            generated_at_ms=_NOW + 2000,
        ),
        # wait 決策沒有 selected 候選，不列入。
        {
            "outcome": "wait", "profile_key": "quota-admission:no-admissible-candidate",
            "selected": None, "generated_at_ms": _NOW,
        },
    ]

    report = admission.unbound_profile_keys_report(rows, bindings=(covered_binding,))

    assert report == (
        admission.UnboundProfileUsage(
            executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_SUBAGENT_BUILD,
            decision_count=2, last_seen_ms=_NOW + 2000,
        ),
    )


def test_unbound_profile_keys_report_recomputes_against_current_bindings_not_stale_snapshot() -> None:
    """唯讀報表不信任 row 裡舊的 `selected_binding_kind` 快照——用『目前』
    傳入的 bindings 重算；設定檔後來補上 identity binding 後，舊缺口應該
    立刻從報表消失。"""
    rows = [
        _admit_row(
            executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_SUBAGENT_BUILD,
            generated_at_ms=_NOW,
        ),
    ]
    descriptor = _pool_descriptor()
    identity_binding = _identity_binding((descriptor,))

    assert admission.unbound_profile_keys_report(rows, bindings=()) != ()
    assert admission.unbound_profile_keys_report(rows, bindings=(identity_binding,)) == ()


# ---------------------------------------------------------------------------
# collector_targets 相容性：identity binding 與既有 profile-kind collector
# 設定共存於同一份 `cortex/quota-pools/v1` 檔案，不影響 collector 既有的精確
# 比對（見 quota_collectors._find_binding／quota_sources._binding_has_profile
# 文件字串：collector_targets 刻意不隨本票新增 identity 比對路徑）。
# ---------------------------------------------------------------------------


def test_collector_config_tolerates_identity_binding_alongside_profile_binding(tmp_path) -> None:
    descriptor = _pool_descriptor(account="account-collector", pool="pool-collector")
    profile_binding = _exact_binding((descriptor,), _RESOLVED_TDD_RED, binding_id="binding-profile")
    identity_binding = _identity_binding((descriptor,), binding_id="binding-identity")

    payload = {
        "schema": "cortex/quota-pools/v1",
        "config_revision": "rev-1116",
        "descriptors": [descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [profile_binding.to_dict(), identity_binding.to_dict()],
        "collector_targets": {
            "codex": [
                {
                    "resource_key": "codex:codex:short", "window_id": "short",
                    "profile_key": _RESOLVED_TDD_RED,
                    "pool_ref": _pool_ref(descriptor),
                },
            ],
        },
    }
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = collectors.load_collector_config(config_path)

    assert config.executors() == ("codex",)
    groups = config.groups_for("codex")
    assert len(groups) == 1
    assert groups[0].profile_key == _RESOLVED_TDD_RED
    assert groups[0].targets[0].binding.to_dict()["binding_id"] == "binding-profile"
    # admission 端仍完整解析出兩個 binding（collector 只是不用 identity 那個）。
    assert len(config.bindings) == 2


# ---------------------------------------------------------------------------
# CLI：`cortex quota bindings --report`
# ---------------------------------------------------------------------------


def test_cli_quota_bindings_report_lists_unbound_resolved_key(tmp_path, capsys) -> None:
    covered_descriptor = _pool_descriptor(account="account-covered", pool="pool-covered")
    covered_binding = _exact_binding((covered_descriptor,), _RESOLVED_TDD_RED)
    config_payload = {
        "schema": "cortex/quota-pools/v1",
        "config_revision": "rev-report-1116",
        "descriptors": [covered_descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [covered_binding.to_dict()],
    }
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    store_path = tmp_path / "decisions.jsonl"
    store = admission.AdmissionDecisionStore(store_path)
    store.record(admission.AdmissionDecision.from_row(
        _admit_row(executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_TDD_RED, generated_at_ms=_NOW)
    ))
    store.record(admission.AdmissionDecision.from_row(
        _admit_row(
            executor=_EXECUTOR, model_id=_MODEL_ID, profile_key=_RESOLVED_SUBAGENT_BUILD,
            generated_at_ms=_NOW + 1000,
        )
    ))

    exit_code = quota_cli.main([
        "bindings", "--report", "--config", str(config_path), "--store", str(store_path), "--json",
    ])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "cortex-porcelain/quota-bindings-report/v1"
    assert out["unbound"] == [
        {
            "executor": _EXECUTOR, "model_id": _MODEL_ID, "profile_key": _RESOLVED_SUBAGENT_BUILD,
            "decision_count": 1, "last_seen_ms": _NOW + 1000,
        },
    ]


def test_cli_quota_bindings_report_requires_flag(tmp_path, capsys) -> None:
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps({
        "schema": "cortex/quota-pools/v1", "config_revision": "rev-1",
        "descriptors": [_pool_descriptor().to_dict()], "unit_catalog": [],
        "bindings": [_exact_binding((_pool_descriptor(),), _RESOLVED_TDD_RED).to_dict()],
    }), encoding="utf-8")

    exit_code = quota_cli.main(["bindings", "--config", str(config_path)])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# quota_shadow.record_terminal_usage：identity binding 也要能記終局 usage
# ---------------------------------------------------------------------------


def test_record_terminal_usage_accepts_identity_binding() -> None:
    from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService

    descriptor = _pool_descriptor(unit_id="native-token")
    binding = _identity_binding((descriptor,))
    service = QuotaShadowService.in_memory()

    job = {
        "id": "job-1", "executor": _EXECUTOR, "model_id": _MODEL_ID,
        "usage": {"input_tokens": 10},
        "started_at": "2026-09-28T00:00:00Z", "finished_at": "2026-09-28T00:01:00Z",
    }
    result = service.record_terminal_usage(
        job, profile_key=_RESOLVED_SUBAGENT_BUILD, binding=binding,
        descriptors=(descriptor,), unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("native-token", "1")},
        observed_at_ms=_NOW,
    )
    assert result.status in ("accepted", "duplicate")


def test_record_terminal_usage_rejects_identity_binding_without_matching_model_id() -> None:
    descriptor = _pool_descriptor(unit_id="native-token")
    binding = _identity_binding((descriptor,))
    service = QuotaShadowService.in_memory()

    job = {
        "id": "job-2", "executor": _EXECUTOR, "model_id": "a-different-model",
        "usage": {"input_tokens": 10},
        "started_at": "2026-09-28T00:00:00Z", "finished_at": "2026-09-28T00:01:00Z",
    }
    result = service.record_terminal_usage(
        job, profile_key=_RESOLVED_SUBAGENT_BUILD, binding=binding,
        descriptors=(descriptor,), unit_catalog=(),
        unit_ref_by_metric={"input_tokens": ("native-token", "1")},
        observed_at_ms=_NOW,
    )
    assert result.status == "invalid"
