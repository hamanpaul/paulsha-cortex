"""RED coverage for the #879 filesystem-event refresh convoy.

The service must turn a filesystem event into bounded refresh work.  In
particular, the event callback itself must not create one refresh thread per
event, and a stop fence must reject late events.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

from paulsha_cortex.monitor.config import MonitorConfig, WorkspaceConfig
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.service import ProjectMonitorService
from paulsha_cortex.monitor.watcher import StubWatcher
from paulsha_cortex.monitor.work_api import WorkReadModelStore


class _FakeStore:
    def __init__(self, project: Path) -> None:
        self._state = ProjectState(
            project_id="project-a",
            workspace="workspace",
            path=str(project),
        )
        self._lock = threading.Lock()
        self.full_refresh_calls = 0
        self.project_refresh_calls = 0
        self.project_refresh_ids: list[str] = []
        self.refresh_done = threading.Event()

    def load(self) -> tuple[()]:
        return ()

    def current_snapshot(self) -> tuple[ProjectState, ...]:
        return (self._state,)

    def refresh(self) -> tuple[()]:
        with self._lock:
            self.full_refresh_calls += 1
        return ()

    def refresh_project(self, project_id: str) -> None:
        with self._lock:
            self.project_refresh_calls += 1
            self.project_refresh_ids.append(project_id)
        self.refresh_done.set()
        return None


class _FakeServer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def serve_forever(self) -> None:
        self.started.set()
        self.stopped.wait(timeout=3.0)

    def stop(self) -> None:
        self.stopped.set()

    def publish_events(self, _events) -> None:
        return None

    def publish_work_events(self, _events) -> None:
        return None


class _FakeWorkRefresher:
    def __init__(self) -> None:
        self.refresh_calls = 0

    def refresh(self, _snapshot, *, include_github: bool):
        del include_github
        self.refresh_calls += 1
        return ()


def _make_service(
    tmp_path: Path,
    *,
    watch_debounce_ms: int = 40,
) -> tuple[ProjectMonitorService, _FakeStore, _FakeServer, Path, Path]:
    workspace = tmp_path / "workspace"
    project = workspace / "project-a"
    project.mkdir(parents=True)
    store = _FakeStore(project)
    server = _FakeServer()
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="workspace"),),
        watch_debounce_ms=watch_debounce_ms,
        socket_path=tmp_path / "run" / "monitor.sock",
    )
    service = ProjectMonitorService(
        config=config,
        watcher=StubWatcher(),
        store=store,
        server=server,
        work_store=WorkReadModelStore.empty(),
        work_refresher=_FakeWorkRefresher(),
    )
    service._project_roots = {"project-a": project}
    return service, store, server, workspace, project


def _run_service(service: ProjectMonitorService, server: _FakeServer) -> threading.Thread:
    # The periodic loops are outside this card's event-convoy contract.  Keep
    # them out of the fixture so thread-count assertions observe only refresh
    # work and the service lifecycle thread.
    service._start_poll_thread = lambda: None  # type: ignore[method-assign]
    service._start_rescan_thread = lambda: None  # type: ignore[method-assign]
    service._start_github_thread = lambda: None  # type: ignore[method-assign]
    runner = threading.Thread(target=service.run_forever, daemon=True)
    runner.start()
    assert server.started.wait(timeout=1.0), "fake monitor server did not start"
    return runner


def _stop_service(service: ProjectMonitorService, runner: threading.Thread) -> None:
    service.stop()
    runner.join(timeout=2.0)
    assert not runner.is_alive(), "service lifecycle thread did not stop"


def test_burst_of_project_events_keeps_thread_count_bounded(tmp_path: Path) -> None:
    service, _store, _server, _workspace, _project = _make_service(
        tmp_path,
        watch_debounce_ms=1000,
    )
    burst_root = tmp_path / "burst-projects"
    projects = tuple(burst_root / f"project-{index}" for index in range(64))
    for burst_project in projects:
        burst_project.mkdir(parents=True)
    service._project_roots = {
        f"project-{index}": burst_project
        for index, burst_project in enumerate(projects)
    }
    baseline = threading.active_count()
    try:
        for burst_project in projects:
            service._handle_fs_event(burst_project / "changed.txt")

        # The event path may mark bounded pending work, but it must not leave a
        # Timer/refresh thread behind for every event in the burst.
        assert threading.active_count() <= baseline + 3
    finally:
        service.stop()


def test_burst_of_project_events_coalesces_to_one_refresh(tmp_path: Path) -> None:
    service, store, server, _workspace, project = _make_service(tmp_path)
    runner = _run_service(service, server)
    try:
        for _ in range(8):
            service._handle_fs_event(project / "changed.txt")

        assert store.refresh_done.wait(timeout=2.0)
        assert store.project_refresh_calls == 1
    finally:
        _stop_service(service, runner)


def test_event_callback_does_not_start_refresh_timer(tmp_path: Path) -> None:
    service, store, _server, _workspace, project = _make_service(tmp_path)
    with mock.patch("paulsha_cortex.monitor.service.threading.Timer") as timer:
        service._handle_fs_event(project / "changed.txt")

    timer.assert_not_called()
    assert store.project_refresh_calls == 0


def test_stop_rejects_late_project_event_without_starting_refresh_timer(
    tmp_path: Path,
) -> None:
    service, store, _server, _workspace, project = _make_service(tmp_path)
    service.stop()

    with mock.patch("paulsha_cortex.monitor.service.threading.Timer") as timer:
        service._handle_fs_event(project / "changed.txt")

    timer.assert_not_called()
    assert store.project_refresh_calls == 0


def test_workspace_event_preserves_full_refresh_semantics(tmp_path: Path) -> None:
    service, store, _server, workspace, _project = _make_service(tmp_path)
    service._handle_fs_event(workspace / "new-project" / "changed.txt")

    assert store.full_refresh_calls == 1
    assert store.project_refresh_calls == 0


def test_one_refresh_round_refreshes_three_projects_and_work_model_once(
    tmp_path: Path,
) -> None:
    service, store, _server, _workspace, _project = _make_service(tmp_path)
    work_refresher = service._work_refresher
    service._pending_refreshes = {
        "project-a": 0.0,
        "project-b": 0.0,
        "project-c": 0.0,
    }

    with mock.patch(
        "paulsha_cortex.monitor.service.time.monotonic",
        return_value=1.0,
    ):
        has_work, full_refresh, project_ids = service._next_refresh()

    assert has_work is True
    assert full_refresh is False
    assert project_ids == ("project-a", "project-b", "project-c")
    assert service._pending_refreshes == {}

    service._run_refresh_round(full_refresh=full_refresh, project_ids=project_ids)

    assert store.project_refresh_ids == ["project-a", "project-b", "project-c"]
    assert work_refresher.refresh_calls == 1


def test_thread_count_warning_is_throttled_for_sixty_seconds(tmp_path: Path) -> None:
    service, _store, _server, _workspace, _project = _make_service(tmp_path)
    clock = iter((0.0, 30.0, 61.0))
    with (
        mock.patch(
            "paulsha_cortex.monitor.service.threading.active_count",
            return_value=999,
        ),
        mock.patch(
            "paulsha_cortex.monitor.service.time.monotonic",
            side_effect=clock,
        ),
        mock.patch("paulsha_cortex.monitor.service.logger.warning") as warning,
    ):
        service._warn_if_thread_count_high()
        service._warn_if_thread_count_high()
        service._warn_if_thread_count_high()

    assert warning.call_count == 2
