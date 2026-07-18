from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

import pytest

from paulsha_cortex.monitor.config import MonitorConfig, WorkspaceConfig, load_config
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.scanner import (
    DegradedDiagnostic,
    ProjectClassification,
    ScanResult,
    scan_workspaces_detailed,
)
from paulsha_cortex.monitor.service import ProjectMonitorService
from paulsha_cortex.monitor.snapshot import SnapshotStore
from paulsha_cortex.monitor.watcher import StubWatcher


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("poll_interval_seconds", 0),
        ("rescan_interval_seconds", -1),
        ("watch_debounce_ms", 0),
        ("poll_interval_seconds", 1.9),
        ("poll_interval_seconds", True),
    ),
)
def test_monitor_config_rejects_non_positive_intervals(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    config_path = tmp_path / "project-cortex.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            workspaces:
              - path: {tmp_path / 'workspace'}
                name: test
            monitor:
              {field}: {value}
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        load_config(config_path=config_path)


def test_refresh_preserves_last_good_state_when_scan_is_degraded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="test"),),
    )
    store = SnapshotStore(config=config)
    store.load()

    degraded = ScanResult(
        states=(),
        degraded_roots=(workspace,),
        diagnostics=(f"degraded: OSError: cannot read {workspace}",),
        degraded_diagnostics=(
            DegradedDiagnostic(
                workspace,
                "test",
                f"degraded: OSError: cannot read {workspace}",
            ),
        ),
    )
    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=degraded,
    ):
        events = store.refresh()

    state = store.get("demo")
    assert state is not None
    assert events and not events[0].removed
    scan_signal = next(signal for signal in state.source_signals if signal.kind == "scan")
    assert scan_signal.note == degraded.diagnostics[0]


def test_refresh_does_not_assign_unrelated_global_diagnostic(tmp_path: Path) -> None:
    west_root = tmp_path / "west"
    east_root = tmp_path / "east"
    for root, project_name in (
        (west_root, "west-project"),
        (east_root, "east-project"),
    ):
        project = root / project_name
        project.mkdir(parents=True)
        (project / ".paul-project.yml").write_text(
            "policy_profile: flat\n",
            encoding="utf-8",
        )
    config = MonitorConfig(
        workspaces=(
            WorkspaceConfig(path=west_root, name="west"),
            WorkspaceConfig(path=east_root, name="east"),
        ),
    )
    store = SnapshotStore(config=config)
    store.load()

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=ScanResult(
            states=(),
            degraded_roots=(west_root, east_root),
            degraded_workspaces=("west", "east"),
            diagnostics=(
                "degraded: PermissionError: west offline",
                "degraded: PermissionError: east offline",
            ),
        ),
    ):
        store.refresh()

    notes = {
        state.project_id: next(
            signal.note for signal in state.source_signals if signal.kind == "scan"
        )
        for state in store.current_snapshot()
    }
    assert notes["west-project"] == f"degraded: scan unavailable under {west_root}"
    assert notes["east-project"] == f"degraded: scan unavailable under {east_root}"


def test_refresh_uses_structured_root_diagnostic_without_prefix_collision(
    tmp_path: Path,
) -> None:
    short_root = tmp_path / "root"
    long_root = tmp_path / "root-old"
    for root, project_name in (
        (short_root, "short-project"),
        (long_root, "long-project"),
    ):
        project = root / project_name
        project.mkdir(parents=True)
        (project / ".paul-project.yml").write_text(
            "policy_profile: flat\n",
            encoding="utf-8",
        )
    store = SnapshotStore(
        config=MonitorConfig(
            workspaces=(
                WorkspaceConfig(path=short_root, name="short"),
                WorkspaceConfig(path=long_root, name="long"),
            ),
        )
    )
    store.load()
    short_diagnostic = f"degraded: PermissionError: {short_root} offline"
    long_diagnostic = f"degraded: PermissionError: {long_root} offline"

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=ScanResult(
            states=(),
            degraded_roots=(short_root, long_root),
            degraded_workspaces=("short", "long"),
            diagnostics=(long_diagnostic, short_diagnostic),
            degraded_diagnostics=(
                DegradedDiagnostic(long_root, "long", long_diagnostic),
                DegradedDiagnostic(short_root, "short", short_diagnostic),
            ),
        ),
    ):
        store.refresh()

    notes = {
        state.project_id: next(
            signal.note for signal in state.source_signals if signal.kind == "scan"
        )
        for state in store.current_snapshot()
    }
    assert notes == {
        "long-project": long_diagnostic,
        "short-project": short_diagnostic,
    }


def test_refresh_diagnostic_does_not_cross_nested_workspace_identity(
    tmp_path: Path,
) -> None:
    parent_root = tmp_path / "root"
    nested_root = parent_root / "nested"
    parent_project = parent_root / "parent-project"
    child_project = nested_root / "child-project"
    for project in (parent_project, child_project):
        project.mkdir(parents=True)
        (project / ".paul-project.yml").write_text(
            "policy_profile: flat\n",
            encoding="utf-8",
        )
    store = SnapshotStore(
        config=MonitorConfig(
            workspaces=(
                WorkspaceConfig(path=parent_root, name="parent"),
                WorkspaceConfig(path=nested_root, name="child"),
            ),
            ignore_dirs=("nested",),
        )
    )
    store.load()
    parent_diagnostic = f"degraded: parent unavailable: {parent_root}"
    child_diagnostic = f"degraded: child unavailable: {nested_root}"

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=ScanResult(
            states=(),
            degraded_roots=(parent_root, nested_root),
            degraded_workspaces=("parent", "child"),
            diagnostics=(parent_diagnostic, child_diagnostic),
            degraded_diagnostics=(
                DegradedDiagnostic(parent_root, "parent", parent_diagnostic),
                DegradedDiagnostic(nested_root, "child", child_diagnostic),
            ),
        ),
    ):
        store.refresh()

    notes = {
        state.project_id: next(
            signal.note for signal in state.source_signals if signal.kind == "scan"
        )
        for state in store.current_snapshot()
    }
    assert notes == {
        "child-project": child_diagnostic,
        "parent-project": parent_diagnostic,
    }


@pytest.mark.parametrize(
    ("parent_name", "child_name"),
    (("parent", "child"), ("same", "same")),
)
def test_outer_workspace_degradation_does_not_freeze_healthy_nested_removal(
    tmp_path: Path,
    parent_name: str,
    child_name: str,
) -> None:
    parent_root = tmp_path / "root"
    nested_root = parent_root / "nested"
    parent_project = parent_root / "parent-project"
    child_project = nested_root / "child-project"
    for project in (parent_project, child_project):
        project.mkdir(parents=True)
        (project / ".paul-project.yml").write_text(
            "policy_profile: flat\n",
            encoding="utf-8",
        )
    store = SnapshotStore(
        config=MonitorConfig(
            workspaces=(
                WorkspaceConfig(path=parent_root, name=parent_name),
                WorkspaceConfig(path=nested_root, name=child_name),
            ),
            ignore_dirs=("nested",),
        )
    )
    store.load()
    parent_diagnostic = f"degraded: parent unavailable: {parent_root}"

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=ScanResult(
            states=(),
            degraded_roots=(parent_root,),
            degraded_workspaces=(parent_name,),
            diagnostics=(parent_diagnostic,),
            degraded_diagnostics=(
                DegradedDiagnostic(parent_root, parent_name, parent_diagnostic),
            ),
        ),
    ):
        events = store.refresh()

    assert store.get("parent-project") is not None
    assert store.get("child-project") is None
    assert any(event.project_id == "child-project" and event.removed for event in events)


def test_targeted_refresh_defers_missing_project_until_parent_scan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="test"),),
    )
    store = SnapshotStore(config=config)
    store.load()
    (project / ".paul-project.yml").unlink()
    project.rmdir()

    events = store.refresh_projects(("demo",))

    state = store.get("demo")
    assert state is not None
    assert events and not events[0].removed
    assert state.source_signals[-1].kind == "scan"

    confirmed = store.refresh()
    assert confirmed and confirmed[0].removed
    assert store.get("demo") is None


def test_targeted_refresh_preserves_tasks_on_transient_subtree_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    todo = project / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text(
        "# todo\n\n## Current Sprint\n\n- [ ] keep this task\n",
        encoding="utf-8",
    )
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="test"),),
    )
    store = SnapshotStore(config=config)
    store.load()
    previous = store.get("demo")
    assert previous is not None
    assert previous.in_progress_stages[0].processing_task.text == "keep this task"

    original_read_text = Path.read_text

    def fail_todo(path: Path, *args, **kwargs):
        if path == todo:
            raise PermissionError("transient")
        return original_read_text(path, *args, **kwargs)

    with mock.patch.object(Path, "read_text", fail_todo):
        events = store.refresh_projects(("demo",))

    state = store.get("demo")
    assert state is not None
    assert state.in_progress_stages[0].processing_task.text == "keep this task"
    assert events and not events[0].removed
    assert state.source_signals[-1].kind == "scan"


def test_targeted_refresh_preserves_state_on_classification_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="test"),),
        legacy_policy="hide",
    )
    store = SnapshotStore(config=config)
    store.load()

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.classify_project_detailed",
        return_value=(ProjectClassification.LEGACY, "degraded: PermissionError: transient"),
    ):
        events = store.refresh_projects(("demo",))

    state = store.get("demo")
    assert state is not None
    assert events and not events[0].removed
    assert state.source_signals[-1].kind == "scan"


def test_refresh_keeps_stable_duplicate_ids_when_one_root_is_degraded(tmp_path: Path) -> None:
    west_root = tmp_path / "west"
    east_root = tmp_path / "east"
    for root in (west_root, east_root):
        project = root / "same"
        project.mkdir(parents=True)
        (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(
        workspaces=(
            WorkspaceConfig(path=west_root, name="west"),
            WorkspaceConfig(path=east_root, name="east"),
        ),
    )
    store = SnapshotStore(config=config)
    store.load()
    before = {state.path: state.project_id for state in store.current_snapshot()}
    west_state = ProjectState(
        project_id="same",
        workspace="west",
        path=str(west_root / "same"),
    )

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=ScanResult(
            states=(west_state,),
            degraded_roots=(east_root,),
            diagnostics=("degraded: east unavailable",),
        ),
    ):
        events = store.refresh()

    after = {state.path: state.project_id for state in store.current_snapshot()}
    assert after == before
    assert all(not event.removed for event in events)


def test_scan_race_after_parent_listing_is_degraded_not_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(
        workspaces=(WorkspaceConfig(path=workspace, name="test"),),
    )
    store = SnapshotStore(config=config)
    store.load()
    original_stat = Path.stat

    def race_stat(path: Path, *args, **kwargs):
        if path == project:
            raise FileNotFoundError("transient race")
        return original_stat(path, *args, **kwargs)

    with mock.patch.object(Path, "stat", race_stat):
        events = store.refresh()

    state = store.get("demo")
    assert state is not None
    assert events and not events[0].removed
    assert state.source_signals[-1].kind == "scan"


def test_todo_stat_error_preserves_last_good_for_full_and_targeted_refresh(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    todo = project / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
    todo.parent.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    todo.write_text("## Current Sprint\n- [ ] keep task\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    original_stat = Path.stat

    def fail_todo_stat(path: Path, *args, **kwargs):
        if path == todo:
            raise PermissionError("todo stat")
        return original_stat(path, *args, **kwargs)

    with mock.patch.object(Path, "stat", fail_todo_stat):
        full_events = store.refresh()
        targeted_events = store.refresh_projects(("demo",))

    state = store.get("demo")
    assert state is not None
    assert state.in_progress_stages[0].processing_task.text == "keep task"
    assert full_events and not full_events[0].removed
    assert all(not event.removed for event in targeted_events)


def test_project_stat_error_preserves_targeted_last_good(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    original_stat = Path.stat

    def fail_project_stat(path: Path, *args, **kwargs):
        if path == project:
            raise PermissionError("project stat")
        return original_stat(path, *args, **kwargs)

    with mock.patch.object(Path, "stat", fail_project_stat):
        events = store.refresh_projects(("demo",))

    assert store.get("demo") is not None
    assert events and not events[0].removed


def test_broken_symlink_does_not_freeze_workspace_removals(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    (workspace / "broken").symlink_to(workspace / "missing-target")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()

    assert store.refresh() == ()
    (project / ".paul-project.yml").unlink()
    project.rmdir()

    events = store.refresh()
    assert events and events[0].removed
    assert store.get("demo") is None


def test_project_symlink_target_outage_retains_last_good_until_link_removed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "mounted-project"
    target.mkdir()
    (target / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    project_link = workspace / "project-link"
    project_link.symlink_to(target, target_is_directory=True)
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    state = store.current_snapshot()[0]
    project_id = state.project_id
    (target / ".paul-project.yml").unlink()
    target.rmdir()

    outage_events = store.refresh()

    retained = store.get(project_id)
    assert retained is not None
    assert outage_events and not outage_events[0].removed
    assert retained.source_signals[-1].kind == "scan"

    project_link.unlink()
    removal_events = store.refresh()
    assert removal_events and removal_events[0].removed
    assert store.get(project_id) is None


@pytest.mark.parametrize("failure", ("workspace", "git", "head"))
def test_watch_refresh_retains_existing_keys_on_transient_stat_error(
    tmp_path: Path,
    failure: str,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    git_dir = project / ".git"
    refs = git_dir / "refs"
    refs.mkdir(parents=True)
    head = git_dir / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    watcher = StubWatcher()
    service = ProjectMonitorService(config=config, watcher=watcher, store=store)
    service._sync_project_roots()
    service._install_watches()
    before = set(service._watched_paths)
    failing_path = {"workspace": workspace, "git": git_dir, "head": head}[failure]
    original_stat = Path.stat

    def transient_stat(path: Path, *args, **kwargs):
        if path == failing_path:
            raise PermissionError(f"{failure} transient")
        return original_stat(path, *args, **kwargs)

    with mock.patch.object(Path, "stat", transient_stat):
        service._install_watches()

    assert service._watched_paths == before


def test_watch_schedule_race_is_retried_without_claiming_key(tmp_path: Path) -> None:
    class RaceWatcher(StubWatcher):
        fail_path: Path | None = None

        def watch(self, path, callback, *, recursive=True):
            if self.fail_path == Path(path):
                raise OSError("schedule race")
            return super().watch(path, callback, recursive=recursive)

    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    refs = project / ".git" / "refs"
    refs.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    watcher = RaceWatcher()
    service = ProjectMonitorService(config=config, watcher=watcher, store=store)
    service._sync_project_roots()
    service._install_watches()
    head = project / ".git" / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    watcher.fail_path = head

    service._install_watches()

    assert (head, False) not in service._watched_paths
    watcher.fail_path = None
    service._install_watches()
    assert (head, False) in service._watched_paths


def test_unwatch_error_drops_stale_claim_so_watch_can_be_reinstalled(
    tmp_path: Path,
) -> None:
    class UnwatchRaceWatcher(StubWatcher):
        fail_once = True

        def unwatch(self, path, *, recursive=True):
            super().unwatch(path, recursive=recursive)
            if self.fail_once:
                self.fail_once = False
                raise OSError("backend removed watch before reporting failure")

    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    watcher = UnwatchRaceWatcher()
    service = ProjectMonitorService(config=config, watcher=watcher, store=store)
    stale_key = (project, False)
    watcher.watch(project, service._handle_fs_event, recursive=False)
    service._watched_paths.add(stale_key)

    service._install_watches()

    assert stale_key not in service._watched_paths
    assert not any(entry[0] == project for entry in watcher.subscriptions)

    service._project_roots = {"demo": project}
    service._install_watches()

    assert stale_key in service._watched_paths
    assert any(entry[0] == project for entry in watcher.subscriptions)


def test_project_resolve_error_marks_workspace_degraded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    original_resolve = Path.resolve

    def fail_project_resolve(path: Path, *args, **kwargs):
        if path == project:
            raise OSError("resolve transient")
        return original_resolve(path, *args, **kwargs)

    with mock.patch.object(Path, "resolve", fail_project_resolve):
        result = scan_workspaces_detailed(config)

    assert result.states == ()
    assert workspace in result.degraded_roots
    assert result.diagnostics and "resolve transient" in result.diagnostics[0]


def test_snapshot_resolve_error_preserves_last_good(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    degraded = ScanResult(
        states=(),
        degraded_roots=(workspace,),
        diagnostics=("degraded: resolve unavailable",),
    )
    original_resolve = Path.resolve

    def fail_resolve(path: Path, *args, **kwargs):
        if path in {workspace, project}:
            raise OSError("resolve transient")
        return original_resolve(path, *args, **kwargs)

    with mock.patch(
        "paulsha_cortex.monitor.snapshot.scan_workspaces_detailed",
        return_value=degraded,
    ), mock.patch.object(Path, "resolve", fail_resolve):
        events = store.refresh()

    assert store.get("demo") is not None
    assert events and not events[0].removed


def test_external_symlink_resolve_error_freezes_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside-target"
    target.mkdir()
    (target / ".paul-project.yml").write_text("policy_profile: flat\n", encoding="utf-8")
    project_link = workspace / "linked-project"
    project_link.symlink_to(target, target_is_directory=True)
    config = MonitorConfig(workspaces=(WorkspaceConfig(path=workspace, name="test"),))
    store = SnapshotStore(config=config)
    store.load()
    before = store.current_snapshot()[0]
    original_resolve = Path.resolve

    def fail_link_resolve(path: Path, *args, **kwargs):
        if path == project_link:
            raise OSError("link resolve transient")
        return original_resolve(path, *args, **kwargs)

    with mock.patch.object(Path, "resolve", fail_link_resolve):
        outage_events = store.refresh()

    retained = store.get(before.project_id)
    assert retained is not None
    assert retained.path == before.path
    assert outage_events and not outage_events[0].removed

    recovery_events = store.refresh()
    recovered = store.get(before.project_id)
    assert recovered is not None
    assert recovered.path == before.path
    assert all(not event.removed for event in recovery_events)
