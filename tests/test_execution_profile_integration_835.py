"""Issue #835：production consumption of the execution-profile schema-core."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import autonomy, execution_adapters, launcher, manager, planning_runtime
from paulsha_cortex.coordinator.model_identities import IdentityRegistry, load_model_identities
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo
from paulsha_cortex.monitor.providers import _validate_workflow_v2_row


def _identity(
    executor: str = "copilot",
    model_id: str = "fictional-model",
    *,
    domain: str = "provider-a",
    capabilities: tuple[str, ...] = ("build",),
):
    return SimpleNamespace(
        executor=executor,
        model_id=model_id,
        independence_domain=domain,
        capabilities=capabilities,
        profile_provenance=None,
        executable=None,
    )


def _descriptor(executor="copilot", model_id="fictional-model", *, effort_values=None):
    adapter = execution_adapters.adapter_for(executor)
    return {
        "schema_version": 1,
        "id": f"fixture-{executor}-{model_id}",
        "adapter": adapter.descriptor_fields(),
        "model": {"id": model_id, "revision": "unreported"},
        "effort_grammar": (
            {"type": "string", "enum": list(effort_values)}
            if effort_values is not None
            else adapter.effort_grammar()
        ),
        "provenance": [],
        "metadata": {},
    }


def _refingerprint(report: dict) -> dict:
    """模擬 producer 對改寫後內容重新簽 fingerprint（PatchMUD report_schema 規則）。"""

    stable = {
        key: value
        for key, value in report.items()
        if key not in ("generated_at", "report_fingerprint")
    }
    encoded = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report["report_fingerprint"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return report


def _patchmud_v2_consumer_case():
    report_path = Path(__file__).parent / "fixtures/patchmud/report-v2/positive.json"
    # 逐位元組使用 PatchMUD 產出的 fixture（不加欄位、不重簽），證明真報表可消費。
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # 產出該 fixture 的 PatchMUD revision（見 consumer 文件）；報表內沒有此欄，
    # consumer 只記為 provenance。
    source_revision = "421fadc7dc16b6ee9020bdc2333ff6fe856accaa"
    row = report["leaderboards"]["clear_rate"]["rows"][0]
    envelope_context = {
        "executor": "copilot",
        "model_id": "fictional-model",
        "persona": "builder",
        "deck": {
            "deck_id": row["deck_id"],
            "content_sha256": row["deck_digest"].removeprefix("sha256:"),
            "encounter_count": len(row["coverage_expected_encounters"]),
            "measured_personas": [row["role"]],
        },
        "patchmud_version": report["producer"]["version"],
        "role": row["role"],
        "benchmark_type": row["benchmark_type"],
        "deck_digest": row["deck_digest"],
        "evaluator_revision": row["evaluator_revision"],
    }
    return (
        report,
        row["profile_id"],
        source_revision,
        report["report_fingerprint"],
        envelope_context,
    )


def test_a1_descriptor_only_model_and_native_effort_resolve_exact_profiles() -> None:
    identity = _identity()
    descriptor = _descriptor(effort_values=("fixture-native-v2",))
    binding = execution_adapters.resolve_profile(
        identity,
        "builder",
        effort="fixture-native-v2",
        descriptor=descriptor,
    )

    assert binding.requested.to_dict()["plane"] == "requested"
    assert binding.requested.to_dict()["conditions"]["effort"] == {
        "state": "known",
        "value": "fixture-native-v2",
    }
    assert binding.resolved.to_dict()["plane"] == "resolved"
    assert binding.resolved.to_dict()["conditions"]["model"]["value"]["id"] == identity.model_id
    assert binding.request_key.startswith("epk:v1:request:")
    assert binding.resolved_key.startswith("epk:v1:resolved:")

    reordered_descriptor = dict(reversed(list(descriptor.items())))
    reordered = execution_adapters.resolve_profile(
        identity,
        "builder",
        effort="fixture-native-v2",
        descriptor=reordered_descriptor,
    )
    assert reordered.resolved_key == binding.resolved_key

    other_adapter = execution_adapters.resolve_profile(
        _identity("cg", "fictional-model"),
        "reviewer",
        effort="fixture-native-v2",
        descriptor=_descriptor("cg", "fictional-model", effort_values=("fixture-native-v2",)),
    )
    assert other_adapter.resolved_key != binding.resolved_key


def _fixture_runtime_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
    commit_required: bool = False,
    effort: str | None = None,
) -> list[str]:
    """虛構 runtime 的受信任 argv 實作（新 runtime 以程式碼提供，descriptor 不能）。"""

    if allow_unsafe:
        raise ValueError("fixture runtime refuses unsafe mode")
    mode = "read-only" if (read_only or review_only) else "workspace-write"
    resolved_effort = execution_adapters.adapter_for("fixture-rt").launch_effort(model, effort)
    argv = ["fixture-rt", "--model", str(model), "--sandbox", mode, "--log", f"{log_dir}/{slice_id}.jsonl"]
    if resolved_effort is not None:
        argv += ["--effort", resolved_effort]
    if commit_required:
        argv.append("--commit")
    return [*argv, "--", prompt]


def _fixture_runtime_usage(log_path: str) -> dict:
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line]
    usage = next(row["usage"] for row in reversed(rows) if row.get("type") == "usage")
    return {"usage": {**usage, "source": "fixture-rt"}, "usage_raw": usage, "usage_reason": None}


def _fixture_runtime_adapter(**overrides) -> execution_adapters.ExecutionAdapter:
    fields = {
        "efforts": ("eco", "turbo"),
        "default_effort": "eco",
        "usage_source": "fixture-jsonl",
        "quota_state": "unknown",
        "argv_builder": _fixture_runtime_argv,
        "usage_extractor": _fixture_runtime_usage,
    }
    fields.update(overrides)
    return execution_adapters.ExecutionAdapter(
        "fixture-rt", "fixture-runtime-protocol", "1", "fixture-adapter-v1", **fields
    )


@pytest.fixture
def isolated_adapters(monkeypatch):
    monkeypatch.setattr(execution_adapters, "_REGISTERED_ADAPTERS", {})
    execution_adapters.reload_adapter_catalog()
    yield
    execution_adapters.reload_adapter_catalog()


def test_a2_registered_adapter_conformance_through_production_launcher(
    isolated_adapters, monkeypatch, tmp_path: Path
) -> None:
    """虛構 runtime 以 `register_adapter` 登記真 `ExecutionAdapter`，逐項走 production
    路徑：resolve → dispatch 硬條件 → `SubprocessLauncher.launch()` 的 argv／spawn；
    terminal／usage／quota／cancel／timeout／工具／sandbox 都用 production 實作驗。"""

    from paulsha_cortex.coordinator import job_workspace, terminal_contract

    adapter = _fixture_runtime_adapter()
    # 程式碼登記之前：descriptor／launcher 都不認得這個 runtime，spawn 前即拒絕。
    with pytest.raises(ValueError, match="unsupported adapter|unknown executor"):
        launcher.SubprocessLauncher(executor="fixture-rt", model="fixture-model")
    execution_adapters.register_adapter(adapter)
    assert execution_adapters.adapter_for("fixture-rt") is adapter
    with pytest.raises(ValueError, match="already registered"):
        execution_adapters.register_adapter(adapter)
    # 新 runtime 沒有受信任 argv 實作、或宣告 launcher 不實作的 cancel／timeout／
    # terminal 契約，登記即拒絕。
    for executor, overrides, reason in (
        ("fixture-noargv", {"argv_builder": None}, "trusted argv"),
        ("fixture-cancel", {"cancellation_contract": "fixture-kill-v9"}, "cancel"),
        ("fixture-timeout", {"timeout_contract": "fixture-never-v1"}, "timeout"),
        ("fixture-terminal", {"terminal_contract": "fixture-terminal-v9"}, "terminal"),
    ):
        with pytest.raises(ValueError, match=reason):
            execution_adapters.register_adapter(
                replace(_fixture_runtime_adapter(**overrides), executor=executor)
            )

    spawns: list[dict] = []

    class _Proc:
        pid = 5150

    def _fake_popen(argv, **kwargs):
        spawns.append({"argv": argv, **kwargs})
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(job_workspace, "prepare_commit_spool", lambda spool_key: tmp_path / "bundle")

    # --- argv／tools／sandbox：builder profile 由真 launcher 契約導出，launch 發出同一組條件。
    identity = SimpleNamespace(
        executor="fixture-rt", model_id="fixture-model", independence_domain="fixture",
        capabilities=("build", "planning"), profile_provenance=None, executable=None,
    )
    builder_launcher = launcher.SubprocessLauncher(
        executor="fixture-rt", model="fixture-model"
    ).as_commit_required()
    binding = execution_adapters.make_launcher_profile(builder_launcher, identity, "builder")
    resolved = binding.resolved.to_dict()["conditions"]
    assert resolved["adapter"]["value"] == adapter.descriptor_fields()
    assert resolved["effort"] == {"state": "known", "value": "eco"}
    assert resolved["sandbox"]["value"]["id"] == "workspace-write"
    assert {"id": "git-commit", "version": "1"} in resolved["toolset"]["value"]
    assert resolved["toolchain"]["value"] == {"id": "fixture-rt", "version": "fixture-adapter-v1"}
    execution_adapters.validate_dispatch_requirements(
        binding, identity=identity, trust_root_valid=True
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    handle = builder_launcher.with_execution_profile(binding).launch(
        slice_id="fixture-job", prompt="PROMPT", worktree=str(worktree), log_dir=str(tmp_path / "logs")
    )
    assert handle.executor == "fixture-rt"
    assert len(spawns) == 1
    script = spawns[0]["argv"][2]
    assert "fixture-rt --model fixture-model --sandbox workspace-write" in script
    assert "--effort eco" in script and "--commit" in script
    # cancel：launcher 的 process-group 邊界（adapter 宣告的契約就是 launcher 實作的那一個）。
    assert adapter.cancel_contract() == "launcher-process-group-v1"
    assert spawns[0]["start_new_session"] is True
    assert adapter.timeout_contract_name() == "job-watchdog-v1"

    planner_launcher = launcher.SubprocessLauncher(
        executor="fixture-rt", model="fixture-model"
    ).as_read_only()
    planner_binding = execution_adapters.make_launcher_profile(planner_launcher, identity, "planner")
    planner_conditions = planner_binding.resolved.to_dict()["conditions"]
    assert planner_conditions["sandbox"]["value"]["id"] == "read-only"
    assert planner_conditions["permissions"]["value"] == [{"id": "read-only", "version": "1"}]
    assert planner_binding.resolved_key != binding.resolved_key
    planner_launcher.with_execution_profile(planner_binding).launch(
        slice_id="fixture-plan", prompt="PLAN", worktree=str(worktree), log_dir=str(tmp_path / "logs")
    )
    assert "--sandbox read-only" in spawns[1]["argv"][2]

    # --- terminal：共用 terminal contract，不是 adapter 自訂規則。
    envelope = adapter.parse_terminal(
        {"schema_version": 2, "kind": "workflow-card", "status": "passed", "gate_evidence": []}
    )
    assert envelope.status == "passed"
    with pytest.raises(terminal_contract.TerminalContractError):
        adapter.parse_terminal({"schema_version": 2, "kind": "workflow-card", "status": "running"})

    # --- usage：adapter 自己的 extractor，經 production fail-soft 邊界。
    log = tmp_path / "usage.jsonl"
    log.write_text(
        json.dumps({"type": "usage", "usage": {"input_tokens": 13, "output_tokens": 5}}) + "\n",
        encoding="utf-8",
    )
    assert adapter.parse_usage(str(log))["usage"]["input_tokens"] == 13
    missing = adapter.parse_usage(str(tmp_path / "missing.jsonl"))
    assert missing["usage"] is None and missing["usage_reason"]

    # --- quota：沒有可信 producer 時明示 unknown，不從 usage 推導剩餘額度。
    assert adapter.quota_capability() == {"state": "unknown", "source": "not-bound"}

    # --- unsupported：未宣告的 effort、與 profile 不符的 launcher 都在 spawn 前拒絕。
    with pytest.raises(ValueError, match="unsupported effort"):
        execution_adapters.resolve_profile(identity, "builder", effort="warp")
    with pytest.raises(ValueError, match="does not match"):
        launcher.SubprocessLauncher(
            executor="fixture-rt", model="fixture-model", effort="turbo"
        ).as_commit_required().with_execution_profile(binding)
    with pytest.raises(ValueError, match="effort must be one of"):
        launcher.SubprocessLauncher(
            executor="fixture-rt", model="fixture-model", effort="warp"
        ).as_commit_required().launch(
            slice_id="fixture-bad", prompt="P", worktree=str(worktree), log_dir=str(tmp_path / "logs")
        )
    with pytest.raises(ValueError, match="unknown executor|unsupported adapter"):
        launcher.SubprocessLauncher(executor="fixture-unregistered", model="fixture-model")
    assert len(spawns) == 2


@pytest.mark.parametrize(
    "case",
    ("unknown-role", "pin-mismatch", "qualification", "same-domain", "trust-root"),
)
def test_a3_hard_conditions_reject_without_quota_override(case: str) -> None:
    identity = _identity(capabilities=("build", "review"))
    # Trust Root 是必填判定（production 由 `trust_root_compatibility()` 提供）；
    # 其餘案例明示它成立，確保拒絕來自各自的硬條件。
    kwargs = {"quota": {"state": "available", "remaining": 999}, "trust_root_valid": True}
    if case == "unknown-role":
        with pytest.raises(ValueError, match="unknown workflow persona"):
            execution_adapters.resolve_profile(identity, "unregistered-role")
        return
    if case == "pin-mismatch":
        identity = _identity(
            "copilot", "different-model", capabilities=("build", "review")
        )
        binding = execution_adapters.resolve_profile(
            identity,
            "reviewer",
            requirements={
                "pin": {"executor": "copilot", "model_id": "fictional-model"}
            },
        )
        kwargs["identity"] = identity
        expected_reason = "explicit model pin"
    elif case == "qualification":
        binding = execution_adapters.resolve_profile(identity, "reviewer")
        kwargs["qualification_required"] = True
        kwargs["qualification"] = {
            "state": "approved",
            "profile_key": "epk:v1:resolved:" + "0" * 64,
            "role": "reviewer",
            "coverage": "complete",
            "receipt": "receipt:stale-profile",
        }
        expected_reason = "qualification"
    elif case == "same-domain":
        binding = execution_adapters.resolve_profile(identity, "reviewer")
        kwargs["builder_domains"] = ("provider-a",)
        expected_reason = "independence domain"
    elif case == "trust-root":
        binding = execution_adapters.resolve_profile(identity, "reviewer")
        kwargs["trust_root_valid"] = False
        kwargs["trust_root_reason"] = "missing reviewer credential grant"
        expected_reason = "Trust Root profile is not valid: missing reviewer credential grant"
    kwargs.setdefault("identity", identity)
    with pytest.raises(ValueError, match=expected_reason):
        execution_adapters.validate_dispatch_requirements(binding, **kwargs)


def test_a3_trust_root_condition_is_mandatory_at_the_dispatch_boundary() -> None:
    """舊介面預設 `trust_root_valid=True`，呼叫端不傳就等於永遠成立；現在必填。"""

    identity = _identity(capabilities=("build",))
    binding = execution_adapters.resolve_profile(identity, "builder")
    with pytest.raises(TypeError, match="trust_root_valid"):
        execution_adapters.validate_dispatch_requirements(binding, identity=identity)


def test_a3_manager_blocks_sized_dispatch_without_exact_qualification(monkeypatch) -> None:
    spawn_calls: list[object] = []
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *args, **kwargs: spawn_calls.append(args)
    )
    identity = _identity(capabilities=("build",))
    run = SimpleNamespace(
        steps=[
            SimpleNamespace(
                phase="build",
                gate_result="passed",
                commit_policy="required",
                domain="provider-a",
            )
        ],
        model_chain_override=None,
        sizing_band="L2",
        facets=(),
    )
    step = SimpleNamespace(persona="builder", card="tdd-red")
    launcher_instance = launcher.SubprocessLauncher(
        executor=identity.executor, model=identity.model_id
    ).as_commit_required()
    with pytest.raises(ValueError, match="exact-profile qualification is unknown"):
        manager._bind_workflow_execution_profile(
            run, step, identity, launcher_instance, qualification_policy="enforce"
        )
    assert spawn_calls == []


@pytest.mark.parametrize("executor", ("claude", "codex", "copilot", "agy"))
@pytest.mark.parametrize(
    ("phase", "persona", "card"),
    (
        ("build", "builder", "tdd-red"),
        ("verify", "reviewer", "verification"),
        ("review", "reviewer", "code-review"),
    ),
)
@pytest.mark.parametrize(
    ("qualification_policy", "expected_action"),
    (("disabled", "dispatch"), ("enforce", "needs_human")),
)
def test_a3_sized_dispatch_requires_exact_qualification_only_under_explicit_policy(
    executor: str,
    phase: str,
    persona: str,
    card: str,
    qualification_policy: str,
    expected_action: str,
    monkeypatch,
) -> None:
    from paulsha_cortex.coordinator import runtime_preflight

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": executor,
                "model_id": f"fixture-{executor}-model",
                "independence_domain": f"domain-{executor}",
                "capabilities": ["build", "review"],
            }
        ]
    )
    if qualification_policy == "enforce":
        identities = replace(identities, qualification_policy=qualification_policy)
    identity = identities.identities[0]
    builder_step = SimpleNamespace(
        phase="build",
        persona="builder",
        card="tdd-red",
        gate_result="passed",
        commit_policy="required",
        domain="builder-domain",
        outputs=(),
    )
    step = SimpleNamespace(
        phase=phase,
        persona=persona,
        card=card,
        gate_result="pending",
        commit_policy="required" if persona == "builder" else None,
        domain="builder-domain" if persona == "builder" else None,
        outputs=(),
    )
    run = SimpleNamespace(
        run_id="run:qualification-policy",
        steps=(builder_step, step) if persona == "reviewer" else (step,),
        primary_domain=None,
        model_chain_override=None,
        sizing_band="L2",
    )

    class FakeLauncher:
        def __init__(self):
            self._commit_required = False
            self._review_only = False

        def as_commit_required(self):
            self._commit_required = True
            return self

        def as_review_only(self, *, terminal_kind):
            self._review_only = True
            self._terminal_kind = terminal_kind
            return self

        def executor_environment(self):
            return runtime_preflight.host_environment(name=f"{executor}:fixture")

    monkeypatch.setattr(
        runtime_preflight,
        "card_runtime_requirements",
        lambda *_args, **_kwargs: (runtime_preflight.RuntimeCapability("module", "pytest"),),
    )
    monkeypatch.setattr(
        runtime_preflight,
        "run_runtime_preflight",
        lambda *, card, identity, environment, **_kwargs: runtime_preflight.RuntimePreflightResult(
            card=card,
            identity_token=f"{identity.executor}/{identity.model_id}",
            environment=environment,
            findings=(),
            checked_at=0.0,
        ),
    )
    monkeypatch.setattr(manager.model_resolution, "compatibility_checker_for", lambda _persona: None)

    decision = manager._runtime_preflight_gate(
        run,
        step,
        identities=identities,
        launcher_factory=lambda _identity: FakeLauncher(),
    )

    assert decision is not None
    assert decision.action == expected_action
    if qualification_policy == "disabled":
        assert decision.identity is identity
        assert decision.launcher is not None
    else:
        assert decision.identity is None
        assert decision.launcher is None
        assert "exact-profile qualification is unknown" in (decision.reason or "")


def test_qualification_enforcement_requires_explicit_model_identity_overlay_policy(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "model-identities.yaml"
    overlay.write_text(
        """\
schema_version: 4
qualification_policy:
  sized_dispatch: enforce
identities:
  - executor: claude
    model_id: fixture-claude-model
    independence_domain: anthropic
    capabilities: [build]
""",
        encoding="utf-8",
    )

    identities = load_model_identities(tmp_path)

    assert identities.qualification_policy == "enforce"


def test_qualification_enforcement_defaults_disabled_without_host_overlay(tmp_path: Path) -> None:
    identities = load_model_identities(tmp_path)

    assert identities.qualification_policy == "disabled"


def test_sized_dispatch_records_unenforced_qualification_diagnostic(tmp_path: Path) -> None:
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "fixture-codex-model",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )
    identity = identities.identities[0]
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(
        combo, cards, "qualification-diagnostic", change="qualification-diagnostic"
    ).workflow_manifest
    assert manifest is not None
    registry = JobRegistry(tmp_path / "qualification-diagnostic.json")
    run = registry._manager_create_workflow_run(
        work_id="qualification-diagnostic",
        repo="owner/repo",
        claim_key="claim:qualification-diagnostic",
        source_revision="revision",
        workspace_root=str(tmp_path),
        combo=manifest.combo,
        current_phase="build",
        steps=manifest.steps,
        sizing_score=5,
        sizing_band="yellow",
    )
    step = SimpleNamespace(persona="builder")
    binding = execution_adapters.resolve_profile(identity, "builder")
    manager._record_resolved_model_chain(
        registry,
        run,
        step,
        identity,
        identities,
        execution_profile_binding=binding,
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.model_qualification == {"builder": "not-enforced"}
    assert persisted.execution_profile_bindings["builder"]["resolved_key"] == binding.resolved_key
    # 舊版 Manager 的 resolved_model_chain row 驗證是封閉集合（required＋
    # envelope_source）；qualification 塞進 row 會讓 rollback 後整份 registry
    # 載入 fail-closed，所以只能落在舊版 from_dict 會忽略的頂層新欄位。
    legacy_row_keys = {
        "executor",
        "model_id",
        "independence_domain",
        "source",
        "envelope_source",
    }
    assert set(persisted.resolved_model_chain["builder"]) <= legacy_row_keys
    # 同版 Monitor 的 canonical v2 projection 先 round-trip WorkflowRun 再以
    # 封閉白名單驗 row；新欄位漏登記會讓整份 workflow projection degraded。
    _validate_workflow_v2_row(persisted.to_dict())


def test_a4_actual_key_tracks_conditions_and_never_infers_observed_values() -> None:
    identity = _identity()
    baseline = execution_adapters.resolve_profile(identity, "builder")
    assert baseline.actual_key is None
    assert baseline.observed.to_dict()["conditions"]["model"]["state"] == "unknown"

    variants = [
        execution_adapters.resolve_profile(identity, "builder", effort="high"),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"loadout_version": "2"}
        ),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"toolchain_version": "2"}
        ),
        execution_adapters.resolve_profile(_identity("cg"), "builder"),
    ]
    assert all(item.resolved_key != baseline.resolved_key for item in variants)
    for metadata in (
        {"timestamps": {"observed_at": "2026-09-26T00:00:00Z"}},
        {"pricing": {"usd": 4.25}, "pricing_provenance": "fixture-price"},
    ):
        with_metadata = execution_adapters.resolve_profile(
            identity, "builder", metadata=metadata
        )
        assert with_metadata.resolved_key == baseline.resolved_key
        complete_observation = execution_adapters.record_observed(
            with_metadata,
            {
                "verified": True,
                "source": "fixture:runtime",
                "profile_key": with_metadata.resolved_key,
                "conditions": deepcopy(with_metadata.resolved.to_dict()["conditions"]),
            },
        )
        baseline_observation = execution_adapters.record_observed(
            baseline,
            {
                "verified": True,
                "source": "fixture:runtime",
                "profile_key": baseline.resolved_key,
                "conditions": deepcopy(baseline.resolved.to_dict()["conditions"]),
            },
        )
        assert complete_observation.actual_key == baseline_observation.actual_key
    observed = execution_adapters.record_observed(baseline, {"model_id": "fictional-model"})
    assert observed.actual_key is None
    assert observed.observed.to_dict()["conditions"]["model"]["state"] == "unknown"
    observed_conditions = deepcopy(baseline.resolved.to_dict()["conditions"])
    trusted_observation = execution_adapters.record_observed(
        baseline,
        {
            "verified": True,
            "source": "fixture:runtime",
            "profile_key": baseline.resolved_key,
            "conditions": observed_conditions,
        },
    )
    assert trusted_observation.actual_key is not None
    changed_conditions = deepcopy(observed_conditions)
    changed_conditions["sandbox"]["value"]["id"] = "other-sandbox"
    changed_observation = execution_adapters.record_observed(
        baseline,
        {
            "verified": True,
            "source": "fixture:runtime",
            "profile_key": baseline.resolved_key,
            "conditions": changed_conditions,
        },
    )
    assert changed_observation.actual_key != trusted_observation.actual_key

    actual_variants = [
        execution_adapters.resolve_profile(identity, "builder", effort="high"),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"loadout_version": "2"}
        ),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"toolchain_version": "2"}
        ),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"tools": ("Read", "Write")}
        ),
        execution_adapters.resolve_profile(
            identity, "builder", launch_contract={"sandbox": "write-forbidden"}
        ),
        execution_adapters.resolve_profile(_identity("cg"), "reviewer"),
    ]
    actual_keys = []
    for variant in [baseline, *actual_variants]:
        actual_conditions = deepcopy(variant.resolved.to_dict()["conditions"])
        actual_keys.append(
            execution_adapters.record_observed(
                variant,
                {
                    "verified": True,
                    "source": "fixture:runtime",
                    "profile_key": variant.resolved_key,
                    "conditions": actual_conditions,
                },
            ).actual_key
        )
    assert all(key is not None for key in actual_keys)
    assert len(set(actual_keys)) == len(actual_keys)


def test_a5_binding_migration_and_workflow_restart_preserve_legacy_bytes(tmp_path: Path) -> None:
    identity = _identity()
    binding = execution_adapters.resolve_profile(identity, "builder")
    encoded = binding.to_dict()
    assert execution_adapters.load_profile_binding(encoded).to_dict() == encoded
    with pytest.raises(ValueError, match="schema"):
        execution_adapters.load_profile_binding({**encoded, "schema_version": 88})
    with pytest.raises(ValueError, match="descriptor"):
        execution_adapters.load_profile_binding({**encoded, "descriptor": {"schema_version": 1}})

    # 舊 WorkflowRun 缺新版 sibling 欄位仍可讀；新 binding 經 serializer/reloader 往返。
    state_path = tmp_path / "registry.json"
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(combo, cards, "profile-migration", change="profile-migration").workflow_manifest
    assert manifest is not None
    registry = JobRegistry(state_path)
    run = registry._manager_create_workflow_run(
        work_id="migration",
        repo="owner/repo",
        claim_key="claim:migration",
        source_revision="rev-a",
        workspace_root=str(tmp_path),
        combo=manifest.combo,
        current_phase="plan",
        steps=manifest.steps,
        attempts={"claim": 1},
        gate_status="running",
    )
    old_payload = run.to_dict()
    old_payload.pop("execution_profile_bindings", None)
    assert WorkflowRun.from_dict(old_payload).execution_profile_bindings is None
    legacy_resolution = {
        "builder": {
            "executor": "copilot",
            "model_id": "fictional-model",
            "independence_domain": "old-domain",
            "source": "run-override",
        }
    }
    run = registry._manager_update_workflow_run(
        run.run_id,
        resolved_model_chain=legacy_resolution,
        attempts={"claim": 1, "build": 2},
        evidence_refs=("receipt:legacy",),
    )
    before_chain = deepcopy(run.resolved_model_chain)
    before_attempts = deepcopy(run.attempts)
    before_evidence = tuple(run.evidence_refs)

    def _persisted_legacy_bytes() -> bytes:
        # 直接讀磁碟上的 registry 狀態（不是記憶體物件），比對 legacy 欄位的序列化位元組。
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        record = next(item for item in payload["workflows"] if item["run_id"] == run.run_id)
        legacy = {key: record[key] for key in ("resolved_model_chain", "attempts", "evidence_refs")}
        return json.dumps(legacy, sort_keys=True).encode()

    legacy_bytes = _persisted_legacy_bytes()
    registry._manager_update_workflow_run(
        run.run_id, execution_profile_bindings={"builder": encoded}
    )
    assert _persisted_legacy_bytes() == legacy_bytes
    registry = JobRegistry(state_path)
    restored = registry.get_workflow_run(run.run_id)
    assert restored.execution_profile_bindings["builder"] == encoded
    assert restored.resolved_model_chain == before_chain
    assert restored.attempts == before_attempts
    assert restored.evidence_refs == before_evidence


def test_a6_workflow_generic_and_planning_consumers_bind_the_profile_before_launch(
    tmp_path: Path,
) -> None:
    identity = _identity("copilot", capabilities=("planning", "build", "review"))
    binding = execution_adapters.resolve_profile(identity, "builder")
    profiled = launcher.SubprocessLauncher(executor="copilot", model=identity.model_id).with_execution_profile(binding)
    assert profiled.execution_profile_binding["resolved_key"] == binding.resolved_key

    workflow_run = SimpleNamespace(
        steps=[SimpleNamespace(phase="build", gate_result="passed", commit_policy="required", domain="provider-a")],
        model_chain_override={"builder": {"executor": "copilot", "model_id": identity.model_id}},
        sizing_band=None,
    )
    workflow_step = SimpleNamespace(persona="builder")
    workflow_binding, workflow_launcher = manager._bind_workflow_execution_profile(
        workflow_run, workflow_step, identity, profiled
    )
    assert workflow_launcher.execution_profile_binding["resolved_key"] == workflow_binding.resolved_key

    generic_launcher = autonomy._bind_dispatch_execution_profile(
        launcher.SubprocessLauncher(executor="copilot", model=identity.model_id).as_commit_required(),
        identity,
        "builder",
        explicit_pin={"executor": "copilot", "model_id": identity.model_id},
    )
    assert generic_launcher.execution_profile_binding is not None

    planning_identity = _identity("codex", capabilities=("planning", "build", "review"))
    planning_binding = execution_adapters.resolve_profile(
        planning_identity, "planner", launch_contract={"read_only": True, "sandbox": "read-only"}
    )
    argv = planning_runtime._planning_argv(
        planning_identity,
        "{}",
        str(tmp_path),
        tmp_path,
        last_message_path=tmp_path / "last-message.json",
        execution_profile=planning_binding,
    )
    assert "--model" in argv and planning_identity.model_id in argv
    assert "--sandbox" in argv and "read-only" in argv

    missing_revision = deepcopy(binding.to_dict())
    assert execution_adapters.load_profile_binding(missing_revision).actual_key is None

    report, expected_profile_key, source_revision, source_digest, envelope_context = (
        _patchmud_v2_consumer_case()
    )
    consumed = execution_adapters.profile_report_consumer(
        report,
        expected_profile_key=expected_profile_key,
        source_revision=source_revision,
        source_digest=source_digest,
        envelope_context=envelope_context,
    )
    assert consumed["schema_version"] == 2
    assert consumed["source_revision"] == source_revision
    assert consumed["envelope_mapping"]["provenance"]["registry_writable"] is False
    observation = consumed["envelope_mapping"]["provenance"]["observation"]
    assert observation["profile_id"] == expected_profile_key
    assert observation["role"] == envelope_context["role"]
    assert observation["benchmark_type"] == envelope_context["benchmark_type"]
    assert observation["deck_digest"] == envelope_context["deck_digest"]
    assert observation["evaluator_revision"] == envelope_context["evaluator_revision"]
    with pytest.raises(
        ValueError, match="profile report does not match exact resolved profile"
    ):
        execution_adapters.profile_report_consumer(
            report,
            expected_profile_key="epk:v1:resolved:" + "0" * 64,
            source_revision=source_revision,
            source_digest=source_digest,
        )


def test_profile_report_consumer_rejects_v1_and_invalid_source_provenance() -> None:
    report, expected_profile_key, source_revision, source_digest, _ = (
        _patchmud_v2_consumer_case()
    )
    legacy_path = Path(__file__).parent / "fixtures/patchmud/legacy-v1/report.json"
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="profile report"):
        execution_adapters.profile_report_consumer(
            legacy,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
        )
    tampered = deepcopy(report)
    tampered["leaderboards"]["clear_rate"]["rows"][0]["clears"] += 1
    with pytest.raises(ValueError, match="fingerprint does not match content"):
        execution_adapters.profile_report_consumer(
            tampered,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
        )
    unsigned = deepcopy(report)
    del unsigned["report_fingerprint"]
    with pytest.raises(ValueError, match="fingerprint is missing or malformed"):
        execution_adapters.profile_report_consumer(
            unsigned,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
        )
    # 內容合法重簽，但呼叫端釘住的仍是原 fingerprint：不得接受。
    resigned = _refingerprint(deepcopy(tampered))
    with pytest.raises(ValueError, match="source revision/digest mismatch"):
        execution_adapters.profile_report_consumer(
            resigned,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
        )
    for bad_revision in ("", "   "):
        with pytest.raises(ValueError, match="source revision/digest mismatch"):
            execution_adapters.profile_report_consumer(
                report,
                expected_profile_key=expected_profile_key,
                source_revision=bad_revision,
                source_digest=source_digest,
            )
    with pytest.raises(ValueError, match="source revision/digest mismatch"):
        execution_adapters.profile_report_consumer(
            report,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest="0" * 64,
        )
    # generated_at 不進 fingerprint：重跑時間不同不影響採信。
    regenerated = deepcopy(report)
    regenerated["generated_at"] = "2026-09-27T00:00:00Z"
    assert execution_adapters.profile_report_consumer(
        regenerated,
        expected_profile_key=expected_profile_key,
        source_revision=source_revision,
        source_digest=source_digest,
    )["schema_version"] == 2


def test_profile_report_consumer_accepts_exact_key_on_non_ranked_row() -> None:
    report, _, source_revision, _, _ = _patchmud_v2_consumer_case()
    non_ranked_row = next(
        row
        for board in report["leaderboards"].values()
        for row in board.get("rows", [])
        if row.get("ranked") is False
    )
    expected_profile_key = non_ranked_row["profile_id"]
    retained = False
    for board in report["leaderboards"].values():
        rows = board.get("rows")
        if not isinstance(rows, list):
            continue
        filtered_rows = []
        for row in rows:
            if row.get("profile_id") != expected_profile_key:
                filtered_rows.append(row)
            elif row is non_ranked_row and not retained:
                filtered_rows.append(row)
                retained = True
        board["rows"] = filtered_rows
    _refingerprint(report)
    consumed = execution_adapters.profile_report_consumer(
        report,
        expected_profile_key=expected_profile_key,
        source_revision=source_revision,
        source_digest=report["report_fingerprint"],
    )
    assert consumed["schema_version"] == 2


def test_profile_report_consumer_requires_exact_v2_envelope_context() -> None:
    report, expected_profile_key, source_revision, source_digest, context = (
        _patchmud_v2_consumer_case()
    )
    missing = dict(context)
    del missing["evaluator_revision"]
    with pytest.raises(ValueError, match="envelope context is incomplete"):
        execution_adapters.profile_report_consumer(
            report,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
            envelope_context=missing,
        )
    extra = {**context, "report_model": "must-not-be-a-cohort-key"}
    with pytest.raises(ValueError, match="envelope context is incomplete"):
        execution_adapters.profile_report_consumer(
            report,
            expected_profile_key=expected_profile_key,
            source_revision=source_revision,
            source_digest=source_digest,
            envelope_context=extra,
        )


def test_a6_manager_records_profile_as_versioned_sibling_of_legacy_chain(tmp_path: Path) -> None:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(combo, cards, "profile-bind", change="profile-bind").workflow_manifest
    assert manifest is not None
    registry = JobRegistry(tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="profile-bind",
        repo="owner/repo",
        claim_key="claim:profile-bind",
        source_revision="rev-a",
        workspace_root=str(tmp_path),
        combo=manifest.combo,
        current_phase="plan",
        steps=manifest.steps,
        attempts={"claim": 1},
        gate_status="running",
    )
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "copilot",
                "model_id": "fixture-builder",
                "independence_domain": "provider-a",
                "capabilities": ["build"],
            }
        ]
    )
    identity = identities.get("copilot", "fixture-builder")
    step = next(item for item in run.steps if item.persona == "builder")
    binding = execution_adapters.resolve_profile(identity, "builder")
    manager._record_resolved_model_chain(
        registry,
        run,
        step,
        identity,
        identities,
        execution_profile_binding=binding,
    )
    restored = registry.get_workflow_run(run.run_id)
    assert restored.resolved_model_chain["builder"]["model_id"] == "fixture-builder"
    assert restored.execution_profile_bindings["builder"]["resolved_key"] == binding.resolved_key
