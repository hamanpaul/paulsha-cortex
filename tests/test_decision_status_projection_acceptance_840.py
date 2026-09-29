"""#840 逐票驗收補齊：前兩輪（PR #1176）後仍為「部分」的條目。

producer 一律是 Manager 真派工（`manager.dispatch_workflow_card` 寫 receipt 與
`WorkflowRun.quota_admission` 指標），reader 走 `WorkflowRegistryProvider`／
`WorkModelRefresher`／`cortex work show`／`manager_daemon` status provider／
`cortex inspect status`，不手寫 receipt：

- AC1：同一份 coordinator registry 裡同 work_id、不同 repo 的兩個 run，work
  show 與 status 投影逐 repo 隔離，額度決策與等待來源（blocking reason）都不
  互相污染。
- AC2：#836 真觀測過期（TTL 已過）經 #839 producer 寫出的 receipt，投影成
  unknown／不可行，不呈現成現在可派。
- AC4：reader 跨 process 重啟、而 decision store 此時不可讀：work show 與
  inspect status 保留重啟前的 last-good 與精確 stale 原因／時間，並仍以目前
  attempt 事實判定是否已被取代。
- AC5：`wait.context` 等投影欄位的敏感值負例（帳號、token、raw prompt）。
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator import manager, manager_daemon, quota_admission
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.quota_ledger import QuotaEventLedger
from paulsha_cortex.coordinator.quota_reservation import QuotaReservationAuthority
from paulsha_cortex.coordinator.quota_shadow import QuotaShadowService
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.monitor import decision_projection as dp
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_api import WorkModelRefresher, WorkReadModelStore
from paulsha_cortex.monitor.work_models import WorkItem
from paulsha_cortex.monitor.work_snapshot import WorkSnapshot, WorkSnapshotStore

from test_quota_admission_acceptance_839 import (
    _CODEX_ROW,
    _RecordingLauncher,
    _dispatcher,
    _freeze,
    _identity_binding,
    _init_worktree,
    _observe,
    _pool_descriptor,
)

_UPDATED_AT = "2026-09-29T00:00:00Z"


def _run(registry: JobRegistry, root: Path, *, repo: str, work_id: str):
    return registry._manager_create_workflow_run(
        work_id=work_id, repo=repo,
        claim_key=f"{repo}/{work_id}/0", source_revision="a" * 64,
        workspace_root=str(root), combo="feature-oneshot", current_phase="build",
        steps=(WorkflowStep(
            phase="build", persona="builder", card="subagent-build", executor=None, model=None,
            domain=None, inputs=(), outputs=(), gate_result="pending",
        ),),
        issue_refs=(), openspec_refs=(), facets=(), gate_status="running",
    )


def _dispatch_context(
    root: Path, store: quota_admission.AdmissionDecisionStore, descriptor, *, remaining: str,
    now_ms: int, enforce: bool = True, ttl_ms: int = 60_000,
) -> quota_admission.DispatchContext:
    shadow = QuotaShadowService(QuotaEventLedger(root / f"quota-events-{descriptor.pool_id}.jsonl"))
    _observe(shadow, descriptor, remaining, now_ms=now_ms, ttl_ms=ttl_ms)
    return quota_admission.DispatchContext(
        authority=QuotaReservationAuthority(root / "reservations.jsonl"),
        store=store, shadow=shadow, descriptors=(descriptor,), unit_catalog=(),
        bindings=(_identity_binding(descriptor, executor="codex", model_id="gpt-primary"),),
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"} if enforce else {},
        config_revision="acceptance-840-v1",
    )


def _dispatch(registry, run, worktree: Path, coordinator: Path, context, launch_calls: list[str]):
    return manager.dispatch_workflow_card(
        _dispatcher(registry, worktree), run=registry.get_workflow_run(run.run_id),
        identities=IdentityRegistry.from_rows([_CODEX_ROW]),
        launcher_factory=lambda identity: _RecordingLauncher(identity.executor, identity.model_id, launch_calls),
        coordinator_root=coordinator, quota_admission_context=context,
    )


def _work_item(run) -> WorkItem:
    return WorkItem(
        work_id=run.work_id, repo=run.repo, title=run.work_id, state="ongoing",
        phase=run.current_phase, facets=run.facets, sources=(), next_actions=(),
        workflow_run_id=run.run_id, updated_at=_UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# AC1：跨 repo 真 registry fixture
# ---------------------------------------------------------------------------


def test_ac1_same_work_id_in_two_repos_real_registry_projections_stay_repo_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from paulsha_cortex import cli

    now_ms = int(time.time() * 1000)
    _freeze(monkeypatch, now_ms)
    coordinator = tmp_path / "coordinator"
    state = coordinator / "jobs.json"
    registry = JobRegistry(state_path=state)
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    shared_work_id = "shared-work"
    acme = _run(registry, tmp_path / "acme", repo="example/acme", work_id=shared_work_id)
    other = _run(registry, tmp_path / "other", repo="example/other", work_id=shared_work_id)
    launch_calls: list[str] = []
    # 同一份 decision store、同一份 registry：acme 額度充足被 admit 並派出 job，
    # other 額度耗盡停在 quota wait（needs_human）。
    admitted = _dispatch(
        registry, acme, worktree, coordinator,
        _dispatch_context(tmp_path / "acme", store, _pool_descriptor(account="acct-acme", pool="pool-acme"),
                          remaining="5", now_ms=now_ms),
        launch_calls,
    )
    waited = _dispatch(
        registry, other, worktree, coordinator,
        _dispatch_context(tmp_path / "other", store, _pool_descriptor(account="acct-other", pool="pool-other"),
                          remaining="0", now_ms=now_ms),
        launch_calls,
    )
    assert "job_id" in admitted and waited["reason"] == "quota-admission-insufficient"
    assert launch_calls == ["codex/gpt-primary"]

    scans = {
        repo: WorkflowRegistryProvider(repo, state_path=state, quota_decision_store=store).scan()
        for repo in ("example/acme", "example/other")
    }
    acme_projection = scans["example/acme"].observations["quota_decisions"][shared_work_id]
    other_projection = scans["example/other"].observations["quota_decisions"][shared_work_id]
    assert acme_projection["run_id"] == acme.run_id
    assert other_projection["run_id"] == other.run_id
    assert acme_projection["personas"]["builder"]["outcome"] == "admit"
    assert acme_projection["personas"]["builder"]["available"] is True
    assert acme_projection["wait"] is None
    assert other_projection["personas"]["builder"]["outcome"] == "wait"
    assert other_projection["wait"]["reason"] == "quota-admission-insufficient"
    assert (
        acme_projection["personas"]["builder"]["decision_id"]
        != other_projection["personas"]["builder"]["decision_id"]
    )

    acme_run = registry.get_workflow_run(acme.run_id)
    other_run = registry.get_workflow_run(other.run_id)
    work_store = WorkReadModelStore(WorkSnapshot(
        sequence=1, written_at=_UPDATED_AT,
        providers={snapshot.provider_id: snapshot for snapshot in scans.values()},
        work_items=(_work_item(acme_run), _work_item(other_run)), source_owners={}, exclusions=(),
    ))

    class _WorkClient:
        def request(self, request):
            return {"ok": True, "data": work_store.get_work_item(request["work_id"], repo=request.get("repo"))}

    rendered = {}
    for repo in ("example/acme", "example/other"):
        assert cli._work_read_main(
            ["work", "show", shared_work_id, "--repo", repo, "--json"], work_client=_WorkClient(),
        ) == 0
        rendered[repo] = json.loads(capsys.readouterr().out)

    assert rendered["example/acme"]["quota_decision"] == acme_projection
    assert rendered["example/other"]["quota_decision"] == other_projection
    # 額度等待來源（#527 blocking reason）同樣以 exact (repo, work_id) 取值：acme
    # 沒有 needs_human，不得借到 other 的 quota wait 理由。
    assert "blocking_reason" not in rendered["example/acme"]
    assert rendered["example/other"]["blocking_reason"]["run_id"] == other.run_id
    assert rendered["example/other"]["blocking_reason"]["reason"] == "quota-admission-insufficient"
    # inspect status 的 run-scoped 投影與 work show 對同一 repo 逐欄一致。
    status_other = manager.workflow_status_entry(registry, other_run, quota_decision_store=store)
    assert {"run_id": other.run_id, **status_other["quota_decision"]} == other_projection


# ---------------------------------------------------------------------------
# AC2：真 producer 的 expired observation 串到投影
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enforce", (False, True), ids=("shadow", "enforce"))
def test_ac2_expired_observation_from_real_producer_is_projected_unknown_not_dispatchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce: bool,
) -> None:
    now_ms = int(time.time() * 1000)
    clock = _freeze(monkeypatch, now_ms)
    coordinator = tmp_path / "coordinator"
    state = coordinator / "jobs.json"
    registry = JobRegistry(state_path=state)
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _run(registry, tmp_path, repo="example/acme", work_id="expired-work")
    # 觀測本身額度充足，但 TTL 60 秒；producer 在 61 秒後才派工——#836 投影
    # 必須把它判成 stale／unknown，而不是沿用過期的 50。
    context = _dispatch_context(
        tmp_path, store, _pool_descriptor(account="acct-expired", pool="pool-expired"),
        remaining="50", now_ms=now_ms, enforce=enforce, ttl_ms=60_000,
    )
    clock[0] = now_ms + 61_000
    launch_calls: list[str] = []
    result = _dispatch(registry, run, worktree, coordinator, context, launch_calls)

    decision_id = registry.get_workflow_run(run.run_id).quota_admission["builder"]["decision_id"]
    receipt = store.get(decision_id)
    assert receipt.policy_config_revision == "acceptance-840-v1"
    if enforce:
        assert result["reason"] == "quota-admission-insufficient"
        assert launch_calls == [] and registry.list_jobs() == []
        # 全數候選被拒的 wait receipt 沒有「選中候選」；過期觀測的證據落在
        # 被排除候選的精確理由上。
        assert receipt.selected is None
        assert [item["exclusion_reason"] for item in receipt.excluded] == ["unknown-remaining-quota"]
    else:
        # shadow 不擋派工，但 receipt 如實記下「選中的候選當下額度 unknown、不可行」。
        assert result["executor"] == "codex"
        assert receipt.selected_observation_state == "unknown"
        assert receipt.selected_feasible is False

    run = registry.get_workflow_run(run.run_id)
    status_projection = manager.workflow_status_entry(registry, run, quota_decision_store=store)["quota_decision"]
    show_projection = WorkflowRegistryProvider(
        "example/acme", state_path=state, quota_decision_store=store,
    ).scan().observations["quota_decisions"]["expired-work"]
    assert {"run_id": run.run_id, **status_projection} == show_projection
    persona = show_projection["personas"]["builder"]
    assert persona["available"] is True
    assert persona["classification"]["observation"] == "unknown"
    assert persona["policy_config_revision"] == "acceptance-840-v1"
    if enforce:
        assert persona["outcome"] == "wait"
        assert persona["excluded"] == [
            {"executor": "codex", "model_id": "gpt-primary", "exclusion_reason": "unknown-remaining-quota"},
        ]
        assert show_projection["wait"]["reason"] == "quota-admission-insufficient"
        assert show_projection["wait"]["retry_eligible"] is True
    else:
        assert persona["mode"] == "shadow"
        assert persona["outcome"] == "admit"
        assert persona["selected_feasible"] is False


# ---------------------------------------------------------------------------
# AC4：reader 跨 process 重啟＋decision store 不可讀的 last-good
# ---------------------------------------------------------------------------


def _ac4_status_payload(provider) -> dict:
    """比照 `manager_daemon` 主迴圈寫 status.json 的形狀。"""
    projection = provider()
    payload = contract.build_status(
        ready=projection["ready"], in_flight=projection["in_flight"], recent_done=projection["recent_done"],
        daemon={"pid": None, "last_tick_at": _UPDATED_AT, "idle": True}, updated_at=_UPDATED_AT,
    )
    payload.update({key: value for key, value in projection.items() if key not in {"ready", "in_flight", "recent_done"}})
    return payload


def _ac4_readers(tmp_path: Path, state: Path, decisions: Path, repo_root: Path):
    refresher_read_store = WorkReadModelStore.empty()
    durable = WorkSnapshotStore(tmp_path / "monitor" / "work-items.snapshot.json")
    bootstrapped = durable.load_for_bootstrap() if durable.path.exists() else None
    if bootstrapped is not None:
        refresher_read_store = WorkReadModelStore(bootstrapped)
    # production 預設：Monitor refresher 與 daemon status provider 都讀預設路徑
    # 的 decision store（與 Manager 寫入的是同一份）。
    assert quota_admission.AdmissionDecisionStore().path == decisions
    refresher = WorkModelRefresher(durable_store=durable, read_store=refresher_read_store)
    status_provider = manager_daemon.build_runtime_status_provider(
        registry=JobRegistry(state_path=state), specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"), scan_specs_fn=lambda _: [],
        ready_units_fn=lambda _metas, _predicate: [], now_fn=lambda: _UPDATED_AT,
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo_root))
    return refresher, refresher_read_store, status_provider, project


def _ac4_first_reader_process(tmp_path: str, state: str, decisions: str, repo_root: str, now_ms: int, result_queue) -> None:
    """第一個 reader process：Monitor refresh（寫 durable snapshot）＋daemon
    status tick（寫 status.json），store 可讀——留下 last-good 後結束。"""
    time.time = lambda: now_ms / 1000
    try:
        refresher, read_store, status_provider, project = _ac4_readers(
            Path(tmp_path), Path(state), Path(decisions), Path(repo_root),
        )
        refresher.refresh((project,), include_github=False)
        contract.atomic_write_json(constants.status_path(), _ac4_status_payload(status_provider))
        result_queue.put(("ok", read_store.get_work_item("work", repo="example/acme")["quota_decision"]))
    except BaseException as exc:  # noqa: BLE001 - 回報給父 process
        result_queue.put(("error", repr(exc)))


@pytest.mark.parametrize("attempt_state", ("still-current", "superseded-while-down"))
def test_ac4_reader_restart_across_processes_keeps_last_good_when_store_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempt_state: str,
) -> None:
    now_ms = int(time.time() * 1000)
    clock = _freeze(monkeypatch, now_ms)
    # Monitor 的 workflow provider 讀預設 coordinator root 的 registry；兩個
    # reader process 都由環境變數解析到同一份（子 process 繼承環境）。
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(tmp_path / "coordinator"))
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path / "control"))
    repo_root = tmp_path / "repo"
    spec = repo_root / "docs/superpowers/specs/work.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nwork_item: work\n---\n# work\n", encoding="utf-8")
    coordinator = tmp_path / "coordinator"
    state = coordinator / "jobs.json"
    registry = JobRegistry(state_path=state)
    store = quota_admission.AdmissionDecisionStore()
    decisions = store.path
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _run(registry, repo_root, repo="example/acme", work_id="work")
    result = _dispatch(
        registry, run, worktree, coordinator,
        _dispatch_context(tmp_path, store, _pool_descriptor(account="acct-restart", pool="pool-restart"),
                          remaining="0", now_ms=now_ms),
        [],
    )
    assert result["reason"] == "quota-admission-insufficient"
    decision_id = registry.get_workflow_run(run.run_id).quota_admission["builder"]["decision_id"]

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    first = context.Process(
        target=_ac4_first_reader_process,
        args=(str(tmp_path), str(state), str(decisions), str(repo_root), now_ms, result_queue),
    )
    first.start()
    status, first_projection = result_queue.get(timeout=120)
    first.join(timeout=60)
    assert status == "ok", first_projection
    assert first.exitcode == 0
    first_persona = first_projection["personas"]["builder"]
    assert (first_persona["available"], first_persona["stale"], first_persona["decision_id"]) == (True, False, decision_id)

    # reader 已經結束；此刻 decision store 變成不可讀（權限被放寬 → fail closed）。
    if attempt_state == "superseded-while-down":
        # 停機期間 operator 以 retry-card 清掉這個 quota wait（needs_human 換成別的
        # 理由）：重啟後就算沒有新 receipt，也不得把舊 attempt 的 wait 當成目前決策。
        registry._manager_update_workflow_run(
            run.run_id,
            needs_human_reason=diagnostic_reason(
                "operator-retry-pending", "fixture: operator 已重置卡片",
                source="test_decision_status_projection_acceptance_840", run_id=run.run_id,
            ),
        )
    clock[0] = now_ms + 5_000
    registry_bytes = state.read_bytes()
    decisions.chmod(0o660)
    try:
        refresher, read_store, status_provider, project = _ac4_readers(tmp_path, state, decisions, repo_root)
        refresher.refresh((project,), include_github=False)
        status_payload = _ac4_status_payload(status_provider)
    finally:
        decisions.chmod(0o600)

    show = read_store.get_work_item("work", repo="example/acme")["quota_decision"]
    show_persona = show["personas"]["builder"]
    status_entry = next(row for row in status_payload["attention"] if row.get("run_id") == run.run_id)
    status_persona = status_entry["quota_decision"]["personas"]["builder"]
    assert show_persona["decision_id"] == status_persona["decision_id"] == decision_id
    if attempt_state == "still-current":
        # 跨 process 的 last-good：沿用重啟前最後一次成功讀到的決策內容，並標
        # stale＋精確原因＋last-good 的時間，不是單純「這次讀不到」。
        for persona in (show_persona, status_persona):
            assert persona["available"] is True
            assert persona["stale"] is True
            assert persona["stale_reason"].startswith("decision-store-read-failed:")
            assert persona["stale_since_ms"] == first_projection["as_of_ms"]
            for field in ("mode", "outcome", "excluded", "classification", "policy_config_revision", "retry_eligible"):
                assert persona[field] == first_persona[field]
        assert show["wait"]["retry_eligible"] is True
        assert status_entry["quota_decision"]["wait"] == show["wait"]
    else:
        assert show_persona["available"] is False
        assert show_persona["gap_reason"] == "quota-decision-attempt-superseded"
        assert show_persona["mismatch_reason"] == "wait-cleared-since-decision"
        assert "mode" not in show_persona and "outcome" not in show_persona
        assert show["wait"] is None
        assert status_persona == show_persona
    # reader 重啟與讀取失敗都不回寫 registry。
    assert state.read_bytes() == registry_bytes


# ---------------------------------------------------------------------------
# AC5：wait.context 等投影欄位的敏感值負例
# ---------------------------------------------------------------------------


_SECRETS = ("acct-secret-840", "sk-live-secret-token", "raw prompt: customer secret")


def test_ac5_wait_context_and_real_producer_projection_do_not_leak_account_token_or_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)
    _freeze(monkeypatch, now_ms)
    coordinator = tmp_path / "coordinator"
    state = coordinator / "jobs.json"
    registry = JobRegistry(state_path=state)
    store = quota_admission.AdmissionDecisionStore(tmp_path / "decisions.jsonl")
    worktree = tmp_path / "wt"
    _init_worktree(worktree)
    run = _run(registry, tmp_path, repo="example/acme", work_id="secret-work")
    # 真 producer：pool 所屬帳號是敏感識別；Manager 派工只該把 allowlist 內的
    # 候選身分與排除理由寫進投影。
    result = _dispatch(
        registry, run, worktree, coordinator,
        _dispatch_context(tmp_path, store, _pool_descriptor(account=_SECRETS[0], pool="pool-secret"),
                          remaining="0", now_ms=now_ms),
        [],
    )
    assert result["reason"] == "quota-admission-insufficient"
    produced = registry.get_workflow_run(run.run_id).needs_human_reason
    # 同一個 quota wait 理由，若 context 夾帶帳號／token／raw prompt（例如未來
    # producer 或手改 registry），投影只能帶出 allowlist 內的 context key。
    registry._manager_update_workflow_run(
        run.run_id,
        needs_human_reason=diagnostic_reason(
            produced["reason"], produced["detail"], source=produced["source"],
            **produced.get("context", {}),
            account_id=_SECRETS[0], api_token=_SECRETS[1], raw_prompt=_SECRETS[2],
        ),
    )
    run = registry.get_workflow_run(run.run_id)

    status_projection = manager.workflow_status_entry(registry, run, quota_decision_store=store)["quota_decision"]
    show_projection = WorkflowRegistryProvider(
        "example/acme", state_path=state, quota_decision_store=store,
    ).scan().observations["quota_decisions"]["secret-work"]

    for projection in (status_projection, show_projection):
        wait = projection["wait"]
        assert wait["reason"] == "quota-admission-insufficient"
        assert wait["context"] == {
            key: value for key, value in produced["context"].items()
            if key in {"run_id", "work_id", "card", "attempted_candidates"}
        }
        assert wait["context"]["run_id"] == run.run_id
        rendered = json.dumps(projection, ensure_ascii=False)
        for secret in _SECRETS:
            assert secret not in rendered
        for key in ("account_id", "api_token", "raw_prompt"):
            assert key not in rendered
