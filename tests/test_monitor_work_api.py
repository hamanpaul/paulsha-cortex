from __future__ import annotations

import json
import socket
import threading
from unittest import mock

import pytest

from sandbox_support import requires_af_unix_bind

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import quota_admission as admission
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.monitor.config import MonitorConfig
from paulsha_cortex.monitor.decision_projection import DecisionReadCache
from paulsha_cortex.monitor.server import MonitorServer
from paulsha_cortex.monitor.server import _Subscriber
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.snapshot import ChangeEvent, SnapshotStore
from paulsha_cortex.monitor.work_api import (
    WorkChangeEvent,
    WorkModelRefresher,
    WorkReadModelStore,
    _workflow_linked_pr_numbers,
)
from paulsha_cortex.monitor.work_models import ProviderSnapshot, WorkItem
from paulsha_cortex.monitor.work_snapshot import WorkSnapshot
from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore


NOW = "2026-07-17T10:00:00Z"


def _item(work_id: str, state: str, *, repo="example/acme", facets=()):
    return WorkItem(
        work_id=work_id,
        repo=repo,
        title=work_id.replace("-", " "),
        state=state,
        phase="plan" if state == "ongoing" else None,
        facets=facets,
        sources=(),
        next_actions=("start",) if state == "todo" else (),
        workflow_run_id="run-1" if state == "ongoing" else None,
        updated_at=NOW,
    )


def _snapshot(*items, sequence=7, providers=None):
    return WorkSnapshot(
        sequence=sequence,
        written_at=NOW,
        providers=providers or {},
        work_items=tuple(items),
        source_owners={},
        exclusions=(),
    )


def test_read_model_list_defaults_hide_done_and_sorts():
    store = WorkReadModelStore(
        _snapshot(_item("z-done", "done"), _item("b-todo", "todo"), _item("a-topic", "topic"))
    )

    envelope = store.list_work_items()

    assert envelope["schema"] == "cortex-work/v1"
    assert envelope["sequence"] == 7
    assert [item["work_id"] for item in envelope["items"]] == ["a-topic", "b-todo"]
    assert envelope["degraded"] is False


def test_terminal_pr_filter_comes_only_from_canonical_workflow_links():
    provider = ProviderSnapshot(
        provider_id="workflow:example/acme",
        status="ok",
        last_attempt_at=NOW,
        last_success_at=NOW,
        revision="workflow-1",
        diagnostics=(),
        sources=(),
        observations={
            "workflow_links": {
                "github_pr:example/acme#54": "terminal-canary",
                "github_pr:example/acme#7": "older-work",
                "github_pr:example/other#99": "other-repo",
                "github_issue:example/acme#31": "terminal-canary",
            }
        },
    )

    assert _workflow_linked_pr_numbers(provider, repo="example/acme") == (7, 54)


def test_read_model_filters_repo_and_normalizes_on_going():
    store = WorkReadModelStore(
        _snapshot(
            _item("active", "ongoing"),
            _item("other", "todo", repo="example/other"),
        )
    )
    envelope = store.list_work_items(repo="example/acme", states=("on-going",))
    assert [item["work_id"] for item in envelope["items"]] == ["active"]
    assert envelope["items"][0]["state"] == "on-going"


def test_hard_gates_are_repo_scoped_while_fleet_health_remains_visible():
    healthy = ProviderSnapshot(
        provider_id="github:example/acme",
        status="ok",
        last_attempt_at=NOW,
        last_success_at=NOW,
        revision="github:healthy",
        diagnostics=(),
        sources=(),
    )
    degraded = ProviderSnapshot(
        provider_id="github:example/other",
        status="degraded",
        last_attempt_at=NOW,
        last_success_at=None,
        revision=None,
        diagnostics=("github:example/other stale",),
        sources=(),
    )
    store = WorkReadModelStore(
        _snapshot(
            _item("healthy", "todo", repo="example/acme"),
            _item(
                "same-repo-degraded",
                "todo",
                repo="example/acme",
                facets=("degraded",),
            ),
            _item("blocked", "todo", repo="example/other"),
            providers={healthy.provider_id: healthy, degraded.provider_id: degraded},
        )
    )

    healthy_envelope = store.get_work_item("healthy", repo="example/acme")
    blocked_envelope = store.list_work_items(repo="example/other")

    assert healthy_envelope["hard_gates"] == {
        "auto_claim": True,
        "merge": True,
        "reasons": [],
    }
    assert healthy_envelope["fleet_health"]["degraded"] is True
    assert store.list_work_items(repo="example/acme")["hard_gates"]["merge"] is False
    assert blocked_envelope["hard_gates"]["auto_claim"] is False
    assert blocked_envelope["hard_gates"]["reasons"] == [
        "github:example/other stale"
    ]


def test_quota_decision_is_scoped_to_exact_repo_not_leaked_across_repos():
    """#840 對抗審查修復（MAJOR，work_api.py 約 301）：`_quota_decision()`
    過去只憑 provider_id 前綴是不是 ``workflow:`` 就挑第一個命中的
    ``quota_decisions[work_id]``，完全沒比對 `repo` 參數。兩個 repo 若剛好
    有同一個 `work_id`（跨 repo 不保證唯一），會把另一個 repo 的
    quota_decision 錯配過來。這裡驗證 exact (repo, work_id) 比對——同一個
    `work_id` 在兩個 repo 各自的 workflow provider 上有『不同』的
    quota_decisions 內容時，`get_work_item(work_id, repo=...)` 只回傳
    對應 repo 的那一份，不互相污染。"""
    shared_work_id = "shared-work-id"
    acme_provider = ProviderSnapshot(
        provider_id="workflow:example/acme",
        status="ok",
        last_attempt_at=NOW,
        last_success_at=NOW,
        revision="workflow-acme-1",
        diagnostics=(),
        sources=(),
        observations={
            "quota_decisions": {
                shared_work_id: {"personas": {"builder": {"decision_id": "adm:v1:acme"}}},
            }
        },
    )
    other_provider = ProviderSnapshot(
        provider_id="workflow:example/other",
        status="ok",
        last_attempt_at=NOW,
        last_success_at=NOW,
        revision="workflow-other-1",
        diagnostics=(),
        sources=(),
        observations={
            "quota_decisions": {
                shared_work_id: {"personas": {"builder": {"decision_id": "adm:v1:other"}}},
            }
        },
    )
    store = WorkReadModelStore(
        _snapshot(
            _item(shared_work_id, "ongoing", repo="example/acme"),
            _item(shared_work_id, "ongoing", repo="example/other"),
            providers={
                acme_provider.provider_id: acme_provider,
                other_provider.provider_id: other_provider,
            },
        )
    )

    acme_envelope = store.get_work_item(shared_work_id, repo="example/acme")
    other_envelope = store.get_work_item(shared_work_id, repo="example/other")

    assert (
        acme_envelope["quota_decision"]["personas"]["builder"]["decision_id"] == "adm:v1:acme"
    )
    assert (
        other_envelope["quota_decision"]["personas"]["builder"]["decision_id"] == "adm:v1:other"
    )


def test_read_model_show_and_explain_contract():
    explanation = {
        "work_id": "active",
        "authoritative_links": [],
        "inferred_signals": [],
        "competing_candidates": [],
        "exclusions": [],
        "reducer_trace": [{"rule": "active_workflow", "accepted": True}],
    }
    store = WorkReadModelStore(
        _snapshot(_item("active", "ongoing")), explanations={"active": explanation}
    )
    assert store.get_work_item("active")["item"]["state"] == "on-going"
    assert store.explain_work_item("active")["explanation"] == explanation


def test_read_model_replace_uses_next_monotonic_sequence_without_double_increment():
    store = WorkReadModelStore(_snapshot(_item("active", "todo"), sequence=7))
    replacement = _snapshot(_item("active", "ongoing"), sequence=8)

    events = store.replace(replacement)

    assert [event.sequence for event in events] == [8]
    assert store.sequence == 8


def _send(sock, payload):
    sock.sendall((json.dumps(payload) + "\n").encode())


def _recv(sock, timeout=2.0):
    sock.settimeout(timeout)
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("socket closed before newline-delimited response")
        data += chunk
    return json.loads(data)


def test_recv_fails_fast_when_socket_closes_before_newline():
    client, server = socket.socketpair()
    server.close()
    try:
        with pytest.raises(EOFError, match="before newline"):
            _recv(client)
    finally:
        client.close()


@requires_af_unix_bind
def test_socket_work_item_read_apis_and_subscription_preserve_legacy(socket_dir):
    project_store = SnapshotStore(config=MonitorConfig(workspaces=()))
    work_store = WorkReadModelStore(_snapshot(_item("active", "ongoing")))
    socket_path = socket_dir / "monitor.sock"
    server = MonitorServer(
        store=project_store, work_store=work_store, socket_path=socket_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.wait_until_ready(timeout=2.0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(client, {"kind": "list_projects"})
            assert _recv(client)["data"]["projects"] == []

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(client, {"kind": "list_work_items"})
            payload = _recv(client)
            assert payload["ok"]
            assert payload["data"]["items"][0]["work_id"] == "active"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(client, {"kind": "get_work_item", "work_id": "active"})
            assert _recv(client)["data"]["item"]["state"] == "on-going"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(client, {"kind": "explain_work_item", "work_id": "active"})
            assert _recv(client)["data"]["explanation"]["work_id"] == "active"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(client, {"kind": "subscribe_work_items", "work_ids": ["active"]})
            initial = _recv(client)
            assert initial["kind"] == "work_snapshot"
            assert initial["schema"] == "cortex-work/v1"
            assert initial["sequence"] == 7
            event = WorkChangeEvent(
                sequence=8,
                work_item=_item("active", "ongoing"),
                removed=False,
            )
            server.publish_work_events((event,))
            changed = _recv(client)
            assert changed["kind"] == "work_change"
            assert changed["schema"] == "cortex-work/v1"
            assert changed["item"]["work_id"] == "active"
    finally:
        server.stop()
        thread.join(timeout=2)


@requires_af_unix_bind
def test_work_subscription_can_scope_duplicate_work_id_by_repo(socket_dir):
    healthy = ProviderSnapshot(
        provider_id="github:example/acme",
        status="ok",
        last_attempt_at=NOW,
        last_success_at=NOW,
        revision="github:healthy",
        diagnostics=(),
        sources=(),
    )
    degraded = ProviderSnapshot(
        provider_id="github:example/other",
        status="degraded",
        last_attempt_at=NOW,
        last_success_at=None,
        revision=None,
        diagnostics=("github:example/other stale",),
        sources=(),
    )
    work_store = WorkReadModelStore(
        _snapshot(
            _item("shared", "todo", repo="example/acme"),
            _item("shared", "todo", repo="example/other"),
            providers={healthy.provider_id: healthy, degraded.provider_id: degraded},
        )
    )
    socket_path = socket_dir / "monitor.sock"
    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        work_store=work_store,
        socket_path=socket_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.wait_until_ready(timeout=2.0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            _send(
                client,
                {
                    "kind": "subscribe_work_items",
                    "repo": "example/acme",
                    "work_ids": ["shared"],
                },
            )
            initial = _recv(client)
            assert [item["repo"] for item in initial["items"]] == ["example/acme"]
            assert initial["degraded"] is False
            assert initial["hard_gates"]["auto_claim"] is True
            assert initial["fleet_health"]["degraded"] is True

            server.publish_work_events(
                (
                    WorkChangeEvent(
                        sequence=8,
                        work_item=_item("shared", "ongoing", repo="example/other"),
                        removed=False,
                    ),
                    WorkChangeEvent(
                        sequence=9,
                        work_item=_item("shared", "ongoing", repo="example/acme"),
                        removed=False,
                    ),
                )
            )
            changed = _recv(client)
            assert changed["sequence"] == 9
            assert changed["item"]["repo"] == "example/acme"
    finally:
        server.stop()
        thread.join(timeout=2)


@requires_af_unix_bind
def test_server_stopped_before_start_never_signals_ready(socket_dir):
    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        socket_path=socket_dir / "monitor.sock",
    )
    server.stop()

    with mock.patch.object(
        server._ready_event,
        "set",
        wraps=server._ready_event.set,
    ) as ready_set:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        thread.join(timeout=2)

    assert not thread.is_alive()
    ready_set.assert_not_called()
    assert not server.wait_until_ready(timeout=0)


@requires_af_unix_bind
def test_old_server_teardown_does_not_unlink_replacement_socket(socket_dir):
    socket_path = socket_dir / "monitor.sock"
    store = SnapshotStore(config=MonitorConfig(workspaces=()))
    old_server = MonitorServer(store=store, socket_path=socket_path)
    old_thread = threading.Thread(target=old_server.serve_forever, daemon=True)
    old_thread.start()
    assert old_server.wait_until_ready(timeout=2.0)

    teardown_entered = threading.Event()
    release_teardown = threading.Event()
    original_teardown = old_server._teardown

    def delayed_teardown(listener, *, unlink_socket, **kwargs):
        teardown_entered.set()
        assert release_teardown.wait(timeout=2.0)
        original_teardown(listener, unlink_socket=unlink_socket, **kwargs)

    replacement_server = MonitorServer(store=store, socket_path=socket_path)
    replacement_thread = threading.Thread(
        target=replacement_server.serve_forever,
        daemon=True,
    )
    try:
        with mock.patch.object(old_server, "_teardown", side_effect=delayed_teardown):
            old_server.stop()
            assert teardown_entered.wait(timeout=2.0)
            replacement_thread.start()
            assert replacement_server.wait_until_ready(timeout=2.0)
            release_teardown.set()
            old_thread.join(timeout=2.0)

        assert socket_path.exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
    finally:
        release_teardown.set()
        replacement_server.stop()
        replacement_thread.join(timeout=2.0)
        old_server.stop()
        old_thread.join(timeout=2.0)


@requires_af_unix_bind
def test_work_subscription_extension_preserves_legacy_queue_full_replacement(socket_dir):
    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        work_store=WorkReadModelStore.empty(),
        socket_path=socket_dir / "unused.sock",
    )
    subscriber = _Subscriber(projects=None)
    for index in range(subscriber.queue.maxsize):
        subscriber.queue.put_nowait({"sequence": index})
    server._subscribers.append(subscriber)
    state = ProjectState(project_id="project", workspace="ws", path="/tmp/project")

    server.publish_events((ChangeEvent("project", 9999, state),))

    newest = None
    while not subscriber.queue.empty():
        newest = subscriber.queue.get_nowait()
    assert newest["sequence"] == 9999


def test_refresher_projects_local_provider_and_freezes_on_collision(tmp_path):
    repo = tmp_path / "repo"
    spec = repo / "docs/superpowers/specs/work.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nwork_item: work\n---\n# work\n", encoding="utf-8")
    durable = WorkSnapshotStore(tmp_path / "state/work-items.snapshot.json")
    read_store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(durable_store=durable, read_store=read_store)
    project = ProjectState(
        project_id="example/acme", workspace="ws", path=str(repo)
    )

    first_events = refresher.refresh((project,), include_github=False)

    assert first_events
    assert read_store.get_work_item("work")["item"]["state"] == "todo"
    assert durable.load().work_items[0].work_id == "work"

    active = repo / "openspec/changes/duplicate/proposal.md"
    archived = repo / "openspec/changes/archive/2026-07-17-duplicate/proposal.md"
    active.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    active.write_text("# active\n", encoding="utf-8")
    archived.write_text("# archive\n", encoding="utf-8")

    refresher.refresh((project,), include_github=False)

    frozen = read_store.get_work_item("work")["item"]
    assert frozen["state"] == "todo"
    assert frozen["facets"] == ["degraded"]
    provider = durable.load().providers["repo:example/acme"]
    assert provider.status == "degraded"
    assert any(source.ref == "docs/superpowers/specs/work.md" for source in provider.sources)


def test_refresher_prunes_provider_state_after_authoritative_project_removal(tmp_path):
    repo = tmp_path / "repo"
    spec = repo / "docs/superpowers/specs/work.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nwork_item: work\n---\n# work\n", encoding="utf-8")
    durable = WorkSnapshotStore(tmp_path / "state/work-items.snapshot.json")
    read_store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(durable_store=durable, read_store=read_store)
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))
    refresher.refresh((project,), include_github=False)

    events = refresher.refresh((), include_github=False)

    assert any(event.removed and event.work_item.work_id == "work" for event in events)
    assert durable.load().providers == {}
    assert read_store.list_work_items(include_done=True)["items"] == []


def test_refresher_quota_decision_cache_survives_across_refresh_ticks_for_last_good_stale(
    tmp_path,
):
    """#840 對抗審查修復第二輪（MAJOR，work_api.py 約 594）：
    `WorkModelRefresher.refresh()` 每輪經 `workflow_provider_factory(repo)`
    重建全新的 `WorkflowRegistryProvider`——provider 內部的 `DecisionReadCache`
    過去無法跨輪存活：第一輪成功讀到 decision，第二輪 `decisions.jsonl`
    權限錯誤／損毀時，`cortex work show` 只剩 `available=false`／
    `stale_reason`，遺失第一輪的 last-good ``mode``／``selected``。這裡驗證
    refresher 兩輪 ``refresh()``：可讀→不可讀，`get_work_item()` 仍顯示
    last-good 的 ``mode``／``selected`` 並帶精確 stale 原因；同時斷言
    `inspect status`（`manager.workflow_status_entry`）在同一份 registry／
    store 狀態下算出與 work show 一致的 ``decision_id``／``mode``／
    ``selected``（各自持有獨立的 `DecisionReadCache`，模擬兩個真實呈現面各自
    的快取生命週期，而非共用同一個實例）。"""

    repo = tmp_path / "repo"
    spec = repo / "docs/superpowers/specs/work.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nwork_item: work\n---\n# work\n", encoding="utf-8")

    registry = JobRegistry()
    # #840 對抗審查第三輪（MAJOR）：步卡 executor／model 必須對齊下面 admit
    # 決策的 `selected`——`WorkflowRegistryProvider.scan()` 現在會比對兩者
    # 是否相符（見 `decision_projection._attempt_mismatch_reason`），不一致
    # 會被判成『目前 attempt 尚無決策』，不是這裡要驗證的 last-good 快取。
    step = WorkflowStep(
        phase="build", persona="builder", card="build-card",
        executor="codex", model="gpt-5.3-codex", domain="test-domain",
        inputs=(), outputs=(), gate_result="pending",
    )
    run = registry._manager_create_workflow_run(
        work_id="work", repo="example/acme", claim_key="example/acme/work/0",
        source_revision="a" * 64, workspace_root=str(repo),
        combo="feature-oneshot", current_phase="build",
        steps=(step,), attempts={"build": 1}, facets=(), gate_status="running",
    )
    store = admission.AdmissionDecisionStore()
    decision = admission.AdmissionDecision(
        decision_id="adm:v1:" + "9" * 64, run_id=run.run_id, card_id="build-card",
        attempt_id="attempt-0", profile_key="epk:v1:resolved:" + "a" * 64,
        mode="shadow", outcome="admit", policy_version=admission.ADMISSION_POLICY_VERSION,
        observation_version="shadow-projection:" + "b" * 16,
        demand_version=admission.DEMAND_FIXTURE_VERSION, qualification_version="not-enforced",
        generated_at_ms=1_900_000_000_000,
        selected={"executor": "codex", "model_id": "gpt-5.3-codex"},
    )
    store.record(decision)
    registry._manager_update_workflow_run(
        run.run_id,
        quota_admission={
            "builder": {"decision_id": decision.decision_id, "mode": "shadow", "outcome": "admit"},
        },
    )
    run = registry.get_workflow_run(run.run_id)

    durable = WorkSnapshotStore(tmp_path / "state/work-items.snapshot.json")
    read_store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(durable_store=durable, read_store=read_store)
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))
    inspect_cache = DecisionReadCache()

    refresher.refresh((project,), include_github=False)
    first_work_show = read_store.get_work_item("work", repo="example/acme")
    first_persona = first_work_show["quota_decision"]["personas"]["builder"]
    assert first_persona["stale"] is False
    assert first_persona["decision_id"] == decision.decision_id
    assert first_persona["mode"] == "shadow"

    first_status = manager.workflow_status_entry(
        registry, run, quota_decision_store=store, quota_decision_cache=inspect_cache,
    )
    first_status_persona = first_status["quota_decision"]["personas"]["builder"]
    assert first_status_persona["stale"] is False
    assert first_status_persona["decision_id"] == decision.decision_id

    # 損毀 store（破壞群組權限位——`AdmissionDecisionStore._check_file` 會 fail
    # closed），模擬第二輪讀取失敗。
    store.path.chmod(0o660)
    try:
        refresher.refresh((project,), include_github=False)
        second_status = manager.workflow_status_entry(
            registry, run, quota_decision_store=store, quota_decision_cache=inspect_cache,
        )
    finally:
        store.path.chmod(0o600)

    second_work_show = read_store.get_work_item("work", repo="example/acme")
    second_persona = second_work_show["quota_decision"]["personas"]["builder"]

    # 修好之前：provider 隨每輪 refresh() 重建、cache 隨之整個消失，這裡
    # `stale` 會是 `False`／`available` 會是 `False`，`mode`／`selected` 直接
    # 不見——不是 last-good。
    assert second_persona["stale"] is True
    assert second_persona["stale_reason"].startswith("decision-store-read-failed:")
    assert second_persona["decision_id"] == decision.decision_id
    assert second_persona["mode"] == "shadow"
    assert second_persona["selected"] == {"executor": "codex", "model_id": "gpt-5.3-codex"}

    second_status_persona = second_status["quota_decision"]["personas"]["builder"]
    assert second_status_persona["stale"] is True
    assert second_status_persona["stale_reason"].startswith("decision-store-read-failed:")

    # `cortex inspect status` 與 `cortex work show` 在同一次損毀狀態下必須算出
    # 一致的 decision／mode／selected——即使各自持有獨立的 `DecisionReadCache`
    # 實例（兩個呈現面本來就是各自的呼叫端，不共用同一個 cache 物件）。
    assert second_status_persona["decision_id"] == second_persona["decision_id"]
    assert second_status_persona["mode"] == second_persona["mode"]
    assert second_status_persona["selected"] == second_persona["selected"]
    assert second_status_persona["outcome"] == second_persona["outcome"]
