---
status: accepted
work_item: porcelain-request
---

## Goals

把 mutation request 從「5 秒 timeout 後不可見」變成可 list/show/wait/logs 的顯性物件（deep research P0 摩擦、canary F8 實證）。

## Why

coordinator 的 mutation CLI 最多等 control response 5 秒；timeout 後 request 其實已持久化並會被 daemon 處理，但使用者沒有任何追蹤面，只能翻 `~/.agents/control` 原始檔。B2 以唯讀 porcelain 補上 request UX，不動 queue 格式與 single-writer。

## What Changes

- 新增 `paulsha_cortex/porcelain/request.py`：`cortex request list/show/wait/logs`（唯讀、`--json`、exit code 0/1/2/3）。
- `porcelain._FAMILY_MODULES` 登記 request 模組。
- README 命令面補 request 家族段（R-16）。
- 刻意**不提供** `request submit`：提交一律走語意化命令（B6 `run` 家族）。

## Capabilities

### New Capabilities

- `porcelain-request-tracking`: mutation request 的唯讀追蹤契約——list/show/wait/logs 與 `cortex-porcelain/request/v1` schema。
