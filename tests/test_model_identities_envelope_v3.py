"""#452 B：registry schema v3（封套四欄位＋profile_provenance）loader／投影測試。

涵蓋 #453 R6-T3（loader 相容三件套）與 #456 R8（roster 正向載入＋兩條負向
fail-closed）。T1/T2 bit-identical 規格在 tests/test_default_envelope_bitidentity.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator import envelope_mapping
from paulsha_cortex.coordinator.model_identities import (
    ACCEPTANCE_MODES_DOMAIN,
    AGY_MODEL_ID,
    CONSISTENCY_SCOPE_DOMAIN,
    DEFAULT_ENVELOPE,
    ENVELOPE_SOURCE_DEFAULT,
    ENVELOPE_SOURCE_MEASURED,
    IdentityRegistry,
    build_capability_lookup,
    evaluate_capability,
    load_model_identities,
    plan_review_envelope_projection,
    project_envelope,
)
from paulsha_cortex.coordinator.workflow import MODEL_CHAIN_PERSONAS


def _provenance(persona: str = "builder", *, executor: str = "claude", model_id: str = "sonnet") -> dict:
    return {
        "fingerprint": {
            "executor": executor,
            "model_id": model_id,
            "persona": persona,
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
        "reasons": {"accepts_bands": "measured:clear-rate-ladder-v1"},
        "observation": {"runs": 8, "clears": 6},
        "profiled_at": "2026-08-12T00:00:00Z",
    }


def _measured_row(**overrides) -> dict:
    row = {
        "executor": "claude",
        "model_id": "sonnet",
        "independence_domain": "anthropic",
        "capabilities": ["planning", "build", "review"],
        "accepts_bands": ["green", "yellow"],
        "profile_provenance": _provenance(),
    }
    row.update(overrides)
    return row


V3_MEASURED_FILE = """\
schema_version: 3
identities:
  - executor: claude
    model_id: sonnet
    independence_domain: anthropic
    capabilities: [planning, build, review]
    accepts_bands: [green, yellow]
    profile_provenance:
      fingerprint:
        executor: claude
        model_id: sonnet
        persona: builder
        deck_id: pilot-v1
        deck_content_sha256: {sha}
        patchmud_version: 0.0.1
      source:
        accepts_bands: measured
        invariant_ceiling: default
        consistency_scope: default
        acceptance_modes: default
      reasons:
        accepts_bands: measured:clear-rate-ladder-v1
      observation:
        runs: 8
        clears: 6
        band_rule: clear-rate-ladder-v1
        red_pinned: false
      profiled_at: 2026-08-12T00:00:00Z
""".format(sha="f" * 64)


# ---------------------------------------------------------------------------
# DEFAULT_ENVELOPE 單一真值與 persona 對齊
# ---------------------------------------------------------------------------


def test_default_envelope_single_source_and_persona_alignment() -> None:
    # #454 spec 非目標第三條：schema v3 落地後單一真值住 model_identities，
    # envelope_mapping re-export 同一個物件（不得出現第二份定值）。
    assert envelope_mapping.DEFAULT_ENVELOPE is DEFAULT_ENVELOPE
    assert set(DEFAULT_ENVELOPE) == set(MODEL_CHAIN_PERSONAS)


# ---------------------------------------------------------------------------
# T3：loader 相容三件套
# ---------------------------------------------------------------------------


def test_v1_and_v2_files_still_load(tmp_path: Path) -> None:
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "model-identities.yaml").write_text(
        "schema_version: 1\nidentities:\n"
        "  - executor: codex\n    model_id: legacy\n    independence_domain: openai\n",
        encoding="utf-8",
    )
    registry = load_model_identities(v1, use_packaged_default=False)
    assert registry.schema_version == 1
    assert registry.require("codex", "legacy").capabilities == ("planning",)

    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "model-identities.yaml").write_text(
        "schema_version: 2\nidentities:\n"
        "  - executor: codex\n    model_id: legacy2\n    independence_domain: openai\n"
        "    capabilities: [build]\n",
        encoding="utf-8",
    )
    registry = load_model_identities(v2, use_packaged_default=False)
    assert registry.schema_version == 2
    assert registry.require("codex", "legacy2").accepts_bands is None


def test_v2_file_rejects_envelope_fields(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        "schema_version: 2\nidentities:\n"
        "  - executor: codex\n    model_id: x\n    independence_domain: openai\n"
        "    accepts_bands: [green]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="accepts_bands unexpected"):
        load_model_identities(tmp_path, use_packaged_default=False)


def test_from_rows_construction_layer_gates_envelope_by_schema_version() -> None:
    # 對抗審查修正：v1/v2 的封套 fail-closed 不只在檔案載入層成立——直接呼叫
    # from_rows(schema_version=2) 構造帶封套欄位的 registry 也必須被擋下
    # （建構層與檔案層同一條規則，閘門由 schema_version 推導）。
    row = _measured_row()
    with pytest.raises(ValueError, match="unexpected"):
        IdentityRegistry.from_rows([row], schema_version=2)
    with pytest.raises(ValueError, match="unexpected"):
        IdentityRegistry.from_rows([row], schema_version=1)
    # v3（含預設 schema_version）照常接受。
    assert IdentityRegistry.from_rows([row], schema_version=3).require(
        "claude", "sonnet"
    ).accepts_bands == ("green", "yellow")


def test_v3_measured_file_roundtrip_and_projection(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(V3_MEASURED_FILE, encoding="utf-8")
    registry = load_model_identities(tmp_path, use_packaged_default=False)
    identity = registry.require("claude", "sonnet")
    assert identity.accepts_bands == ("green", "yellow")
    assert identity.invariant_ceiling is None

    projection = project_envelope(identity, "builder")
    assert projection.source["accepts_bands"] == ENVELOPE_SOURCE_MEASURED
    assert projection.source["invariant_ceiling"] == ENVELOPE_SOURCE_DEFAULT
    assert tuple(projection.envelope["accepts_bands"]) == ("green", "yellow")
    # 指紋 persona=builder，planner 查表不得沿用 builder 實測值。
    planner_projection = project_envelope(identity, "planner")
    assert planner_projection.all_default
    assert tuple(planner_projection.envelope["accepts_bands"]) == ("green", "yellow", "red")


def test_default_projection_for_unmeasured_identity_and_overlay(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        "schema_version: 2\nidentities:\n"
        "  - executor: local\n    model_id: overlay\n    independence_domain: local\n"
        "    capabilities: [build]\n",
        encoding="utf-8",
    )
    registry = load_model_identities(tmp_path, use_packaged_default=True)
    overlay_identity = registry.require("local", "overlay")
    projection = project_envelope(overlay_identity, "builder")
    assert projection.all_default
    assert projection.envelope["invariant_ceiling"] is None
    assert tuple(projection.envelope["consistency_scope"]) == CONSISTENCY_SCOPE_DOMAIN
    assert tuple(projection.envelope["acceptance_modes"]) == ACCEPTANCE_MODES_DOMAIN


def test_invariant_ceiling_none_is_bypass_not_zero() -> None:
    registry = IdentityRegistry.from_rows([_measured_row()])
    identity = registry.require("claude", "sonnet")
    # #453 R2：sentinel None MUST NOT 讀成 0——判準 2 為 bypass，不觸發
    # envelope-exceeded 類過濾（invariant_count=99 仍不被判否）。
    evaluation = evaluate_capability(
        identity, persona="builder", sizing_band="green", invariant_count=99
    )
    by_name = {item.name: item for item in evaluation.criteria}
    assert by_name["invariant_ceiling"].state == "bypass"
    assert evaluation.verdict is True
    # plan-review seam 投影亦維持 bypass（兩鍵任一 default → None，#454 R5）。
    assert plan_review_envelope_projection(identity, persona="builder") is None


# ---------------------------------------------------------------------------
# v3 封套欄位 fail-closed 驗證
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"accepts_bands": []}, "accepts_bands"),
        ({"accepts_bands": ["green", "green"]}, "duplicates"),
        ({"accepts_bands": ["purple"]}, "outside domain"),
        ({"invariant_ceiling": -1, "accepts_bands": None}, "invariant_ceiling"),
        ({"invariant_ceiling": True, "accepts_bands": None}, "invariant_ceiling"),
        ({"consistency_scope": ["exotic"], "accepts_bands": None}, "outside domain"),
        ({"acceptance_modes": ["yolo"], "accepts_bands": None}, "outside domain"),
    ],
)
def test_envelope_field_validation_fail_closed(mutation: dict, message: str) -> None:
    row = _measured_row()
    for key, value in mutation.items():
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
    measured = [name for name in ("accepts_bands", "invariant_ceiling", "consistency_scope", "acceptance_modes") if name in row]
    row["profile_provenance"]["source"] = {
        name: ("measured" if name in measured else "default")
        for name in ("accepts_bands", "invariant_ceiling", "consistency_scope", "acceptance_modes")
    }
    with pytest.raises(ValueError, match=message):
        IdentityRegistry.from_rows([row])


def test_envelope_fields_require_provenance_and_source_consistency() -> None:
    row = _measured_row()
    row.pop("profile_provenance")
    with pytest.raises(ValueError, match="require profile_provenance"):
        IdentityRegistry.from_rows([row])

    # source 宣告 measured 的欄位必須實際存在（registry 永不寫入預設值）。
    row = _measured_row()
    row["profile_provenance"]["source"]["invariant_ceiling"] = "measured"
    with pytest.raises(ValueError, match="do not match"):
        IdentityRegistry.from_rows([row])

    # fingerprint 身分對不上該列 → 拒載。
    row = _measured_row()
    row["profile_provenance"]["fingerprint"]["model_id"] = "other"
    with pytest.raises(ValueError, match="identity mismatch"):
        IdentityRegistry.from_rows([row])

    # persona 非法 → 拒載。
    row = _measured_row()
    row["profile_provenance"]["fingerprint"]["persona"] = "manager"
    with pytest.raises(ValueError, match="persona invalid"):
        IdentityRegistry.from_rows([row])


# ---------------------------------------------------------------------------
# #456 R8：roster 正向載入＋兩條負向 fail-closed
# ---------------------------------------------------------------------------


def test_packaged_roster_positive_load_and_bindings(tmp_path: Path) -> None:
    registry = load_model_identities(tmp_path, use_packaged_default=True)
    by_key = {(item.executor, item.model_id): item for item in registry.identities}
    assert set(by_key) == {
        ("agy", AGY_MODEL_ID),
        ("copilot", "gpt-5.4"),
        ("claude", "sonnet"),
        ("codex", "gpt-5.3-codex-spark"),
        ("cg", "glm-5.2"),
    }
    agy = by_key[("agy", AGY_MODEL_ID)]
    assert agy.independence_domain == "google"
    assert agy.live_probe == "agy-plan-sandbox"
    assert set(agy.capabilities) == {"planning", "review"}
    assert by_key[("copilot", "gpt-5.4")].capabilities == ("build",)
    assert by_key[("cg", "glm-5.2")].independence_domain == "zhipu"
    # roster 全體為候選宣告：無任何實測封套欄位（#453 R4 資料層一眼可辨）。
    for identity in registry.identities:
        assert identity.measured_envelope_fields() == ()
        assert identity.profile_provenance is None


def test_roster_negative_agy_planning_binding_fail_closed() -> None:
    with pytest.raises(ValueError, match="agy planning requires google"):
        IdentityRegistry.from_rows(
            [
                {
                    "executor": "agy",
                    "model_id": AGY_MODEL_ID,
                    "independence_domain": "openai",
                    "capabilities": ["planning"],
                    "live_probe": "agy-plan-sandbox",
                }
            ]
        )


def test_roster_negative_duplicate_identity_fail_closed() -> None:
    row = {
        "executor": "claude",
        "model_id": "sonnet",
        "independence_domain": "anthropic",
        "capabilities": ["review"],
    }
    with pytest.raises(ValueError, match="duplicate identity"):
        IdentityRegistry.from_rows([row, dict(row)])


# ---------------------------------------------------------------------------
# capability lookup seam（#453 R5 / #454 R5）
# ---------------------------------------------------------------------------


def test_capability_lookup_returns_none_for_default_and_answers_for_measured() -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
                "capabilities": ["build"],
            },
            _measured_row(),
        ]
    )
    # 全 default 身分 → None（#453 R5：維持 envelope_unavailable bypass 字節）。
    lookup = build_capability_lookup(registry, persona="builder", sizing_band="green")
    assert lookup("copilot/gpt-5.4") is None
    # 未註冊／解析不出 → None。
    assert lookup("unknown/x") is None
    assert lookup("garbage") is None
    # 實測身分真答：band 在窗內 True、窗外 False，且被排除原因可觀測。
    observations: list = []
    lookup_green = build_capability_lookup(
        registry, persona="builder", sizing_band="green", observations=observations
    )
    assert lookup_green("claude/sonnet") is True
    lookup_red = build_capability_lookup(
        registry, persona="builder", sizing_band="red", observations=observations
    )
    assert lookup_red("claude/sonnet") is False
    failed = observations[-1].failed_criteria()
    assert [item.name for item in failed] == ["sizing_band"]
    assert "red" in failed[0].detail
    # 六項全評估、不短路（#209 R1）。
    assert [item.name for item in observations[-1].criteria] == [
        "sizing_band",
        "invariant_ceiling",
        "consistency_scope",
        "acceptance_modes",
        "capabilities",
        "track_record",
    ]
