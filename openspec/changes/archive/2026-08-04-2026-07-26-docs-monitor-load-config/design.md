---
status: accepted
work_item: docs-monitor-load-config
---

# docs-monitor-load-config Design

## Decisions

### D1 行為正確，僅缺文件與測試

`load_config(config_path=...)` 顯式傳入時不合併 ambient hippo projects 是正確行為（explicit = fully explicit）。不需改 code，僅需補文件與測試。

### D2 文件位置

在 `docs/` 新增或在既有 monitor 文件中新增段落，記錄 `load_config` 的 explicit/ambient 合併語意：
- 不帶 `config_path`：ambient mode，合併偵測到的 hippo projects。
- 帶 `config_path`：explicit mode，僅載入指定 config，不合併 ambient。

### D3 測試策略

測試以 fixture 建立 ambient hippo project（`tmp_path` 模擬），驗證顯式 `config_path` 時不合併、不帶時合併。