"""測試用的 AF_UNIX socket 目錄 fixture（#608）。

## 問題

Linux 的 `sun_path` 只有 108 bytes（含結尾 NUL，可用 107，見
`paulsha_cortex.monitor.socket_path`）。pytest 的 `tmp_path` 與
`tempfile.mkdtemp()` 都掛在 `TMPDIR` 底下，路徑長度因此是**環境給的**：

    <TMPDIR>/pytest-of-<user>/pytest-<N>/<test-name-30-chars><M>/monitor.sock
    └──── 環境決定 ────┘└──────────── 固定約 75 bytes ────────────┘

`TMPDIR` 是 `/tmp` 時總長 79 bytes，一切正常；CI／sandbox 給一個 40～70 bytes
的暫存根時就直接撞牆。實測（0817，本 repo `origin/main`）：

| `len(TMPDIR)` | 全套 pytest 結果 |
| --- | --- |
| 4（`/tmp`） | 全綠 |
| 47 | **4 failed**（`test_monitor_work_api` 三測 ＋ `test_doctor` 一測） |
| 66 | **17 failed**（再加上 `test_stage9_project_monitor_service` 十三測） |
| 72 | 全綠，但 AF_UNIX 家族**全部靜默 skip**（見下） |

## 為什麼這是 P1

manager 的 gate ledger 對 candidate 重跑全套 pytest 是採信的硬 gate（#540）。
上表的 `failed` 進 ledger 之後，與「這次交付真的沒過」**長得一模一樣**——合格
candidate 會被 `GateContradictionError` 拒掉。#565（`/tmp/.git` 污染）、#586
（sandbox 擋 `bind`）之後，這是同族第三例。

`len(TMPDIR) > 70` 那一列更陰險：#586 的 `af_unix_bind_available()` 探針自己也建
在 `TMPDIR` 底下，探針路徑先超限 → `bind()` 失敗 → 被判成「sandbox 禁止 bind」→
整個 AF_UNIX 家族帶著**錯誤的理由**靜默 skip。套件是綠的，覆蓋卻沒了。

## 修法

socket 一律建在**短固定根**下，與 `TMPDIR` 無關（見 `short_socket_root()`）。
需要 socket 的測試不再用 `tmp_path` / `mkdtemp()` 當 socket 的家，環境形狀因此
無法再讓全套 pytest 紅掉或靜默 skip。

不改用抽象命名空間（`\\0` 前綴）的理由見
`paulsha_cortex.monitor.socket_path` 的模組 docstring——抽象 socket 沒有權限位，
會把現行 `chmod 0o600` 的 monitor socket 對整個 network namespace 打開。

只有**需要 bind／connect 的路徑**該搬到這裡。同一個測試的工作區、設定檔、快照
等等照舊留在 `tmp_path`：那些沒有長度上限，搬過來只會失去 pytest 的保留與清理。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from paulsha_cortex.monitor.socket_path import (
    SUN_PATH_MAX_USABLE_BYTES,
    socket_path_fits,
    socket_path_length,
)

__all__ = [
    "SOCKET_NAME_HEADROOM_BYTES",
    "assert_socket_path_fits",
    "make_short_socket_dir",
    "short_socket_dir",
    "short_socket_root",
]

#: 目錄底下還要留給 socket 檔名（含中間層）的 byte 數。
#: 本 repo 最長的實際檔名是 `run/project-monitor.sock`（24 bytes），取 40 留餘裕。
SOCKET_NAME_HEADROOM_BYTES = 40

#: 每個測試目錄的隨機字尾長度（hex 字元數）。短到不吃預算、長到不會撞名。
_TOKEN_HEX_BYTES = 5  # -> 10 個 hex 字元

#: 候選根，依「短且穩定」排序。`/tmp` 在 Linux 上一定存在且最短；`/run/user/<uid>`
#: 是 systemd 的 per-user runtime dir（tmpfs，正是給 socket 用的）；最後才回頭問
#: `tempfile.gettempdir()`——它會讀 `TMPDIR`，是本模組要避開的東西，只當保底。
_STATIC_ROOT_CANDIDATES = ("/tmp", "/var/tmp")


def _candidate_roots() -> tuple[Path, ...]:
    candidates = [Path(item) for item in _STATIC_ROOT_CANDIDATES]
    try:
        candidates.append(Path("/run/user") / str(os.getuid()))
    except AttributeError:  # pragma: no cover - 非 POSIX
        pass
    # 保底：連 /tmp 都不可寫的環境還是要有東西可用，即使它可能很長。
    candidates.append(Path(tempfile.gettempdir()))
    return tuple(candidates)


def _usable(root: Path) -> bool:
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        return False
    # 容器目錄 + token 目錄 + 檔名都要塞得下，否則這個根等於沒用。
    container = root / _container_name()
    probe = container / ("0" * (_TOKEN_HEX_BYTES * 2 + 4))
    return socket_path_length(probe) + 1 + SOCKET_NAME_HEADROOM_BYTES <= SUN_PATH_MAX_USABLE_BYTES


def _container_name() -> str:
    # per-uid：共用主機上不同使用者不會撞到彼此的 0o700 目錄。
    try:
        return f"psc-sock-{os.getuid()}"
    except AttributeError:  # pragma: no cover - 非 POSIX
        return "psc-sock"


def short_socket_root() -> Path:
    """回傳一個**短且與 `TMPDIR` 無關**的 socket 根，必要時建立它。

    刻意不快取：測試會 monkeypatch `TMPDIR` 來驗證「換了環境仍然短」，快取會讓
    那個驗證變成只在量第一次的結果。這個函式只做幾次 `stat`，不值得快取。
    """

    for root in _candidate_roots():
        if not _usable(root):
            continue
        container = root / _container_name()
        try:
            container.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            continue
        # 容器可能是別人（別的 uid、別的 umask）留下的既有目錄；不可寫就換下一個
        # 候選，而不是等到 `mkdir` 子目錄時才炸在一個看起來像產品缺陷的地方。
        if not os.access(container, os.W_OK | os.X_OK):
            continue
        return container
    raise RuntimeError(
        "找不到任何可用且夠短的 AF_UNIX socket 根（試過 "
        f"{', '.join(str(item) for item in _candidate_roots())}）；"
        f"sun_path 可用上限為 {SUN_PATH_MAX_USABLE_BYTES} bytes。見 issue #608。"
    )


def make_short_socket_dir(*, prefix: str = "t") -> Path:
    """在短根底下建一個空目錄並回傳；呼叫端負責清理。

    給 `unittest.TestCase` 用（搭配 `addCleanup`）。pytest 風格的測試用
    :func:`short_socket_dir` 或 conftest 的 `socket_dir` fixture。
    """

    root = short_socket_root()
    # prefix 只為了 `ls /tmp` 時看得出是誰留下的；截短以免吃掉預算。
    label = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_")[:8]
    path = root / f"{label}-{secrets.token_hex(_TOKEN_HEX_BYTES)}"
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


@contextlib.contextmanager
def short_socket_dir(*, prefix: str = "t") -> Iterator[Path]:
    """:func:`make_short_socket_dir` 的 context manager 版本（結束即刪除）。"""

    path = make_short_socket_dir(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def assert_socket_path_fits(path: Path) -> Path:
    """路徑塞不進 `sun_path` 時當場 fail，並回傳原路徑。

    給測試自我檢查用：fixture 的保證一旦被某次改動破壞，要炸在「fixture 壞了」
    這句話上，而不是炸在某個看起來像產品缺陷的 `bind()` 失敗上。
    """

    if not socket_path_fits(path):
        raise AssertionError(
            f"socket fixture 產生的路徑超過 sun_path 上限："
            f"{socket_path_length(path)} > {SUN_PATH_MAX_USABLE_BYTES} bytes：{path}"
        )
    return path
