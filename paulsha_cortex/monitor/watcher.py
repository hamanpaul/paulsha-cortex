"""Filesystem watching abstraction (design §4 decision #2).

Watcher is the public Protocol; consumers depend on it, not on watchdog.
We ship two impls:

  * StubWatcher       — pure-Python, manual `trigger(path)` for tests.
  * WatchdogFileWatcher — production impl wrapping watchdog.observers.Observer
                           with a debounce window so bursts coalesce.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Iterable, Protocol

WatcherCallback = Callable[[Path], None]


class Watcher(Protocol):
    def watch(
        self,
        path: Path,
        callback: WatcherCallback,
        *,
        recursive: bool = True,
    ) -> None: ...

    def unwatch(self, path: Path, *, recursive: bool = True) -> None: ...

    def stop(self) -> None: ...


class StubWatcher:
    """In-process watcher used by tests; events fire only when `trigger`
    is called explicitly. Mirrors the Watcher Protocol so consumers can
    swap it in for production."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[Path, WatcherCallback, bool]] = []
        self._stopped = False

    @property
    def subscriptions(self) -> tuple[tuple[Path, WatcherCallback, bool], ...]:
        return tuple(self._subscriptions)

    def watch(
        self,
        path: Path,
        callback: WatcherCallback,
        *,
        recursive: bool = True,
    ) -> None:
        self._subscriptions.append((Path(path), callback, recursive))

    def unwatch(self, path: Path, *, recursive: bool = True) -> None:
        target = Path(path)
        self._subscriptions = [
            entry
            for entry in self._subscriptions
            if not (entry[0] == target and entry[2] == recursive)
        ]

    def trigger(self, path: Path) -> None:
        if self._stopped:
            return
        target = Path(path)
        for watched_path, callback, recursive in list(self._subscriptions):
            if watched_path == target:
                callback(target)
                continue
            if watched_path.exists() and watched_path.is_file():
                continue
            if recursive:
                try:
                    target.relative_to(watched_path)
                except ValueError:
                    continue
                callback(target)
                continue
            if target.parent == watched_path:
                callback(target)

    def stop(self) -> None:
        self._stopped = True
        self._subscriptions.clear()


# --- watchdog adapter -----------------------------------------------------

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:  # pragma: no cover - guard for environments without watchdog
    HAS_WATCHDOG = False


if HAS_WATCHDOG:

    class _DebouncedHandler(FileSystemEventHandler):
        def __init__(
            self,
            *,
            watch_path: Path,
            callback: WatcherCallback,
            debounce_seconds: float,
            recursive: bool,
        ) -> None:
            super().__init__()
            self._watch_path = watch_path
            self._callback = callback
            self._debounce = debounce_seconds
            self._recursive = recursive
            self._lock = threading.Lock()
            self._timer: threading.Timer | None = None
            self._latest_path: Path | None = None

        def _flush(self) -> None:
            with self._lock:
                path = self._latest_path or self._watch_path
                self._timer = None
                self._latest_path = None
            try:
                self._callback(path)
            except Exception:  # pragma: no cover - defensive
                pass

        def _schedule(self, path: Path) -> None:
            with self._lock:
                self._latest_path = path
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(self._debounce, self._flush)
                self._timer.daemon = True
                self._timer.start()

        def _matches(self, path: Path) -> bool:
            if self._watch_path.is_file():
                return path == self._watch_path
            if self._recursive:
                try:
                    path.relative_to(self._watch_path)
                except ValueError:
                    return False
                return True
            return path == self._watch_path or path.parent == self._watch_path

        def on_any_event(self, event: FileSystemEvent) -> None:
            for raw_path in (
                event.src_path,
                getattr(event, "dest_path", ""),
            ):
                if not raw_path:
                    continue
                try:
                    path = Path(raw_path)
                except Exception:  # pragma: no cover
                    continue
                if self._matches(path):
                    self._schedule(path)
                    return

        def cancel(self) -> None:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None

    class WatchdogFileWatcher:
        """Production Watcher backed by watchdog with a debounce window."""

        def __init__(self, *, debounce_ms: int = 500) -> None:
            self._observer = Observer()
            self._handlers: list[_DebouncedHandler] = []
            self._watches: dict[tuple[Path, bool], tuple[_DebouncedHandler, object]] = {}
            self._debounce_seconds = max(0.0, debounce_ms / 1000.0)
            self._started = False
            self._stopped = False

        def watch(
            self,
            path: Path,
            callback: WatcherCallback,
            *,
            recursive: bool = True,
        ) -> None:
            if self._stopped:
                raise RuntimeError("watcher is stopped")
            watch_path = Path(path)
            watch_key = (watch_path, recursive)
            if watch_key in self._watches:
                return
            observer_root = watch_path.parent if watch_path.is_file() else watch_path
            handler = _DebouncedHandler(
                watch_path=watch_path,
                callback=callback,
                debounce_seconds=self._debounce_seconds,
                recursive=recursive,
            )
            self._handlers.append(handler)
            watch = self._observer.schedule(handler, str(observer_root), recursive=recursive)
            self._watches[watch_key] = (handler, watch)
            if not self._started:
                self._observer.start()
                self._started = True

        def unwatch(self, path: Path, *, recursive: bool = True) -> None:
            watch_key = (Path(path), recursive)
            pair = self._watches.pop(watch_key, None)
            if pair is None:
                return
            handler, watch = pair
            handler.cancel()
            self._handlers = [item for item in self._handlers if item is not handler]
            if self._started:
                try:
                    self._observer.unschedule(watch)
                except Exception:  # pragma: no cover
                    pass

        def stop(self) -> None:
            self._stopped = True
            for handler in self._handlers:
                handler.cancel()
            if self._started:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=2.0)
                except Exception:  # pragma: no cover
                    pass
                self._started = False
            self._watches.clear()
            self._handlers.clear()

else:  # pragma: no cover - exercised only when watchdog missing

    class WatchdogFileWatcher:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "watchdog is not installed; install requirements-stage9.txt"
            )

        def watch(self, path, callback) -> None:  # noqa: D401
            raise RuntimeError("watchdog not available")

        def unwatch(self, path, *, recursive: bool = True) -> None:
            return None

        def stop(self) -> None:
            return None
