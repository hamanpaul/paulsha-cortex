"""trust-root 隔離（R0.5 D6）Phase 1：純程式碼、不需 root 的地基。

本子套件實作 `docs/superpowers/specs/trust-root-isolation-spec.md` 的 **Phase 1**
（見 spec §R10「落地」）。Phase 1 **只**交付不需 root、不改部署拓撲的部分：

- `registry`：R1 trust-root 資產登記表（宣告式單一真相，供 Phase 2 權限產生器取用）
  ＋雙向等式測試。
- `selfcheck`：R3 Manager 啟動自檢，用登記表對照現行部署實況並輸出結構化診斷；
  Phase 1 **只 WARN**（不 fail-closed，那是 Phase 2 R3 切換）。
- `capability`：R7 敏感 action 的 capability 通道與降級運轉開關；無 capability 時
  fail-closed 拒絕，支援 operator 逐案核可（裁決 10-5）。

**Phase 1 不提供**（需 Phase 2 的 OS 邊界才完整，見各模組 docstring 與 PR body）：
真正的不可寫強制（同 UID 下檔案 mode 對 owner 無效）、跨 process／重啟持久的
single-use nonce ledger、capability 通道的 Unix socket OS 隔離。Phase 1 建立的是
**契約與 fail-closed 語意**，Phase 2 以獨立 UID／目錄 owner 把它變成 kernel 強制。
"""
from __future__ import annotations

from . import capability, registry, selfcheck

__all__ = ["registry", "selfcheck", "capability"]
