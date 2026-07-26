---
status: accepted
work_item: docs-monitor-load-config
---

# docs-monitor-load-config Specification

`#143`：文件化 `monitor/config.py:load_config` 的 explicit vs ambient 行為並補測試。

## Requirements

### R1 文件化 explicit/ambient 行為

`docs/**` MUST 記錄 `load_config` 的行為：
- 不帶 `config_path`：ambient mode，合併偵測到的 hippo projects。
- 帶 `config_path`：explicit mode，僅載入指定 config，不合併 ambient hippo projects。

### R2 測試覆蓋

`tests/` MUST 含測試驗證：
- 顯式 `config_path` 時不合併 ambient hippo projects。
- 不帶 `config_path` 時仍合併 ambient。

### R3 限制

- docs-only + test-only；不改 code。
- `python3 -m policy_check --repo .` 0 fail。