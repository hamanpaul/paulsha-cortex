"""#610：測試套件的「不得出實網」守衛。

## 為什麼需要

`builder` 在 codex sandbox（network allowlist）內跑單體 `python3 -m pytest -q`
會被 egress 攔截直接殺掉整個 process（`Network access to "github.com" was
blocked` → `exit -1`），誠實的 builder 因此永遠無法宣告 `passed`。真兇是
**測試自身的 hermeticity 缺陷**：測試在真實 repo checkout 內觸發
`git fetch origin main`（origin = github.com），或直接 spawn 真的 `gh`。

正常環境（Manager ledger、CI）網路是通的，所以這種缺陷四年來從未被測試結果
揭露——只有在斷網／allowlist 環境才會爆。守衛的目的就是把「靜默出網」變成
**當場失敗並指名測試**，不必等到某個 sandbox 把整個 run 殺掉才發現。

## 兩層攔截

1. **socket 層**：`socket.socket.connect` / `connect_ex` / `create_connection`。
   白名單：AF_UNIX（#586 的 sun_path 家族要用）、AF_INET/AF_INET6 的 loopback
   （127.0.0.0/8、`::1`、`localhost`、未指定位址），其餘 family（AF_NETLINK…）
   一律放行。這層擋的是 in-process 的 `urllib` / `http.client` / `asyncio`。

2. **subprocess 層**：`subprocess.Popen.__init__`。實測的四起事故全部發生在
   這一層——socket patch 看不到子行程自己開的 socket。`git` 只有真的會走
   transport 的 subcommand（fetch/pull/push/clone/ls-remote）才檢查，並且用
   `git ls-remote --get-url` 把 `url.<local>.insteadOf` 改寫後的**實際** URL
   解出來再判 loopback／本機路徑；`gh`、`curl`、`wget` 這類純網路 client 則
   一律視為違規（`--version` / `--help` 例外）。

## 已知邊界

守衛住在 pytest 的 process 裡，只看得到「本 process 直接 spawn 的子行程」。
測試若先 spawn 一個 python 子行程、由該子行程再去 `git fetch`，守衛看不到
（實測目前全套沒有這種路徑）。真要覆蓋孫行程得走 sitecustomize/LD_PRELOAD，
代價與風險都高於它擋下的殘量，暫不做。

## 逃生口

`PSC_TEST_ALLOW_NETWORK=1`（在 `pytest_configure` 當下讀，早於會清掉 `PSC_*`
的 autouse fixture）整場停用守衛，供本機除錯用。程式內另有
:func:`allow_network` context manager 給守衛自己的測試與「本質上就是需要網路
的整合測試」使用。
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import threading
from pathlib import PurePath
from typing import Any, Iterable, Sequence

__all__ = [
    "ALLOW_ENV",
    "NetworkGuardViolation",
    "allow_network",
    "check_command",
    "check_socket_address",
    "current_test",
    "drain_violations",
    "enabled",
    "install",
    "set_current_test",
    "uninstall",
]

ALLOW_ENV = "PSC_TEST_ALLOW_NETWORK"

#: `gh` 之外仍會直接出網的 client；`git` 另外處理（它多數 subcommand 是本機的）。
NETWORK_BINARIES = frozenset(
    {
        "gh",
        "hub",
        "glab",
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nc",
        "ncat",
        "telnet",
        "pip",
        "pip3",
        "npm",
        "npx",
        "yarn",
        "pnpm",
    }
)

#: 這些旗標只印本地資訊，不出網。
OFFLINE_ONLY_FLAGS = frozenset({"--version", "-V", "--help", "-h", "help", "version"})

#: `git` 真的會開 transport 的 subcommand。
GIT_NETWORK_SUBCOMMANDS = frozenset(
    {"clone", "fetch", "pull", "push", "ls-remote", "remote-http", "remote-https"}
)

#: `git <sub>` 之後、真正的位置參數之前可以出現、而且**自己吃掉下一個 token**
#: 的選項（其餘 `-x` / `--x` 一律當成不吃參數的旗標）。
GIT_VALUE_OPTIONS = frozenset(
    {
        "-o",
        "--upload-pack",
        "--receive-pack",
        "--exec",
        "--depth",
        "--shallow-since",
        "--shallow-exclude",
        "--reference",
        "--separate-git-dir",
        "--branch",
        "-b",
        "--origin",
        "--template",
        "--config",
        "-c",
        "--server-option",
        "--negotiation-tip",
        "--jobs",
        "-j",
        "--recurse-submodules-default",
        "--filter",
        "--push-option",
        "--receive-pack",
        "--refmap",
    }
)

_SCP_LIKE = re.compile(r"^[^/@]*@[^/:]+:")

_LOOPBACK_HOSTNAMES = frozenset({"", "localhost", "localhost.localdomain", "ip6-localhost"})


class NetworkGuardViolation(RuntimeError):
    """測試嘗試對外連線。訊息一律帶上測試 nodeid 與被連的目標。"""


_state = threading.local()
_lock = threading.Lock()
_current_test: str | None = None
_violations: list[str] = []
_enabled = False
_installed = False

_real_socket_connect = None
_real_socket_connect_ex = None
_real_create_connection = None
_real_popen_init = None


# --- 內部：re-entrancy ---------------------------------------------------------
#
# 守衛自己要 spawn `git ls-remote --get-url` 來解 insteadOf；那次 spawn 不能再
# 被守衛檢查一輪（會無限遞迴）。


def _reentrant() -> bool:
    return getattr(_state, "inside", False)


@contextlib.contextmanager
def _reentrancy():
    previous = getattr(_state, "inside", False)
    _state.inside = True
    try:
        yield
    finally:
        _state.inside = previous


@contextlib.contextmanager
def allow_network():
    """暫時放行對外連線——守衛自身的測試與 network-marked 整合測試專用。"""

    previous = getattr(_state, "allowed", False)
    _state.allowed = True
    try:
        yield
    finally:
        _state.allowed = previous


def _bypassed() -> bool:
    return not _enabled or _reentrant() or getattr(_state, "allowed", False)


# --- 對外狀態 -----------------------------------------------------------------


def enabled() -> bool:
    return _enabled


def current_test() -> str:
    return _current_test or "<session>"


def set_current_test(nodeid: str | None) -> None:
    global _current_test
    _current_test = nodeid


def drain_violations() -> list[str]:
    """取出並清空本測試累積的違規紀錄。"""

    with _lock:
        drained = list(_violations)
        _violations.clear()
    return drained


def _violate(kind: str, target: str, detail: str = "") -> NetworkGuardViolation:
    message = (
        f"測試 {current_test()} 嘗試對外連線（{kind}）：{target}"
        f"{f' — {detail}' if detail else ''}\n"
        "測試必須 hermetic：改用本機 fixture（tmp git repo 當 remote、注入假的 "
        "runner/provider），不得打真實網路。\n"
        f"本機除錯若真的需要網路：{ALLOW_ENV}=1 python3 -m pytest ..."
    )
    with _lock:
        _violations.append(message)
    return NetworkGuardViolation(message)


# --- socket 層 -----------------------------------------------------------------


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        # AF_INET 的位址一定是 str；不是的話交給 socket 自己去噴型別錯誤。
        return True
    candidate = host.strip()
    if candidate.lower() in _LOOPBACK_HOSTNAMES:
        return True
    if candidate.startswith("::ffff:"):
        candidate = candidate[len("::ffff:") :]
    try:
        parsed = ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def check_socket_address(family: Any, address: Any) -> None:
    """判定一次 socket connect；違規時 raise :class:`NetworkGuardViolation`。

    公開出來讓守衛自己的測試能在**不真的發出封包**的前提下驗證判準。
    """

    if _bypassed():
        return
    if family not in (socket.AF_INET, socket.AF_INET6):
        # AF_UNIX（#586）、AF_NETLINK…：本機通訊，不是 egress。
        return
    if not isinstance(address, tuple) or not address:
        return
    if _is_loopback_host(address[0]):
        return
    port = address[1] if len(address) > 1 else "?"
    raise _violate("socket", f"{address[0]}:{port}")


def _guarded_connect(self, address):
    check_socket_address(getattr(self, "family", None), address)
    return _real_socket_connect(self, address)


def _guarded_connect_ex(self, address):
    check_socket_address(getattr(self, "family", None), address)
    return _real_socket_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    if not _bypassed() and isinstance(address, tuple) and address and not _is_loopback_host(address[0]):
        port = address[1] if len(address) > 1 else "?"
        raise _violate("socket", f"{address[0]}:{port}")
    return _real_create_connection(address, *args, **kwargs)


# --- subprocess 層 -------------------------------------------------------------


def _argv_of(args: Any, shell: bool) -> list[str]:
    if isinstance(args, (str, bytes, PurePath)):
        text = args.decode() if isinstance(args, bytes) else str(args)
        if not shell:
            return [text]
        try:
            return shlex.split(text)
        except ValueError:
            return [text]
    if isinstance(args, Sequence):
        out: list[str] = []
        for token in args:
            out.append(token.decode() if isinstance(token, bytes) else str(token))
        return out
    return []


def _program_name(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    return PurePath(argv[0]).name


def _raw_git_output(argv: list[str], cwd: str | None) -> str | None:
    """用未被守衛攔截的 subprocess 問 git，失敗一律回 None。"""

    with _reentrancy():
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _split_git_command(argv: Sequence[str], cwd: str | None) -> tuple[str, list[str], str | None]:
    """回傳 ``(subcommand, 其後的參數, 作用的 repo 目錄)``。"""

    repo_dir = cwd
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "-C":
            if index + 1 < len(argv):
                repo_dir = os.path.join(repo_dir or os.getcwd(), argv[index + 1])
            index += 2
            continue
        if token == "-c":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(argv):
        return "", [], repo_dir
    return argv[index], list(argv[index + 1 :]), repo_dir


def _first_positional(tokens: Iterable[str]) -> str | None:
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in GIT_VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _is_local_git_url(url: str) -> bool:
    candidate = url.strip()
    if not candidate:
        # 解不出 URL：多半是 `git -C <非 repo>`，那道 git 指令自己就會先失敗，
        # 不會出網。這裡 fail-open，避免守衛製造偽陽性。
        return True
    if candidate.startswith(("/", "./", "../", "~")):
        return True
    lowered = candidate.lower()
    if lowered.startswith("file://"):
        return True
    if "://" in candidate:
        host = candidate.split("://", 1)[1].split("/", 1)[0]
        host = host.rsplit("@", 1)[-1]
        if host.startswith("["):
            host = host[1 : host.find("]")] if "]" in host else host[1:]
        else:
            host = host.split(":", 1)[0]
        return _is_loopback_host(host)
    if _SCP_LIKE.match(candidate):
        host = candidate.split("@", 1)[1].split(":", 1)[0]
        return _is_loopback_host(host)
    # 沒有 scheme 也沒有 `user@host:` → 相對路徑，本機。
    return True


def _check_git(argv: Sequence[str], cwd: str | None) -> None:
    subcommand, rest, repo_dir = _split_git_command(argv, cwd)
    if subcommand not in GIT_NETWORK_SUBCOMMANDS:
        return
    target = _first_positional(rest)
    if subcommand == "clone":
        raw = target or ""
        resolved = _raw_git_output(["git", "ls-remote", "--get-url", raw], repo_dir) if raw else ""
    else:
        raw = target or "origin"
        resolved = _raw_git_output(
            ["git", "-C", str(repo_dir or os.getcwd()), "ls-remote", "--get-url", raw],
            None,
        )
    effective = resolved if resolved else raw
    if _is_local_git_url(effective):
        return
    raise _violate(
        "git",
        effective,
        f"git {subcommand} (repo={repo_dir or os.getcwd()})",
    )


def check_command(args: Any, *, shell: bool = False, cwd: Any = None) -> None:
    """判定一次 subprocess spawn；違規時 raise :class:`NetworkGuardViolation`。

    公開出來讓守衛自己的測試能在**不真的 spawn**的前提下驗證判準。
    """

    if _bypassed():
        return
    argv = _argv_of(args, shell)
    if not argv:
        return
    program = _program_name(argv)
    cwd_text = str(cwd) if cwd is not None else None
    if program == "git":
        _check_git(argv, cwd_text)
        return
    if program in NETWORK_BINARIES:
        if any(token in OFFLINE_ONLY_FLAGS for token in argv[1:]):
            return
        raise _violate("subprocess", " ".join(argv[:6]), f"{program} 是純網路 client")


def _guarded_popen_init(self, *args, **kwargs):
    try:
        command = kwargs["args"] if "args" in kwargs else (args[0] if args else None)
        shell = kwargs.get("shell", args[8] if len(args) > 8 else False)
        cwd = kwargs.get("cwd", args[9] if len(args) > 9 else None)
    except Exception:  # pragma: no cover - 只防禦奇怪的呼叫形狀
        command, shell, cwd = None, False, None
    if command is not None:
        check_command(command, shell=bool(shell), cwd=cwd)
    return _real_popen_init(self, *args, **kwargs)


# --- 安裝／卸載 ---------------------------------------------------------------


def install() -> bool:
    """安裝守衛；回傳是否真的啟用（逃生口打開時回 ``False``）。"""

    global _enabled, _installed
    global _real_socket_connect, _real_socket_connect_ex, _real_create_connection
    global _real_popen_init

    if os.environ.get(ALLOW_ENV, "").strip() in {"1", "true", "yes", "on"}:
        _enabled = False
        return False
    if _installed:
        _enabled = True
        return True

    _real_socket_connect = socket.socket.connect
    _real_socket_connect_ex = socket.socket.connect_ex
    _real_create_connection = socket.create_connection
    _real_popen_init = subprocess.Popen.__init__

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
    subprocess.Popen.__init__ = _guarded_popen_init

    _installed = True
    _enabled = True
    return True


def uninstall() -> None:
    global _enabled, _installed
    if not _installed:
        _enabled = False
        return
    socket.socket.connect = _real_socket_connect
    socket.socket.connect_ex = _real_socket_connect_ex
    socket.create_connection = _real_create_connection
    subprocess.Popen.__init__ = _real_popen_init
    _installed = False
    _enabled = False
