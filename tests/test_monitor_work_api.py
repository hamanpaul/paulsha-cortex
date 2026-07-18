from __future__ import annotations

import json
import socket
import threading
from unittest import mock

from paulsha_cortex.monitor.config import MonitorConfig
from paulsha_cortex.monitor.server import MonitorServer
from paulsha_cortex.monitor.server import _Subscriber
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.snapshot import ChangeEvent, SnapshotStore
from paulsha_cortex.monitor.work_api import (
    WorkChangeEvent,
    WorkModelRefresher,
    WorkReadModelStore,
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
        data += sock.recv(4096)
    return json.loads(data)


def test_socket_work_item_read_apis_and_subscription_preserve_legacy(tmp_path):
    project_store = SnapshotStore(config=MonitorConfig(workspaces=()))
    work_store = WorkReadModelStore(_snapshot(_item("active", "ongoing")))
    socket_path = tmp_path / "monitor.sock"
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


def test_server_stopped_before_start_never_signals_ready(tmp_path):
    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        socket_path=tmp_path / "monitor.sock",
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


def test_old_server_teardown_does_not_unlink_replacement_socket(tmp_path):
    socket_path = tmp_path / "monitor.sock"
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


def test_work_subscription_extension_preserves_legacy_queue_full_replacement(tmp_path):
    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        work_store=WorkReadModelStore.empty(),
        socket_path=tmp_path / "unused.sock",
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
