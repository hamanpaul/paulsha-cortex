"""v4 R1（方案 A）coverage validator shadow 骨架的測試。

重點是**零行為變更的證明**：無論 coverage validator 判 pass 或 fail，production 的
``validate_manager_spine()`` 結果與本 PR 前逐位元組相同；shadow 只多寫 telemetry。
"""

from __future__ import annotations

import json

import pytest

from paulsha_cortex.coordinator import coverage
from paulsha_cortex.coordinator.coverage import (
    SAFETY_STAGE_NAMES,
    SAFETY_STAGES,
    ResponsibilityCoverage,
    SafetyStage,
    compare_manifest,
    coverage_verdict,
    evaluate_coverage,
    resolve_card_satisfies,
    run_coverage_shadow,
    shadow_enabled,
    stage_for_phase,
    topology_verdict,
)
from paulsha_cortex.coordinator.workflow import (
    WORKFLOW_PHASES,
    WorkflowManifest,
    WorkflowStep,
)
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, Card, DeckSchemaError, load_cards
from paulsha_cortex.coordinator.work_bridge import default_workflow_manifest


# --------------------------------------------------------------------------
# 型別：SAFETY_STAGES / phase adapter
# --------------------------------------------------------------------------


def test_safety_stages_map_one_to_one_with_phases() -> None:
    assert len(SAFETY_STAGES) == len(set(SAFETY_STAGES)) == len(WORKFLOW_PHASES)
    mapped = {stage_for_phase(phase) for phase in WORKFLOW_PHASES}
    assert mapped == set(SAFETY_STAGES)
    assert SAFETY_STAGE_NAMES == {stage.value for stage in SAFETY_STAGES}


def test_manager_authority_stages_are_claim_and_ship() -> None:
    assert stage_for_phase("claim") in coverage.MANAGER_AUTHORITY_STAGES
    assert stage_for_phase("ship") in coverage.MANAGER_AUTHORITY_STAGES
    assert coverage.MANAGER_AUTHORITY_STAGES == frozenset(
        {SafetyStage.INTAKE, SafetyStage.DELIVERY}
    )


def test_stage_for_phase_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        stage_for_phase("not-a-phase")


# --------------------------------------------------------------------------
# satisfies adapter（宣告優先、phase 兜底）
# --------------------------------------------------------------------------


def _card(**overrides) -> Card:
    base = dict(
        id="c", kind="skill", type="headless", card_class="core", skill_ref="skills/x"
    )
    base.update(overrides)
    return Card(**base)


def test_resolve_card_satisfies_prefers_declaration() -> None:
    card = _card(phase="build", satisfies=("review", "verification"))
    assert resolve_card_satisfies(card) == (SafetyStage.REVIEW, SafetyStage.VERIFICATION)


def test_resolve_card_satisfies_falls_back_to_phase() -> None:
    card = _card(phase="review")
    assert resolve_card_satisfies(card) == (SafetyStage.REVIEW,)


def test_resolve_card_satisfies_skips_unknown_names() -> None:
    card = _card(phase="build", satisfies=("bogus", "planning"))
    assert resolve_card_satisfies(card) == (SafetyStage.PLANNING,)


def test_resolve_card_satisfies_empty_when_no_signal() -> None:
    assert resolve_card_satisfies(_card(phase=None)) == ()


# --------------------------------------------------------------------------
# deck schema：optional satisfies
# --------------------------------------------------------------------------


def test_existing_default_cards_have_empty_satisfies() -> None:
    cards = load_cards(DEFAULT_CARDS_PATH)
    assert cards
    assert all(card.satisfies == () for card in cards.values())


def test_schema_parses_optional_satisfies(tmp_path) -> None:
    path = tmp_path / "cards.yaml"
    path.write_text(
        "version: 0\n"
        "cards:\n"
        "  - id: with-satisfies\n"
        "    kind: skill\n"
        "    type: headless\n"
        "    class: core\n"
        "    skill_ref: skills/a\n"
        "    phase: review\n"
        "    satisfies: [review]\n"
        "  - id: without-satisfies\n"
        "    kind: skill\n"
        "    type: headless\n"
        "    class: core\n"
        "    skill_ref: skills/b\n"
        "    phase: build\n",
        encoding="utf-8",
    )
    cards = load_cards(path)
    assert cards["with-satisfies"].satisfies == ("review",)
    assert cards["without-satisfies"].satisfies == ()


def test_schema_rejects_duplicate_satisfies(tmp_path) -> None:
    path = tmp_path / "cards.yaml"
    path.write_text(
        "version: 0\n"
        "cards:\n"
        "  - id: dup\n"
        "    kind: skill\n"
        "    type: headless\n"
        "    class: core\n"
        "    skill_ref: skills/a\n"
        "    phase: review\n"
        "    satisfies: [review, review]\n",
        encoding="utf-8",
    )
    with pytest.raises(DeckSchemaError):
        load_cards(path)


# --------------------------------------------------------------------------
# coverage validator
# --------------------------------------------------------------------------


def _valid_manifest() -> WorkflowManifest:
    return default_workflow_manifest("demo-work", change=None, combo_name="feature-oneshot")


def _topology_broken_manifest() -> WorkflowManifest:
    """全部 7 phase 都在、但順序反轉——topology fail、coverage pass。"""
    valid = _valid_manifest()
    return WorkflowManifest(
        combo=valid.combo,
        task_slug=valid.task_slug,
        steps=tuple(reversed(valid.steps)),
    )


def test_evaluate_coverage_complete_on_valid_manifest() -> None:
    cov = evaluate_coverage(_valid_manifest())
    assert cov.is_complete
    assert cov.missing == ()
    assert cov.covered == frozenset(SAFETY_STAGES)
    assert coverage_verdict(cov).passed


def test_coverage_missing_responsibility_when_phase_absent() -> None:
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="only-build",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
    )
    manifest = WorkflowManifest(combo="c", task_slug="s", steps=(step,))
    cov = evaluate_coverage(manifest)
    assert not cov.is_complete
    assert SafetyStage.IMPLEMENTATION in cov.covered
    assert SafetyStage.REVIEW in cov.missing
    verdict = coverage_verdict(cov)
    assert not verdict.passed
    assert "review" in verdict.reason


def test_responsibility_coverage_to_dict_roundtrips_stage_values() -> None:
    cov: ResponsibilityCoverage = evaluate_coverage(_valid_manifest())
    payload = cov.to_dict()
    assert payload["missing"] == []
    assert set(payload["covered"]) == SAFETY_STAGE_NAMES
    assert all(payload["satisfied_by"][stage.value] for stage in SAFETY_STAGES)


# --------------------------------------------------------------------------
# shadow 比對：disagreement 只可能是 topology-fail / coverage-pass
# --------------------------------------------------------------------------


def test_valid_manifest_yields_agreement() -> None:
    comparison = compare_manifest(_valid_manifest(), callsite="test")
    assert comparison.topology.passed
    assert comparison.coverage_verdict.passed
    assert comparison.agreement
    assert comparison.disagreement_kind is None


def test_topology_broken_manifest_is_a_disagreement() -> None:
    comparison = compare_manifest(_topology_broken_manifest(), callsite="test")
    assert comparison.topology.passed is False
    assert comparison.coverage_verdict.passed is True
    assert comparison.agreement is False
    assert comparison.disagreement_kind == "topology-fail-coverage-pass"
    payload = comparison.to_dict()
    assert payload["disagreement"]["kind"] == "topology-fail-coverage-pass"
    assert payload["topology"]["passed"] is False
    assert payload["coverage"]["passed"] is True


# --------------------------------------------------------------------------
# 零行為變更的證明
# --------------------------------------------------------------------------


def test_shadow_does_not_change_topology_pass_result(tmp_path) -> None:
    manifest = _valid_manifest()
    run_coverage_shadow(manifest, callsite="test", root=tmp_path)
    # production gate 仍照舊通過，未被 shadow 影響。
    manifest.validate_manager_spine()


def test_shadow_does_not_change_topology_fail_result(tmp_path) -> None:
    manifest = _topology_broken_manifest()
    # coverage validator 判 pass，但 production gate 仍必須照舊 raise（逐位元組不變）。
    comparison = run_coverage_shadow(manifest, callsite="test", root=tmp_path)
    assert comparison is not None and comparison.coverage_verdict.passed
    with pytest.raises(ValueError):
        manifest.validate_manager_spine()


def test_topology_verdict_reason_matches_raised_message() -> None:
    manifest = _topology_broken_manifest()
    verdict = topology_verdict(manifest)
    with pytest.raises(ValueError) as excinfo:
        manifest.validate_manager_spine()
    # shadow 對 topology 的判定就是 production 邏輯本身——原因訊息逐字一致。
    assert verdict.reason == str(excinfo.value)


def test_manifest_bytes_carry_no_satisfies() -> None:
    manifest = _valid_manifest()
    serialized = json.dumps(manifest.to_dict(), sort_keys=True)
    assert "satisfies" not in serialized
    for step in manifest.to_dict()["steps"]:
        assert "satisfies" not in step


def test_run_coverage_shadow_never_raises_even_if_compare_breaks(tmp_path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("shadow internal explosion")

    monkeypatch.setattr(coverage, "compare_manifest", boom)
    # 內部炸掉也必須被吞掉、回 None，絕不外溢到 production。
    assert run_coverage_shadow(_valid_manifest(), callsite="test", root=tmp_path) is None


def test_write_shadow_record_never_raises_on_unwritable_root(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    comparison = compare_manifest(_valid_manifest(), callsite="test")
    # root 是檔案不是目錄 → mkdir 失敗 → 吞掉回 None。
    assert coverage.write_shadow_record(comparison, root=blocker / "sub") is None


# --------------------------------------------------------------------------
# 回滾開關與 telemetry 落點
# --------------------------------------------------------------------------


def test_shadow_enabled_default_on() -> None:
    assert shadow_enabled({}) is True
    assert shadow_enabled({"PSC_RESPONSIBILITY_COVERAGE": "on"}) is True
    assert shadow_enabled({"PSC_RESPONSIBILITY_COVERAGE": "anything"}) is True


def test_shadow_enabled_off_disables() -> None:
    assert shadow_enabled({"PSC_RESPONSIBILITY_COVERAGE": "off"}) is False
    assert shadow_enabled({"PSC_RESPONSIBILITY_COVERAGE": " OFF "}) is False


def test_env_off_skips_comparison_and_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PSC_RESPONSIBILITY_COVERAGE", "off")
    assert run_coverage_shadow(_valid_manifest(), callsite="test", root=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_env_on_writes_one_telemetry_file_with_required_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PSC_RESPONSIBILITY_COVERAGE", raising=False)
    comparison = run_coverage_shadow(
        _topology_broken_manifest(),
        callsite="manager.start",
        context={"work_id": "w-1", "repo": "owner/repo"},
        root=tmp_path,
    )
    assert comparison is not None
    files = [p for p in tmp_path.iterdir() if p.suffix == ".json"]
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    # 至少含：manifest 識別、兩方判定、disagreement 細節。
    assert record["manifest"]["combo"] and record["manifest"]["task_slug"]
    assert record["callsite"] == "manager.start"
    assert record["context"] == {"work_id": "w-1", "repo": "owner/repo"}
    assert record["topology"]["passed"] is False
    assert record["coverage"]["passed"] is True
    assert record["disagreement"]["kind"] == "topology-fail-coverage-pass"
    assert record["schema_version"] == coverage.SHADOW_TELEMETRY_SCHEMA


def test_default_telemetry_root_follows_coordinator_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.delenv("PSC_RESPONSIBILITY_COVERAGE", raising=False)
    run_coverage_shadow(_valid_manifest(), callsite="test")
    written = list((tmp_path / "coverage-shadow").glob("*.json"))
    assert len(written) == 1
