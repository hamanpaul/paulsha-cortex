"""#503 測試支援：slice-lane dispatch 現在會逐字交付 pinned spec 內容並驗 sha256。

舊 fixture 用不存在的假路徑（`/specs/<id>.md`）加假 hash（`"0" * 64`）預算 `_pinned_inputs`；
dispatch 端改為「讀回 spec 檔、hash 必須等於 pin 值」之後，這種 fixture 會在派工前 fail-closed。
本模組把假路徑落成私有 tmp 目錄下的真檔案，並回傳真 hash，讓既有測試維持原語意。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="cortex-pinned-specs-"))
DEFAULT_SPEC_BODY = "---\ndispatch: auto\n---\n\nfixture spec body\n"


def materialize_spec(spec_path: str | os.PathLike[str], *, body: str = DEFAULT_SPEC_BODY) -> tuple[str, str]:
    """回 ``(absolute_path, sha256)``。

    路徑已是真檔案 → 原路徑＋其實際 hash；否則把它搬到私有 tmp root 下寫出 ``body``。
    同一個假路徑重複呼叫回同一個檔案（內容不改寫）。
    """

    path = Path(spec_path)
    if not path.is_file():
        # 能就地建立（tmp_path 底下的真路徑）就就地建，保住 repo root 推斷；
        # 建不了（`/specs/<id>.md` 這種假路徑）才搬到私有 tmp root。
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError:
            relocated = _ROOT / (path.name if path.is_absolute() else path)
            relocated.parent.mkdir(parents=True, exist_ok=True)
            if not relocated.is_file():
                relocated.write_text(body, encoding="utf-8")
            path = relocated
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()
