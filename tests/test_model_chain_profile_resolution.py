"""#452 C：claim 解析——measured 側寫優先序、band 過濾、resolved source 稽核。

風險鎖定（issue #452 關鍵風險）：新增 4 身分進 packaged registry 不得改變現行
熱路徑決策——planner 候選首位仍為 agy、select_secondary_planner 無 probe 的新
身分不可達。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, workflow
from paulsha_cortex.coordinator.model_identities import (
    AGY_MODEL_ID,
    IdentityRegistry,
    load_model_identities,
    select_secondary_planner,
)


def _measured_builder_row(executor: str, model_id: str, domain: str, bands: list[str]) -> dict:
    return {
        "executor": executor,
        "model_id": model_id,
        "independence_domain": domain,
        "capabilities": ["build"],
        "accepts_bands": bands,
        "profile_provenance": {
            "fingerprint": {
                "executor": executor,
                "model_id": model_id,
                "persona": "builder",
                "deck_id": "pilot-v1",
                "deck_content_sha256": "f" * 64,
                "patchmud_version": "0.0.1",
            },
            "source": {
                "accepts_bands": "measured",
                "invariant_ceiling": "default",
                "consistency_scope": "default",
                "acceptance_modes": "default",
            },
        },
    }


def _default_builder_row(executor: str, model_id: str, domain: str) -> dict:
    return {
        "executor": executor,
        "model_id": model_id,
        "independence_domain": domain,
        "capabilities": ["build"],
    }


def _run(*, sizing_band=None, override=None, primary_domain=None) -> SimpleNamespace:
    return SimpleNamespace(
        primary_domain=primary_domain,
        steps=[],
        sizing_band=sizing_band,
        model_chain_override=override,
        resolved_model_chain=None,
        run_id="run-1",
    )


def _step(persona: str) -> SimpleNamespace:
    return SimpleNamespace(persona=persona)


REGISTRY = IdentityRegistry.from_rows(
    [
        _default_builder_row("copilot", "gpt-5.4", "openai"),
        _measured_builder_row("claude", "sonnet", "anthropic", ["green", "yellow"]),
    ]
)


def test_measured_profile_candidates_sort_before_default() -> None:
    # 解析優先序：measured 側寫 > registry/預設（同層維持 registry 順序）。
    candidates = manager._workflow_identity_candidates(
        _run(sizing_band="green"), _step("builder"), REGISTRY
    )
    assert [(item.executor, item.model_id) for item in candidates] == [
        ("claude", "sonnet"),
        ("copilot", "gpt-5.4"),
    ]


def test_overlay_builder_survives_packaged_primary_domain_preference(tmp_path: Path) -> None:
    # 對抗審查修正鎖定：host overlay 宣告的 builder 與 packaged roster 併存時，
    # primary_domain 偏好命中 packaged 身分（僅為候選宣告、不隱含本機可用）
    # 不得把 overlay builder 整組擠出候選清單——preferred 排前、其餘保留在後，
    # #262 preflight re-route 才有 fallback 可用。
    overlay = tmp_path / "model-identities.yaml"
    overlay.write_text(
        "schema_version: 3\n"
        "identities:\n"
        "  - executor: codex\n"
        "    model_id: gpt-5.4\n"
        "    independence_domain: openai\n"
        "    capabilities: [build]\n",
        encoding="utf-8",
    )
    roster = load_model_identities(tmp_path, use_packaged_default=True)
    candidates = manager._workflow_identity_candidates(
        _run(primary_domain="anthropic"), _step("builder"), roster
    )
    keys = [(item.executor, item.model_id) for item in candidates]
    # #534：解析層是排序主鍵——operator 在 host overlay 指定的 builder 排第一，
    # primary_domain 偏好降級為**同層內**的次要偏好，不再有機會讓 packaged 候選
    # （僅為候選宣告、未經評估）壓過人工指定。
    assert keys[0] == ("codex", "gpt-5.4")
    # direct mode 保留既有 operator overlay／packaged 候選語意；hardened
    # compatibility gate 只在 Trust Root runner 明示啟用時套用。
    assert ("claude", "sonnet") in keys
    assert keys.index(("codex", "gpt-5.4")) < keys.index(("claude", "sonnet"))


def test_measured_band_filter_excludes_with_observable_reason(caplog) -> None:
    # red 不在 measured accepts_bands → 該身分被剔除，僅剩 default 身分。
    # 對抗審查修正（#209 R1）：部分剔除（仍有存活候選）時排除理由不得靜默
    # 丟棄——必須落 manager log。
    import logging

    with caplog.at_level(logging.INFO, logger="paulsha_cortex.coordinator.manager"):
        candidates = manager._workflow_identity_candidates(
            _run(sizing_band="red"), _step("builder"), REGISTRY
        )
    assert [(item.executor, item.model_id) for item in candidates] == [
        ("copilot", "gpt-5.4"),
    ]
    exclusion_logs = [
        record.getMessage()
        for record in caplog.records
        if "measured 側寫剔除候選" in record.getMessage()
    ]
    assert len(exclusion_logs) == 1
    assert "claude/sonnet" in exclusion_logs[0]
    assert "sizing_band=red" in exclusion_logs[0]
    # band 未知（planning 尚未產出）→ 不過濾（#453 零過濾不變量）。
    candidates = manager._workflow_identity_candidates(
        _run(sizing_band=None), _step("builder"), REGISTRY
    )
    assert len(candidates) == 2

    # 全部被 measured 過濾剔除 → fail-closed，且被排除原因可觀測（#209 R1）。
    measured_only = IdentityRegistry.from_rows(
        [_measured_builder_row("claude", "sonnet", "anthropic", ["green"])]
    )
    with pytest.raises(ValueError, match="accepts_bands.*sizing_band=yellow"):
        manager._workflow_identity_candidates(
            _run(sizing_band="yellow"), _step("builder"), measured_only
        )


def test_override_beats_measured_band_filter() -> None:
    # #205 覆寫優先序最高：measured 過濾不得推翻 operator 明確指定。
    override = {"builder": {"executor": "claude", "model_id": "sonnet"}}
    candidates = manager._workflow_identity_candidates(
        _run(sizing_band="red", override=override), _step("builder"), REGISTRY
    )
    assert [(item.executor, item.model_id) for item in candidates] == [("claude", "sonnet")]


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _manager_update_workflow_run(self, run_id, **kwargs):
        self.calls.append((run_id, kwargs))
        return SimpleNamespace(run_id=run_id, **kwargs)


def test_resolved_model_chain_source_distinguishes_profile_and_default() -> None:
    # #534：`source` 改記解析層，封套來源移到 `envelope_source`。REGISTRY 為手工
    # 建構（無 loader 蓋章）→ 一律視為 operator 指定的第 1 層。
    registry = _RecordingRegistry()
    run = _run(sizing_band="green")
    measured_identity = REGISTRY.require("claude", "sonnet")
    manager._record_resolved_model_chain(registry, run, _step("builder"), measured_identity)
    resolved = registry.calls[-1][1]["resolved_model_chain"]
    assert resolved["builder"]["source"] == "operator-overlay"
    assert resolved["builder"]["envelope_source"] == "measured"

    default_identity = REGISTRY.require("copilot", "gpt-5.4")
    manager._record_resolved_model_chain(registry, run, _step("builder"), default_identity)
    resolved = registry.calls[-1][1]["resolved_model_chain"]
    assert resolved["builder"]["source"] == "operator-overlay"
    assert resolved["builder"]["envelope_source"] == "default"

    override_run = _run(
        sizing_band="green",
        override={"builder": {"executor": "claude", "model_id": "sonnet"}},
    )
    manager._record_resolved_model_chain(
        registry, override_run, _step("builder"), measured_identity
    )
    resolved = registry.calls[-1][1]["resolved_model_chain"]
    assert resolved["builder"]["source"] == "run-override"


def test_workflow_validation_accepts_new_and_legacy_sources() -> None:
    for source in (
        "override",
        "registry",
        "patchmud-profile",
        "default-envelope",
        "run-override",
        "operator-overlay",
        "evaluated-roster",
        "packaged-fallback",
    ):
        workflow._validate_model_chain_resolution(
            {
                "builder": {
                    "executor": "claude",
                    "model_id": "sonnet",
                    "independence_domain": "anthropic",
                    "source": source,
                }
            },
            field_name="resolved_model_chain",
        )
    with pytest.raises(ValueError, match="source 非法"):
        workflow._validate_model_chain_resolution(
            {
                "builder": {
                    "executor": "claude",
                    "model_id": "sonnet",
                    "independence_domain": "anthropic",
                    "source": "vibes",
                }
            },
            field_name="resolved_model_chain",
        )


# ---------------------------------------------------------------------------
# roster 前後熱路徑決策不變（issue #452 關鍵風險的 T1 golden 面）
# ---------------------------------------------------------------------------


_PRE_ROSTER_REGISTRY = IdentityRegistry.from_rows(
    [
        {
            "executor": "agy",
            "model_id": AGY_MODEL_ID,
            "independence_domain": "google",
            "capabilities": ["planning"],
            "live_probe": "agy-plan-sandbox",
        }
    ]
)


def test_planner_selection_unchanged_after_roster(tmp_path: Path) -> None:
    roster = load_model_identities(tmp_path, use_packaged_default=True)
    run = _run(sizing_band=None)
    before = manager._select_workflow_identity(run, _step("planner"), _PRE_ROSTER_REGISTRY)
    after = manager._select_workflow_identity(run, _step("planner"), roster)
    # roster 落地前後 planner 首選同為 agy canonical 身分（agy 列首位）。
    assert (before.executor, before.model_id) == (after.executor, after.model_id) == (
        "agy",
        AGY_MODEL_ID,
    )


def test_secondary_planner_unreachable_without_probe(tmp_path: Path) -> None:
    roster = load_model_identities(tmp_path, use_packaged_default=True)
    # 無 probe（新身分皆無 live probe 覆蓋）→ 前後皆 needs_human，同一 reason：
    # select_secondary_planner 只在 probe.ready 才選，新 roster 身分不可達。
    before = select_secondary_planner(
        registry=_PRE_ROSTER_REGISTRY, primary=("agy", AGY_MODEL_ID), probes={}
    )
    after = select_secondary_planner(
        registry=roster, primary=("agy", AGY_MODEL_ID), probes={}
    )
    assert (before.state, before.reason) == (after.state, after.reason) == (
        "needs_human",
        "no-heterogeneous-planner",
    )
