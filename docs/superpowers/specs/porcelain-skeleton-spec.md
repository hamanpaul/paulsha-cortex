---
status: accepted
work_item: porcelain-skeleton
---

# porcelain-skeleton Specification

porcelain 計畫（epic #84）B1：為七家族建立可外掛的路由骨架，並承接 #120 的 help 可發現性缺口。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` 為準。

## Requirements

### Porcelain 命令註冊表

`paulsha_cortex/porcelain/` SHALL 提供命令註冊表：每個家族以（名稱、單行說明、`run(argv) -> int` 進入點）登記；名稱重複 MUST 立即失敗。B1 的正式註冊清單 SHALL 為空（家族由 B2+ 各自登記）。

### 頂層路由整合

`cortex <name>` 於 `paulsha_cortex/cli.py` SHALL 在既有命令之後、coordinator 透傳之前查詢註冊表並分派；未註冊名稱的行為 MUST 與現行完全一致（透傳 coordinator）。既有命令（install/deck/monitor/list/work/doctor/relay-hook 與 coordinator 面）行為 MUST 不變。

### Help 可發現性（承接 #120）

`cortex --help` 與 `_USAGE` SHALL 列出 `--version`；註冊表非空時 `cortex --help` SHALL 動態附加 porcelain commands 區段（B1 為空時不出現該區段）。

### 限制

stdlib-only；`test_zero_dependency_runtime` MUST 續綠；新增單元測試（先 RED 後 GREEN）覆蓋註冊表契約、路由分派與 help 文案。
