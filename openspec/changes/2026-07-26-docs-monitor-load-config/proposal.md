---
status: accepted
work_item: docs-monitor-load-config
---

## Goals

文件化 `monitor/config.py:183` 的行為變更：`load_config(config_path=...)` 顯式傳入 config_path 時不再合併 ambient hippo projects，並新增測試覆蓋此行為。

## Why

`#143` 回報：`paulsha_cortex/monitor/config.py:183` 變更行為——`load_config(config_path=...)` 顯式傳入 config_path 時不再合併 ambient hippo projects。這是正確行為（explicit = fully explicit），但未文件化。需要補文件並加測試。

## What Changes

- `docs/**`：記錄 `load_config` 的 explicit vs ambient 行為——顯式傳入 config_path 時 fully explicit，不合併 ambient hippo projects。
- `tests/`：新增測試覆蓋顯式 config_path 不合併 ambient hippo projects 的行為。

## Capabilities

### Modified Capabilities

- `monitor-config`：文件化 `load_config` 的 explicit/ambient 合併語意；測試覆蓋。