---
status: accepted
work_item: porcelain-skeleton
---

## Goals

建立 porcelain 七家族的可外掛路由骨架（B1），消除後續批次對共享路由檔的衝突，並承接 #120 的 `--version` help 可發現性。

## Why

porcelain 計畫（epic #84）B2–B7 六個家族批次都要在 `cortex` CLI 掛新命令；若各批次直接改 `cli.py` 的 if-chain 與靜態 help，序列衝突與 R-16 同步負擔會隨批次數放大。B1 以註冊表把「登記」下放給家族模組，路由與 help 一次外掛化。

## What Changes

- 新增 `paulsha_cortex/porcelain/`（`PorcelainCommand`、`COMMANDS`、`register`、`load_commands`；B1 家族清單為空）。
- `paulsha_cortex/cli.py`：coordinator 透傳前查註冊表分派；`_USAGE`/`_HELP` 補 `--version`；`--help` 動態附加非空 porcelain 區段。
- 新增註冊表與路由單元測試（TDD）。
- 不改任何既有命令行為；stdlib-only。

## Capabilities

### New Capabilities

- `porcelain-command-registry`: porcelain 命令的登記與頂層路由契約——家族模組自行 register，`cli.py` 統一分派與 help 呈現。
