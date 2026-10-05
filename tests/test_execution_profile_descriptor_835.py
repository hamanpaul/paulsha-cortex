"""#835 refine 補齊：adapter descriptor 資料載入、Trust Root 接線、重啟與 legacy manifest。

- AC1：production 的 adapter／effort 能力來自 descriptor 資料檔（packaged 內建預設＋
  config root overlay），不是寫死在 Python 的表；新增原生 effort／model 預設只改
  descriptor，錯誤 descriptor fail-closed。
- AC3：Trust Root 相容性由 profile 硬條件在 manager 正式派工路徑判定，拒絕時持久化
  `execution-profile-blocked` needs_human；pin／同 domain reviewer／unknown role 同樣
  在公開派工入口被擋下，零 job、零 launch。
- AC5：legacy manifest（pre-#835 Manager 寫下的 registry，含 #581 unknown role）重啟
  後可讀、legacy 欄位位元組不變；凍結 chain 的 run 重啟後重派得到相同 binding。

legacy fixture `fixtures/execution_profile/legacy-registry-pre-835.json` 由 #835 合併前
的 main（a1292b462155f95e36a3aac9265685acb95c44ed）以 `JobRegistry` 實際寫出，不是以
現行程式碼剝欄位模擬。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import execution_adapters, launcher, manager
from paulsha_cortex.coordinator.launcher import LaunchHandle, SubprocessLauncher
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
from paulsha_cortex.coordinator.workflow import WorkflowStep


LEGACY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "execution_profile" / "legacy-registry-pre-835.json"
)
LEGACY_FROZEN_RUN = "workflow-ee02c5bc2838087cb4d7"
LEGACY_UNKNOWN_ROLE_RUN = "workflow-9f0fc4e6240f8f362c85"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _yaml(value: object, indent: int = 0) -> str:
    """最小 YAML emitter（只產生 `_yaml.safe_load` 子集可讀的形狀）。"""

    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            raise AssertionError("empty mappings are not expressible in the YAML subset")
        lines = []
        for key, item in value.items():
            if isinstance(item, dict):
                lines.append(f"{pad}{key}:")
                lines.append(_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    raise AssertionError(f"unsupported top-level YAML value: {value!r}")


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    raise AssertionError(f"unsupported YAML scalar: {value!r}")


def _packaged_entry(executor: str) -> dict[str, object]:
    """Packaged descriptor 條目的可修改副本（由真實資料檔讀出，不在測試內重打一份）。"""

    from paulsha_cortex._yaml import safe_load

    payload = safe_load(execution_adapters.packaged_catalog_path().read_text(encoding="utf-8"))
    return json.loads(json.dumps(payload["adapters"][executor]))


def _write_overlay(config_root: Path, adapters: dict[str, object], **top: object) -> Path:
    payload = {"schema_version": top.pop("schema_version", 1), **top, "adapters": adapters}
    path = config_root / execution_adapters.ADAPTER_CATALOG_FILENAME
    path.write_text(_yaml(payload) + "\n", encoding="utf-8")
    return path


def _identity(executor: str, model_id: str, *, domain: str = "provider-a", capabilities=("build",)):
    return SimpleNamespace(
        executor=executor,
        model_id=model_id,
        independence_domain=domain,
        capabilities=tuple(capabilities),
        profile_provenance=None,
        executable=None,
    )


@pytest.fixture
def config_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """隔離 config root、程式碼登記的 adapter 與 catalog 快取。"""

    root = tmp_path / "project-config"
    root.mkdir()
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(root))
    monkeypatch.setattr(execution_adapters, "_REGISTERED_ADAPTERS", {})
    execution_adapters.reload_adapter_catalog()
    yield root
    execution_adapters.reload_adapter_catalog()


# ---------------------------------------------------------------------------
# AC1：descriptor 載入
# ---------------------------------------------------------------------------


#: #835 合併時（寫死 `_ADAPTERS` 表）產生的 binding；descriptor 資料化之後同一組
#: identity／persona 必須逐位元組重現，否則既有 run 的 frozen binding、#842
#: qualification receipt 與 #844 stage-execution reuse key 都會在升級後漂移。
_PRE_DESCRIPTOR_BINDINGS = (
    ("copilot", "gpt-5.4", "builder", "4391f32d2c2760b5853968b31cd010a9f923d3dc5c635314c3085c036d9a9bfc"),
    ("claude", "sonnet", "reviewer", "c9f22fc96e849fb15f848478577c34bcdb5c9741a0640e8cb66f7e6b2eef8283"),
    ("codex", "gpt-6-luna", "builder", "671fa2caa1c1310b1080f7fe873ceccfa2b05d15f72c6e42e1ac56272110f906"),
    ("codex", "gpt-5.3-codex-spark", "planner", "fbbaed8d5d68db5e29b534a6c793da4a395778092315fcb66addbc6a773e693f"),
    ("codex", "gpt-unknown", "reviewer", "21ae9b09ddcacd8f3ee038fba44433e1e2106c36b6627b45b17c0de74fe89b64"),
    ("agy", "gemini-3.1-pro-high", "planner", "7dd0b30971d39d6ef28bfa1ebf87ecd877f273d37d8569dab20b061b35b60bfe"),
    ("cg", "glm-5.2", "reviewer", "2fc6efdf8b5eb1881b3bb49f3a5accb19d2c6684d18561648ea033276b5db33b"),
)


@pytest.mark.parametrize(("executor", "model_id", "persona", "digest"), _PRE_DESCRIPTOR_BINDINGS)
def test_packaged_descriptor_reproduces_pre_descriptor_bindings_byte_for_byte(
    config_root: Path, executor: str, model_id: str, persona: str, digest: str
) -> None:
    identity = _identity(
        executor, model_id, domain=f"d-{executor}", capabilities=("build", "review", "planning")
    )
    binding = execution_adapters.resolve_profile(identity, persona)

    encoded = json.dumps(binding.to_dict(), sort_keys=True).encode()
    assert hashlib.sha256(encoded).hexdigest() == digest
    adapter = execution_adapters.adapter_for(executor)
    assert adapter.descriptor_source == str(execution_adapters.packaged_catalog_path())


def test_packaged_descriptor_is_the_production_adapter_source(config_root: Path) -> None:
    """內建 adapter 能力由 packaged 資料檔提供，程式碼只提供受信任的 argv 實作。"""

    catalog = execution_adapters.adapter_catalog()
    assert set(catalog) == {"copilot", "claude", "codex", "agy", "cg"}
    for executor, adapter in catalog.items():
        entry = _packaged_entry(executor)
        assert adapter.protocol_id == entry["protocol_id"]
        assert adapter.protocol_version == entry["protocol_version"]
        assert adapter.runtime_version == entry["runtime_version"]
        assert adapter.usage_source == entry["usage_source"]
        assert adapter.quota_state == entry["quota_state"]
        effort = entry["effort"]
        assert adapter.efforts == (tuple(effort["values"]) if effort else ())
        assert adapter.default_effort == (effort["default"] if effort else None)
    # 模組內不再有寫死 adapter 能力的表。
    assert not hasattr(execution_adapters, "_ADAPTERS")


def test_descriptor_overlay_adds_native_effort_and_model_default_without_code_change(
    config_root: Path,
) -> None:
    copilot = _packaged_entry("copilot")
    copilot["effort"]["values"] = [*copilot["effort"]["values"], "fixture-native-v2"]
    copilot["effort"]["model_defaults"] = {"fictional-model": "fixture-native-v2"}
    codex = _packaged_entry("codex")
    codex["effort"]["model_defaults"]["gpt-fictional-9"] = "max"
    overlay = _write_overlay(config_root, {"copilot": copilot, "codex": codex})

    adapter = execution_adapters.adapter_for("copilot")
    assert adapter.descriptor_source == str(overlay)
    assert "fixture-native-v2" in adapter.efforts

    # resolver：新 model 的預設 effort 來自 descriptor；requested 仍不冒充。
    identity = _identity("copilot", "fictional-model")
    binding = execution_adapters.resolve_profile(identity, "builder")
    assert binding.resolved.conditions["effort"] == {"state": "known", "value": "fixture-native-v2"}
    assert binding.requested.conditions["effort"]["state"] == "unknown"

    # 真 argv builder 與 resolver 讀同一份 descriptor：argv 發出同一個原生 effort。
    argv = launcher.build_copilot_argv(
        prompt="P", slice_id="s", log_dir="/lg", model="fictional-model"
    )
    assert argv[argv.index("--effort") + 1] == "fixture-native-v2"
    # 既有 model 仍走 adapter 預設。
    argv_default = launcher.build_copilot_argv(prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4")
    assert argv_default[argv_default.index("--effort") + 1] == "xhigh"

    # 明示 effort：descriptor 宣告過就接受，未宣告就在 spawn 前拒絕。
    explicit = execution_adapters.resolve_profile(
        _identity("copilot", "gpt-5.4"), "builder", effort="fixture-native-v2"
    )
    assert explicit.resolved.conditions["effort"]["value"] == "fixture-native-v2"
    assert "fixture-native-v2" in launcher.build_copilot_argv(
        prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4", effort="fixture-native-v2"
    )
    with pytest.raises(ValueError, match="unsupported effort"):
        execution_adapters.resolve_profile(identity, "builder", effort="not-declared")
    with pytest.raises(ValueError, match="effort must be one of"):
        launcher.build_copilot_argv(
            prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4", effort="not-declared"
        )

    # production 綁定路徑（manager → make_launcher_profile → SubprocessLauncher）。
    run = SimpleNamespace(
        steps=[], model_chain_override=None, sizing_band=None, facets=()
    )
    step = SimpleNamespace(persona="builder", card="tdd-red")
    workflow_binding, bound = manager._bind_workflow_execution_profile(
        run,
        step,
        identity,
        SubprocessLauncher(executor="copilot", model="fictional-model").as_commit_required(),
    )
    assert workflow_binding.resolved.conditions["effort"]["value"] == "fixture-native-v2"
    assert bound.execution_profile_binding["resolved_key"] == workflow_binding.resolved_key

    # codex：新 model 的原生 effort 同樣只改 descriptor，resolver 與 argv 一致。
    codex_binding = execution_adapters.resolve_profile(_identity("codex", "gpt-fictional-9"), "builder")
    assert codex_binding.resolved.conditions["effort"] == {"state": "known", "value": "max"}
    codex_argv = launcher.build_codex_argv(
        prompt="P", slice_id="s", log_dir="/lg", model="gpt-fictional-9"
    )
    assert 'model_reasoning_effort="max"' in codex_argv

    # 移除 overlay：回到 packaged 內建預設（沒有任何 Python 狀態殘留）。
    overlay.unlink()
    assert execution_adapters.adapter_for("copilot").descriptor_source == str(
        execution_adapters.packaged_catalog_path()
    )
    reverted = execution_adapters.resolve_profile(identity, "builder")
    assert reverted.resolved.conditions["effort"] == {"state": "known", "value": "xhigh"}


def _mutations():
    def unknown_top(root):
        _write_overlay(root, {"copilot": _packaged_entry("copilot")}, extra_policy="x")

    def future_schema(root):
        _write_overlay(root, {"copilot": _packaged_entry("copilot")}, schema_version=2)

    def missing_schema(root):
        path = root / execution_adapters.ADAPTER_CATALOG_FILENAME
        path.write_text(_yaml({"adapters": {"copilot": _packaged_entry("copilot")}}) + "\n", encoding="utf-8")

    def unknown_adapter_key(root):
        entry = _packaged_entry("copilot")
        entry["argv"] = ["copilot", "--yolo"]
        _write_overlay(root, {"copilot": entry})

    def missing_field(root):
        entry = _packaged_entry("copilot")
        del entry["protocol_version"]
        _write_overlay(root, {"copilot": entry})

    def default_not_declared(root):
        entry = _packaged_entry("copilot")
        entry["effort"]["default"] = "ultra"
        _write_overlay(root, {"copilot": entry})

    def model_default_not_declared(root):
        entry = _packaged_entry("codex")
        entry["effort"]["model_defaults"]["gpt-6-luna"] = "ultra"
        _write_overlay(root, {"codex": entry})

    def duplicate_effort(root):
        entry = _packaged_entry("copilot")
        entry["effort"]["values"] = ["low", "low"]
        entry["effort"]["default"] = "low"
        _write_overlay(root, {"copilot": entry})

    def untrusted_adapter(root):
        _write_overlay(root, {"fixture-unregistered": _packaged_entry("copilot")})

    def effort_without_argv_channel(root):
        entry = _packaged_entry("claude")
        entry["effort"] = {"values": ["low", "high"], "default": "high"}
        _write_overlay(root, {"claude": entry})

    def invalid_quota_state(root):
        entry = _packaged_entry("copilot")
        entry["quota_state"] = "infinite"
        _write_overlay(root, {"copilot": entry})

    def non_string_version(root):
        entry = _packaged_entry("copilot")
        entry["protocol_version"] = 1
        _write_overlay(root, {"copilot": entry})

    return {
        "unknown-top-level-key": (unknown_top, "unknown"),
        "future-schema-version": (future_schema, "schema"),
        "missing-schema-version": (missing_schema, "schema"),
        "unknown-adapter-key": (unknown_adapter_key, "unknown"),
        "missing-field": (missing_field, "missing"),
        "default-not-declared": (default_not_declared, "default"),
        "model-default-not-declared": (model_default_not_declared, "model default"),
        "duplicate-effort": (duplicate_effort, "duplicate"),
        "adapter-without-trusted-code": (untrusted_adapter, "trusted"),
        "effort-without-argv-channel": (effort_without_argv_channel, "cannot express native effort"),
        "invalid-quota-state": (invalid_quota_state, "quota"),
        "non-string-version": (non_string_version, "protocol_version"),
    }


@pytest.mark.parametrize("case", sorted(_mutations()))
def test_invalid_descriptor_is_rejected_fail_closed(config_root: Path, case: str) -> None:
    write, reason = _mutations()[case]
    write(config_root)

    with pytest.raises(execution_adapters.ExecutionAdapterError, match=reason):
        execution_adapters.load_adapter_catalog(config_root)
    # production 路徑不得靜默退回 packaged 預設：descriptor 壞掉即拒絕解析 profile。
    with pytest.raises(execution_adapters.ExecutionAdapterError, match=reason):
        execution_adapters.resolve_profile(_identity("copilot", "gpt-5.4"), "builder")


def test_persisted_bindings_stay_loadable_when_descriptor_is_invalid(config_root: Path) -> None:
    """descriptor 壞掉只擋新的派工決策，不讓既有 registry／歷史 binding 讀不出來。"""

    binding = execution_adapters.resolve_profile(_identity("copilot", "gpt-5.4"), "builder")
    _write_overlay(config_root, {"fixture-unregistered": _packaged_entry("copilot")})
    assert execution_adapters.load_profile_binding(binding.to_dict()).resolved_key == binding.resolved_key


# ---------------------------------------------------------------------------
# AC3／AC5：manager 公開派工路徑的硬條件負例與持久化、legacy manifest、重啟
# ---------------------------------------------------------------------------


class _WorktreeCreator:
    def __init__(self, path: Path):
        self._path = path
        self.calls: list[str] = []

    def create(
        self,
        branch: str,
        base_sha: str | None = None,
        *,
        job_id: str | None = None,
        owner_identity: dict[str, str] | None = None,
        attempt_id: str | None = None,
    ) -> Path:
        self.calls.append(branch)
        return self._path


class _Launcher:
    """記錄 launch 的 fake launcher；只在測試路徑外層替代 process spawn。"""

    launches: list[tuple[str, str]] = []

    def __init__(self, executor: str, model_id: str) -> None:
        self._executor = executor
        self._model_id = model_id

    def as_commit_required(self):
        return self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> ExecutorEnvironment:
        return ExecutorEnvironment(
            name=f"{self._executor}-workflow",
            interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""),
            home=os.path.expanduser("~"),
            provider_identity=self._executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        _Launcher.launches.append((self._executor, self._model_id))
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model_id,
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


@pytest.fixture(autouse=True)
def _reset_launches():
    _Launcher.launches = []
    yield
    _Launcher.launches = []


def _init_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00Z"
    env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True, env=env)


def _dispatch(registry, run, identities, tmp_path: Path, **kwargs):
    creator = _WorktreeCreator(tmp_path / "wt")
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": creator}
    )()
    result = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda identity: _Launcher(identity.executor, identity.model_id),
        coordinator_root=tmp_path / "coordinator",
        **kwargs,
    )
    return result, creator


def _step(phase: str, persona: str, card: str, *, gate_result: str = "pending", domain=None, commit_policy=None):
    return WorkflowStep(
        phase=phase, persona=persona, card=card, executor=None, model=None, domain=domain,
        inputs=(), outputs=(), gate_result=gate_result, commit_policy=commit_policy,
    )


def _new_run(registry: JobRegistry, tmp_path: Path, *, steps, current_phase="build", **kwargs):
    return registry._manager_create_workflow_run(
        work_id="execution-profile-835", repo="owner/repo",
        claim_key="claim:v1:" + "e" * 64, source_revision="f" * 40,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase=current_phase,
        steps=steps, gate_status="running", **kwargs,
    )


def _assert_profile_blocked(state_path: Path, run_id: str, result, *, detail: str) -> None:
    assert result is not None
    assert result["reason"] == "execution-profile-blocked"
    assert detail in result["detail"]
    # 重新載入 registry（等同 Manager 重啟後讀到的狀態）：needs_human 已持久化。
    reloaded = JobRegistry(state_path).get_workflow_run(run_id)
    assert "needs_human" in reloaded.facets
    assert reloaded.needs_human_reason["reason"] == "execution-profile-blocked"
    assert detail in reloaded.needs_human_reason["detail"]
    assert JobRegistry(state_path).list_jobs() == []
    assert _Launcher.launches == []


def _hardened_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from paulsha_cortex.coordinator import job_runner

    monkeypatch.setenv(job_runner.JOB_RUNNER_ENV, job_runner.RUNNER_SYSTEMD_RUN)


def _pinned_claude_builder_run(registry: JobRegistry, tmp_path: Path, card: str):
    run = _new_run(
        registry, tmp_path,
        steps=(_step("build", "builder", card),),
        model_chain_override={"builder": {"executor": "claude", "model_id": "fixture-claude"}},
    )
    identities = IdentityRegistry.from_rows(
        [{"executor": "claude", "model_id": "fixture-claude", "independence_domain": "anthropic", "capabilities": ["build"]}]
    )
    return run, identities


def test_manager_dispatch_blocks_trust_root_incompatible_identity_and_persists_needs_human(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trust Root 硬條件在正式派工路徑生效：run-scoped pin 繞過候選排序的 identity
    在加固 runner 下缺 builder credential grant，沒有 runtime 需求宣告的卡（不經
    preflight gate）由 profile gate 拒絕並持久化 needs_human，而不是讓例外打穿 tick。"""

    _init_worktree(tmp_path / "wt")
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path)
    run, identities = _pinned_claude_builder_run(registry, tmp_path, "worktree-isolation")
    _hardened_runner(monkeypatch)

    result, creator = _dispatch(registry, run, identities, tmp_path)

    _assert_profile_blocked(state_path, run.run_id, result, detail="Trust Root profile is not valid")
    assert "credential grant" in result["detail"]
    assert creator.calls == []


def test_runtime_gate_records_trust_root_profile_rejection_per_candidate(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 runtime 需求宣告的卡：Trust Root 拒絕是候選層級的 profile 拒絕，全數候選
    被擋時持久化的 needs_human 指名 Trust Root 條件。"""

    _init_worktree(tmp_path / "wt")
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path)
    run, identities = _pinned_claude_builder_run(registry, tmp_path, "subagent-build")
    _hardened_runner(monkeypatch)

    result, creator = _dispatch(registry, run, identities, tmp_path)

    assert result["reason"] == "runtime-preflight-capability_missing"
    reloaded = JobRegistry(state_path).get_workflow_run(run.run_id)
    assert "needs_human" in reloaded.facets
    assert "Trust Root profile is not valid" in reloaded.needs_human_reason["detail"]
    assert JobRegistry(state_path).list_jobs() == []
    assert _Launcher.launches == [] and creator.calls == []


def test_trust_root_gate_passes_compatible_identity_under_hardened_runner(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一個 gate 對合規 identity 放行（證明上一個負例拒絕的是 Trust Root 條件本身）。"""

    _hardened_runner(monkeypatch)
    run = SimpleNamespace(steps=[], model_chain_override=None, sizing_band=None, facets=())
    step = SimpleNamespace(persona="builder", card="tdd-red")
    codex = _identity("codex", "fixture-codex")
    binding, _ = manager._bind_workflow_execution_profile(run, step, codex, _Launcher("codex", "fixture-codex"))
    assert binding.resolved.conditions["adapter"]["value"]["id"] == "codex-cli"
    claude = _identity("claude", "fixture-claude")
    with pytest.raises(execution_adapters.ExecutionAdapterError, match="Trust Root"):
        manager._bind_workflow_execution_profile(run, step, claude, _Launcher("claude", "fixture-claude"))


def test_slice_lane_binding_applies_trust_root_without_capability_gate(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.coordinator import autonomy

    _hardened_runner(monkeypatch)
    # slice lane 不以 capability 篩選 spec 指名的 identity（#381）：沒有 build
    # capability 的合規 identity 仍可綁定。
    codex = _identity("codex", "fixture-codex", capabilities=())
    bound = autonomy._bind_dispatch_execution_profile(
        SubprocessLauncher(executor="codex", model="fixture-codex").as_commit_required(),
        codex,
        "builder",
        explicit_pin={"executor": "codex", "model_id": "fixture-codex"},
    )
    assert bound.execution_profile_binding is not None
    claude = _identity("claude", "fixture-claude", capabilities=())
    with pytest.raises(execution_adapters.ExecutionAdapterError, match="Trust Root"):
        autonomy._bind_dispatch_execution_profile(
            SubprocessLauncher(executor="claude", model="fixture-claude").as_commit_required(),
            claude,
            "builder",
            explicit_pin={"executor": "claude", "model_id": "fixture-claude"},
        )


def test_manager_dispatch_blocks_forced_identity_that_violates_frozen_pin(
    config_root: Path, tmp_path: Path
) -> None:
    _init_worktree(tmp_path / "wt")
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path)
    run = _new_run(
        registry, tmp_path,
        steps=(_step("build", "builder", "subagent-build"),),
        model_chain_override={"builder": {"executor": "codex", "model_id": "gpt-pinned"}},
    )
    identities = IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "gpt-pinned", "independence_domain": "openai", "capabilities": ["build"]},
            {"executor": "codex", "model_id": "gpt-other", "independence_domain": "openai", "capabilities": ["build"]},
        ]
    )

    result, creator = _dispatch(
        registry, run, identities, tmp_path, forced_identity=identities.get("codex", "gpt-other")
    )

    _assert_profile_blocked(state_path, run.run_id, result, detail="explicit model pin")
    assert creator.calls == []


def test_manager_dispatch_blocks_forced_reviewer_in_builder_domain(
    config_root: Path, tmp_path: Path
) -> None:
    _init_worktree(tmp_path / "wt")
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path)
    run = _new_run(
        registry, tmp_path,
        current_phase="review",
        steps=(
            _step("build", "builder", "subagent-build", gate_result="passed", domain="openai", commit_policy="required"),
            _step("review", "reviewer", "code-review"),
        ),
        candidate_head="c" * 40,
    )
    identities = IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "gpt-builder", "independence_domain": "openai", "capabilities": ["build", "review"]},
            {"executor": "claude", "model_id": "claude-reviewer", "independence_domain": "anthropic", "capabilities": ["review"]},
        ]
    )

    result, creator = _dispatch(
        registry, run, identities, tmp_path, forced_identity=identities.get("codex", "gpt-builder")
    )

    _assert_profile_blocked(state_path, run.run_id, result, detail="independence domain")
    assert creator.calls == []


def test_invalid_descriptor_refuses_dispatch_without_sticky_needs_human(
    config_root: Path, tmp_path: Path
) -> None:
    """descriptor 壞掉是部署設定錯誤：派工在建立任何 worktree／job 前拒絕，但不把
    run 寫成 needs_human；descriptor 修正後同一個 run 直接恢復派工。"""

    _init_worktree(tmp_path / "wt")
    state_path = tmp_path / "jobs.json"
    registry = JobRegistry(state_path)
    run = _new_run(registry, tmp_path, steps=(_step("build", "builder", "subagent-build"),))
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-any", "independence_domain": "openai", "capabilities": ["build"]}]
    )
    overlay = _write_overlay(config_root, {"fixture-unregistered": _packaged_entry("codex")})

    with pytest.raises(execution_adapters.ExecutionAdapterDescriptorError, match="trusted code"):
        _dispatch(registry, run, identities, tmp_path)
    persisted = JobRegistry(state_path).get_workflow_run(run.run_id)
    assert "needs_human" not in persisted.facets
    assert persisted.needs_human_reason is None
    assert JobRegistry(state_path).list_jobs() == [] and _Launcher.launches == []

    overlay.unlink()
    result, _ = _dispatch(JobRegistry(state_path), persisted, identities, tmp_path)
    assert result is not None and "job_id" in result
    assert _Launcher.launches == [("codex", "gpt-any")]


def _legacy_registry(tmp_path: Path) -> Path:
    state_path = tmp_path / "jobs.json"
    shutil.copyfile(LEGACY_FIXTURE, state_path)
    return state_path


def _legacy_run_record(state_path: Path, run_id: str) -> dict[str, object]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return next(item for item in payload["workflows"] if item["run_id"] == run_id)


_LEGACY_IMMUTABLE_FIELDS = (
    "claim_key", "combo", "created_at", "evidence_refs", "model_chain_override",
    "repo", "source_revision", "work_id",
)


def test_legacy_manifest_unknown_role_is_blocked_with_persisted_needs_human(
    config_root: Path, tmp_path: Path
) -> None:
    """#581 unknown role：pre-#835 manifest 的未知 persona 不默認 build，而是在建立任何
    job／worktree 前以 profile gate 持久化 needs_human；legacy 欄位位元組不變。"""

    _init_worktree(tmp_path / "wt")
    state_path = _legacy_registry(tmp_path)
    before = _legacy_run_record(state_path, LEGACY_UNKNOWN_ROLE_RUN)
    registry = JobRegistry(state_path)
    run = registry.get_workflow_run(LEGACY_UNKNOWN_ROLE_RUN)
    assert run.steps[0].persona == "architect"
    assert run.execution_profile_bindings is None
    identities = IdentityRegistry.from_rows(
        [{"executor": "codex", "model_id": "gpt-any", "independence_domain": "openai", "capabilities": ["build"]}]
    )

    result, creator = _dispatch(registry, run, identities, tmp_path)

    _assert_profile_blocked(state_path, run.run_id, result, detail="unknown workflow persona: architect")
    assert creator.calls == []
    after = _legacy_run_record(state_path, LEGACY_UNKNOWN_ROLE_RUN)
    for field in (*_LEGACY_IMMUTABLE_FIELDS, "steps", "attempts", "resolved_model_chain"):
        assert json.dumps(after[field], sort_keys=True) == json.dumps(before[field], sort_keys=True)
    assert "execution_profile_bindings" not in after or after["execution_profile_bindings"] is None


def test_legacy_manifest_restart_redispatch_reuses_frozen_chain_binding(
    config_root: Path, tmp_path: Path
) -> None:
    """pre-#835 run（凍結 chain、無 profile sibling）：派工新增 binding 但不改 legacy
    欄位；Manager 重啟（重新載入 registry 與 descriptor catalog）後 in-flight job 原樣
    沿用，重派（retry-build）解析出位元組相同的 binding 與相同 identity。"""

    _init_worktree(tmp_path / "wt")
    state_path = _legacy_registry(tmp_path)
    before = _legacy_run_record(state_path, LEGACY_FROZEN_RUN)
    registry = JobRegistry(state_path)
    run = registry.get_workflow_run(LEGACY_FROZEN_RUN)
    assert run.execution_profile_bindings is None
    assert run.model_chain_override == {"builder": {"executor": "codex", "model_id": "gpt-legacy-builder"}}
    # 排序在前的 claude 若被選中代表凍結 chain 被重新解析掉了。
    identities = IdentityRegistry.from_rows(
        [
            {"executor": "claude", "model_id": "claude-first", "independence_domain": "anthropic", "capabilities": ["build"]},
            {"executor": "codex", "model_id": "gpt-legacy-builder", "independence_domain": "openai", "capabilities": ["build"]},
        ]
    )

    first, _ = _dispatch(registry, run, identities, tmp_path)
    assert first is not None and "job_id" in first
    assert _Launcher.launches == [("codex", "gpt-legacy-builder")]
    persisted = _legacy_run_record(state_path, LEGACY_FROZEN_RUN)
    first_binding = persisted["execution_profile_bindings"]["builder"]
    assert first_binding["resolved"]["conditions"]["model"]["value"]["id"] == "gpt-legacy-builder"
    for field in _LEGACY_IMMUTABLE_FIELDS:
        assert json.dumps(persisted[field], sort_keys=True) == json.dumps(before[field], sort_keys=True)
    # legacy planner provenance（舊 source 值 "registry"）原樣保留，只新增 builder 列。
    assert persisted["resolved_model_chain"]["planner"] == before["resolved_model_chain"]["planner"]

    # --- Manager 重啟：新的 registry 物件、catalog 快取清空。
    execution_adapters.reload_adapter_catalog()
    registry = JobRegistry(state_path)
    restarted = registry.get_workflow_run(LEGACY_FROZEN_RUN)
    assert restarted.execution_profile_bindings["builder"] == first_binding

    in_flight, _ = _dispatch(registry, restarted, identities, tmp_path)
    assert in_flight["job_id"] == first["job_id"]
    assert _Launcher.launches == [("codex", "gpt-legacy-builder")]
    assert _legacy_run_record(state_path, LEGACY_FROZEN_RUN)["execution_profile_bindings"]["builder"] == first_binding

    registry.update_headless_result(first["job_id"], status="exited", exit_code=1)
    execution_adapters.reload_adapter_catalog()
    registry = JobRegistry(state_path)
    retried, _ = _dispatch(
        registry, registry.get_workflow_run(LEGACY_FROZEN_RUN), identities, tmp_path, force_new_card=True
    )
    assert retried is not None and retried["job_id"] != first["job_id"]
    assert _Launcher.launches[-1] == ("codex", "gpt-legacy-builder")
    after_retry = _legacy_run_record(state_path, LEGACY_FROZEN_RUN)
    assert json.dumps(after_retry["execution_profile_bindings"]["builder"], sort_keys=True) == json.dumps(
        first_binding, sort_keys=True
    )
    for field in _LEGACY_IMMUTABLE_FIELDS:
        assert json.dumps(after_retry[field], sort_keys=True) == json.dumps(before[field], sort_keys=True)
