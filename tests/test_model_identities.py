from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.model_identities import (
    AGY_LIVE_PROBE,
    AGY_MODEL_ID,
    STDOUT_EXCERPT_LIMIT,
    CapabilityProbe,
    IdentityRegistry,
    load_model_identities,
    probe_agy_capability,
    select_secondary_planner,
    stdout_excerpt,
    strip_code_fence,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return type(
        "Completed",
        (),
        {"returncode": returncode, "stdout": stdout, "stderr": stderr},
    )()


def test_packaged_v3_registry_has_google_agy_default() -> None:
    registry = load_model_identities()

    agy = registry.require("agy", AGY_MODEL_ID)
    assert registry.schema_version == 3
    assert agy.independence_domain == "google"
    # #799：agy 的 writable launcher 與 registry build capability 必須成對。
    assert agy.capabilities == ("planning", "build", "review")
    assert agy.live_probe == "agy-plan-sandbox"


def test_packaged_default_is_composed_with_existing_v1_primary_identities(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 1
identities:
  - executor: codex
    model_id: gpt-primary
    independence_domain: openai
""",
        encoding="utf-8",
    )

    registry = load_model_identities(tmp_path, use_packaged_default=True)

    assert registry.schema_version == 3
    assert registry.require("agy", AGY_MODEL_ID).independence_domain == "google"
    assert registry.require("codex", "gpt-primary").independence_domain == "openai"


def test_packaged_default_puts_custom_identities_before_packaged_defaults(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 2
identities:
  - executor: claude
    model_id: planner
    independence_domain: anthropic
    capabilities: [planning, review]
  - executor: copilot
    model_id: build
    independence_domain: github
    capabilities: [build]
""",
        encoding="utf-8",
    )

    registry = load_model_identities(tmp_path, use_packaged_default=True)

    assert [
        (identity.executor, identity.model_id)
        for identity in registry.identities
    ] == [
        ("claude", "planner"),
        ("copilot", "build"),
        ("agy", AGY_MODEL_ID),
        ("copilot", "gpt-5.4"),
        ("claude", "sonnet"),
        ("codex", "gpt-5.3-codex-spark"),
        ("cg", "glm-5.2"),
    ]


def test_packaged_default_without_custom_file_returns_packaged_only(tmp_path: Path) -> None:
    registry = load_model_identities(tmp_path, use_packaged_default=True)

    # #452 B／#456 R3：packaged roster 登錄 5 個候選身分，agy 維持首位
    # （PLANNER_PRIORITY 熱路徑選擇不變）。
    assert [
        (identity.executor, identity.model_id)
        for identity in registry.identities
    ] == [
        ("agy", AGY_MODEL_ID),
        ("copilot", "gpt-5.4"),
        ("claude", "sonnet"),
        ("codex", "gpt-5.3-codex-spark"),
        ("cg", "glm-5.2"),
    ]


def test_v2_registry_is_strict_and_rejects_unknown_or_duplicate_rows(tmp_path: Path) -> None:
    path = tmp_path / "model-identities.yaml"
    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
    unexpected: no
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected"):
        load_model_identities(tmp_path)

    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate identity"):
        load_model_identities(tmp_path)

    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="agy-plan-sandbox"):
        load_model_identities(tmp_path)

    path.write_text("schema_version: true\nidentities: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_model_identities(tmp_path)


def test_foreign_review_uses_v2_registry_without_leaking_planner_metadata(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import review

    (tmp_path / "model-identities.yaml").write_text(
        f"""\
schema_version: 2
identities:
  - executor: agy
    model_id: {AGY_MODEL_ID}
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
""",
        encoding="utf-8",
    )

    mapping = review.load_model_identity_registry(tmp_path)

    # legacy 投影只帶三欄，不外洩 capabilities／live_probe 等 planner metadata。
    assert mapping[("agy", AGY_MODEL_ID)] == {
        "executor": "agy",
        "model_id": AGY_MODEL_ID,
        "independence_domain": "google",
    }
    # #490：foreign review 改用與 manager／tick 同一份合併 registry，packaged
    # 身分不必再被複製進 host overlay 才解析得到。
    assert ("claude", "sonnet") in mapping
    assert all(set(row) == {"executor", "model_id", "independence_domain"} for row in mapping.values())


def test_agy_probe_requires_model_listing_and_safe_headless_smoke() -> None:
    calls: list[dict] = []

    def runner(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if argv == ["agy", "models"]:
            return _completed(stdout=f"gemini-3.1-pro-low\n{AGY_MODEL_ID}\n")
        return _completed(
            stdout=f'{{"capability":"cortex-plan-sandbox","model":"{AGY_MODEL_ID}"}}\n'
        )

    probe = probe_agy_capability(runner=runner, timeout_seconds=11)

    assert probe.ready is True
    assert probe.identity == ("agy", AGY_MODEL_ID, "google")
    assert calls[1]["argv"][:2] == ["agy", "--print"]
    assert calls[1]["argv"][calls[1]["argv"].index("--mode") + 1] == "plan"
    assert "--sandbox" in calls[1]["argv"]
    assert "--dangerously-skip-permissions" not in calls[1]["argv"]
    assert calls[1]["shell"] is False
    assert calls[1]["timeout"] == 11


@pytest.mark.parametrize(
    ("model_stdout", "smoke_result", "reason"),
    [
        ("Gemini 3.1 Pro (Low)\n", _completed(), "model-not-listed"),
        (
            f"{AGY_MODEL_ID}\n",
            _completed(returncode=2, stderr="unsupported flag"),
            "smoke-failed",
        ),
        (f"{AGY_MODEL_ID}\n", _completed(stdout="not-json"), "malformed-output"),
        (
            f"{AGY_MODEL_ID}\n",
            _completed(stdout='{"capability":"wrong","model":"Gemini 3.1 Pro (High)"}'),
            "identity-mismatch",
        ),
    ],
)
def test_agy_probe_fails_closed_on_drift(model_stdout, smoke_result, reason) -> None:
    responses = iter([_completed(stdout=model_stdout), smoke_result])
    probe = probe_agy_capability(runner=lambda *args, **kwargs: next(responses))

    assert probe.ready is False
    assert probe.reason == reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 裸輸入：原樣（只 strip）回傳。
        ('{"a":1}', '{"a":1}'),
        ('  {"a":1}\n', '{"a":1}'),
        # ```json 前綴（#670 實測到的真實形態）。
        ('```json\n{"a":1}\n```', '{"a":1}'),
        # 裸 ``` 前綴。
        ('```\n{"a":1}\n```', '{"a":1}'),
        # 尾隨空白／換行，以及 fence 前後的空白。
        ('```json\n{"a":1}\n```\n\n  ', '{"a":1}'),
        ('\n  ```JSON\n{"a":1}\n```  \n', '{"a":1}'),
        # 沒有尾隨 fence（輸出被截斷）也要能取出本體。
        ('```json\n{"a":1}', '{"a":1}'),
        # 單行 fence：info string 有無皆可，且不得把本體當成 info string 吃掉。
        ('```json {"a":1}```', '{"a":1}'),
        ('```{"a":1}```', '{"a":1}'),
        # CRLF。
        ('```json\r\n{"a":1}\r\n```', '{"a":1}'),
        # 空字串。
        ("", ""),
        # 帶前言散文的 fence **不**處理：維持與 planning_runtime 頂層一致的
        # 嚴格「整串才算」語意，交給呼叫端 json.loads 失敗。
        ('Here you go:\n```json\n{"a":1}\n```', 'Here you go:\n```json\n{"a":1}\n```'),
    ],
)
def test_strip_code_fence_handles_fenced_and_bare_inputs(raw: str, expected: str) -> None:
    assert strip_code_fence(raw) == expected


def test_agy_probe_accepts_fenced_json_from_the_model() -> None:
    """#670：模型把正確 JSON 包進 code fence 時，probe 必須照樣 ready。"""
    fenced = f'```json\n{{"capability":"cortex-plan-sandbox","model":"{AGY_MODEL_ID}"}}\n```\n'
    responses = iter([_completed(stdout=f"{AGY_MODEL_ID}\n"), _completed(stdout=fenced)])

    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))

    assert probe.ready is True
    assert probe.reason is None


def test_agy_probe_accepts_bare_fence_without_language_tag() -> None:
    fenced = f'```\n{{"capability":"cortex-plan-sandbox","model":"{AGY_MODEL_ID}"}}\n```'
    responses = iter([_completed(stdout=f"{AGY_MODEL_ID}\n"), _completed(stdout=fenced)])

    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))

    assert probe.ready is True


def test_agy_probe_still_reports_identity_mismatch_for_fenced_wrong_content() -> None:
    """剝 fence 不得順手吞掉「內容真的不對」——這是 #670 最容易改壞的一格。

    fence 剝法只處理結構；本體 JSON 合法但欄位不符時，reason 必須仍是
    `identity-mismatch`（拓撲／身分問題），不能因為「有 fence」就被誤救成
    ready，也不能退化成 `malformed-output`。
    """
    fenced = '```json\n{"capability":"wrong","model":"some-other-model"}\n```'
    responses = iter([_completed(stdout=f"{AGY_MODEL_ID}\n"), _completed(stdout=fenced)])

    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))

    assert probe.ready is False
    assert probe.reason == "identity-mismatch"
    assert probe.diagnostic is not None
    assert "wrong" in probe.diagnostic


def test_agy_probe_malformed_output_diagnostic_carries_stdout_excerpt() -> None:
    """#670：`malformed-output` 過去 diagnostic 為 None，現場零線索。"""
    responses = iter(
        [
            _completed(stdout=f"{AGY_MODEL_ID}\n"),
            _completed(stdout="I cannot comply with that request.\nPlease clarify.\n"),
        ]
    )

    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))

    assert probe.ready is False
    assert probe.reason == "malformed-output"
    assert probe.diagnostic is not None
    assert "I cannot comply with that request." in probe.diagnostic
    # 多行 stdout 壓成單行，不污染 log。
    assert "\n" not in probe.diagnostic


def test_agy_probe_malformed_output_diagnostic_is_bounded() -> None:
    responses = iter(
        [_completed(stdout=f"{AGY_MODEL_ID}\n"), _completed(stdout="x" * 4096)]
    )

    probe = probe_agy_capability(runner=lambda *a, **k: next(responses))

    assert probe.reason == "malformed-output"
    assert probe.diagnostic is not None
    assert len(probe.diagnostic) <= STDOUT_EXCERPT_LIMIT + 1


def test_stdout_excerpt_collapses_whitespace_and_marks_empty() -> None:
    assert stdout_excerpt("  a\n\n b\t c  ") == "a b c"
    assert stdout_excerpt("") == "<empty>"
    assert stdout_excerpt("   \n  ") == "<empty>"
    assert stdout_excerpt("y" * 300, limit=10) == "y" * 10 + "…"


def test_agy_probe_accepts_two_column_models_listing() -> None:
    """#670 實測附帶發現：`agy models` 已改成 `id\\tDisplay Name` 兩欄輸出。

    整行比對時字面與正規化都不中，probe 100% 死在 `model-not-listed`——比
    fence 偽失敗更早、更絕對。要能認出 id 欄，且傳給 `--model` 的必須是
    kebab id，不是顯示名。
    """
    listing = (
        "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
        f"{AGY_MODEL_ID}\tGemini 3.1 Pro (High)\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    )
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(stdout=listing)
        return _completed(
            stdout=f'{{"capability":"cortex-plan-sandbox","model":"{AGY_MODEL_ID}"}}'
        )

    probe = probe_agy_capability(runner=runner)

    assert probe.ready is True
    assert calls[1][calls[1].index("--model") + 1] == AGY_MODEL_ID


def test_agy_probe_two_column_listing_prefers_id_column_over_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兩欄輸出且只有顯示名語意相符時，仍必須挑 id 欄，不能回傳顯示名。"""
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.model_identities.AGY_MODEL_ID", "Gemini 3.1 Pro (High)"
    )
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(stdout="gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n")
        return _completed(
            stdout='{"capability":"cortex-plan-sandbox","model":"Gemini 3.1 Pro (High)"}'
        )

    probe = probe_agy_capability(runner=runner)

    assert probe.ready is True
    assert calls[1][calls[1].index("--model") + 1] == "gemini-3.1-pro-high"


def test_agy_probe_prompt_forbids_code_fences_at_the_source() -> None:
    """保底之外，prompt 本身也要顯式禁止 fence（#670 源頭壓制）。"""
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(stdout=f"{AGY_MODEL_ID}\n")
        return _completed(
            stdout=f'{{"capability":"cortex-plan-sandbox","model":"{AGY_MODEL_ID}"}}'
        )

    probe = probe_agy_capability(runner=runner)

    assert probe.ready is True
    prompt = calls[1][calls[1].index("--print") + 1]
    assert "no code fences" in prompt
    # #670 實測：`--json-schema` 在 `--output-format text`（build_agy_argv 現行
    # 形態）下被 CLI 直接拒絕（rc=1），因此 argv 保持不變。
    assert "--json-schema" not in calls[1]
    assert "--output-format" not in calls[1]


def test_secondary_selection_uses_priority_and_excludes_primary_domain() -> None:
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
                "model_id": "claude-sonnet-4.6",
                "independence_domain": "anthropic",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        ]
    )
    probes = {
        ("agy", AGY_MODEL_ID): CapabilityProbe.ready_for("agy", AGY_MODEL_ID, "google"),
        ("claude", "claude-sonnet-4.6"): CapabilityProbe.ready_for(
            "claude", "claude-sonnet-4.6", "anthropic"
        ),
        ("codex", "gpt-5.4"): CapabilityProbe.ready_for("codex", "gpt-5.4", "openai"),
    }

    selected = select_secondary_planner(
        registry=registry,
        primary=("codex", "gpt-5.4"),
        probes=probes,
    )
    assert selected.state == "ready"
    assert selected.identity and selected.identity.executor == "agy"

    selected = select_secondary_planner(
        registry=registry,
        primary=("agy", AGY_MODEL_ID),
        probes=probes,
    )
    assert selected.identity and selected.identity.executor == "claude"

    probes[("agy", AGY_MODEL_ID)] = CapabilityProbe(
        False,
        "agy",
        AGY_MODEL_ID,
        "google",
        "smoke-failed",
    )
    selected = select_secondary_planner(
        registry=registry,
        primary=("codex", "gpt-5.4"),
        probes=probes,
    )
    assert selected.identity and selected.identity.executor == "claude"


def test_secondary_selection_fails_closed_for_unknown_or_same_domain_only() -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "secondary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        ]
    )
    probes = {
        ("codex", "secondary"): CapabilityProbe.ready_for("codex", "secondary", "openai")
    }

    unknown = select_secondary_planner(
        registry=registry,
        primary=("codex", "unknown"),
        probes=probes,
    )
    assert (unknown.state, unknown.reason) == ("needs_human", "primary-identity-unknown")

    same = select_secondary_planner(
        registry=registry,
        primary=("codex", "primary"),
        probes=probes,
    )
    assert (same.state, same.reason) == ("needs_human", "no-heterogeneous-planner")


def test_agy_model_id_matches_agy_cli_kebab_id() -> None:
    """`agy models` 現在輸出 kebab id，而不是顯示名（issue #255 根因）。

    這條測試釘住 canonical 值本身，避免未來又悄悄退回顯示名或其他跟
    `agy models` 實際輸出脫節的拼法。
    """
    assert AGY_MODEL_ID == "gemini-3.1-pro-high"
    assert AGY_MODEL_ID.islower()
    assert " " not in AGY_MODEL_ID


def test_agy_probe_tolerates_display_name_vs_kebab_id_drift(monkeypatch) -> None:
    """即使 `agy models` 的輸出格式再跟 canonical 常數脫節一次，probe 也不該
    一字不差比對失敗——這條測試刻意跟目前的 AGY_MODEL_ID 拼法無關，用
    monkeypatch 固定一個 canonical 值，驗證「正規化後語意相同即可」這條規則
    本身，而不是巧合碰上兩者剛好相等。

    容錯比對用正規化（大小寫／標點）判斷語意相同的那一行，並且用
    `agy models` 實際印出的字面值去呼叫 `--model`，而不是硬塞 canonical
    id（CLI 不一定認得 canonical 拼法）。
    """
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.model_identities.AGY_MODEL_ID", "sample-model-x"
    )
    calls: list[dict] = []
    drifted_line = "Sample Model X"  # 與 canonical 值語意相同、格式不同

    def runner(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if argv == ["agy", "models"]:
            return _completed(stdout=f"other-model\n{drifted_line}\n")
        return _completed(
            stdout='{"capability":"cortex-plan-sandbox","model":"sample-model-x"}\n'
        )

    probe = probe_agy_capability(runner=runner)

    assert probe.ready is True
    assert probe.identity == ("agy", "sample-model-x", "google")
    model_index = calls[1]["argv"].index("--model") + 1
    assert calls[1]["argv"][model_index] == drifted_line


def test_agy_probe_model_not_listed_diagnostic_surfaces_available_models() -> None:
    """失敗時 diagnostic 要帶出實際清單，不能只留下一個空的 reason 讓人猜。"""
    probe = probe_agy_capability(
        runner=lambda *a, **k: _completed(
            stdout="gemini-3.6-flash-high\ngemini-3.6-flash-medium\n"
        )
    )

    assert probe.ready is False
    assert probe.reason == "model-not-listed"
    assert probe.diagnostic is not None
    assert AGY_MODEL_ID in probe.diagnostic
    assert "gemini-3.6-flash-high" in probe.diagnostic


def test_v1_legacy_display_name_still_promotes_to_canonical_agy_planning_identity(
    tmp_path: Path,
) -> None:
    """既有 v1 設定檔若還寫著舊顯示名，也要繼續被視為 canonical agy 身分。"""
    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 1
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
""",
        encoding="utf-8",
    )

    registry = load_model_identities(tmp_path, use_packaged_default=False)

    legacy = registry.require("agy", "Gemini 3.1 Pro (High)")
    assert legacy.capabilities == ("planning",)
    assert legacy.live_probe == AGY_LIVE_PROBE


def test_v1_identity_is_a_probe_bound_planning_fallback(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 1
identities:
  - executor: codex
    model_id: primary
    independence_domain: openai
  - executor: claude
    model_id: legacy-secondary
    independence_domain: anthropic
""",
        encoding="utf-8",
    )
    registry = load_model_identities(tmp_path)

    unavailable = select_secondary_planner(registry=registry, primary=("codex", "primary"), probes={})
    assert unavailable.reason == "no-heterogeneous-planner"

    selected = select_secondary_planner(
        registry=registry,
        primary=("codex", "primary"),
        probes={
            ("claude", "legacy-secondary"): CapabilityProbe.ready_for(
                "claude", "legacy-secondary", "anthropic"
            )
        },
    )
    assert selected.identity and selected.identity.executor == "claude"
