"""#534：模型引擎三層解析鏈（operator overlay → 評估合格清單 → packaged 候選池）。

現場（issue #534）：packaged roster 的註解明載「列序即候選優先序：agy 維持首位，
保住既有 planner 熱路徑選擇不變」，於是 planner 解析到 `agy/gemini-3.1-pro-high`
——而 operator 當日在 host overlay 宣告的可用引擎清單**根本不含**它。人工指定被
內建列序壓過，未評估的候選宣告佔住 planner 熱路徑。

本檔同時收編三個相鄰現場：
- #509：overlay shadow packaged 直接 `raise ValueError` 打掛 periodic tick；
  doctor 卻回報 PASS。
- #490：retry-review 只載 host overlay，packaged 身分被判 unknown。
- #475：operator 自訂的 Claude-compatible 身分不得被 packaged 同 executor 身分
  靜默取代（executable 綁定屬 launcher 層，本票不做）。

測試 fixture 一律比照 operator 現行部署：agy 的 capabilities 為 `[build]`
（#568 暫時下架 review），**不假設 agy 有 review**。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, model_resolution, workflow
from paulsha_cortex.coordinator.model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    load_model_identities,
    select_secondary_planner,
)

# operator 於 2026-08-14 實際部署的 overlay（#534／#509 的現場），逐列照抄：
# build：copilot/MAI-Code-1.1-Flash、codex/gpt-5.6-luna、agy（#568 後只剩 build）
# planning/review：claude/claude-opus-5
_OPERATOR_OVERLAY = """\
schema_version: 3
identities:
  - executor: claude
    model_id: claude-opus-5
    independence_domain: anthropic
    capabilities: [planning, review]
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
  - executor: copilot
    model_id: MAI-Code-1.1-Flash
    independence_domain: microsoft
    capabilities: [build]
  - executor: agy
    model_id: gemini-3.6-flash-high
    independence_domain: google
    capabilities: [build]
"""


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def _run(**kwargs):
    defaults = {
        "run_id": "run-534",
        "steps": (),
        "primary_domain": None,
        "sizing_band": None,
        "model_chain_override": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _step(persona: str):
    return SimpleNamespace(persona=persona, phase="build", card="card")


# ---------------------------------------------------------------------------
# 第 1 層：overlay 絕對優先（#534 主訴）
# ---------------------------------------------------------------------------


def test_operator_overlay_outranks_packaged_roster_for_every_persona(tmp_path: Path) -> None:
    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    registry = load_model_identities(tmp_path)

    for persona, expected in (
        ("planner", ("claude", "claude-opus-5")),
        ("builder", ("codex", "gpt-5.6-luna")),
        ("reviewer", ("claude", "claude-opus-5")),
    ):
        identity = manager._select_workflow_identity(_run(), _step(persona), registry)
        assert (identity.executor, identity.model_id) == expected, persona

    # 修正前的實際行為：packaged 的 agy 佔住 planner 首位。這條釘住「不得回頭」。
    planner = manager._select_workflow_identity(_run(), _step("planner"), registry)
    assert (planner.executor, planner.model_id) != ("agy", AGY_MODEL_ID)


def test_packaged_candidates_are_kept_as_lower_priority_fallback(tmp_path: Path) -> None:
    """降級為候選池≠剔除：#262 的 preflight re-route 仍需要次佳候選。"""

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    registry = load_model_identities(tmp_path)

    candidates = manager._workflow_identity_candidates(_run(), _step("builder"), registry)
    keys = [(item.executor, item.model_id) for item in candidates]
    assert keys[0] == ("codex", "gpt-5.6-luna")
    assert ("copilot", "gpt-5.4") in keys  # packaged 候選仍在，但排在 overlay 之後
    overlay_keys = {("codex", "gpt-5.6-luna"), ("copilot", "MAI-Code-1.1-Flash"), ("agy", "gemini-3.6-flash-high")}
    last_overlay = max(keys.index(key) for key in overlay_keys if key in keys)
    assert keys.index(("copilot", "gpt-5.4")) > last_overlay


def test_primary_domain_preference_no_longer_outranks_operator_overlay(tmp_path: Path) -> None:
    """#452 的 primary_domain 偏好降級為**同層內**的次要偏好。"""

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    registry = load_model_identities(tmp_path)

    # primary_domain=anthropic 會把 packaged 的 claude/sonnet 排到 packaged 層之首，
    # 但整個 packaged 層仍在 overlay 層之後。
    candidates = manager._workflow_identity_candidates(
        _run(primary_domain="anthropic"), _step("builder"), registry
    )
    keys = [(item.executor, item.model_id) for item in candidates]
    assert keys[0] == ("codex", "gpt-5.6-luna")
    assert keys.index(("codex", "gpt-5.6-luna")) < keys.index(("claude", "sonnet"))


def test_secondary_planner_is_not_pinned_to_agy_anymore(tmp_path: Path) -> None:
    """`PLANNER_PRIORITY` 寫死 agy 首位、且只認三組 (executor, domain)。

    overlay 宣告的 planner 若不在那三組裡（例如 cg），舊實作**永遠不可達**。
    """

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: cg
    model_id: glm-5.3
    independence_domain: zhipu
    capabilities: [planning]
  - executor: claude
    model_id: claude-opus-5
    independence_domain: anthropic
    capabilities: [planning]
""",
    )
    registry = load_model_identities(tmp_path)
    probes = {
        ("cg", "glm-5.3"): CapabilityProbe.ready_for("cg", "glm-5.3", "zhipu"),
        ("claude", "claude-opus-5"): CapabilityProbe.ready_for(
            "claude", "claude-opus-5", "anthropic"
        ),
        ("agy", AGY_MODEL_ID): CapabilityProbe.ready_for("agy", AGY_MODEL_ID, "google"),
    }

    selection = select_secondary_planner(
        registry=registry, primary=("codex", "gpt-5.3-codex-spark"), probes=probes
    )

    assert selection.state == "ready"
    assert selection.identity is not None
    assert (selection.identity.executor, selection.identity.model_id) == ("cg", "glm-5.3")


def test_packaged_only_deployment_keeps_packaged_roster_order(tmp_path: Path) -> None:
    """沒有 overlay 的部署＝operator 未宣告任何東西：packaged 順序照舊，
    fallback 政策為 allow（不吵），但 provenance 仍如實標記第 3 層。"""

    registry = load_model_identities(tmp_path)

    assert registry.resolution_context.policy.packaged_fallback == "allow"
    planner = manager._select_workflow_identity(_run(), _step("planner"), registry)
    assert (planner.executor, planner.model_id) == ("agy", AGY_MODEL_ID)
    assert (
        manager._resolution_layer_for(planner, "planner", registry)
        == model_resolution.RESOLUTION_LAYER_PACKAGED
    )


# ---------------------------------------------------------------------------
# 第 2 層：patchmud 評估合格清單（model-eval-roster.yaml）
# ---------------------------------------------------------------------------

_EVAL_ROSTER = """\
schema_version: 1
entries:
  - executor: claude
    model_id: sonnet
    roles: [build, review]
    verdict: pass
    evaluated_at: "2026-08-14"
    eval_source: patchmud
    eval_ref: patchmud-deck-v1/report-2026-08-14
    review_status: approved
    reviewer: operator
    reviewed_at: "2026-08-14"
  - executor: cg
    model_id: glm-5.2
    roles: [planning, review]
    verdict: pass
    evaluated_at: "2026-08-13"
    eval_source: patchmud
    review_status: pending
"""


def test_evaluated_roster_ranks_between_overlay_and_packaged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )
    _write(tmp_path, "model-eval-roster.yaml", _EVAL_ROSTER)
    registry = load_model_identities(tmp_path)

    candidates = manager._workflow_identity_candidates(_run(), _step("builder"), registry)
    keys = [(item.executor, item.model_id) for item in candidates]

    # 1. overlay；2. 評估合格且人工複核通過的 packaged claude/sonnet；3. 其餘 packaged。
    assert keys[0] == ("codex", "gpt-5.6-luna")
    assert keys[1] == ("claude", "sonnet")
    assert keys.index(("claude", "sonnet")) < keys.index(("copilot", "gpt-5.4"))
    assert (
        manager._resolution_layer_for(candidates[1], "builder", registry)
        == model_resolution.RESOLUTION_LAYER_EVALUATED
    )


def test_evaluation_without_human_review_does_not_qualify(tmp_path: Path) -> None:
    """裁決第 3 條的閘門：評估過≠人工核可，`review_status: pending` 不入第 2 層。"""

    _write(tmp_path, "model-eval-roster.yaml", _EVAL_ROSTER)
    registry = load_model_identities(tmp_path)
    roster = registry.resolution_context.eval_roster

    assert roster.approves("claude", "sonnet", "build") is True
    assert roster.approves("cg", "glm-5.2", "planning") is False
    assert roster.approves("claude", "sonnet", "planning") is False  # 角色不符
    assert [entry.key for entry in roster.pending_entries()] == [("cg", "glm-5.2")]


def test_eval_roster_requires_auditable_human_review_marker() -> None:
    with pytest.raises(ValueError, match="requires reviewer and reviewed_at"):
        model_resolution.parse_eval_roster(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "executor": "claude",
                        "model_id": "sonnet",
                        "roles": ["build"],
                        "verdict": "pass",
                        "evaluated_at": "2026-08-14",
                        "eval_source": "patchmud",
                        "review_status": "approved",
                    }
                ],
            }
        )


def test_eval_roster_is_strict_and_fail_closed() -> None:
    base = {
        "executor": "claude",
        "model_id": "sonnet",
        "roles": ["build"],
        "verdict": "pass",
        "evaluated_at": "2026-08-14",
        "eval_source": "patchmud",
        "review_status": "approved",
        "reviewer": "operator",
        "reviewed_at": "2026-08-14",
    }
    for mutation, match in (
        ({"unexpected": "boom"}, "unexpected"),
        ({"verdict": "maybe"}, "verdict invalid"),
        ({"review_status": "ok"}, "review_status invalid"),
        ({"roles": ["deploy"]}, "roles invalid"),
    ):
        with pytest.raises(ValueError, match=match):
            model_resolution.parse_eval_roster(
                {"schema_version": 1, "entries": [{**base, **mutation}]}
            )
    with pytest.raises(ValueError, match="schema_version"):
        model_resolution.parse_eval_roster({"schema_version": 2, "entries": []})
    with pytest.raises(ValueError, match="duplicate entry"):
        model_resolution.parse_eval_roster(
            {"schema_version": 1, "entries": [dict(base), dict(base)]}
        )


def test_broken_eval_roster_degrades_instead_of_killing_the_tick(tmp_path: Path) -> None:
    """#509 的教訓：設定資料問題不得讓整條調度迴圈停止。

    壞掉的清單只會讓第 2 層變空（保守方向），並留下 fail 級診斷給 doctor。
    """

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    _write(tmp_path, "model-eval-roster.yaml", "schema_version: 1\nentries:\n  - boom\n")

    registry = load_model_identities(tmp_path)  # 不得丟例外

    roster = registry.resolution_context.eval_roster
    assert roster.entries == ()
    assert roster.load_error is not None
    assert any(
        note.code == "eval-roster-unreadable" and note.severity == "fail"
        for note in registry.resolution_context.notes
    )
    # 解析照常運作，operator 的人工指定不受影響。
    identity = manager._select_workflow_identity(_run(), _step("builder"), registry)
    assert (identity.executor, identity.model_id) == ("codex", "gpt-5.6-luna")


# ---------------------------------------------------------------------------
# #509：shadow 不再打掛 tick；overlay 可 demote／park packaged 身分
# ---------------------------------------------------------------------------


def test_overlay_shadowing_packaged_no_longer_aborts_the_load(tmp_path: Path) -> None:
    """#509 現場：`claude/sonnet` 逐欄不等即 raise → tick 連續失敗 → 斷路器開。"""

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: claude
    model_id: sonnet
    independence_domain: anthropic
    capabilities: [review]
""",
    )

    registry = load_model_identities(tmp_path)  # 修正前這行 raise ValueError

    identity = registry.require("claude", "sonnet")
    assert identity.capabilities == ("review",)  # 以 overlay 為準（人工指定優先）
    assert identity.origin == model_resolution.IDENTITY_ORIGIN_OVERLAY
    assert [note.code for note in registry.resolution_context.notes] == [
        "unflagged-packaged-override"
    ]
    # 同鍵只登錄一次，不會出現 overlay／packaged 兩份。
    assert sum(
        1
        for item in registry.identities
        if (item.executor, item.model_id) == ("claude", "sonnet")
    ) == 1


def test_explicit_override_packaged_flag_is_the_sanctioned_syntax(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: claude
    model_id: sonnet
    independence_domain: anthropic
    capabilities: [review]
    override_packaged: true
""",
    )

    registry = load_model_identities(tmp_path)

    assert [note.code for note in registry.resolution_context.notes] == ["packaged-override"]
    assert all(note.severity == "info" for note in registry.resolution_context.notes)


def test_overlay_can_park_and_demote_packaged_identities(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        _OPERATOR_OVERLAY
        + """\
packaged_overrides:
  - executor: agy
    model_id: gemini-3.1-pro-high
    action: park
    reason: operator 未核可此引擎
  - executor: copilot
    model_id: gpt-5.4
    action: demote
    reason: 尚未評估，僅供最後手段
""",
    )
    registry = load_model_identities(tmp_path)

    # park：身分仍在 registry（doctor 的 canonical 檢查不破），但不進候選。
    assert registry.get("agy", AGY_MODEL_ID) is not None
    planner_keys = [
        (item.executor, item.model_id)
        for item in manager._workflow_identity_candidates(_run(), _step("planner"), registry)
    ]
    assert ("agy", AGY_MODEL_ID) not in planner_keys

    # demote：留在候選池但沉到同層最後。
    builder_keys = [
        (item.executor, item.model_id)
        for item in manager._workflow_identity_candidates(_run(), _step("builder"), registry)
    ]
    assert builder_keys[-1] == ("copilot", "gpt-5.4")


def test_packaged_overrides_are_fail_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        _OPERATOR_OVERLAY
        + """\
packaged_overrides:
  - executor: agy
    model_id: does-not-exist
    action: park
    reason: typo
""",
    )
    with pytest.raises(ValueError, match="不存在的 packaged 身分"):
        load_model_identities(tmp_path)

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: claude
    model_id: sonnet
    independence_domain: anthropic
    capabilities: [review]
    override_packaged: true
packaged_overrides:
  - executor: claude
    model_id: sonnet
    action: park
    reason: 與 identities 矛盾
""",
    )
    with pytest.raises(ValueError, match="同時宣告同一身分"):
        load_model_identities(tmp_path)


# ---------------------------------------------------------------------------
# 第 3 層政策：fail-loud／fail-closed
# ---------------------------------------------------------------------------


def test_packaged_fallback_warns_when_resolution_falls_through(tmp_path: Path, caplog) -> None:
    import logging

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )
    registry = load_model_identities(tmp_path)
    assert registry.resolution_context.policy.packaged_fallback == "warn"

    with caplog.at_level(logging.WARNING, logger="paulsha_cortex.coordinator.manager"):
        identity = manager._select_workflow_identity(_run(), _step("planner"), registry)

    assert (identity.executor, identity.model_id) == ("agy", AGY_MODEL_ID)
    assert "packaged-fallback" in caplog.text
    assert (
        manager._resolution_layer_for(identity, "planner", registry)
        == model_resolution.RESOLUTION_LAYER_PACKAGED
    )


def test_deny_policy_fails_closed_with_actionable_message(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
resolution_policy:
  packaged_fallback: deny
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )
    registry = load_model_identities(tmp_path)

    # build 有 overlay 身分 → 照常解析。
    identity = manager._select_workflow_identity(_run(), _step("builder"), registry)
    assert (identity.executor, identity.model_id) == ("codex", "gpt-5.6-luna")

    # planning 只剩 packaged 候選 → 不得靜默使用未核可模型。
    with pytest.raises(ValueError) as excinfo:
        manager._select_workflow_identity(_run(), _step("planner"), registry)
    message = str(excinfo.value)
    assert "no resolvable identity" in message
    assert "model-eval-roster.yaml" in message


def test_resolution_policy_value_is_validated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
resolution_policy:
  packaged_fallback: sometimes
identities: []
""",
    )
    with pytest.raises(ValueError, match="packaged_fallback invalid"):
        load_model_identities(tmp_path)


# ---------------------------------------------------------------------------
# provenance：resolved_model_chain 記錄解析層
# ---------------------------------------------------------------------------


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _manager_update_workflow_run(self, run_id, **kwargs):
        self.calls.append((run_id, kwargs))
        return SimpleNamespace(run_id=run_id, **kwargs)


def test_resolved_model_chain_records_the_resolution_layer(tmp_path: Path) -> None:
    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    _write(tmp_path, "model-eval-roster.yaml", _EVAL_ROSTER)
    identities = load_model_identities(tmp_path)
    jobs = _RecordingRegistry()
    run = _run()

    for identity, expected in (
        (identities.require("codex", "gpt-5.6-luna"), "operator-overlay"),
        (identities.require("claude", "sonnet"), "evaluated-roster"),
        (identities.require("copilot", "gpt-5.4"), "packaged-fallback"),
    ):
        manager._record_resolved_model_chain(
            jobs, run, _step("builder"), identity, identities
        )
        row = jobs.calls[-1][1]["resolved_model_chain"]["builder"]
        assert row["source"] == expected
        assert row["envelope_source"] in {"measured", "default"}
        # workflow 契約必須接受新值域（durable evidence 寫得進去）。
        workflow._validate_model_chain_resolution(
            jobs.calls[-1][1]["resolved_model_chain"], field_name="resolved_model_chain"
        )


def test_legacy_resolved_model_chain_rows_still_load() -> None:
    """#534 之前寫下的 run 紀錄（無 envelope_source、legacy source）必須照舊可載入。"""

    for source in ("override", "registry", "patchmud-profile", "default-envelope"):
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
    with pytest.raises(ValueError, match="envelope_source 非法"):
        workflow._validate_model_chain_resolution(
            {
                "builder": {
                    "executor": "claude",
                    "model_id": "sonnet",
                    "independence_domain": "anthropic",
                    "source": "operator-overlay",
                    "envelope_source": "guessed",
                }
            },
            field_name="resolved_model_chain",
        )


# ---------------------------------------------------------------------------
# #490：retry-review 與 manager／tick 解析同一份 registry
# ---------------------------------------------------------------------------


def test_retry_review_resolves_packaged_identity_without_overlay_duplicate(
    tmp_path: Path,
) -> None:
    """#490 復現：overlay 只宣告 builder，reviewer 用 packaged claude/sonnet。

    修正前 `load_model_identity_registry` 走 `use_packaged_default=False`，
    retry-review 記下 `reviewer-identity-unknown`；operator 只能把 packaged 那列
    複製進 overlay，複製回來又踩 #509 的 shadow 中止——死結。
    """

    from paulsha_cortex.coordinator import review

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )

    registry = review.load_model_identity_registry(tmp_path)
    assert ("claude", "sonnet") in registry

    decision = review.select_foreign_reviewer(
        registry=registry,
        builder_executor="codex",
        builder_model_id="gpt-5.6-luna",
        review_executor="claude",
        review_model_id="sonnet",
        tier="shareable",
    )

    assert decision["reason"] != "reviewer-identity-unknown"
    assert decision["state"] == "ready"
    assert decision["reviewer"]["model_id"] == "sonnet"


def test_review_and_manager_see_the_same_identity_set(tmp_path: Path) -> None:
    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    from paulsha_cortex.coordinator import review

    review_keys = set(review.load_model_identity_registry(tmp_path))
    manager_keys = {
        (item.executor, item.model_id) for item in load_model_identities(tmp_path).identities
    }

    assert review_keys == manager_keys


# ---------------------------------------------------------------------------
# #475：operator 自訂身分不得被 packaged 同 executor 身分靜默取代
# ---------------------------------------------------------------------------


def test_operator_declared_claude_compatible_identity_wins_its_slot(tmp_path: Path) -> None:
    """#475 現場的解析層切片。

    operator 用 Claude Code 介面但把 API 導向自架 Gemma4，於 overlay 宣告
    `claude/gemma4-26b-a4b-nvfp4`。packaged roster 同 executor 有 `claude/sonnet`
    ——若 packaged 內建列序仍能壓過人工指定，job record 的 model_id 就會與真實
    provider 不一致（#475 的「靜默跑錯模型」）。本測試釘住：解析結果就是
    operator 宣告的那顆，且 provenance 明示來自第 1 層。
    （executable／launcher 綁定屬 launcher 層，仍為 #475 的未竟部分。）
    """

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: claude
    model_id: gemma4-26b-a4b-nvfp4
    independence_domain: self-hosted-gemma
    capabilities: [build]
""",
    )
    identities = load_model_identities(tmp_path)

    identity = manager._select_workflow_identity(_run(), _step("builder"), identities)

    assert (identity.executor, identity.model_id) == ("claude", "gemma4-26b-a4b-nvfp4")
    assert identity.independence_domain == "self-hosted-gemma"
    assert (
        manager._resolution_layer_for(identity, "builder", identities)
        == model_resolution.RESOLUTION_LAYER_OVERLAY
    )


# ---------------------------------------------------------------------------
# 向後相容：既有 overlay 檔案不得因升級而失效
# ---------------------------------------------------------------------------


def test_existing_overlay_files_stay_valid_without_any_new_field(tmp_path: Path) -> None:
    """新能力全為選配欄位：v1／v2／v3 既有檔案照載、照解析。"""

    for text in (
        "schema_version: 1\nidentities:\n  - executor: codex\n    model_id: gpt-primary\n"
        "    independence_domain: openai\n",
        "schema_version: 2\nidentities:\n  - executor: copilot\n    model_id: build-one\n"
        "    independence_domain: microsoft\n    capabilities: [build]\n",
        "schema_version: 3\nidentities:\n  - executor: codex\n    model_id: gpt-5.6-luna\n"
        "    independence_domain: openai\n    capabilities: [build]\n",
    ):
        _write(tmp_path, "model-identities.yaml", text)
        registry = load_model_identities(tmp_path)
        assert registry.resolution_context.packaged_overrides == ()
        assert registry.resolution_context.eval_roster.entries == ()
        assert manager._workflow_identity_candidates(_run(), _step("builder"), registry)


def test_hand_built_registries_keep_pre_534_ordering() -> None:
    """程式內組裝的 registry（測試替身、其他呼叫端）沒有 loader 蓋章：
    一律視為呼叫端自行宣告的第 1 層，順序與 #534 之前逐項相同。"""

    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
            {
                "executor": "claude",
                "model_id": "planner-two",
                "independence_domain": "anthropic",
                "capabilities": ["planning"],
            },
        ]
    )

    ranked = model_resolution.rank_candidates(
        list(registry.identities), role="planning", context=registry.resolution_context
    )

    assert [(item.executor, item.model_id) for item in ranked.ordered] == [
        ("agy", AGY_MODEL_ID),
        ("claude", "planner-two"),
    ]
    assert ranked.warnings == ()


# ---------------------------------------------------------------------------
# doctor：診斷 overlay 宣告與生效解析不一致（#509 的假 PASS）
# ---------------------------------------------------------------------------


def test_doctor_resolution_probe_reports_layer_and_config_root(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _model_resolution_probe

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    result = _model_resolution_probe({"PSC_PROJECT_CONFIG_ROOT": str(tmp_path)}, tmp_path)

    assert result.status == "pass"
    assert "planner=claude/claude-opus-5[operator-overlay]" in result.detail
    # #509：doctor 必須說出自己讀的是哪個 config root，否則與 daemon 讀不同檔案
    # 也看不出來。
    assert str(tmp_path) in result.detail


def test_doctor_resolution_probe_flags_broken_eval_roster(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _model_resolution_probe

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    _write(tmp_path, "model-eval-roster.yaml", "schema_version: 9\nentries: []\n")

    result = _model_resolution_probe({"PSC_PROJECT_CONFIG_ROOT": str(tmp_path)}, tmp_path)

    assert result.status == "fail"
    assert "eval-roster-unreadable" in result.detail


def test_doctor_resolution_probe_warns_on_packaged_hot_path(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _model_resolution_probe

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )
    result = _model_resolution_probe({"PSC_PROJECT_CONFIG_ROOT": str(tmp_path)}, tmp_path)

    assert result.status == "warn"
    assert "planner" in result.detail
    assert result.required is False


def test_doctor_resolution_probe_fails_when_a_persona_has_no_candidate(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _model_resolution_probe

    _write(
        tmp_path,
        "model-identities.yaml",
        """\
schema_version: 3
resolution_policy:
  packaged_fallback: deny
identities:
  - executor: codex
    model_id: gpt-5.6-luna
    independence_domain: openai
    capabilities: [build]
""",
    )
    result = _model_resolution_probe({"PSC_PROJECT_CONFIG_ROOT": str(tmp_path)}, tmp_path)

    assert result.status == "fail"
    assert "planner" in result.detail and "reviewer" in result.detail
    assert result.required is True


def test_inspect_models_rows_expose_the_resolution_layer(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator.model_profile import envelope_display_rows

    _write(tmp_path, "model-identities.yaml", _OPERATOR_OVERLAY)
    _write(tmp_path, "model-eval-roster.yaml", _EVAL_ROSTER)
    rows = envelope_display_rows(load_model_identities(tmp_path))

    layers = {
        (row["executor"], row["model_id"], row["persona"]): row["resolution_layer"]
        for row in rows
    }
    assert layers[("claude", "claude-opus-5", "planner")] == "operator-overlay"
    assert layers[("claude", "sonnet", "builder")] == "evaluated-roster"
    assert layers[("cg", "glm-5.2", "planner")] == "packaged-fallback"
