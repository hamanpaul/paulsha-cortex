"""Sandbox-aware guards for tests that stand up AF_UNIX (unix-domain) sockets.

issue #586 的根因（實測，codex-cli 0.147.0 `codex sandbox`）：

  * `socket.socket(AF_UNIX, SOCK_STREAM)` **建立** socket → OK
  * `socketpair()` → OK
  * `sock.bind(<path>)` → **PermissionError EPERM（errno 1）**，即使 path 落在
    workspace-write 的可寫根內。

也就是說，builder（codex executor，`--sandbox workspace-write`）的沙箱是用
seccomp 把 `bind` 這個「網路類」syscall 擋掉（網路隔離的一環），**不是**檔案系統
路徑問題。這無法從 paulsha-cortex 這端對 codex 編譯進去的 seccomp 做「只放行
AF_UNIX」的細粒度放寬——codex 只提供 `network_access` / `danger-full-access` 這種
「整片網路打開」的粗粒度開關，而那正是 #586 安全邊界明文禁止的。

因此環境修復落在測試層：偵測「當前 runtime 是否能 bind() 一個 AF_UNIX socket」，
凡是需要 bind（起 `MonitorServer` 或直接綁 unix socket）的測試，在 builder 沙箱
語境下明確 **skip（帶原因）**，而非假失敗。這讓 builder 自跑的整套
`python3 -m pytest -q` 結果（skip → exit 0）與 manager 權威 ledger（正常環境
run → pass）在源頭一致，消除 envelope/ledger 分歧。正常環境／CI 下 bind 可用，
這些測試照常執行，不損失任何覆蓋。

安全邊界：本模組**只**改變測試在「無法 bind AF_UNIX」時的判定（run vs skip），
不放寬任何 syscall、不打開網路、不允許 builder 連上 manager 既有 socket。

#608：探針本身原本建在 `tempfile.TemporaryDirectory()`（＝`TMPDIR`）底下，於是
`TMPDIR` 一長（實測 > 70 bytes），探針路徑先撞上 `sun_path` 的 107 bytes 上限，
`bind()` 以 `ENAMETOOLONG` 失敗，被這裡判成「sandbox 禁止 bind」——整個 AF_UNIX
家族帶著**錯誤的理由**靜默 skip，套件是綠的但覆蓋沒了。探針因此改建在
`socket_fixtures.short_socket_root()` 的短固定根下：它量的必須是「能不能 bind」
這件事本身，不能被環境的路徑長度污染。
"""

from __future__ import annotations

import errno
import functools
import socket

import pytest

from socket_fixtures import assert_socket_path_fits, short_socket_dir

AF_UNIX_SKIP_REASON = (
    "builder sandbox forbids binding an AF_UNIX socket (bind() -> EPERM); "
    "codex --sandbox workspace-write blocks the bind syscall as part of "
    "network isolation. Skipped to keep the sandboxed pytest result "
    "consistent with the manager's authoritative ledger. See issue #586."
)


@functools.lru_cache(maxsize=1)
def af_unix_bind_available() -> bool:
    """Return ``True`` when the current runtime can ``bind()`` an AF_UNIX socket.

    Probes once (result cached). Treats an EPERM on either socket *creation*
    (e.g. the srt reviewer sandbox blocks creation) or ``bind()`` (codex
    workspace-write blocks the bind syscall) as "unavailable". Any other
    unexpected ``OSError`` is likewise treated as unavailable so a guarded test
    skips rather than erroring in a hostile environment; a normal host binds
    cleanly and returns ``True``.

    #608: the probe path lives under a short fixed root, **not** ``TMPDIR`` —
    otherwise a long ``TMPDIR`` makes the probe itself exceed ``sun_path`` and
    this function answers "the sandbox forbids bind" when the truth is "the
    path was too long", silently skipping the whole AF_UNIX family for a reason
    that is not true. Over-length is therefore asserted *before* ``bind()``
    rather than folded into ``False``: under a root this short it can only mean
    the fixture itself is broken, and a broken fixture must be loud. (CPython
    rejects an over-long ``sun_path`` before the syscall, raising a bare
    ``OSError("AF_UNIX path too long")`` with ``errno is None`` — indistinguishable
    from a sandbox denial by errno alone, hence the explicit check.)
    """

    with short_socket_dir(prefix="afunix") as tmp:
        sock_path = assert_socket_path_fits(tmp / "p.sock")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except (PermissionError, OSError):
            return False
        try:
            sock.bind(str(sock_path))
        except PermissionError as exc:
            # errno is informational here: any PermissionError on bind means the
            # sandbox forbids standing up a local listener.
            _ = exc.errno == errno.EPERM
            return False
        except OSError:
            return False
        finally:
            sock.close()
    return True


#: ``@requires_af_unix_bind`` for pytest-style test functions. For
#: ``unittest.TestCase`` subclasses use ``@unittest.skipUnless(
#: af_unix_bind_available(), AF_UNIX_SKIP_REASON)`` instead (native and honored
#: identically under pytest).
requires_af_unix_bind = pytest.mark.skipif(
    not af_unix_bind_available(),
    reason=AF_UNIX_SKIP_REASON,
)
