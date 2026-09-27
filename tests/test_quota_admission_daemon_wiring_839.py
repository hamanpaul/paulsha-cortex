"""#839 對抗審查 BLOCKER（manager_daemon.py 從未建構／傳入
``quota_admission_context``，production 完全不可達）：驗證 daemon 側的
production 接線本身——設定檔載入（a）、五個 dispatch／resume 呼叫點接線
（b）、periodic tick 收斂掃描（c）。

`_dispatch_workflow_card` 本身的准入邏輯（shadow／enforce／race 落敗換
候選／429 settle 等）已由 ``test_quota_admission_dispatch_wiring_839.py``
覆蓋，這裡刻意不重複——只驗證 manager_daemon 這一層『有沒有把 context 真的
接上』與『設定檔載入本身對不對』。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator import quota_admission
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _build_step() -> WorkflowStep:
    return WorkflowStep(
        phase="build", persona="builder", card="build", executor="codex", model="gpt-5",
        domain="openai", inputs=(), outputs=(),
    )


def _make_run(registry: JobRegistry, tmp_path: Path, *, current_phase: str = "build"):
    return registry._manager_create_workflow_run(
        work_id="839-daemon-wiring", repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(tmp_path), combo="feature-oneshot", current_phase=current_phase,
        steps=(_build_step(),), issue_refs=("hamanpaul/paulsha-cortex#839",),
        gate_status="running",
    )


def _pool_ref(*, account: str, pool: str) -> dict[str, str]:
    return {"authority_id": "operator-budget-authority", "account_id": account, "pool_id": pool, "revision": "1"}


def _descriptor_payload(*, account: str, pool: str) -> dict:
    unit = {"unit_id": "token", "version": "1", "quantity_kind": "amount", "semantics_ref": "fixture:native-token/v1"}
    return schema.parse_pool_descriptor(
        {
            "schema_version": 1, "authority_id": "operator-budget-authority", "account_id": account,
            "pool_id": pool, "revision": "1", "authority_ref": "fixture:operator-pool-map/v1",
            "provenance_refs": ["fixture:pool-map/v1"], "units": [unit],
            "windows": [
                {"window_id": "short", "kind": "rolling", "unit_ref": {"unit_id": "token", "version": "1"}, "duration_ms": 300_000},
            ],
        }
    ).to_dict()


def _binding_payload(*, account: str, pool: str, profile_key: str) -> dict:
    descriptor = schema.parse_pool_descriptor(_descriptor_payload(account=account, pool=pool))
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": f"binding-{profile_key[-8:]}", "revision": "1",
            "subject": {"kind": "profile", "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}}},
            "constraints": [
                {"state": "known", "value": {"pool_ref": _pool_ref(account=account, pool=pool), "window_id": "short"}},
            ],
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=(descriptor,),
    ).to_dict()


def _write_quota_pools_config(path: Path, *, account: str, pool: str, profile_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": quota_admission.QUOTA_POOLS_CONFIG_SCHEMA,
                "config_revision": "test-1",
                "descriptors": [_descriptor_payload(account=account, pool=pool)],
                "unit_catalog": [],
                "bindings": [_binding_payload(account=account, pool=pool, profile_key=profile_key)],
            }
        ),
        encoding="utf-8",
    )


def _resolved_profile_key(run, step, executor: str, model_id: str, *, capability: str = "build") -> str:
    class _Launcher:
        def executor_environment(self):
            from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
            import os
            import sys

            return ExecutorEnvironment(
                name=f"{executor}-workflow", interpreter=(sys.executable,),
                path=os.environ.get("PATH", ""), home=os.path.expanduser("~"), provider_identity=executor,
            )

    candidates = manager._workflow_identity_candidates(run, step, IdentityRegistry.from_rows(
        [{"executor": executor, "model_id": model_id, "independence_domain": "openai", "capabilities": [capability]}]
    ))
    identity = next(c for c in candidates if c.executor == executor)
    binding, _ = manager._bind_workflow_execution_profile(run, step, identity, _Launcher())
    return binding.resolved_key


# ---------------------------------------------------------------------------
# baseline：沒有設定檔 —— 五個呼叫點都要傳 quota_admission_context（值為
# None），且行為與 #839 落地前逐字相同。
# ---------------------------------------------------------------------------


def test_workflow_action_start_passes_quota_admission_context_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()
    monkeypatch.setattr(manager, "apply_workflow_action", lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"})

    spy_calls: list[dict] = []

    def _fake_dispatch(*args, **kwargs):
        spy_calls.append(kwargs)
        return registry.create_job(
            task="839-start", persona="builder", branch="feature/839-start", pane="",
            worktree=str(tmp_path / "wt"), workflow_run_id=run.run_id, workflow_card="build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", _fake_dispatch)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )
    result = executor(build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator"))

    assert "job_id" in result
    assert len(spy_calls) == 1
    assert "quota_admission_context" in spy_calls[0]
    assert spy_calls[0]["quota_admission_context"] is None  # 沒有設定檔——整條線沒接上


def test_workflow_action_resume_passes_quota_admission_context_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    spy_calls: list[dict] = []

    def _fake_resume(*args, **kwargs):
        spy_calls.append(kwargs)
        return {"run_id": run.run_id, "current_phase": run.current_phase}

    monkeypatch.setattr(manager, "resume_workflow_run", _fake_resume)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )
    result = executor(
        build_request(req_type="workflow-action", args={"action": "resume", "run_id": run.run_id}, requested_by="operator")
    )

    assert result["run_id"] == run.run_id
    assert len(spy_calls) == 1
    assert "quota_admission_context" in spy_calls[0]
    assert spy_calls[0]["quota_admission_context"] is None


def test_work_action_resume_passes_quota_admission_context_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path, current_phase="verify")
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    def fake_work_action_fn(*, args, requested_by):
        return {"result": {"action": args["action"], "run": run.to_dict()}}

    spy_calls: list[dict] = []

    def _fake_resume(*args, **kwargs):
        spy_calls.append(kwargs)
        return {"run_id": run.run_id, "current_phase": run.current_phase}

    monkeypatch.setattr(manager, "resume_workflow_run", _fake_resume)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]), work_action_fn=fake_work_action_fn,
    )
    executor(
        build_request(
            req_type="work-action",
            args={"action": "resume", "repo": "hamanpaul/paulsha-cortex", "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert len(spy_calls) == 1
    assert "quota_admission_context" in spy_calls[0]
    assert spy_calls[0]["quota_admission_context"] is None


def test_work_action_forced_retry_passes_quota_admission_context_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    def fake_work_action_fn(*, args, requested_by):
        return {"result": {"action": args["action"], "run": run.to_dict()}}

    spy_calls: list[dict] = []

    def _fake_dispatch(*args, **kwargs):
        spy_calls.append(kwargs)
        return registry.create_job(
            task="839-retry", persona="builder", branch="feature/839-retry", pane="",
            worktree=str(tmp_path / "wt"), workflow_run_id=run.run_id, workflow_card="build",
        )

    monkeypatch.setattr(manager, "dispatch_workflow_card", _fake_dispatch)
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]), work_action_fn=fake_work_action_fn,
    )
    executor(
        build_request(
            req_type="work-action",
            args={"action": "retry-card", "repo": "hamanpaul/paulsha-cortex", "work_id": run.work_id},
            requested_by="operator",
        )
    )

    assert len(spy_calls) == 1
    assert "quota_admission_context" in spy_calls[0]
    assert spy_calls[0]["quota_admission_context"] is None


def test_periodic_tick_passes_quota_admission_context_and_calls_reconcile_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    resume_calls: list[dict] = []

    def _fake_resume(*args, **kwargs):
        resume_calls.append(kwargs)
        return {"run_id": run.run_id, "current_phase": run.current_phase}

    reconcile_calls: list[dict] = []

    def _fake_reconcile(*, registry, quota_admission_context):
        reconcile_calls.append({"registry": registry, "quota_admission_context": quota_admission_context})
        return {"wired": quota_admission_context is not None}

    monkeypatch.setattr(manager, "resume_workflow_run", _fake_resume)
    monkeypatch.setattr(manager, "reconcile_quota_admission_reservations", _fake_reconcile)

    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
        scan_specs_fn=lambda _dir: [], auto_claim_fn=lambda: [],
        run_tick_fn=lambda *a, **k: {"dispatched": []},
    )
    summary = runner()

    assert len(resume_calls) == 1
    assert "quota_admission_context" in resume_calls[0]
    assert resume_calls[0]["quota_admission_context"] is None
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["registry"] is registry
    assert reconcile_calls[0]["quota_admission_context"] is None
    assert "quota_admission_reconcile" not in summary  # wired=False 時不進 summary


# ---------------------------------------------------------------------------
# 設定檔載入（a）：不存在／有效／無效 三態，以及 enforce／shadow 分流。
# ---------------------------------------------------------------------------


def test_context_builder_returns_none_when_config_file_absent() -> None:
    assert manager_daemon._quota_admission_context_for() is None


def test_context_builder_returns_dispatch_context_when_config_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "quota-pools.json"
    _write_quota_pools_config(config_path, account="acct-codex", pool="pool-codex", profile_key="epk:v1:resolved:" + "a" * 64)
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))

    context = manager_daemon._quota_admission_context_for()
    assert isinstance(context, quota_admission.DispatchContext)
    assert len(context.descriptors) == 1
    assert context.descriptors[0].pool_id == "pool-codex"
    assert len(context.bindings) == 1


def test_context_builder_shadow_degrades_to_none_on_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))
    # PSC_QUOTA_ADMISSION_ENFORCE 未設——shadow 模式，無效設定不得擋派工。

    context = manager_daemon._quota_admission_context_for()
    assert context is None


def test_context_builder_enforce_returns_quota_config_invalid_sentinel_on_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))
    monkeypatch.setenv("PSC_QUOTA_ADMISSION_ENFORCE", "on")

    context = manager_daemon._quota_admission_context_for()
    assert isinstance(context, quota_admission.QuotaConfigInvalid)


def test_context_builder_caches_by_content_digest_not_reparsed_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "quota-pools.json"
    _write_quota_pools_config(config_path, account="acct-codex", pool="pool-codex", profile_key="epk:v1:resolved:" + "a" * 64)
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))

    parse_calls = []
    real_parse = quota_admission.parse_quota_pools_config

    def _spy_parse(payload):
        parse_calls.append(payload)
        return real_parse(payload)

    monkeypatch.setattr(quota_admission, "parse_quota_pools_config", _spy_parse)
    manager_daemon._QUOTA_POOLS_CONFIG_CACHE.clear()

    first = manager_daemon._quota_admission_context_for()
    second = manager_daemon._quota_admission_context_for()
    assert isinstance(first, quota_admission.DispatchContext)
    assert isinstance(second, quota_admission.DispatchContext)
    assert len(parse_calls) == 1  # 第二次內容 digest 未變，直接用快取，不重新解析


# ---------------------------------------------------------------------------
# 端到端（真實 manager.dispatch_workflow_card，不 mock）：shadow 記 receipt
# 不擋派、enforce+設定無效 fail closed 回 quota-config-invalid。
# ---------------------------------------------------------------------------


class _FakeWorktreeCreator:
    def __init__(self, path: Path):
        self._path = path

    def create(self, branch: str, base_sha: str | None = None, *, job_id: str | None = None) -> Path:
        return self._path


def _init_worktree(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)


def test_end_to_end_shadow_daemon_dispatch_records_receipt_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    # #839 與本測試無關的既有前置閘門：builder 卡派工前 manager_daemon 會查
    # 一份真的 WorkAuthority（`_builder_todo_admission_for_run`），這裡直接
    # 讓它回 None（等同「沒有 todo authority 監控」，與 quota admission 無關），
    # 不必為它另建一份 GitHub issue snapshot fixture。
    monkeypatch.setattr(manager_daemon, "_builder_todo_admission_for_run", lambda run: None)
    step = manager._current_workflow_step(run)
    profile_key = _resolved_profile_key(run, step, "codex", "gpt-5")

    config_path = tmp_path / "quota-pools.json"
    _write_quota_pools_config(config_path, account="acct-codex", pool="pool-codex", profile_key=profile_key)
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))
    # PSC_QUOTA_ADMISSION_ENFORCE 未設——shadow，不擋派工。
    manager_daemon._QUOTA_POOLS_CONFIG_CACHE.clear()

    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    dispatcher = type(
        "D", (), {"_registry": registry, "_git_runner": None, "_worktree_creator": _FakeWorktreeCreator(worktree)}
    )()
    from paulsha_cortex.coordinator.launcher import LaunchHandle
    import os
    import sys

    class _Launcher:
        def as_commit_required(self):
            return self

        def as_read_only(self):
            return self

        def as_review_only(self, *, terminal_kind: str):
            return self

        def executor_environment(self):
            from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment

            return ExecutorEnvironment(
                name="codex-workflow", interpreter=(sys.executable,), path=os.environ.get("PATH", ""),
                home=os.path.expanduser("~"), provider_identity="codex",
            )

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(executor="codex", model_id="gpt-5", session_name=slice_id, pid=4242,
                                 log_path=str(Path(log_dir) / f"{slice_id}.jsonl"))

    monkeypatch.setattr(manager, "apply_workflow_action", lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"})
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows(
            [{"executor": "codex", "model_id": "gpt-5", "independence_domain": "openai", "capabilities": ["build"]}]
        ),
        launcher=_Launcher(),
    )
    result = executor(build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator"))

    assert "job_id" in result  # shadow 不擋派——即使這個 pool 完全沒有觀測資料（unknown）
    decision_id = quota_admission.decision_id_for(
        run_id=run.run_id, card_id=step.card, attempt_id=f"{run.run_id}:{step.card}:n0", profile_key=profile_key,
        mode="shadow",
    )
    store = quota_admission.AdmissionDecisionStore()
    decision = store.get(decision_id)
    assert decision is not None
    assert decision.mode == "shadow"
    assert decision.outcome == "admit"


def test_end_to_end_enforce_invalid_config_fails_closed_via_daemon_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _make_run(registry, tmp_path)
    dispatcher = type("D", (), {"_registry": registry, "_git_runner": None})()

    config_path = tmp_path / "quota-pools.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("PSC_QUOTA_POOLS_CONFIG", str(config_path))
    monkeypatch.setenv("PSC_QUOTA_ADMISSION_ENFORCE", "on")
    manager_daemon._QUOTA_POOLS_CONFIG_CACHE.clear()

    monkeypatch.setattr(manager, "apply_workflow_action", lambda *a, **k: {"run_id": run.run_id, "current_phase": "build"})

    def _must_not_dispatch(*args, **kwargs):
        raise AssertionError("quota-config-invalid 必須在建立任何 job 之前 fail closed")

    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher, specs_dir=str(tmp_path / "specs"), handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )
    result = executor(build_request(req_type="workflow-action", args={"action": "start"}, requested_by="operator"))

    assert "job_id" not in result
    assert result["dispatch"]["reason"] == "quota-config-invalid"
    assert registry.list_jobs() == []
    updated_run = registry.get_workflow_run(run.run_id)
    assert "needs_human" in updated_run.facets

    # 對抗審查第二輪 MAJOR（manager.py:11413）：quota-config-invalid 路徑
    # 同樣要留一筆 decision receipt 並更新 WorkflowRun.quota_admission
    # 投影——不再只標 needs_human。
    assert updated_run.quota_admission is not None
    projection = updated_run.quota_admission["builder"]
    assert projection["mode"] == "enforced"
    assert projection["outcome"] == "wait"
    store = quota_admission.AdmissionDecisionStore()
    decision = store.get(projection["decision_id"])
    assert decision is not None
    assert decision.outcome == "wait"
    assert decision.reason == "quota-config-invalid"
