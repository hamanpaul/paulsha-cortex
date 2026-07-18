"""Unix domain socket server for the Project Monitor read API.

Wire contract (per design §3.6 + spec §B6):

  Request line  (one JSON object per newline):
    {"kind": "list_projects"}
    {"kind": "get_project_state", "project_id": "<id>"}
    {"kind": "subscribe"}                              # all projects
    {"kind": "subscribe", "projects": ["<id>", ...]}   # filter
    {"kind": "list_work_items"}
    {"kind": "get_work_item", "work_id": "<id>"}
    {"kind": "explain_work_item", "work_id": "<id>"}
    {"kind": "subscribe_work_items", "repo": "owner/repo",
     "work_ids": ["<id>", ...]}                    # filters optional

  Unary response (one JSON object on one line, then close):
    {"ok": true,  "data": <payload>}
    {"ok": false, "error": "<reason>"}

  Subscribe response (newline-delimited JSON event stream):
    {"sequence": <int>, "kind": "snapshot", "projects": [<state>, ...]}
    {"sequence": <int>, "kind": "change",   "project":  <state>}
    {"schema": "cortex-work/v1", "sequence": <int>,
     "kind": "work_snapshot", "items": [<work-item>, ...]}
    {"schema": "cortex-work/v1", "sequence": <int>,
     "kind": "work_change", "item": <work-item>, "removed": <bool>}

  Subscription events are streamed directly and do not use the unary ok/data
  wrapper.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .snapshot import ChangeEvent, SnapshotStore
from .work_api import (
    WORK_API_SCHEMA,
    AmbiguousWorkItemError,
    WorkChangeEvent,
    WorkReadModelStore,
)

ACCEPT_TIMEOUT_SECONDS = 0.25
SUBSCRIBE_QUEUE_GET_TIMEOUT = 0.25
EVENT_QUEUE_MAXSIZE = 1024
SOCKET_PROBE_TIMEOUT_SECONDS = 0.2
_SOCKET_PATH_LOCK = threading.Lock()
_SOCKET_OWNERS: dict[str, object] = {}


class _Subscriber:
    def __init__(self, *, projects: tuple[str, ...] | None) -> None:
        self.projects = projects  # None = all
        self.queue: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self.alive = True

    def matches(self, project_id: str) -> bool:
        if self.projects is None:
            return True
        return project_id in self.projects


class _WorkSubscriber:
    def __init__(
        self,
        *,
        repo: str | None,
        work_ids: tuple[str, ...] | None,
    ) -> None:
        self.repo = repo
        self.work_ids = work_ids
        self.queue: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self.alive = True

    def matches(self, repo: str, work_id: str) -> bool:
        return (self.repo is None or repo == self.repo) and (
            self.work_ids is None or work_id in self.work_ids
        )


class MonitorServer:
    """Long-lived Unix-socket server. Single instance per service."""

    def __init__(
        self,
        *,
        store: SnapshotStore,
        socket_path: Path,
        work_store: WorkReadModelStore | None = None,
    ) -> None:
        self._store = store
        self._work_store = work_store
        self._socket_path = Path(socket_path)
        self._listener: socket.socket | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._subscribers: list[_Subscriber] = []
        self._work_subscribers: list[_WorkSubscriber] = []
        self._subscribers_lock = threading.Lock()
        self._connection_threads: list[threading.Thread] = []
        self._connection_threads_lock = threading.Lock()
        self._serve_thread: threading.Thread | None = None

    # --- lifecycle ---

    def _prepare_socket_path(self) -> None:
        if not self._socket_path.exists():
            return
        try:
            mode = self._socket_path.stat().st_mode
        except OSError as exc:
            raise RuntimeError(f"無法檢查 monitor socket path：{self._socket_path}") from exc
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(
                f"monitor socket path 已存在且不是 Unix socket：{self._socket_path}"
            )

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(SOCKET_PROBE_TIMEOUT_SECONDS)
            probe.connect(str(self._socket_path))
        except TimeoutError as exc:
            raise RuntimeError(
                f"live monitor already listening on {self._socket_path}"
            ) from exc
        except OSError:
            try:
                self._socket_path.unlink()
            except OSError:
                pass
            return
        finally:
            probe.close()

        raise RuntimeError(f"live monitor already listening on {self._socket_path}")

    def serve_forever(self) -> None:
        # Atomic bind + permission tightening.
        if self._stop_event.is_set():
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        socket_identity: tuple[int, int, int] | None = None
        owner_token: object | None = None
        try:
            with _SOCKET_PATH_LOCK:
                self._prepare_socket_path()
                previous_umask = os.umask(0o177)
                try:
                    listener.bind(str(self._socket_path))
                    bound = True
                finally:
                    os.umask(previous_umask)
                os.chmod(str(self._socket_path), 0o600)
                listener.listen(16)
                listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
                path_stat = self._socket_path.lstat()
                socket_identity = (
                    path_stat.st_dev,
                    path_stat.st_ino,
                    path_stat.st_ctime_ns,
                )
                owner_token = object()
                _SOCKET_OWNERS[str(self._socket_path)] = owner_token
            with self._lifecycle_lock:
                self._listener = listener
                if self._stop_event.is_set():
                    return
                self._ready_event.set()

            while not self._stop_event.is_set():
                try:
                    conn, _addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.settimeout(None)
                t = threading.Thread(
                    target=self._handle_connection, args=(conn,), daemon=True
                )
                t.start()
                with self._connection_threads_lock:
                    self._connection_threads = [
                        thread for thread in self._connection_threads if thread.is_alive()
                    ]
                    self._connection_threads.append(t)
        finally:
            self._teardown(
                listener,
                unlink_socket=bound,
                socket_identity=socket_identity,
                owner_token=owner_token,
            )

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            self._ready_event.clear()
            listener = self._listener
        # Mark all subscribers dead so their threads can exit.
        with self._subscribers_lock:
            for sub in self._subscribers:
                sub.alive = False
            for sub in self._work_subscribers:
                sub.alive = False
        # Best-effort: close the listener so any in-flight accept errors out.
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait until the Unix socket is listening, not merely bound."""
        return self._ready_event.wait(timeout)

    def _teardown(
        self,
        listener: socket.socket,
        *,
        unlink_socket: bool,
        socket_identity: tuple[int, int, int] | None,
        owner_token: object | None,
    ) -> None:
        with self._lifecycle_lock:
            self._ready_event.clear()
            if self._listener is listener:
                self._listener = None
        try:
            listener.close()
        except OSError:
            pass
        if unlink_socket and socket_identity is not None and owner_token is not None:
            with _SOCKET_PATH_LOCK:
                owner_key = str(self._socket_path)
                if _SOCKET_OWNERS.get(owner_key) is not owner_token:
                    return
                _SOCKET_OWNERS.pop(owner_key, None)
                try:
                    path_stat = self._socket_path.lstat()
                    current_identity = (
                        path_stat.st_dev,
                        path_stat.st_ino,
                        path_stat.st_ctime_ns,
                    )
                    if current_identity == socket_identity:
                        self._socket_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    # --- public publish surface ---

    def publish_events(self, events: Iterable[ChangeEvent]) -> None:
        events = tuple(events)
        if not events:
            return
        with self._subscribers_lock:
            for sub in list(self._subscribers):
                if not sub.alive:
                    continue
                for evt in events:
                    if not sub.matches(evt.project_id):
                        continue
                    payload = {
                        "sequence": evt.sequence,
                        "kind": "change",
                        "project": _state_to_dict(evt.project_state),
                        "removed": evt.removed,
                    }
                    try:
                        sub.queue.put_nowait(payload)
                    except queue.Full:
                        # Drop oldest to make room (at-least-once with bounded
                        # buffer; consumers detect gaps via sequence numbers).
                        try:
                            sub.queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            sub.queue.put_nowait(payload)
                        except queue.Full:
                            pass

    def publish_work_events(self, events: Iterable[WorkChangeEvent]) -> None:
        events = tuple(events)
        if not events:
            return
        with self._subscribers_lock:
            for sub in list(self._work_subscribers):
                if not sub.alive:
                    continue
                for event in events:
                    if not sub.matches(event.work_item.repo, event.work_item.work_id):
                        continue
                    payload = {
                        "schema": WORK_API_SCHEMA,
                        "sequence": event.sequence,
                        "kind": "work_change",
                        "item": event.work_item.to_dict(),
                        "removed": event.removed,
                    }
                    try:
                        sub.queue.put_nowait(payload)
                    except queue.Full:
                        try:
                            sub.queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            sub.queue.put_nowait(payload)
                        except queue.Full:
                            pass

    # --- request handling ---

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            line = _read_line(conn, timeout=2.0)
            if not line:
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                _write_line(conn, _error(f"invalid JSON: {error}"))
                return
            if not isinstance(request, dict):
                _write_line(conn, _error("request payload must be a JSON object"))
                return

            kind = request.get("kind")
            if kind == "list_projects":
                self._handle_list_projects(conn)
            elif kind == "get_project_state":
                self._handle_get_project_state(conn, request)
            elif kind == "subscribe":
                self._handle_subscribe(conn, request)
            elif kind == "list_work_items":
                self._handle_list_work_items(conn, request)
            elif kind == "get_work_item":
                self._handle_get_work_item(conn, request, explain=False)
            elif kind == "explain_work_item":
                self._handle_get_work_item(conn, request, explain=True)
            elif kind == "subscribe_work_items":
                self._handle_subscribe_work_items(conn, request)
            else:
                _write_line(conn, _error(f"unknown request kind: {kind!r}"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            current = threading.current_thread()
            with self._connection_threads_lock:
                self._connection_threads = [
                    thread
                    for thread in self._connection_threads
                    if thread is not current and thread.is_alive()
                ]

    def _handle_list_projects(self, conn: socket.socket) -> None:
        states = self._store.current_snapshot()
        payload = {
            "ok": True,
            "data": {
                "projects": [_state_to_dict(s) for s in states],
            },
        }
        _write_line(conn, payload)

    def _handle_get_project_state(self, conn: socket.socket, request: dict) -> None:
        project_id = request.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            _write_line(conn, _error("project_id is required"))
            return
        state = self._store.get(project_id)
        if state is None:
            _write_line(conn, _error(f"unknown project: {project_id}"))
            return
        _write_line(conn, {"ok": True, "data": _state_to_dict(state)})

    def _handle_subscribe(self, conn: socket.socket, request: dict) -> None:
        filter_projects = request.get("projects")
        if filter_projects is None:
            projects = None
        elif isinstance(filter_projects, list) and all(
            isinstance(project_id, str) and project_id for project_id in filter_projects
        ):
            projects = tuple(filter_projects)
        else:
            _write_line(conn, _error("projects must be a list of non-empty strings"))
            return
        sub = _Subscriber(projects=projects)
        with self._subscribers_lock:
            self._subscribers.append(sub)
        try:
            # Send initial snapshot with the store's current sequence so any
            # subsequent change event is strictly greater for this subscriber.
            snap_seq = self._store.sequence
            states = self._store.current_snapshot()
            initial = {
                "sequence": snap_seq,
                "kind": "snapshot",
                "projects": [
                    _state_to_dict(s)
                    for s in states
                    if sub.matches(s.project_id)
                ],
            }
            _write_line(conn, initial)

            # Stream events until subscriber dies or peer disconnects.
            while sub.alive and not self._stop_event.is_set():
                try:
                    evt = sub.queue.get(timeout=SUBSCRIBE_QUEUE_GET_TIMEOUT)
                except queue.Empty:
                    if _peer_closed(conn):
                        return
                    continue
                _write_line(conn, evt)
        finally:
            sub.alive = False
            with self._subscribers_lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    def _require_work_store(self, conn: socket.socket) -> WorkReadModelStore | None:
        if self._work_store is None:
            _write_line(conn, _error("work read model unavailable"))
            return None
        return self._work_store

    def _handle_list_work_items(self, conn: socket.socket, request: dict) -> None:
        store = self._require_work_store(conn)
        if store is None:
            return
        repo = request.get("repo")
        states = request.get("states", [])
        include_done = request.get("include_done", False)
        explain = request.get("explain", False)
        if repo is not None and (not isinstance(repo, str) or not repo):
            _write_line(conn, _error("repo must be a non-empty string"))
            return
        if not isinstance(states, list) or any(not isinstance(state, str) for state in states):
            _write_line(conn, _error("states must be a list of strings"))
            return
        if not isinstance(include_done, bool) or not isinstance(explain, bool):
            _write_line(conn, _error("include_done/explain must be booleans"))
            return
        try:
            data = store.list_work_items(
                repo=repo, states=states, include_done=include_done, explain=explain
            )
        except ValueError as error:
            _write_line(conn, _error(str(error)))
            return
        _write_line(conn, {"ok": True, "data": data})

    def _handle_get_work_item(
        self, conn: socket.socket, request: dict, *, explain: bool
    ) -> None:
        store = self._require_work_store(conn)
        if store is None:
            return
        work_id = request.get("work_id")
        repo = request.get("repo")
        if not isinstance(work_id, str) or not work_id:
            _write_line(conn, _error("work_id is required"))
            return
        if repo is not None and (not isinstance(repo, str) or not repo):
            _write_line(conn, _error("repo must be a non-empty string"))
            return
        try:
            data = (
                store.explain_work_item(work_id, repo=repo)
                if explain
                else store.get_work_item(work_id, repo=repo)
            )
        except AmbiguousWorkItemError:
            _write_line(
                conn,
                _error(f"ambiguous work item: {work_id}; specify repo"),
            )
            return
        except KeyError:
            _write_line(conn, _error(f"unknown work item: {work_id}"))
            return
        _write_line(conn, {"ok": True, "data": data})

    def _handle_subscribe_work_items(self, conn: socket.socket, request: dict) -> None:
        store = self._require_work_store(conn)
        if store is None:
            return
        repo = request.get("repo")
        if repo is not None and (not isinstance(repo, str) or not repo):
            _write_line(conn, _error("repo must be a non-empty string"))
            return
        raw_ids = request.get("work_ids")
        if raw_ids is None:
            work_ids = None
        elif isinstance(raw_ids, list) and all(
            isinstance(work_id, str) and work_id for work_id in raw_ids
        ):
            work_ids = tuple(raw_ids)
        else:
            _write_line(conn, _error("work_ids must be a list of non-empty strings"))
            return
        sub = _WorkSubscriber(repo=repo, work_ids=work_ids)
        with self._subscribers_lock:
            self._work_subscribers.append(sub)
        try:
            initial = store.list_work_items(include_done=True)
            initial["kind"] = "work_snapshot"
            initial["items"] = [
                item
                for item in initial["items"]
                if sub.matches(item["repo"], item["work_id"])
            ]
            _write_line(conn, initial)
            while sub.alive and not self._stop_event.is_set():
                try:
                    event = sub.queue.get(timeout=SUBSCRIBE_QUEUE_GET_TIMEOUT)
                except queue.Empty:
                    if _peer_closed(conn):
                        return
                    continue
                _write_line(conn, event)
        finally:
            sub.alive = False
            with self._subscribers_lock:
                if sub in self._work_subscribers:
                    self._work_subscribers.remove(sub)


# --- helpers ---


def _state_to_dict(state) -> dict:
    payload = asdict(state)
    # asdict already converts dataclasses recursively; PosixPath etc. would
    # leak only via fields we don't have. Ensure JSON-safety just in case.
    return json.loads(json.dumps(payload, default=str))


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


_MAX_REQUEST_LINE = 65536  # 64 KiB：超長請求行上限，防記憶體/CPU 被拖垮（GitHub review #2）


def _read_line(conn: socket.socket, *, timeout: float = 2.0, max_len: int = _MAX_REQUEST_LINE) -> bytes:
    conn.settimeout(timeout)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            ch = conn.recv(1)
            if not ch:
                break
            chunks.append(ch)
            total += 1
            if ch == b"\n":
                break
            if total >= max_len:   # 超過上限即中止讀取（不吞整行）
                break
    except socket.timeout:
        return b""
    finally:
        conn.settimeout(None)
    return b"".join(chunks)


def _write_line(conn: socket.socket, payload: dict) -> None:
    line = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode(
        "utf-8"
    )
    try:
        conn.sendall(line)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _peer_closed(conn: socket.socket) -> bool:
    try:
        conn.setblocking(False)
        try:
            data = conn.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return False
        except OSError:
            return True
        return len(data) == 0
    finally:
        try:
            conn.setblocking(True)
        except OSError:
            pass
