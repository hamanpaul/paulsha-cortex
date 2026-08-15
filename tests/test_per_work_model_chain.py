"""#205：per-work planner/builder/reviewer 模型鏈覆寫。

驗收依據：
- docs/superpowers/specs/per-work-model-chain-spec.md（R1~R4）
- docs/superpowers/specs/per-work-model-chain-design.md（D1~D5）
- openspec/changes/2026-07-30-per-work-model-chain/specs/persona-workflow-orchestration/spec.md

測試名稱依 docs/superpowers/plans/per-work-model-chain.md 第 1 節列出的清單。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "per-work-model-chain", change="per-work-model-chain")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _create_run(
    registry: JobRegistry,
    *,
    work_id: str,
    claim_key: str,
    model_chain_override: dict | None = None,
):
    manifest = _manifest()
    return registry._manager_create_workflow_run(
        work_id=work_id,
        repo="owner/repo",
        claim_key=claim_key,
        source_revision="rev-a",
        workspace_root="/tmp/workspace",
        combo=manifest.combo,
        current_phase="plan",
        steps=manifest.steps,
        attempts={"claim": 1, "define": 1, "plan": 1},
        gate_status="running",
        model_chain_override=model_chain_override,
    )


def _step(run, *, phase: str, persona: str):
    return next(item for item in run.steps if item.phase == phase and item.persona == persona)


def _identity_run(*, primary_domain: str | None = None, build_domain: str | None = None, override=None):
    steps = []
    if build_domain is not None:
        steps.append(SimpleNamespace(phase="build", gate_result="passed", domain=build_domain))
    return SimpleNamespace(primary_domain=primary_domain, steps=steps, model_chain_override=override)


_BUILDER_IDENTITIES = IdentityRegistry.from_rows(
    [
        {
            "executor": "codex",
            "model_id": "spark",
            "independence_domain": "openai",
            "capabilities": ["build"],
        },
        {
            "executor": "copilot",
            "model_id": "builder-two",
            "independence_domain": "microsoft",
            "capabilities": ["build"],
        },
        {
            "executor": "claude",
            "model_id": "planner-one",
            "independence_domain": "anthropic",
            "capabilities": ["planning"],
        },
        {
            "executor": "claude",
            "model_id": "reviewer-anthropic",
            "independence_domain": "anthropic",
            "capabilities": ["review"],
        },
        {
            "executor": "agy",
            "model_id": "reviewer-google",
            "independence_domain": "google",
            "capabilities": ["review"],
        },
    ]
)


def test_override_applies_to_target_run_only() -> None:
    """R1：為某 run 指定 builder 模型後，其他 active run 尚未派出的 card 選擇
    結果不變（跟一份完全沒有覆寫的 identities 選出來的結果一致）。"""
    override_run = _identity_run(override={"builder": {"executor": "copilot", "model_id": "builder-two"}})
    other_run = _identity_run(override=None)
    builder_step = SimpleNamespace(persona="builder")

    overridden = manager._select_workflow_identity(override_run, builder_step, _BUILDER_IDENTITIES)
    unaffected = manager._select_workflow_identity(other_run, builder_step, _BUILDER_IDENTITIES)

    assert (overridden.executor, overridden.model_id) == ("copilot", "builder-two")
    # 其他 run 完全沒被波及：選出跟共享 registry 第一個 build candidate 一致。
    assert (unaffected.executor, unaffected.model_id) == ("codex", "spark")
    # 共享 registry 物件本身沒被覆寫動過手腳（同一個 identities 實例，identity 順序原封不動）。
    assert [item.model_id for item in _BUILDER_IDENTITIES.identities] == [
        "spark", "builder-two", "planner-one", "reviewer-anthropic", "reviewer-google",
    ]


def test_override_frozen_at_claim(tmp_path: Path) -> None:
    """R2/D2：覆寫值於 claim 時凍結；claim 後即使共享 registry 順序改變
    （這裡用另一份 identities 模擬），該 run 仍沿用凍結時指定的 identity。"""
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _create_run(
        registry,
        work_id="frozen-work",
        claim_key="owner/repo/frozen-work/rev-a",
        model_chain_override={"builder": {"executor": "copilot", "model_id": "builder-two"}},
    )
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.model_chain_override == {
        "builder": {"executor": "copilot", "model_id": "builder-two"}
    }

    # 模擬「claim 後共享 registry 順序改變」：換一份把 copilot 排到最後的 identities。
    reordered_identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "spark",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            {
                "executor": "copilot",
                "model_id": "builder-two",
                "independence_domain": "microsoft",
                "capabilities": ["build"],
            },
        ]
    )
    build_step = _step(persisted, phase="build", persona="builder")
    selected = manager._select_workflow_identity(persisted, build_step, reordered_identities)
    assert (selected.executor, selected.model_id) == ("copilot", "builder-two")


def test_resume_and_retry_use_frozen_chain(tmp_path: Path) -> None:
    """R2：resume／retry-build／retry-verify／retry-review 一路推進（透過
    _manager_update_workflow_run 多次呼叫，均未再傳 model_chain_override）
    MUST NOT 重新依共享 registry 選擇——每一步之後凍結值都原封不動。"""
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _create_run(
        registry,
        work_id="resume-work",
        claim_key="owner/repo/resume-work/rev-a",
        model_chain_override={"reviewer": {"executor": "agy", "model_id": "reviewer-google"}},
    )
    original_override = registry.get_workflow_run(run.run_id).model_chain_override

    # resume：推進到 build
    run = registry._manager_update_workflow_run(run.run_id, current_phase="build")
    assert registry.get_workflow_run(run.run_id).model_chain_override == original_override

    # retry-build 類語意動作：重寫 attempts，不涉及 model_chain_override
    run = registry._manager_update_workflow_run(run.run_id, attempts={**run.attempts, "build": run.attempts.get("build", 0) + 1})
    assert registry.get_workflow_run(run.run_id).model_chain_override == original_override

    # retry-verify / retry-review 類推進
    run = registry._manager_update_workflow_run(run.run_id, current_phase="verify")
    run = registry._manager_update_workflow_run(run.run_id, current_phase="review")
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.model_chain_override == original_override
    assert persisted.model_chain_override == {
        "reviewer": {"executor": "agy", "model_id": "reviewer-google"}
    }

    # 沿用凍結值選出的 reviewer identity 與 override 指定一致，不因推進而漂移。
    reviewer_step = _step(persisted, phase="review", persona="reviewer")
    selected = manager._select_workflow_identity(persisted, reviewer_step, _BUILDER_IDENTITIES)
    assert (selected.executor, selected.model_id) == ("agy", "reviewer-google")


def test_partial_override_falls_back_per_segment() -> None:
    """D3：只覆寫 builder 時，planner／reviewer 回退共享 registry 選擇
    （與完全沒有覆寫時選出的結果一致）。"""
    override = {"builder": {"executor": "copilot", "model_id": "builder-two"}}
    run = _identity_run(primary_domain=None, build_domain=None, override=override)
    baseline_run = _identity_run(primary_domain=None, build_domain=None, override=None)

    builder_step = SimpleNamespace(persona="builder")
    planner_step = SimpleNamespace(persona="planner")
    reviewer_step = SimpleNamespace(persona="reviewer")

    builder_selected = manager._select_workflow_identity(run, builder_step, _BUILDER_IDENTITIES)
    assert (builder_selected.executor, builder_selected.model_id) == ("copilot", "builder-two")

    planner_selected = manager._select_workflow_identity(run, planner_step, _BUILDER_IDENTITIES)
    planner_baseline = manager._select_workflow_identity(baseline_run, planner_step, _BUILDER_IDENTITIES)
    assert (planner_selected.executor, planner_selected.model_id) == (
        planner_baseline.executor,
        planner_baseline.model_id,
    )
    assert (planner_selected.executor, planner_selected.model_id) == ("claude", "planner-one")

    reviewer_selected = manager._select_workflow_identity(run, reviewer_step, _BUILDER_IDENTITIES)
    reviewer_baseline = manager._select_workflow_identity(baseline_run, reviewer_step, _BUILDER_IDENTITIES)
    assert (reviewer_selected.executor, reviewer_selected.model_id) == (
        reviewer_baseline.executor,
        reviewer_baseline.model_id,
    )
    assert (reviewer_selected.executor, reviewer_selected.model_id) == ("claude", "reviewer-anthropic")


def test_override_violating_capability_fails_closed() -> None:
    """D4：覆寫指定不具備該 persona 所需 capability 的 identity 時 MUST fail
    closed 並回報原因，MUST NOT 靜默退回共享 registry 的預設選擇。"""
    # codex/spark 只有 build capability，拿來覆寫 reviewer 違反 capability 檢查。
    override = {"reviewer": {"executor": "codex", "model_id": "spark"}}
    run = _identity_run(override=override)
    reviewer_step = SimpleNamespace(persona="reviewer")

    with pytest.raises(ValueError, match="不符既有約束"):
        manager._select_workflow_identity(run, reviewer_step, _BUILDER_IDENTITIES)

    # 沒有靜默退回共享預設：捕捉例外之後，共享路徑原本會選出的 identity 依然
    # 只能透過「沒有覆寫」的呼叫拿到，覆寫呼叫本身必須整個失敗，不能回傳任何值。
    baseline_run = _identity_run(override=None)
    fallback = manager._select_workflow_identity(baseline_run, reviewer_step, _BUILDER_IDENTITIES)
    assert (fallback.executor, fallback.model_id) == ("claude", "reviewer-anthropic")


def test_override_violating_independence_domain_fails_closed() -> None:
    """D4：覆寫使 builder 與 reviewer 落在同一 independence domain 時 MUST
    fail closed 並回報原因，不得靜默退回。"""
    run = _identity_run(
        build_domain="anthropic",
        override={"reviewer": {"executor": "claude", "model_id": "reviewer-anthropic"}},
    )
    reviewer_step = SimpleNamespace(persona="reviewer")

    with pytest.raises(ValueError, match="independence_domain"):
        manager._select_workflow_identity(run, reviewer_step, _BUILDER_IDENTITIES)


def test_unknown_identity_lists_available_candidates() -> None:
    """R3：覆寫指定的 model 不存在於 registry 時 fail closed，且錯誤訊息列出
    該 capability 下可用的 identity（不能只說「找不到」）。"""
    override = {"builder": {"executor": "codex", "model_id": "does-not-exist"}}
    run = _identity_run(override=override)
    builder_step = SimpleNamespace(persona="builder")

    with pytest.raises(ValueError) as excinfo:
        manager._select_workflow_identity(run, builder_step, _BUILDER_IDENTITIES)

    message = str(excinfo.value)
    assert "不存在" in message
    # 至少列出一個實際可用的 build candidate，而不是空清單或只回報 unknown。
    assert "codex/spark" in message
    assert "copilot/builder-two" in message


def test_evidence_records_resolution_and_source(tmp_path: Path) -> None:
    """R4/D5：run 的 durable evidence（WorkflowRun.resolved_model_chain）必須
    記錄三段各自實際解析到的 executor／model／independence_domain，以及來源
    （run-scoped 覆寫 vs 共享 registry）——不能只記模型名，來源要能分辨。"""
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _create_run(
        registry,
        work_id="evidence-work",
        claim_key="owner/repo/evidence-work/rev-a",
        model_chain_override={"builder": {"executor": "copilot", "model_id": "builder-two"}},
    )
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.resolved_model_chain is None

    builder_step = _step(persisted, phase="build", persona="builder")
    builder_identity = manager._select_workflow_identity(persisted, builder_step, _BUILDER_IDENTITIES)
    manager._record_resolved_model_chain(registry, persisted, builder_step, builder_identity)

    after_builder = registry.get_workflow_run(run.run_id)
    assert after_builder.resolved_model_chain == {
        "builder": {
            "executor": "copilot",
            "model_id": "builder-two",
            "independence_domain": "microsoft",
            "source": "run-override",
            "envelope_source": "default",
        }
    }

    # planner 段沒有覆寫，走共享 registry；來源標記必須能分辨兩者不同。
    # #534：非覆寫段的 source 記**解析層**——手工建構的 registry 視為 operator
    # 指定（第 1 層）；封套來源改記在 envelope_source。
    planner_step = _step(after_builder, phase="plan", persona="planner")
    planner_identity = manager._select_workflow_identity(after_builder, planner_step, _BUILDER_IDENTITIES)
    manager._record_resolved_model_chain(registry, after_builder, planner_step, planner_identity)

    final_run = registry.get_workflow_run(run.run_id)
    assert final_run.resolved_model_chain["builder"]["source"] == "run-override"
    assert final_run.resolved_model_chain["planner"] == {
        "executor": "claude",
        "model_id": "planner-one",
        "independence_domain": "anthropic",
        "source": "operator-overlay",
        "envelope_source": "default",
    }
    # builder 段的紀錄沒有被 planner 段的更新蓋掉（逐段合併，不是整段覆蓋）。
    assert final_run.resolved_model_chain["builder"]["model_id"] == "builder-two"
