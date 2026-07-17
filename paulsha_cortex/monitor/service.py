from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from .config import MonitorConfig
from .fs import checked_resolve, checked_stat_mode
from .server import MonitorServer
from .snapshot import ChangeEvent, SnapshotStore
from .watcher import HAS_WATCHDOG, StubWatcher, WatchdogFileWatcher, Watcher


class ProjectMonitorService:
    """Stage 9 long-lived service runtime.

    Composes the snapshot store, filesystem watcher, and Unix-socket server.
    State remains in memory; project truth is always re-derived from files.
    """

    def __init__(
        self,
        *,
        config: MonitorConfig,
        watcher: Watcher | None = None,
        store: SnapshotStore | None = None,
        server: MonitorServer | None = None,
    ) -> None:
        self._config = config
        self._store = store or SnapshotStore(config=config)
        if watcher is not None:
            self._watcher = watcher
        elif HAS_WATCHDOG:
            self._watcher = WatchdogFileWatcher(
                debounce_ms=config.watch_debounce_ms
            )
        else:
            self._watcher = StubWatcher()
        self._server = server or MonitorServer(
            store=self._store,
            socket_path=config.socket_path,
        )
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._rescan_thread: threading.Thread | None = None
        self._debounce_lock = threading.Lock()
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._watch_state_lock = threading.RLock()
        self._project_roots: dict[str, Path] = {}
        self._watched_paths: set[tuple[Path, bool]] = set()

    def run_forever(self) -> None:
        self._prepare_run_dir()
        self._store.load()
        self._sync_project_roots()
        self._install_watches()
        self._start_poll_thread()
        self._start_rescan_thread()
        try:
            self._server.serve_forever()
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_debounce_timers()
        self._watcher.stop()
        self._server.stop()

    def _prepare_run_dir(self) -> None:
        run_dir = self._config.socket_path.parent
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(run_dir), 0o700)

    def _start_poll_thread(self) -> None:
        if self._poll_thread is not None:
            return
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _start_rescan_thread(self) -> None:
        if self._rescan_thread is not None:
            return
        self._rescan_thread = threading.Thread(target=self._rescan_loop, daemon=True)
        self._rescan_thread.start()

    def _poll_loop(self) -> None:
        interval = max(0.1, float(self._config.poll_interval_seconds))
        while not self._stop_event.wait(interval):
            self._publish_refresh(self._store.refresh())

    def _rescan_loop(self) -> None:
        interval = max(0.1, float(self._config.rescan_interval_seconds))
        while not self._stop_event.wait(interval):
            with self._watch_state_lock:
                tracked_ids = tuple(self._project_roots)
            if not tracked_ids:
                continue
            self._publish_refresh(self._store.refresh_projects(tracked_ids))

    def _sync_project_roots(self) -> None:
        with self._watch_state_lock:
            self._project_roots = {
                state.project_id: Path(state.path)
                for state in self._store.current_snapshot()
                if not state.legacy
            }

    def _install_watches(self) -> None:
        with self._watch_state_lock:
            desired_keys: list[tuple[Path, bool]] = []
            seen: set[tuple[Path, bool]] = set()
            for workspace in self._config.workspaces:
                workspace_mode, workspace_error = checked_stat_mode(workspace.path)
                if (
                    workspace_error is not None
                    or workspace_mode is None
                    or not stat.S_ISDIR(workspace_mode)
                ):
                    for key in self._existing_watch_keys_under(workspace.path):
                        if key not in seen:
                            seen.add(key)
                            desired_keys.append(key)
                    continue
                key = (workspace.path, False)
                if key not in seen:
                    seen.add(key)
                    desired_keys.append(key)
            for project_root in self._project_roots.values():
                for watch_path, recursive in self._watch_specs(project_root):
                    key = (watch_path, recursive)
                    if key in seen:
                        continue
                    seen.add(key)
                    desired_keys.append(key)
            stale_keys = self._watched_paths - seen
            for watch_path, recursive in stale_keys:
                try:
                    self._watcher.unwatch(watch_path, recursive=recursive)
                except OSError:
                    continue
                self._watched_paths.discard((watch_path, recursive))
            for watch_path, recursive in desired_keys:
                watch_key = (watch_path, recursive)
                if watch_key in self._watched_paths:
                    continue
                try:
                    self._watcher.watch(
                        watch_path,
                        self._handle_fs_event,
                        recursive=recursive,
                    )
                except OSError:
                    # A check-to-schedule race is transient. Do not claim the
                    # key; the next synchronization will retry it.
                    continue
                self._watched_paths.add(watch_key)

    def _existing_watch_keys_under(self, root: Path) -> tuple[tuple[Path, bool], ...]:
        retained: list[tuple[Path, bool]] = []
        for key in self._watched_paths:
            watch_path, _recursive = key
            if watch_path == root:
                retained.append(key)
                continue
            try:
                watch_path.relative_to(root)
            except ValueError:
                continue
            retained.append(key)
        return tuple(retained)

    def _watch_specs(self, project_root: Path) -> tuple[tuple[Path, bool], ...]:
        specs: list[tuple[Path, bool]] = [(project_root, False)]
        git_dir, git_error = self._resolve_git_dir_checked(project_root)
        if git_error is not None:
            return tuple(dict.fromkeys([*specs, *self._existing_watch_keys_under(project_root)]))
        if git_dir is None:
            return tuple(specs)
        head_path = git_dir / "HEAD"
        refs_path = git_dir / "refs"
        head_mode, head_error = checked_stat_mode(head_path)
        if head_error is not None and (head_path, False) in self._watched_paths:
            specs.append((head_path, False))
        elif head_mode is not None:
            specs.append((head_path, False))
        refs_mode, refs_error = checked_stat_mode(refs_path)
        if refs_error is not None and (refs_path, True) in self._watched_paths:
            specs.append((refs_path, True))
        elif refs_mode is not None:
            specs.append((refs_path, True))
        return tuple(specs)

    def _resolve_git_dir(self, project_root: Path) -> Path | None:
        git_dir, _error = self._resolve_git_dir_checked(project_root)
        return git_dir

    def _resolve_git_dir_checked(
        self,
        project_root: Path,
    ) -> tuple[Path | None, str | None]:
        git_entry = project_root / ".git"
        git_mode, git_error = checked_stat_mode(git_entry)
        if git_error is not None:
            return None, git_error
        if git_mode is None:
            return None, None
        if stat.S_ISDIR(git_mode):
            return git_entry, None
        if not stat.S_ISREG(git_mode):
            return None, None
        try:
            first_line = git_entry.read_text(encoding="utf-8").splitlines()[0].strip()
        except OSError as error:
            return None, f"degraded: {type(error).__name__}: {error}"
        except IndexError:
            return None, None
        prefix = "gitdir:"
        if not first_line.lower().startswith(prefix):
            return None, None
        raw_path = first_line[len(prefix):].strip()
        if not raw_path:
            return None, None
        resolved = Path(raw_path)
        if not resolved.is_absolute():
            resolved, resolve_error = checked_resolve(git_entry.parent / resolved)
            if resolve_error is not None:
                return None, resolve_error
        return resolved, None

    def _handle_fs_event(self, path: Path) -> None:
        project_id = self._project_id_for_path(Path(path))
        if project_id is None:
            self._publish_refresh(self._store.refresh())
            return
        with self._watch_state_lock:
            project_root = self._project_roots.get(project_id)
        project_mode, project_error = (
            checked_stat_mode(project_root)
            if project_root is not None
            else (None, None)
        )
        if project_root is not None and (
            project_error is not None
            or project_mode is None
            or not stat.S_ISDIR(project_mode)
        ):
            # The workspace parent is still readable, so a full scan can
            # authoritatively distinguish deletion from an unavailable root.
            self._publish_refresh(self._store.refresh())
            return
        self._schedule_project_refresh(project_id)

    def _project_id_for_path(self, path: Path) -> str | None:
        best_match: tuple[str, int] | None = None
        with self._watch_state_lock:
            project_roots = tuple(self._project_roots.items())
        for project_id, project_root in project_roots:
            try:
                path.relative_to(project_root)
            except ValueError:
                continue
            root_depth = len(project_root.parts)
            if best_match is None or root_depth > best_match[1]:
                best_match = (project_id, root_depth)
        return best_match[0] if best_match is not None else None

    def _schedule_project_refresh(self, project_id: str) -> None:
        delay_seconds = max(0.0, self._config.watch_debounce_ms / 1000.0)
        with self._debounce_lock:
            previous = self._debounce_timers.get(project_id)
            if previous is not None:
                previous.cancel()
            timer = threading.Timer(
                delay_seconds,
                self._flush_project_refresh,
                args=(project_id,),
            )
            timer.daemon = True
            self._debounce_timers[project_id] = timer
            timer.start()

    def _flush_project_refresh(self, project_id: str) -> None:
        with self._debounce_lock:
            self._debounce_timers.pop(project_id, None)
        if self._stop_event.is_set():
            return
        event = self._store.refresh_project(project_id)
        self._sync_project_roots()
        self._install_watches()
        if event is not None:
            self._server.publish_events((event,))

    def _publish_refresh(self, events: tuple[ChangeEvent, ...]) -> None:
        self._sync_project_roots()
        self._install_watches()
        if events:
            self._server.publish_events(events)

    def _cancel_debounce_timers(self) -> None:
        with self._debounce_lock:
            timers = tuple(self._debounce_timers.values())
            self._debounce_timers.clear()
        for timer in timers:
            timer.cancel()

    def _shutdown(self) -> None:
        self._stop_event.set()
        self._cancel_debounce_timers()
        self._watcher.stop()
        self._server.stop()
        if (
            self._poll_thread is not None
            and self._poll_thread.is_alive()
            and threading.current_thread() is not self._poll_thread
        ):
            self._poll_thread.join(timeout=2.0)
        if (
            self._rescan_thread is not None
            and self._rescan_thread.is_alive()
            and threading.current_thread() is not self._rescan_thread
        ):
            self._rescan_thread.join(timeout=2.0)
