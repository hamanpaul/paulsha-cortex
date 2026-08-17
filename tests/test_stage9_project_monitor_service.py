"""Stage 9 Phase 3 — service runtime, watcher, Unix socket server tests.

Public API surface this file locks (Phase 3 Red):

    from paulsha_cortex.monitor.snapshot import (
        SnapshotStore, ChangeEvent, EventStream,
    )
    from paulsha_cortex.monitor.watcher import (
        Watcher, StubWatcher, WatchdogFileWatcher,
    )
    from paulsha_cortex.monitor.server import MonitorServer
    from paulsha_cortex.monitor.service import ProjectMonitorService

Wire contract for the Unix socket (locked by Stage9ServerTests):

  Request  : single JSON object per line
             {"kind": "list_projects"}
             {"kind": "get_project_state", "project_id": "<id>"}
             {"kind": "subscribe"}                                # all
             {"kind": "subscribe", "projects": ["<id>", ...]}     # filter
             unknown kind → {"ok": false, "error": "<reason>"}
  Response : for unary requests → single JSON object on one line
             {"ok": true, "data": <payload>}
             for subscribe → newline-delimited JSON event stream:
             {"sequence": 1, "kind": "snapshot", "projects": [...]}
             {"sequence": 2, "kind": "change", "project": {...}}
"""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sandbox_support import AF_UNIX_SKIP_REASON, af_unix_bind_available
from socket_fixtures import make_short_socket_dir

# Imports from the Phase 3 modules (do not exist yet — Red).
try:
    from paulsha_cortex.monitor.config import MonitorConfig, WorkspaceConfig
    from paulsha_cortex.monitor.scanner import scan_workspaces
    from paulsha_cortex.monitor.snapshot import (
        ChangeEvent,
        SnapshotStore,
    )
    from paulsha_cortex.monitor.watcher import (
        StubWatcher,
        Watcher,
    )
    from paulsha_cortex.monitor.server import MonitorServer
    from paulsha_cortex.monitor.service import ProjectMonitorService
    from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore

    PHASE3_AVAILABLE = True
    PHASE3_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    PHASE3_AVAILABLE = False
    PHASE3_IMPORT_ERROR = exc

# Optional watchdog integration (real fs events).
try:
    from paulsha_cortex.monitor.watcher import WatchdogFileWatcher
    import watchdog  # noqa: F401  (only used to gate the integration test)

    HAS_WATCHDOG_INTEGRATION = True
except ImportError:
    HAS_WATCHDOG_INTEGRATION = False


# --- helpers -------------------------------------------------------------


def _require_phase3(test: unittest.TestCase) -> None:
    if not PHASE3_AVAILABLE:
        test.fail(
            f"paulsha_cortex.monitor service layer not implemented yet "
            f"(Phase 3 Red): {PHASE3_IMPORT_ERROR}"
        )


def _mkdir_resilient(path: Path, *, attempts: int = 25, delay: float = 0.02) -> None:
    """`path.mkdir(parents=True, exist_ok=True)`, retried past a transient
    `PermissionError` (issue #425).

    This suite starts several `MonitorServer` instances per test, and
    `MonitorServer.serve_forever()` briefly flips the *process-wide* umask
    around its Unix-socket `bind()` call (see
    `paulsha_cortex/monitor/server.py`). `os.umask()` is not thread-local —
    it is one value shared by every thread in the process — so if this
    thread's `mkdir(parents=True)` happens to create a new ancestor
    directory during that window, the ancestor can inherit an overly
    restrictive mode (e.g. `0o600`, no execute/traverse bit). Any later
    `mkdir`/`stat` underneath that ancestor then raises `PermissionError`;
    in CI this has shown up as `pathlib` raising `PermissionError` from
    `Path.mkdir` / `Path.is_dir` while building a fresh
    `docs/superpowers/workstreams/<stage>` tree, flaky only on Python 3.13
    where the narrower timing of the rewritten pathlib internals makes the
    (pre-existing, version-independent) race more likely to be observed.

    The race window is a handful of microseconds — the duration of one
    `bind()` call — so a short bounded retry lets the owning thread restore
    the umask and clears the flake without masking a genuine, persistent
    permission problem, which keeps failing past `attempts` and still
    raises here.
    """
    last_error: PermissionError | None = None
    for _ in range(attempts):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _make_workspace(root: Path, project_name: str, todo_body: str) -> Path:
    proj = root / project_name
    ws = proj / "docs" / "superpowers" / "workstreams" / "stage1-demo"
    _mkdir_resilient(ws)
    (proj / ".project-policy.yml").write_text("policy_profile: stage-driven\n")
    (ws / "todo.md").write_text(textwrap.dedent(todo_body))
    return proj


DEFAULT_TODO = """\
# stage1-demo / todo

## Current Sprint

- [ ] processing alpha
- [ ] next beta

## Blockers

## Evidence / Links

## Handoff Notes
"""


def _socket_recv_line(sock: socket.socket, timeout: float = 2.0) -> bytes:
    """Read until newline. Raises TimeoutError if no line within `timeout`."""
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    while True:
        ch = sock.recv(1)
        if not ch:
            break
        chunks.append(ch)
        if ch == b"\n":
            break
    return b"".join(chunks)


def _socket_send_request(sock: socket.socket, request: dict) -> None:
    sock.sendall((json.dumps(request) + "\n").encode("utf-8"))


# --- mkdir resilience (issue #425 regression) ---------------------------


class MkdirResilientTests(unittest.TestCase):
    """`_mkdir_resilient` must ride out a transient PermissionError on an
    ancestor directory instead of propagating it (issue #425)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-mkdir-resilient-"))

    def tearDown(self) -> None:
        import shutil

        # Best-effort: if a test forgot to restore permissions, do it here so
        # cleanup itself cannot raise and mask the real test outcome.
        for entry in self.tmp.rglob("*"):
            try:
                entry.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retries_past_transient_permission_error_on_ancestor(self) -> None:
        """A directory that briefly loses its execute bit (simulating the
        MonitorServer umask race) must not fail `_mkdir_resilient` — it
        should retry until the ancestor becomes traversable again, then
        create the target."""
        blocker = self.tmp / "blocker"
        blocker.mkdir()
        blocker.chmod(0o600)  # no execute bit: children become untraversable

        def _unblock() -> None:
            time.sleep(0.05)
            blocker.chmod(0o755)

        unblocker = threading.Thread(target=_unblock, daemon=True)
        unblocker.start()
        try:
            target = blocker / "projRace" / "docs" / "superpowers" / "workstreams" / "stage1-demo"
            _mkdir_resilient(target)
            self.assertTrue(target.is_dir())
        finally:
            unblocker.join(timeout=2.0)
            blocker.chmod(0o755)

    def test_raises_after_exhausting_attempts_on_persistent_permission_error(self) -> None:
        """A *persistent* permission problem (never restored) must still
        surface as a `PermissionError` — the retry is bounded, not a
        silent swallow."""
        blocker = self.tmp / "blocker-persistent"
        blocker.mkdir()
        blocker.chmod(0o600)
        try:
            with self.assertRaises(PermissionError):
                _mkdir_resilient(blocker / "child", attempts=3, delay=0.01)
        finally:
            blocker.chmod(0o755)

    def test_make_workspace_survives_ancestor_permission_race(self) -> None:
        """End-to-end: `_make_workspace` (the exact call site from the CI
        traceback in #425) must succeed even while its root is transiently
        non-traversable."""
        blocker = self.tmp / "ws"
        blocker.mkdir()
        blocker.chmod(0o600)

        def _unblock() -> None:
            time.sleep(0.05)
            blocker.chmod(0o755)

        unblocker = threading.Thread(target=_unblock, daemon=True)
        unblocker.start()
        try:
            proj = _make_workspace(blocker, "projRace", DEFAULT_TODO)
            todo = proj / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
            self.assertTrue(todo.exists())
        finally:
            unblocker.join(timeout=2.0)
            blocker.chmod(0o755)


# --- B-store / SnapshotStore --------------------------------------------


class Stage9SnapshotStoreTests(unittest.TestCase):
    """Snapshot store: in-memory truth + diff + monotonic sequence."""

    def setUp(self) -> None:
        _require_phase3(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-store-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_config(self) -> MonitorConfig:
        return MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws", name="ws"),),
            legacy_policy="list-only",
        )

    def test_store_load_returns_initial_snapshot_with_no_events(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = self._build_config()

        store = SnapshotStore(config=cfg)
        events = store.load()

        self.assertEqual(events, ())
        snapshot = store.current_snapshot()
        ids = {p.project_id for p in snapshot}
        self.assertIn("projA", ids)

    def test_store_refresh_emits_event_when_project_state_changes(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        proj = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = self._build_config()

        store = SnapshotStore(config=cfg)
        store.load()

        # Mutate the underlying todo and refresh — store must emit a change.
        new_body = DEFAULT_TODO.replace("processing alpha", "processing alpha v2")
        (proj / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md").write_text(
            new_body
        )

        events = store.refresh()
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertIsInstance(evt, ChangeEvent)
        self.assertEqual(evt.project_id, "projA")

    def test_store_refresh_emits_no_event_when_state_unchanged(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = self._build_config()
        store = SnapshotStore(config=cfg)
        store.load()

        events = store.refresh()  # identical state
        self.assertEqual(events, ())

    def test_store_refresh_removes_deleted_project_from_snapshot(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        doomed = _make_workspace(self.tmp / "ws", "projB", DEFAULT_TODO)
        cfg = self._build_config()
        store = SnapshotStore(config=cfg)
        store.load()

        import shutil

        shutil.rmtree(doomed)

        events = store.refresh()

        ids = {project.project_id for project in store.current_snapshot()}
        self.assertEqual(ids, {"projA"})
        self.assertIsNone(store.get("projB"))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].removed)
        self.assertEqual(events[0].project_id, "projB")

    def test_store_refresh_project_reclassifies_tracked_to_hidden_legacy(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        proj = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws", name="ws"),),
            legacy_policy="hide",
        )
        store = SnapshotStore(config=cfg)
        store.load()

        import shutil

        (proj / ".project-policy.yml").unlink()
        shutil.rmtree(proj / "docs")

        events = store.refresh_projects(("projA",))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].removed)
        self.assertIsNone(store.get("projA"))

    def test_store_refresh_emits_event_when_only_source_signals_change(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        proj = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = self._build_config()
        store = SnapshotStore(config=cfg)
        store.load()

        archive_root = proj / "openspec" / "changes" / "archive"
        _mkdir_resilient(archive_root)

        events = store.refresh_projects(("projA",))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].project_id, "projA")
        self.assertFalse(events[0].removed)

    def test_store_assigns_monotonic_sequence_to_events(self) -> None:
        _mkdir_resilient(self.tmp / "ws")
        proj = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        cfg = self._build_config()
        store = SnapshotStore(config=cfg)
        store.load()

        todo_path = (
            proj / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        )
        todo_path.write_text(DEFAULT_TODO.replace("alpha", "alpha-1"))
        first = store.refresh()
        todo_path.write_text(DEFAULT_TODO.replace("alpha", "alpha-2"))
        second = store.refresh()

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertGreater(second[0].sequence, first[0].sequence)

    def test_store_preserves_duplicate_project_basenames(self) -> None:
        west_root = self.tmp / "west"
        east_root = self.tmp / "east"
        _mkdir_resilient(west_root)
        _mkdir_resilient(east_root)
        _make_workspace(west_root, "same", DEFAULT_TODO)
        _make_workspace(east_root, "same", DEFAULT_TODO)
        cfg = MonitorConfig(
            workspaces=(
                WorkspaceConfig(path=west_root, name="west"),
                WorkspaceConfig(path=east_root, name="east"),
            ),
            legacy_policy="list-only",
        )
        store = SnapshotStore(config=cfg)
        store.load()
        snapshot = store.current_snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(len({project.project_id for project in snapshot}), 2)


# --- Stub watcher -------------------------------------------------------


class Stage9StubWatcherTests(unittest.TestCase):
    """Test-friendly Watcher impl that fires on manual trigger."""

    def setUp(self) -> None:
        _require_phase3(self)

    def test_stub_watcher_invokes_callback_on_trigger(self) -> None:
        received: list[Path] = []
        watcher: Watcher = StubWatcher()
        watcher.watch(Path("/tmp/whatever"), received.append)

        watcher.trigger(Path("/tmp/whatever"))

        self.assertEqual(received, [Path("/tmp/whatever")])

    def test_stub_watcher_stop_prevents_further_callbacks(self) -> None:
        received: list[Path] = []
        watcher = StubWatcher()
        watcher.watch(Path("/tmp/x"), received.append)
        watcher.stop()

        watcher.trigger(Path("/tmp/x"))

        self.assertEqual(received, [])


# --- MonitorServer (Unix socket) ----------------------------------------


@unittest.skipUnless(af_unix_bind_available(), AF_UNIX_SKIP_REASON)
class Stage9ServerTests(unittest.TestCase):
    """Unix domain socket server: list / get / subscribe + permissions."""

    def setUp(self) -> None:
        _require_phase3(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-server-"))
        # #608：只有**要 bind 的 socket** 需要躲開 TMPDIR 長度（sun_path 上限
        # 107 bytes）；workspace／snapshot 沒有長度上限，照舊留在 self.tmp。
        self.sock_dir = make_short_socket_dir(prefix="stage9s")
        _mkdir_resilient(self.tmp / "ws")
        _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        _make_workspace(self.tmp / "ws", "projB", DEFAULT_TODO)
        self.cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws", name="ws"),),
            legacy_policy="list-only",
        )
        self.store = SnapshotStore(config=self.cfg)
        self.store.load()
        self.socket_path = self.sock_dir / "monitor.sock"
        self.server = MonitorServer(store=self.store, socket_path=self.socket_path)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        # Readiness gate (#464): the socket *path* exists as soon as bind()
        # returns, but its mode is only tightened to 0o600 by the os.chmod()
        # that follows — polling for path existence lets tests observe the
        # bind-default mode (0o777 & ~umask) whenever the server thread is
        # scheduled out between bind() and chmod(). wait_until_ready() blocks
        # on _ready_event, which is set strictly after chmod()+listen(), so
        # it is the deterministic happens-before gate, not a timing tweak.
        self.assertTrue(
            self.server.wait_until_ready(timeout=2.0),
            msg="server did not become ready (bind+chmod+listen) in time",
        )
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.server.stop()
        self.server_thread.join(timeout=2.0)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.sock_dir, ignore_errors=True)

    def _connect(self) -> socket.socket:
        deadline = time.time() + 1.0
        last_error: OSError | None = None
        while time.time() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(self.socket_path))
            except ConnectionRefusedError as error:
                last_error = error
                sock.close()
                time.sleep(0.02)
                continue
            self.addCleanup(sock.close)
            return sock
        raise AssertionError(
            f"server socket refused connections for 1s: {last_error}"
        )

    def test_server_responds_to_list_projects_request(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "list_projects"})
        line = _socket_recv_line(sock)

        payload = json.loads(line)
        self.assertTrue(payload["ok"])
        ids = {p["project_id"] for p in payload["data"]["projects"]}
        self.assertEqual(ids, {"projA", "projB"})

    def test_server_responds_to_get_project_state_request(self) -> None:
        sock = self._connect()
        _socket_send_request(
            sock, {"kind": "get_project_state", "project_id": "projA"}
        )
        line = _socket_recv_line(sock)

        payload = json.loads(line)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["project_id"], "projA")

    def test_server_subscribe_streams_initial_snapshot_then_change_event(
        self,
    ) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})

        # First message = full snapshot with sequence 0 (or implementation-defined start).
        first_line = _socket_recv_line(sock)
        snapshot_msg = json.loads(first_line)
        self.assertEqual(snapshot_msg["kind"], "snapshot")
        self.assertIn("sequence", snapshot_msg)

        # Mutate a project then push a refresh through the store.
        proj_a = self.tmp / "ws" / "projA"
        todo = proj_a / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        todo.write_text(DEFAULT_TODO.replace("alpha", "alpha-mut"))
        new_events = self.store.refresh()
        self.assertEqual(len(new_events), 1)
        # The server is responsible for fanning the event out to subscribers.
        self.server.publish_events(new_events)

        change_line = _socket_recv_line(sock, timeout=3.0)
        change_msg = json.loads(change_line)
        self.assertEqual(change_msg["kind"], "change")
        self.assertGreater(change_msg["sequence"], snapshot_msg["sequence"])
        self.assertEqual(change_msg["project"]["project_id"], "projA")

    def test_server_socket_has_0600_permission(self) -> None:
        mode = stat.S_IMODE(self.socket_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_wait_until_ready_blocks_until_socket_mode_tightened(self) -> None:
        """Regression for #464: the socket *path* exists as soon as bind()
        returns, but the mode is only tightened to 0o600 by the os.chmod()
        that follows — so "path exists" is a strictly weaker readiness gate
        than wait_until_ready(). The old setUp polled for path existence and
        would occasionally stat the bind-default mode (0o777 & ~umask, e.g.
        0o755 on CI) whenever the server thread was scheduled out between
        bind() and chmod().

        Like `_slow_bind` below (#439), this interposes on os.chmod to park
        the server thread deterministically *inside* the bind→chmod window
        and proves: (1) the path already exists there, (2) wait_until_ready()
        correctly refuses to report ready, (3) once chmod proceeds, ready
        flips True and the mode is 0o600. This is also the guard rail against
        a future refactor moving _ready_event.set() before chmod().
        """
        real_chmod = os.chmod
        chmod_entered = threading.Event()
        release_chmod = threading.Event()

        def _slow_chmod(path, mode, **kwargs):
            chmod_entered.set()
            release_chmod.wait(timeout=2.0)
            return real_chmod(path, mode, **kwargs)

        other_socket_path = self.sock_dir / "chmod-race.sock"
        server = MonitorServer(store=self.store, socket_path=other_socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            with mock.patch(
                "paulsha_cortex.monitor.server.os.chmod",
                side_effect=_slow_chmod,
            ):
                thread.start()
                self.assertTrue(
                    chmod_entered.wait(timeout=2.0),
                    msg="serve_forever never reached os.chmod()",
                )
                # Server thread is now parked inside the bind→chmod window:
                # the path is visible but the mode is still the bind default
                # (its exact value depends on the ambient umask, so it is
                # deliberately not asserted here).
                self.assertTrue(
                    other_socket_path.exists(),
                    msg="socket path should exist right after bind()",
                )
                self.assertFalse(
                    server.wait_until_ready(timeout=0.2),
                    msg=(
                        "wait_until_ready() must not report ready inside the "
                        "bind→chmod window — path existence is a weaker gate"
                    ),
                )
                release_chmod.set()
                self.assertTrue(
                    server.wait_until_ready(timeout=2.0),
                    msg="server did not become ready after chmod was released",
                )
                mode = stat.S_IMODE(other_socket_path.stat().st_mode)
                self.assertEqual(mode, 0o600)
        finally:
            release_chmod.set()
            server.stop()
            thread.join(timeout=2.0)

    def test_serve_forever_does_not_touch_process_umask(self) -> None:
        """Regression for #439: `serve_forever()` must rely solely on the
        explicit `os.chmod(0o600)` for socket permissions, not a
        `os.umask()` flip/restore dance around `bind()`. `os.umask()` is
        process-global (not thread-local); flipping it — even briefly —
        exposes every other thread in the process to a window where a
        concurrent `mkdir(parents=True)` can inherit an overly restrictive
        mode (see `test_concurrent_mkdir_keeps_execute_bit_during_serve_forever_bind`
        below and issue #425). Asserting `os.umask` is never called is the
        direct, non-timing-sensitive proof that the race class is gone.
        """
        other_socket_path = self.sock_dir / "no-umask-call.sock"
        server = MonitorServer(store=self.store, socket_path=other_socket_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with mock.patch(
            "paulsha_cortex.monitor.server.os.umask",
            side_effect=AssertionError(
                "serve_forever() must not call os.umask() (issue #439)"
            ),
        ):
            thread.start()
            try:
                deadline = time.time() + 2.0
                while time.time() < deadline and not other_socket_path.exists():
                    time.sleep(0.02)
                self.assertTrue(
                    other_socket_path.exists(),
                    msg="server socket did not bind in time",
                )
            finally:
                server.stop()
                thread.join(timeout=2.0)

    def test_concurrent_mkdir_keeps_execute_bit_during_serve_forever_bind(self) -> None:
        """Regression for #439/#425: while `serve_forever()` is inside its
        `bind()` call, a *different* thread's `mkdir(parents=True)` must not
        be affected by any process-wide umask tightening.

        The real race window used to be the duration of one `bind()`
        syscall — a handful of microseconds, too narrow to hit
        deterministically in a test. To observe it reliably, this test
        interposes a short sleep inside `socket.socket.bind` (test-only;
        production code is untouched) so the server thread is provably
        parked *inside* `bind()` while the main thread does a concurrent
        `mkdir(parents=True)` elsewhere. Before the #439 fix this would flake
        onto mode `0o600` (no execute bit) whenever the mkdir landed inside
        the old `os.umask(0o177)` window; after the fix `serve_forever()`
        never touches the umask, so the new directory always keeps the
        ambient umask's execute bit.
        """
        real_bind = socket.socket.bind
        bind_entered = threading.Event()
        release_bind = threading.Event()

        def _slow_bind(sock_self: socket.socket, address: str) -> None:
            bind_entered.set()
            release_bind.wait(timeout=2.0)
            return real_bind(sock_self, address)

        other_socket_path = self.sock_dir / "umask-race.sock"
        server = MonitorServer(store=self.store, socket_path=other_socket_path)
        new_dir = self.tmp / "concurrent-race" / "nested" / "leaf"
        try:
            with mock.patch.object(socket.socket, "bind", _slow_bind):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertTrue(
                        bind_entered.wait(timeout=2.0),
                        msg="serve_forever never reached bind()",
                    )
                    # Server thread is now parked inside bind(). If
                    # serve_forever still flipped the process umask around
                    # bind() the way it did before #439, this is exactly the
                    # window in which it would be tightened.
                    new_dir.mkdir(parents=True)
                finally:
                    release_bind.set()
                    thread.join(timeout=2.0)
        finally:
            server.stop()
        mode = stat.S_IMODE(new_dir.stat().st_mode)
        self.assertTrue(
            mode & stat.S_IXUSR,
            msg=(
                "concurrent mkdir(parents=True) lost its execute bit during "
                f"serve_forever's bind() window (mode={oct(mode)})"
            ),
        )

    def test_server_rejects_unknown_request_kind(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "definitely_not_a_real_kind"})
        line = _socket_recv_line(sock)

        payload = json.loads(line)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

    def test_server_rejects_non_object_request_payload(self) -> None:
        sock = self._connect()
        sock.sendall(b'[\"subscribe\"]\n')
        payload = json.loads(_socket_recv_line(sock))
        self.assertFalse(payload["ok"])
        self.assertIn("JSON object", payload["error"])

    def test_server_rejects_non_string_project_id(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "get_project_state", "project_id": ["projA"]})
        payload = json.loads(_socket_recv_line(sock))
        self.assertFalse(payload["ok"])
        self.assertIn("project_id", payload["error"])

    def test_server_rejects_invalid_subscribe_projects_filter(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe", "projects": "projA"})
        payload = json.loads(_socket_recv_line(sock))
        self.assertFalse(payload["ok"])
        self.assertIn("projects", payload["error"])

    def test_server_discards_finished_connection_threads(self) -> None:
        for _ in range(3):
            sock = self._connect()
            _socket_send_request(sock, {"kind": "list_projects"})
            _socket_recv_line(sock)
            sock.close()
        # 1.0s -> 2.0s (matches the other generous polls in this file, e.g.
        # the deadlines above): the accept loop now registers each handler
        # thread in `_connection_threads` *before* starting it (issue #445),
        # so discard is no longer lazy/racy and this bound only needs to
        # absorb ordinary CPU-scheduling delay under a loaded CI runner, not
        # a permanently-orphaned dead-thread entry.
        deadline = time.time() + 2.0
        while time.time() < deadline and self.server._connection_threads:
            time.sleep(0.02)
        self.assertEqual(self.server._connection_threads, [])

    def test_server_rejects_live_socket_instead_of_stealing_it(self) -> None:
        _mkdir_resilient(self.tmp / "ws2")
        _make_workspace(self.tmp / "ws2", "projZ", DEFAULT_TODO)
        other_cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws2", name="ws2"),),
            legacy_policy="list-only",
        )
        other_store = SnapshotStore(config=other_cfg)
        other_store.load()
        contender = MonitorServer(store=other_store, socket_path=self.socket_path)
        errors: list[Exception] = []

        def run_contender() -> None:
            try:
                contender.serve_forever()
            except Exception as exc:  # pragma: no cover - captured for assertions
                errors.append(exc)

        contender_thread = threading.Thread(target=run_contender, daemon=True)
        contender_thread.start()
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline and not errors:
                time.sleep(0.02)

            sock = self._connect()
            _socket_send_request(sock, {"kind": "list_projects"})
            payload = json.loads(_socket_recv_line(sock))
            self.assertTrue(payload["ok"])
            self.assertEqual(
                {project["project_id"] for project in payload["data"]["projects"]},
                {"projA", "projB"},
            )
            self.assertTrue(errors, "second server should fail instead of stealing the live socket")
            self.assertRegex(str(errors[0]), "live monitor|already.*monitor")
        finally:
            contender.stop()
            contender_thread.join(timeout=2.0)

    def test_server_reclaims_stale_socket_file(self) -> None:
        stale_path = self.sock_dir / "stale-monitor.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(stale_path))
        stale.close()

        reclaimed = MonitorServer(store=self.store, socket_path=stale_path)
        reclaimed_thread = threading.Thread(target=reclaimed.serve_forever, daemon=True)
        reclaimed_thread.start()
        try:
            deadline = time.time() + 1.0
            sock: socket.socket | None = None
            last_error: OSError | None = None
            while time.time() < deadline:
                candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    candidate.connect(str(stale_path))
                except OSError as error:
                    last_error = error
                    candidate.close()
                    time.sleep(0.02)
                    continue
                sock = candidate
                break
            self.assertIsNotNone(sock, msg=f"replacement server did not bind stale socket path: {last_error}")
            self.addCleanup(sock.close)
            _socket_send_request(sock, {"kind": "list_projects"})
            payload = json.loads(_socket_recv_line(sock))
            self.assertTrue(payload["ok"])
            self.assertEqual(
                {project["project_id"] for project in payload["data"]["projects"]},
                {"projA", "projB"},
            )
        finally:
            reclaimed.stop()
            reclaimed_thread.join(timeout=2.0)

    def test_server_refuses_to_delete_non_socket_path(self) -> None:
        bad_path = self.sock_dir / "not-a-socket"
        bad_path.write_text("occupied", encoding="utf-8")
        contender = MonitorServer(store=self.store, socket_path=bad_path)
        with self.assertRaisesRegex(RuntimeError, "不是 Unix socket"):
            contender._prepare_socket_path()
        self.assertTrue(bad_path.exists())

    def test_server_timeout_probe_treats_socket_as_live(self) -> None:
        busy_path = self.sock_dir / "busy-monitor.sock"
        busy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        busy.bind(str(busy_path))
        busy.close()
        contender = MonitorServer(store=self.store, socket_path=busy_path)

        class _TimeoutProbe:
            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout

            def connect(self, path: str) -> None:
                raise TimeoutError("probe timed out")

            def close(self) -> None:
                return None

        with mock.patch(
            "paulsha_cortex.monitor.server.socket.socket",
            return_value=_TimeoutProbe(),
        ):
            with self.assertRaisesRegex(RuntimeError, "live monitor|already.*monitor"):
                contender._prepare_socket_path()

        self.assertTrue(busy_path.exists())


# --- ProjectMonitorService end-to-end -----------------------------------


@unittest.skipUnless(af_unix_bind_available(), AF_UNIX_SKIP_REASON)
class Stage9ServiceTests(unittest.TestCase):
    """Full service: scanner + (stub) watcher + server."""

    def setUp(self) -> None:
        _require_phase3(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-svc-"))
        # #608：run dir 底下要 bind socket，必須落在短固定根（見上）。
        self.sock_dir = make_short_socket_dir(prefix="stage9v")
        _mkdir_resilient(self.tmp / "ws")
        self.project_dir = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        _mkdir_resilient(self.project_dir / ".git" / "refs" / "heads")
        (self.project_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.project_dir / ".git" / "refs" / "heads" / "main").write_text("deadbeef\n")
        _mkdir_resilient(self.project_dir / "node_modules" / "pkg")
        (self.project_dir / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
        self.run_dir = self.sock_dir / "run"
        self.socket_path = self.run_dir / "project-monitor.sock"
        self.cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws", name="ws"),),
            legacy_policy="list-only",
            socket_path=self.socket_path,
            watch_debounce_ms=80,
            rescan_interval_seconds=1,
        )
        self.stub_watcher = StubWatcher()
        self.service = ProjectMonitorService(
            config=self.cfg,
            watcher=self.stub_watcher,
            durable_work_store=WorkSnapshotStore(
                self.tmp / "state/work-items.snapshot.json"
            ),
        )
        self.service_thread = threading.Thread(
            target=self.service.run_forever, daemon=True
        )
        self.service_thread.start()
        for _ in range(100):
            if self.socket_path.exists():
                break
            time.sleep(0.02)
        self.assertTrue(self.socket_path.exists(), msg="service socket never bound")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.service.stop()
        self.service_thread.join(timeout=3.0)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.sock_dir, ignore_errors=True)

    def _connect(self) -> socket.socket:
        deadline = time.time() + 1.0
        last_error: OSError | None = None
        while time.time() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(self.socket_path))
            except ConnectionRefusedError as error:
                last_error = error
                sock.close()
                time.sleep(0.02)
                continue
            self.addCleanup(sock.close)
            return sock
        raise AssertionError(
            f"service socket refused connections for 1s: {last_error}"
        )

    def test_service_creates_run_dir_with_0700_permission(self) -> None:
        self.assertTrue(self.run_dir.exists())
        mode = stat.S_IMODE(self.run_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_service_projects_local_work_items_into_new_socket_api(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "list_work_items"})
        payload = json.loads(_socket_recv_line(sock))
        self.assertTrue(payload["ok"])
        items = payload["data"]["items"]
        self.assertTrue(items)
        self.assertEqual({item["state"] for item in items}, {"todo"})

    def test_service_emits_event_when_underlying_todo_changes(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})
        snapshot_line = _socket_recv_line(sock)
        snapshot_msg = json.loads(snapshot_line)
        self.assertEqual(snapshot_msg["kind"], "snapshot")

        # Mutate the todo, then trigger the stub watcher.
        proj_a = self.tmp / "ws" / "projA"
        todo = proj_a / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        todo.write_text(DEFAULT_TODO.replace("alpha", "alpha-after"))
        self.stub_watcher.trigger(todo)

        change_line = _socket_recv_line(sock, timeout=3.0)
        change_msg = json.loads(change_line)
        self.assertEqual(change_msg["kind"], "change")
        self.assertEqual(change_msg["project"]["project_id"], "projA")

    def test_service_coalesces_burst_changes_into_single_event_per_project(
        self,
    ) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})
        snapshot_line = _socket_recv_line(sock)
        json.loads(snapshot_line)  # consume initial snapshot

        proj_a = self.tmp / "ws" / "projA"
        todo = proj_a / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"

        # Burst three writes within the debounce window.
        for i in range(3):
            todo.write_text(DEFAULT_TODO.replace("alpha", f"alpha-burst-{i}"))
            self.stub_watcher.trigger(todo)

        # Wait for debounce to flush and the change event to arrive.
        change_line = _socket_recv_line(sock, timeout=3.0)
        change_msg = json.loads(change_line)
        self.assertEqual(change_msg["kind"], "change")

        # Within a short follow-up window, no second change event for the same
        # project should arrive (coalescing).
        try:
            extra = _socket_recv_line(sock, timeout=0.5)
            extra_msg = json.loads(extra)
            self.fail(
                f"expected no further change event after coalescing, got {extra_msg!r}"
            )
        except (TimeoutError, socket.timeout):
            pass

    def test_service_watches_project_root_non_recursive_and_git_control_paths(self) -> None:
        subscriptions = {
            (str(path), recursive)
            for path, _callback, recursive in self.stub_watcher.subscriptions
        }

        self.assertIn((str(self.tmp / "ws"), False), subscriptions)
        self.assertIn((str(self.project_dir), False), subscriptions)
        self.assertIn((str(self.project_dir / ".git" / "HEAD"), False), subscriptions)
        self.assertIn((str(self.project_dir / ".git" / "refs"), True), subscriptions)
        self.assertNotIn((str(self.project_dir), True), subscriptions)
        self.assertFalse(any("node_modules" in path for path, _recursive in subscriptions))

    def test_service_falls_back_to_stub_watcher_without_watchdog(self) -> None:
        from paulsha_cortex.monitor import service as service_module

        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=self.tmp / "ws", name="ws"),),
            legacy_policy="list-only",
            socket_path=self.sock_dir / "fallback.sock",
        )
        with mock.patch.object(service_module, "HAS_WATCHDOG", False):
            fallback_service = ProjectMonitorService(config=cfg)
        self.assertIsInstance(fallback_service._watcher, StubWatcher)
        fallback_service.stop()

    def test_service_prunes_and_readds_project_watch_keys_after_recreate(self) -> None:
        import shutil

        expected = {
            (self.project_dir, False),
            (self.project_dir / ".git" / "HEAD", False),
            (self.project_dir / ".git" / "refs", True),
        }
        shutil.rmtree(self.project_dir)
        self.stub_watcher.trigger(self.project_dir)
        deadline = time.time() + 2.0
        while time.time() < deadline and any(key in self.service._watched_paths for key in expected):
            time.sleep(0.02)
        self.assertFalse(any(key in self.service._watched_paths for key in expected))

        self.project_dir = _make_workspace(self.tmp / "ws", "projA", DEFAULT_TODO)
        _mkdir_resilient(self.project_dir / ".git" / "refs" / "heads")
        (self.project_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.project_dir / ".git" / "refs" / "heads" / "main").write_text("deadbeef\n")
        self.stub_watcher.trigger(self.project_dir)
        deadline = time.time() + 2.0
        while time.time() < deadline and not all(key in self.service._watched_paths for key in expected):
            time.sleep(0.02)
        self.assertTrue(all(key in self.service._watched_paths for key in expected))

    def test_service_install_watches_is_safe_under_concurrent_prune(self) -> None:
        stale_key = (self.tmp / "ws" / "stale-project", False)
        self.service._watched_paths.add(stale_key)
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def run() -> None:
            try:
                barrier.wait()
                self.service._install_watches()
            except Exception as exc:  # pragma: no cover - captured for assertion
                errors.append(exc)

        threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertEqual(errors, [])

    def test_service_branch_switch_trigger_still_emits_change_event_immediately(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})
        json.loads(_socket_recv_line(sock))  # consume initial snapshot

        todo = self.project_dir / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        todo.write_text(DEFAULT_TODO.replace("alpha", "alpha-head-trigger"))
        self.stub_watcher.trigger(self.project_dir / ".git" / "HEAD")

        change_msg = json.loads(_socket_recv_line(sock, timeout=3.0))
        self.assertEqual(change_msg["kind"], "change")
        self.assertEqual(change_msg["project"]["project_id"], "projA")
        self.assertFalse(change_msg["removed"])

    def test_service_deep_file_change_is_seen_after_periodic_rescan(self) -> None:
        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})
        json.loads(_socket_recv_line(sock))  # consume initial snapshot

        todo = self.project_dir / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        todo.write_text(DEFAULT_TODO.replace("alpha", "alpha-rescan"))

        change_msg = json.loads(_socket_recv_line(sock, timeout=3.0))
        self.assertEqual(change_msg["kind"], "change")
        self.assertEqual(change_msg["project"]["project_id"], "projA")

    def test_service_emits_removed_event_when_project_deleted(self) -> None:
        import shutil

        sock = self._connect()
        _socket_send_request(sock, {"kind": "subscribe"})
        json.loads(_socket_recv_line(sock))  # consume initial snapshot

        shutil.rmtree(self.project_dir)
        self.stub_watcher.trigger(self.project_dir)

        change_msg = json.loads(_socket_recv_line(sock, timeout=3.0))
        self.assertEqual(change_msg["kind"], "change")
        self.assertEqual(change_msg["project"]["project_id"], "projA")
        self.assertTrue(change_msg["removed"])

        query = self._connect()
        _socket_send_request(query, {"kind": "list_projects"})
        payload = json.loads(_socket_recv_line(query))
        self.assertEqual(payload["data"]["projects"], [])


# --- Real watchdog integration (optional) -------------------------------


@unittest.skipUnless(af_unix_bind_available(), AF_UNIX_SKIP_REASON)
@unittest.skipUnless(
    HAS_WATCHDOG_INTEGRATION,
    "requires real watchdog installed and WatchdogFileWatcher present",
)
class Stage9WatchdogIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _require_phase3(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-watchdog-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_watchdog_file_watcher_fires_on_real_file_change(self) -> None:
        target = self.tmp / "watched.txt"
        target.write_text("v1")
        received: list[Path] = []
        signal = threading.Event()

        def callback(path: Path) -> None:
            received.append(path)
            signal.set()

        watcher = WatchdogFileWatcher(debounce_ms=80)
        watcher.watch(self.tmp, callback)
        try:
            time.sleep(0.2)  # let the observer settle
            target.write_text("v2")
            self.assertTrue(
                signal.wait(timeout=3.0),
                msg="watchdog callback never fired within 3s",
            )
        finally:
            watcher.stop()

        self.assertGreaterEqual(len(received), 1)

    def test_watchdog_file_watcher_fires_on_file_rename_destination(self) -> None:
        git_dir = self.tmp / ".git"
        _mkdir_resilient(git_dir)
        head_path = git_dir / "HEAD"
        head_path.write_text("ref: refs/heads/main\n")
        received: list[Path] = []
        signal = threading.Event()

        def callback(path: Path) -> None:
            received.append(path)
            signal.set()

        watcher = WatchdogFileWatcher(debounce_ms=80)
        watcher.watch(head_path, callback)
        try:
            time.sleep(0.2)  # let the observer settle
            lock_path = git_dir / "HEAD.lock"
            lock_path.write_text("ref: refs/heads/topic\n")
            os.replace(lock_path, head_path)
            self.assertTrue(
                signal.wait(timeout=3.0),
                msg="watchdog callback never fired for HEAD lockfile rename",
            )
        finally:
            watcher.stop()

        self.assertIn(head_path, received)

    def test_watchdog_file_watcher_unwatch_releases_watch_state(self) -> None:
        target = self.tmp / "project"
        target.mkdir()
        watcher = WatchdogFileWatcher(debounce_ms=80)
        try:
            watcher.watch(target, lambda _path: None, recursive=False)
            self.assertEqual(len(watcher._watches), 1)
            watcher.unwatch(target, recursive=False)
            self.assertEqual(len(watcher._watches), 0)
            self.assertEqual(len(watcher._handlers), 0)
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
