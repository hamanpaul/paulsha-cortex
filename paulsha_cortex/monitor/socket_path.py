"""AF_UNIX socket 路徑長度契約（#608）。

## 為什麼需要一個模組

`struct sockaddr_un` 的 `sun_path` 在 Linux 上是 **108 bytes 的固定陣列**（見
`sys/un.h`），其中最後一個 byte 必須留給結尾 NUL，因此**可用路徑上限是 107
bytes**。超過時 `bind()` / `connect()` 直接失敗，CPython 把它翻成一句沒有數字、
沒有路徑、也沒有出處的 ``OSError: AF_UNIX path too long``。

這條上限是**環境形狀**（`TMPDIR`、`PSC_RUN_ROOT` 有多長）決定的，不是程式對錯
決定的。而 manager 的 gate ledger 對 candidate 重跑全套 pytest 是採信的硬 gate
（#540）——環境形狀一旦能讓 gate 紅掉，「這次交付真的沒過」與「這台機器的暫存
根太長」在 ledger 上就長得一模一樣，合格 candidate 會被 `GateContradictionError`
拒絕。#565（`/tmp/.git` 污染）、#586（sandbox 擋 `bind`）之後，這是同族第三例。

## 兩條防線

1. **測試不碰這條線**：測試用的 socket 一律建在短固定根下（見
   `tests/socket_fixtures.py`），與 `TMPDIR` 長度無關。環境再怎麼長，全套 pytest
   都不會因為 `sun_path` 紅掉，因此**不可能**產生一筆看起來像 gate 失敗的 ledger
   記錄。這是本 issue 的主修法。
2. **真的超限時 fail closed 且說得出原因**：operator 若把 `PSC_RUN_ROOT` 設成一個
   過長的路徑，production 仍會失敗——但要失敗在一個**指名道姓**的
   :class:`SocketPathTooLongError` 上（附實際 byte 數、上限、超出量與路徑），而
   不是退化成「服務起不來」「socket 沒在聽」這種與缺陷混淆的症狀。

## 為什麼不用 abstract namespace

Linux 的抽象命名空間（`sun_path` 以 `\\0` 開頭）確實不吃檔案系統路徑長度，issue
裡也點名了這個選項，但本 repo **刻意不採用**：

* 抽象 socket 不是檔案系統物件，**沒有任何權限位**。monitor socket 現在是
  `chmod 0o600`（見 `server.serve_forever`），改抽象等於把它對同 network
  namespace 內的所有行程開放——這是安全退步，不是環境健壯化。
* `server` 既有的 stale-socket 回收、`live monitor already listening` 偵測、
  teardown 時的 identity 比對 unlink，全都建立在「socket 是一個檔案」上。
* 抽象 socket **仍然**吃 108 bytes 的上限，只是不吃路徑長度；它並沒有真的移除
  這條契約，只是換一個地方踩。

因此本模組的角色是「把 108 這個常數與它的判定收在一處」，讓 production 與測試
共用同一份真實來源，而不是各自寫死一個數字。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "SUN_PATH_MAX_BYTES",
    "SUN_PATH_MAX_USABLE_BYTES",
    "SocketPathTooLongError",
    "socket_path_length",
    "socket_path_fits",
    "socket_path_limit_detail",
    "validate_socket_path",
]

#: `struct sockaddr_un.sun_path` 的固定長度（Linux；含結尾 NUL）。
SUN_PATH_MAX_BYTES = 108

#: 實際可用的路徑 byte 數——最後一個 byte 保留給結尾 NUL。
SUN_PATH_MAX_USABLE_BYTES = SUN_PATH_MAX_BYTES - 1


class SocketPathTooLongError(ValueError):
    """AF_UNIX socket 路徑超過 `sun_path` 上限。

    刻意**不**繼承 :class:`OSError`：呼叫端普遍用 ``except OSError`` 表示
    「transport 出事了（沒在聽／連不上）」，而路徑過長是**設定／環境的前置條件
    不成立**，兩者的處置完全不同。掛在 :class:`ValueError` 下，既有的 transport
    錯誤處理就不會把它誤讀成「monitor 沒在跑」。
    """


def socket_path_length(path: str | os.PathLike[str]) -> int:
    """回傳該路徑寫進 `sun_path` 時佔用的 **byte** 數（不含結尾 NUL）。

    用 :func:`os.fsencode` 而非 ``len(str(path))``：kernel 收的是 bytes，非 ASCII
    路徑（例如中文目錄名）一個字元可能佔 3 bytes，用字元數會低估。
    """

    return len(os.fsencode(os.fspath(path)))


def socket_path_fits(path: str | os.PathLike[str]) -> bool:
    """該路徑是否塞得進 `sun_path`（含結尾 NUL）。"""

    return socket_path_length(path) <= SUN_PATH_MAX_USABLE_BYTES


def socket_path_limit_detail(
    path: str | os.PathLike[str],
    *,
    role: str = "AF_UNIX socket",
) -> str:
    """回傳一句可直接放進錯誤訊息／doctor detail 的診斷。

    刻意把「實際幾 bytes、上限幾 bytes、超出幾 bytes、哪一段太長」全部寫出來：
    讀到這句話的人（或 agent）不必再自己量一次，就能判斷這是環境問題而非缺陷。
    """

    actual = socket_path_length(path)
    excess = actual - SUN_PATH_MAX_USABLE_BYTES
    return (
        f"{role} path exceeds the AF_UNIX sun_path limit: {actual} bytes > "
        f"{SUN_PATH_MAX_USABLE_BYTES} usable bytes (sun_path is "
        f"{SUN_PATH_MAX_BYTES} bytes including the trailing NUL); "
        f"{excess} bytes too long. path={os.fspath(path)!s} "
        "— this is an environment/configuration limit, not a defect in the "
        "code under test; shorten the socket root (PSC_RUN_ROOT / TMPDIR). "
        "See issue #608."
    )


def validate_socket_path(
    path: str | os.PathLike[str],
    *,
    role: str = "AF_UNIX socket",
) -> Path:
    """路徑塞得下就原樣回傳，塞不下就 raise :class:`SocketPathTooLongError`。

    fail closed 的理由見模組 docstring：超限時**不得**降級成「靜默不啟動」或
    「連不上」，那正是會被誤記成交付失敗的形狀。
    """

    if not socket_path_fits(path):
        raise SocketPathTooLongError(socket_path_limit_detail(path, role=role))
    return Path(path)
