---
status: accepted
work_item: docs-monitor-load-config
---

# docs-monitor-load-config Design

## Decisions

### D1 不改 code

`load_config` 的 explicit/ambient 行為正確，僅缺文件與測試覆蓋。直接補文件與測試。

### D2 文件結構

在 monitor 文件中新增「Config Loading Semantics」段落：
- **Ambient mode**（不帶 `config_path`）：自動偵測並合併 hippo projects。
- **Explicit mode**（帶 `config_path`）：僅載入指定 config，不合併 ambient。

附範例 YAML 與 CLI 用法。

### D3 測試策略

以 `tmp_path` 模擬 ambient hippo project 存在，分別測試帶/不帶 `config_path` 的合併行為差異。