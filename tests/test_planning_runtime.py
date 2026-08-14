from __future__ import annotations

import json
import os
from pathlib import Path

from paulsha_cortex.coordinator import planning_runtime
import pytest

from paulsha_cortex.coordinator.model_identities import AGY_MODEL_ID, IdentityRegistry, ModelIdentity


def _completed(stdout: str = "", returncode: int = 0):
    return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()


def test_production_runtime_loads_registry_and_probes_only_safe_launchers(
    monkeypatch, tmp_path: Path
) -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    calls: list[list[str]] = []
    invocation_cwds: list[Path] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        if "cwd" in kwargs:
            invocation_cwds.append(Path(kwargs["cwd"]))
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        marker = "Return only this compact JSON object and perform no tool calls: "
        if marker in prompt:
            return _completed(prompt.split(marker, 1)[1] + "\n")
        marker = "Return only this JSON object and do not call tools: "
        if marker in prompt:
            return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(json.dumps({"unexpected": True}))

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )

    assert runtime.identity_registry is registry
    assert runtime.probes[("agy", AGY_MODEL_ID)].ready is True
    assert runtime.probes[("codex", "primary")].ready is True
    assert all("--dangerously-bypass-approvals-and-sandbox" not in argv for argv in calls)
    codex_calls = [argv for argv in calls if argv and argv[0] == "codex"]
    assert codex_calls and all(
        argv[argv.index("--sandbox") + 1] == "read-only" for argv in codex_calls
    )
    assert all("--skip-git-repo-check" in argv for argv in codex_calls)
    agy_calls = [
        argv for argv in calls if argv and argv[0] == "agy" and argv != ["agy", "models"]
    ]
    assert agy_calls and all("--sandbox" in argv and "--mode" in argv for argv in agy_calls)
    assert invocation_cwds and all(path != tmp_path for path in invocation_cwds)
    assert all("cortex-planning-" in str(path) for path in invocation_cwds)

    claude_argv = planning_runtime._planning_argv(
        ModelIdentity("claude", "claude-plan", "anthropic", ("planning",)),
        "prompt",
        str(tmp_path / "runtime-output"),
        tmp_path,
    )
    # issue #404：plan 模式的系統提示與確定性回聲任務衝突，安全層改由
    # no-tools＋disposable sandbox＋樹快照＋hermetic 配置共同承擔。
    assert "--permission-mode" not in claude_argv
    assert claude_argv[claude_argv.index("--tools") + 1] == ""


def test_secondary_prompt_embeds_bounded_repo_sources_without_tool_access(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "openspec" / "changes" / "demo" / "proposal.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nstatus: draft\n---\n## Why\nEvidence.\n", encoding="utf-8")
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    prompts: list[str] = []

    def runner(argv, **kwargs):
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        prompts.append(prompt)
        for marker in (
            "Return only this compact JSON object and perform no tool calls: ",
            "Return only this JSON object and do not call tools: ",
        ):
            if marker in prompt:
                return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(
            json.dumps(
                {
                    "schema_version": 1,
                    "question_pack_id": "qp-demo",
                    "evidence": [
                        {
                            "question_id": "q-demo",
                            "claims": ["The proposal records the intended evidence."],
                            "source_refs": ["openspec/changes/demo/proposal.md"],
                        }
                    ],
                }
            )
        )

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )
    result = runtime.secondary_planner(
        {
            "schema_version": 1,
            "pack_id": "qp-demo",
            "questions": [
                {
                    "question_id": "q-demo",
                    "kind": "missing-spec",
                    "prompt": "What is required?",
                    "source_refs": ["openspec/changes/demo/proposal.md"],
                }
            ],
        },
        registry.require("agy", AGY_MODEL_ID),
    )

    assert result["question_pack_id"] == "qp-demo"
    assert "Do not call tools" in prompts[-1]
    assert "Evidence." in prompts[-1]
    # issue #401：secondary planner 的 prompt 也必須強制純 JSON 輸出契約，
    # 降低模型回散文推理夾雜 JSON、觸發 `_extract_json` fail-closed 的機率。
    assert (
        "Output contract: reply with exactly one JSON object and nothing else"
        in prompts[-1]
    )
    assert planning_runtime._planning_destinations(
        {
            "questions": [
                {"source_refs": ["openspec/changes/demo/proposal.md"]}
            ]
        }
    )["plan"] == "docs/superpowers/plans/demo.md"


def test_planning_destinations_derives_slug_from_workstream_todo_anchor() -> None:
    """issue #408：small-fix 等無 openspec-propose 卡的 combo，work item 錨點是
    workstream todo——destinations 必須能從
    docs/superpowers/workstreams/<slug>/todo.md 推導，否則 integrator 拿到空
    destinations、發明路徑、必被 governed-roots 驗證拒收。"""
    destinations = planning_runtime._planning_destinations(
        {
            "questions": [
                {"source_refs": ["docs/superpowers/workstreams/fix-demo-v2/todo.md"]},
                {"source_refs": ["docs/superpowers/workstreams/fix-demo-v2/todo.md"]},
            ]
        }
    )
    assert destinations == {
        "spec": "docs/superpowers/specs/fix-demo-v2-spec.md",
        "design": "docs/superpowers/specs/fix-demo-v2-design.md",
        "plan": "docs/superpowers/plans/fix-demo-v2.md",
    }
    # openspec 錨點優先：兩種 ref 並存時仍以 openspec slug 為準。
    assert planning_runtime._planning_destinations(
        {
            "questions": [
                {"source_refs": [
                    "openspec/changes/demo/proposal.md",
                    "docs/superpowers/workstreams/other/todo.md",
                ]}
            ]
        }
    )["plan"] == "docs/superpowers/plans/demo.md"
    # 歧義（多個 workstream slug）維持 fail-closed 空 dict。
    assert planning_runtime._planning_destinations(
        {
            "questions": [
                {"source_refs": ["docs/superpowers/workstreams/a/todo.md"]},
                {"source_refs": ["docs/superpowers/workstreams/b/todo.md"]},
            ]
        }
    ) == {}


def test_planning_argv_claude_branch_omits_permission_mode(tmp_path: Path) -> None:
    """issue #404：plan 模式的系統提示（「必須產出計畫或呼叫
    ExitPlanMode」）與這裡「必須回傳純 JSON」的確定性回聲任務衝突——issue
    404 的實測矩陣顯示模型會以此為由拒絕直接回 JSON。安全層改由
    no-tools（`--tools ""`）＋`_invoke_json` 的一次性 disposable
    sandbox＋operator 樹快照比對＋hermetic `CLAUDE_CONFIG_DIR` 共同承擔，
    不再依賴 plan 模式。"""
    argv = planning_runtime._planning_argv(
        ModelIdentity("claude", "claude-plan", "anthropic", ("planning",)),
        "prompt",
        str(tmp_path / "runtime-output"),
        tmp_path,
    )
    assert "--permission-mode" not in argv
    assert "plan" not in argv
    assert argv[argv.index("--tools") + 1] == ""


def test_planning_json_parser_accepts_only_whole_fenced_object(tmp_path: Path) -> None:
    output = tmp_path / "missing.json"
    assert planning_runtime._extract_json(
        '```json\n{"schema_version": 1}\n```\n', output
    ) == {"schema_version": 1}
    with pytest.raises(ValueError, match="no JSON object"):
        planning_runtime._extract_json(
            'Commentary.\n```json\n{"schema_version": 1}\n```\n', output
        )


def _cli_envelope(result: str) -> dict:
    """建構一個近似 claude CLI 成功 envelope 的 dict：`result` 才是模型輸出，
    其餘 20+ 鍵（`api_error_status` 等）是 launcher 包裝，不該被下游驗證
    誤當成輸出本體的欄位。"""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4321,
        "duration_api_ms": 4000,
        "num_turns": 1,
        "session_id": "session-401",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "api_error_status": None,
        "result": result,
    }


def test_extract_json_pulls_embedded_object_out_of_envelope_prose(tmp_path: Path) -> None:
    """issue #401：模型（實測 sonnet 對 questioner prompt）有時不遵守「只回
    JSON」指示，在 CLI envelope 的 `result` 欄位回散文推理夾雜 JSON。修復後
    必須從散文中抽出內嵌 JSON 物件，而不是整個 envelope 一起回傳、也不是
    直接放棄。"""
    envelope = _cli_envelope(
        "Let me think through this step by step.\n\n"
        'The question pack is {"schema_version": 1, "question_pack_id": "qp-1"} '
        "which resolves the completeness report.\n\nDone."
    )
    output = tmp_path / "missing.json"
    result = planning_runtime._extract_json(json.dumps(envelope), output)
    assert result == {"schema_version": 1, "question_pack_id": "qp-1"}


def test_extract_json_raises_on_pure_prose_result_without_leaking_envelope(
    tmp_path: Path,
) -> None:
    """issue #401 的核心迴歸：`result` 是純散文（不含任何 JSON 物件）時，
    修復前的程式碼會 fall through 把整個 envelope dict（含
    `api_error_status` 等 20+ 鍵）當成輸出本體回傳，讓下游
    `validate_question_pack` 報出 `unexpected key: api_error_status` 這種
    完全誤導的診斷。修復後必須 raise ValueError，訊息帶散文片段方便除錯，
    且絕不能包含 envelope 自身的鍵名。"""
    prose = "I believe the answer is forty-two, but I will not format it as JSON here."
    envelope = _cli_envelope(prose)
    output = tmp_path / "missing.json"
    with pytest.raises(ValueError) as excinfo:
        planning_runtime._extract_json(json.dumps(envelope), output)
    message = str(excinfo.value)
    assert "forty-two" in message
    assert "api_error_status" not in message


def test_extract_json_envelope_result_pure_json_string_is_unchanged(tmp_path: Path) -> None:
    """既有行為不變：`result` 本身就是純 JSON 字串（模型有遵守指示）時，
    照樣直接抽出巢狀物件。"""
    envelope = _cli_envelope(json.dumps({"schema_version": 1, "question_pack_id": "qp-2"}))
    output = tmp_path / "missing.json"
    result = planning_runtime._extract_json(json.dumps(envelope), output)
    assert result == {"schema_version": 1, "question_pack_id": "qp-2"}


def test_extract_json_top_level_output_without_envelope_keys_is_unchanged(
    tmp_path: Path,
) -> None:
    """既有行為不變：頂層 candidate 本身就是輸出 JSON（不含 result/content/
    message/text 任一鍵，非 envelope 形），維持現行為直接回傳。"""
    output = tmp_path / "missing.json"
    payload = {"schema_version": 1, "question_pack_id": "qp-3"}
    result = planning_runtime._extract_json(json.dumps(payload), output)
    assert result == payload


def test_questioner_and_integrator_prompts_include_json_output_contract(
    monkeypatch, tmp_path: Path
) -> None:
    """issue #401：questioner／integrator 的 prompt 過去只用「Return only
    ... JSON」這類軟性措辭，未強制「純 JSON、不得夾雜散文」。兩處都必須附加
    明確的輸出契約字句。"""
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    prompts: list[str] = []

    def runner(argv, **kwargs):
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        prompts.append(prompt)
        for marker in (
            "Return only this compact JSON object and perform no tool calls: ",
            "Return only this JSON object and do not call tools: ",
        ):
            if marker in prompt:
                return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(json.dumps({"schema_version": 1, "question_pack_id": "qp-x"}))

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )

    runtime.primary_questioner({"missing": ["x"]})
    runtime.primary_integrator(
        {"schema_version": 1, "question_pack_id": "qp-x", "questions": []},
        {"schema_version": 1, "question_pack_id": "qp-x", "evidence": []},
    )

    contract = "Output contract: reply with exactly one JSON object and nothing else"
    assert len(prompts) >= 2
    assert contract in prompts[-2]
    assert contract in prompts[-1]
    # issue #406：integrator prompt 必須把 validate_primary_integration 的
    # 結構約束講成語意（不只是欄位名），否則模型會把 artifact_refs 留空。
    integrator_prompt = prompts[-1]
    assert "Resolve every question exactly once" in integrator_prompt
    assert "without its 'missing-' prefix" in integrator_prompt
    assert "NON-EMPTY list of the destination path(s)" in integrator_prompt
    assert "must equal the union of all artifact_refs" in integrator_prompt


def test_integrator_prompt_states_echo_back_field_sources(monkeypatch, tmp_path: Path) -> None:
    """issue #516：`_validate_primary_integration()` 要求 `question_pack_id` 與
    `secondary_evidence_hash` 與輸入完全相符，兩個值都已在模型輸入裡（分別是
    `question_pack.pack_id` 與 `secondary_evidence.evidence_hash`），模型只需原樣
    複製。但輸入欄位名（`evidence_hash`）與輸出欄位名（`secondary_evidence_hash`）
    不同，後者字面上像是要模型自己算 hash——prompt 只列欄位名時模型必然猜錯，
    planning 反覆以 `primary integration evidence hash mismatch` 失敗。"""
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    prompts: list[str] = []

    def runner(argv, **kwargs):
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        prompts.append(prompt)
        return _completed(json.dumps({"schema_version": 1, "question_pack_id": "qp-x"}))

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )
    runtime.primary_integrator(
        {"schema_version": 1, "pack_id": "qp-x", "questions": []},
        {
            "schema_version": 1,
            "question_pack_id": "qp-x",
            "evidence": [],
            "evidence_hash": "deadbeef",
        },
    )

    integrator_prompt = prompts[-1]
    # 兩個 echo-back 欄位都必須指名值的來源（輸入的哪個欄位），而非只列欄位名。
    assert "copied verbatim from the input question_pack.pack_id" in integrator_prompt
    assert (
        "copied verbatim from the input secondary_evidence.evidence_hash" in integrator_prompt
    )
    # 明確禁止模型自行計算 hash——這正是 #516 的誤解來源。
    assert "do not compute, derive, or invent a hash" in integrator_prompt


def test_planning_source_material_rejects_symlink_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "linked.md"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        planning_runtime._planning_source_material(
            {"questions": [{"source_refs": ["linked.md"]}]}, root=tmp_path
        )


def test_planning_runtime_rejects_any_worktree_mutation(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    baseline = tmp_path / "tracked.md"
    baseline.write_text("original\n", encoding="utf-8")

    def runner(argv, **kwargs):
        baseline.write_text("mutated\n", encoding="utf-8")
        (tmp_path / "unexpected.md").write_text("leak\n", encoding="utf-8")
        return _completed("failure\n", returncode=9)

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )
    assert baseline.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "unexpected.md").exists()


def test_planning_runtime_checks_disposable_sandbox_even_on_nonzero(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    baseline = tmp_path / "tracked.md"
    baseline.write_text("operator\n", encoding="utf-8")

    def runner(argv, **kwargs):
        (Path(kwargs["cwd"]) / "leak.md").write_text("sandbox mutation\n", encoding="utf-8")
        return _completed("failed\n", returncode=3)

    with pytest.raises(ValueError, match="disposable read-only sandbox"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )
    assert baseline.read_text(encoding="utf-8") == "operator\n"
    assert not (tmp_path / "leak.md").exists()


def test_planning_runtime_detects_and_rolls_back_directory_and_metadata_pollution(
    tmp_path: Path,
) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.md"
    tracked.write_text("operator\n", encoding="utf-8")
    tracked.chmod(0o640)
    empty = tmp_path / "empty"
    empty.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    directory_link = tmp_path / "dir-link"
    directory_link.symlink_to("target", target_is_directory=True)

    def runner(argv, **kwargs):
        tracked.chmod(0o600)
        empty.rmdir()
        (tmp_path / "pollution-empty").mkdir()
        directory_link.unlink()
        directory_link.symlink_to("empty", target_is_directory=True)
        return _completed(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )

    assert tracked.stat().st_mode & 0o777 == 0o640
    assert empty.is_dir()
    assert not (tmp_path / "pollution-empty").exists()
    assert directory_link.is_symlink()
    assert os.readlink(directory_link) == "target"


def test_tree_snapshot_covers_empty_directories_directory_links_and_modes(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    baseline_mode = empty.lstat().st_mode & 0o7777
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "dir-link"
    link.symlink_to("target", target_is_directory=True)
    baseline = planning_runtime._tree_snapshot(tmp_path)

    empty.rmdir()
    assert planning_runtime._tree_snapshot(tmp_path) != baseline
    empty.mkdir()
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    empty.chmod(0o700)
    assert planning_runtime._tree_snapshot(tmp_path) != baseline
    empty.chmod(baseline_mode)
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    link.unlink()
    link.symlink_to("empty", target_is_directory=True)
    assert planning_runtime._tree_snapshot(tmp_path) != baseline


def test_snapshot_permission_error_still_restores_operator_tree(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    protected = tmp_path / "protected"
    protected.mkdir()
    tracked = protected / "tracked.md"
    tracked.write_text("baseline\n", encoding="utf-8")
    xattr_supported = True
    try:
        os.setxattr(tracked, "user.cortex-test", b"baseline")
    except (AttributeError, OSError):
        xattr_supported = False
    protected.chmod(0o750)

    def runner(argv, **kwargs):
        tracked.write_text("polluted\n", encoding="utf-8")
        if xattr_supported:
            os.setxattr(tracked, "user.cortex-test", b"polluted")
        protected.chmod(0)
        return _completed(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity, "return JSON", worktree=tmp_path, runner=runner,
            timeout_seconds=30,
        )

    assert protected.stat().st_mode & 0o777 == 0o750
    assert tracked.read_text(encoding="utf-8") == "baseline\n"
    if xattr_supported:
        assert os.getxattr(tracked, "user.cortex-test") == b"baseline"


def test_tree_snapshot_ignores_pycache_directories_and_pyc_files(tmp_path: Path) -> None:
    """Issue #397：本機部署讓 daemon 與 planning launcher 共用同一棵工作樹，
    daemon 對既有模組的 lazy import 會隨時重編 `__pycache__/*.pyc`。這些是
    可由原始碼重生的 bytecode 快取，不是 operator 內容變更，不該被算進
    mismatch 判定——否則共享工作樹拓撲下每次 planning 呼叫都有機率被
    daemon 的正常 churn 誤判成「planner 汙染 operator worktree」。
    """
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    baseline = planning_runtime._tree_snapshot(tmp_path)

    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    pyc = pycache / "tracked.cpython-312.pyc"
    pyc.write_bytes(b"\x00\x01fake-bytecode-v1")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    # daemon 重新編譯，bytecode 內容整個換掉（模擬 timestamp 命中後的 rewrite）
    pyc.write_bytes(b"\xff\xfe totally different recompiled bytecode")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    # 散落在既有目錄下、不在 __pycache__ 資料夾內的 .pyc 同樣視為 bytecode
    stray = tmp_path / "stray.pyc"
    stray.write_bytes(b"stray-bytecode")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    # 巢狀套件目錄底下的 __pycache__ 也要能被忽略（非只有 root 層級）
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")
    baseline_with_pkg = planning_runtime._tree_snapshot(tmp_path)
    nested_pycache = nested / "__pycache__"
    nested_pycache.mkdir()
    (nested_pycache / "mod.cpython-312.pyc").write_bytes(b"nested-bytecode")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline_with_pkg


def test_tree_snapshot_still_detects_non_pycache_file_mutations(tmp_path: Path) -> None:
    """忽略 __pycache__／.pyc 不得放寬既有 fail-closed：任何其他新增或修改
    的檔案仍必須讓快照改變，才能繼續攔下真正的 operator worktree 污染。"""
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    baseline = planning_runtime._tree_snapshot(tmp_path)

    (tmp_path / "evil.txt").write_text("polluted\n", encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) != baseline


def test_copy_planning_sandbox_excludes_pycache(tmp_path: Path) -> None:
    """sandbox 是要丟棄的一次性複本，bytecode 可由原始碼重生、不必複製；
    同時避免 copytree 期間 daemon 正在改寫／刪除 .pyc 造成 race read 例外
    （複製到一半 .pyc 消失導致 shutil.copytree 炸掉，而這與 planner 汙染
    無關，不該讓整段流程失敗）。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    pycache = worktree / "__pycache__"
    pycache.mkdir()
    (pycache / "tracked.cpython-312.pyc").write_bytes(b"bytecode")
    nested = worktree / "pkg"
    nested.mkdir()
    (nested / "mod.py").write_text("y = 2\n", encoding="utf-8")
    nested_pycache = nested / "__pycache__"
    nested_pycache.mkdir()
    (nested_pycache / "mod.cpython-312.pyc").write_bytes(b"nested-bytecode")
    (nested / "compiled.pyc").write_bytes(b"stray-in-pkg")

    destination = tmp_path / "sandbox"
    planning_runtime._copy_planning_sandbox(worktree, destination)

    assert (destination / "tracked.py").is_file()
    assert not (destination / "__pycache__").exists()
    assert (destination / "pkg" / "mod.py").is_file()
    assert not (destination / "pkg" / "__pycache__").exists()
    assert not (destination / "pkg" / "compiled.pyc").exists()


def test_planning_runtime_tolerates_shared_worktree_pycache_churn(tmp_path: Path) -> None:
    """Issue #397 的整合回歸：`_invoke_json` 在快照窗口內若只看到
    `__pycache__` churn（daemon 對共享工作樹既有模組的 lazy import 重編），
    不得 raise「planning launcher modified operator worktree」。修復前，
    這個測試會在 runner 回傳成功後才被 finally 區塊的 operator 快照比對
    炸掉，即使底層 launcher 呼叫本身完全正常。"""
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")

    def runner(argv, **kwargs):
        pycache = tmp_path / "__pycache__"
        pycache.mkdir(exist_ok=True)
        (pycache / "tracked.cpython-312.pyc").write_bytes(os.urandom(32))
        return _completed(json.dumps({"ok": True}))

    result = planning_runtime._invoke_json(
        identity,
        "return JSON",
        worktree=tmp_path,
        runner=runner,
        timeout_seconds=30,
    )
    assert result == {"ok": True}
    # 沒有觸發 mismatch，就不該跑 restore；churn 出來的 .pyc 原樣留在原地。
    assert (tmp_path / "__pycache__" / "tracked.cpython-312.pyc").exists()


def test_tree_snapshot_ignores_root_level_runtime_directory(tmp_path: Path) -> None:
    """Issue #399：本機部署常見拓撲是 manager daemon 以 repo 為
    `WorkingDirectory` 常駐，`.gitignore:8` 明列的 `/runtime/` 是它的狀態殘留
    （例如 `runtime/handoff/wf-*.json` 每個 periodic tick 都會被重寫、內容
    含時間戳必變）。這棵目錄不受版控、且 verification gate 是讀 git diff 來
    判斷候選檔案，gitignored 的內容本就不會進候選清單，跳過它的雜湊盲點
    可控；不跳過的話，快照窗口內只要撞上一次 tick 就會被 `_invoke_json`
    的 operator 前後比對誤判成「planner 汙染 operator worktree」。
    """
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    baseline = planning_runtime._tree_snapshot(tmp_path)

    runtime_dir = tmp_path / "runtime"
    handoff = runtime_dir / "handoff"
    handoff.mkdir(parents=True)
    handoff_file = handoff / "wf-1.json"
    handoff_file.write_text('{"tick": 1}\n', encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    # daemon 每 tick 都整個重寫檔案內容（含時間戳）
    handoff_file.write_text('{"tick": 2}\n', encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    # 新增另一個 handoff 檔案同樣不該觸發 mismatch
    (handoff / "wf-2.json").write_text('{"tick": 3}\n', encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) == baseline


def test_tree_snapshot_still_hashes_nested_runtime_lookalike_directory(
    tmp_path: Path,
) -> None:
    """跳過規則只能鎖定快照 root 直下的 `runtime/`；深層同名目錄
    （例如 `pkg/runtime/`）不是 daemon 狀態殘留，仍必須被雜湊，
    避免用比對 dir name 而非 relative path 的實作方式過度排除。"""
    nested_runtime = tmp_path / "pkg" / "runtime"
    nested_runtime.mkdir(parents=True)
    tracked = nested_runtime / "file.txt"
    tracked.write_text("original\n", encoding="utf-8")
    baseline = planning_runtime._tree_snapshot(tmp_path)

    tracked.write_text("mutated\n", encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) != baseline

    tracked.write_text("original\n", encoding="utf-8")
    (nested_runtime / "new-file.txt").write_text("added\n", encoding="utf-8")
    assert planning_runtime._tree_snapshot(tmp_path) != baseline


def test_copy_planning_sandbox_excludes_root_level_runtime_directory(
    tmp_path: Path,
) -> None:
    """sandbox 是拋棄式複本，daemon 的 `/runtime/` 狀態殘留不必複製；
    與 `_tree_snapshot` 保持同語意，只排除 worktree root 直下的
    `runtime/`，深層同名目錄仍應被複製。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    handoff = worktree / "runtime" / "handoff"
    handoff.mkdir(parents=True)
    (handoff / "wf-1.json").write_text('{"tick": 1}\n', encoding="utf-8")
    nested_runtime = worktree / "pkg" / "runtime"
    nested_runtime.mkdir(parents=True)
    (nested_runtime / "file.txt").write_text("keep-me\n", encoding="utf-8")

    destination = tmp_path / "sandbox"
    planning_runtime._copy_planning_sandbox(worktree, destination)

    assert (destination / "tracked.py").is_file()
    assert not (destination / "runtime").exists()
    assert (destination / "pkg" / "runtime" / "file.txt").is_file()


def test_planning_runtime_tolerates_shared_worktree_runtime_handoff_churn(
    tmp_path: Path,
) -> None:
    """Issue #399 的整合回歸：`_invoke_json` 在快照窗口內若只看到
    `runtime/handoff/wf-*.json` churn（manager daemon periodic tick 重寫，
    issue #373 的迴圈使其每 ~55 秒必然發生一次），不得 raise「planning
    launcher modified operator worktree」。修復前，這個測試會在 runner
    回傳成功後才被 finally 區塊的 operator 快照比對炸掉，即使底層
    launcher 呼叫本身完全正常。"""
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")

    def runner(argv, **kwargs):
        handoff = tmp_path / "runtime" / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        (handoff / "wf-1.json").write_text(
            json.dumps({"tick": os.urandom(4).hex()}), encoding="utf-8"
        )
        return _completed(json.dumps({"ok": True}))

    result = planning_runtime._invoke_json(
        identity,
        "return JSON",
        worktree=tmp_path,
        runner=runner,
        timeout_seconds=30,
    )
    assert result == {"ok": True}
    # 沒有觸發 mismatch，就不該跑 restore；churn 出來的 handoff 檔案原樣留在原地。
    assert (tmp_path / "runtime" / "handoff" / "wf-1.json").exists()


def test_operator_restore_fault_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.md"
    tracked.write_text("baseline\n", encoding="utf-8")
    real_restore = planning_runtime._restore_operator_tree

    def runner(argv, **kwargs):
        tracked.write_text("polluted\n", encoding="utf-8")
        return _completed(json.dumps({"ok": True}))

    def restore_then_fail(worktree, baseline):
        real_restore(worktree, baseline)
        raise OSError("restore fsync fault")

    monkeypatch.setattr(planning_runtime, "_restore_operator_tree", restore_then_fail)
    with pytest.raises(RuntimeError, match="restore failed"):
        planning_runtime._invoke_json(
            identity, "return JSON", worktree=tmp_path, runner=runner,
            timeout_seconds=30,
        )
    assert tracked.read_text(encoding="utf-8") == "baseline\n"


def test_invoke_json_seeds_hermetic_claude_config_dir_from_home_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #404：拿掉 `--permission-mode plan` 之後，claude 呼叫若不做
    任何額外隔離，會直接繼承 operator `~/.claude`（superpowers
    plugin、記憶 hooks、user 層 CLAUDE.md、user MCP servers 全部注入）。
    `_invoke_json` 對 claude 身分必須在本次呼叫專用的 tempdir 下建一個
    一次性 hermetic config 目錄，只播種登入用的 credentials，藉此同時
    隔離上述注入項，但不影響登入態。"""
    fake_home = tmp_path / "fake-home"
    credentials_dir = fake_home / ".claude"
    credentials_dir.mkdir(parents=True)
    credentials_file = credentials_dir / ".credentials.json"
    credentials_file.write_text('{"token": "fake-token"}', encoding="utf-8")
    credentials_file.chmod(0o600)
    monkeypatch.setenv("HOME", str(fake_home))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.md").write_text("operator\n", encoding="utf-8")

    identity = ModelIdentity("claude", "claude-plan", "anthropic", ("planning",))
    captured: dict[str, object] = {}

    def runner(argv, **kwargs):
        env = kwargs.get("env")
        assert env is not None, "claude 呼叫必須帶 env 覆寫"
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        captured["config_dir_exists"] = config_dir.is_dir()
        captured["config_dir_mode"] = config_dir.stat().st_mode & 0o777
        seeded = config_dir / ".credentials.json"
        captured["seeded_exists"] = seeded.is_file()
        captured["seeded_content"] = seeded.read_text(encoding="utf-8")
        captured["seeded_mode"] = seeded.stat().st_mode & 0o777
        captured["env_inherits_path"] = env.get("PATH") == os.environ.get("PATH")
        return _completed(json.dumps({"ok": True}))

    result = planning_runtime._invoke_json(
        identity, "return JSON", worktree=worktree, runner=runner, timeout_seconds=30,
    )

    assert result == {"ok": True}
    assert captured["config_dir_exists"] is True
    assert captured["config_dir_mode"] == 0o700
    assert captured["seeded_exists"] is True
    assert captured["seeded_content"] == '{"token": "fake-token"}'
    assert captured["seeded_mode"] == 0o600
    assert captured["env_inherits_path"] is True


def test_invoke_json_skips_config_dir_env_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #404：查無登入憑證時不代為猜測——維持不設
    `CLAUDE_CONFIG_DIR`，讓 claude CLI 依原生行為自行回報 not logged
    in；`--bare` 或空的 `CLAUDE_CONFIG_DIR` 都會弄丟登入態（issue 404
    實測矩陣已驗證不可用），因此缺檔時不得改用空目錄頂替。"""
    fake_home = tmp_path / "fake-home-empty"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.md").write_text("operator\n", encoding="utf-8")

    identity = ModelIdentity("claude", "claude-plan", "anthropic", ("planning",))
    captured: dict[str, object] = {}

    def runner(argv, **kwargs):
        captured["has_env"] = "env" in kwargs
        return _completed(json.dumps({"ok": True}))

    result = planning_runtime._invoke_json(
        identity, "return JSON", worktree=worktree, runner=runner, timeout_seconds=30,
    )

    assert result == {"ok": True}
    assert captured["has_env"] is False


def test_invoke_json_does_not_set_env_override_for_non_claude_executors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #404 的行為外溢防呆：hermetic `CLAUDE_CONFIG_DIR` 只給
    claude 身分；codex／agy 呼叫必須維持原本不帶 env 覆寫的行為，即使
    operator 帳號下確實存在 claude 登入憑證。"""
    fake_home = tmp_path / "fake-home"
    credentials_dir = fake_home / ".claude"
    credentials_dir.mkdir(parents=True)
    (credentials_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.md").write_text("operator\n", encoding="utf-8")

    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    captured: dict[str, object] = {}

    def runner(argv, **kwargs):
        captured["has_env"] = "env" in kwargs
        return _completed(json.dumps({"ok": True}))

    result = planning_runtime._invoke_json(
        identity, "return JSON", worktree=worktree, runner=runner, timeout_seconds=30,
    )

    assert result == {"ok": True}
    assert captured["has_env"] is False
