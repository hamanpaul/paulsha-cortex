---
status: accepted
work_item: porcelain-inspect
---

## Goals

把六個分散的唯讀查詢命令收斂成 `cortex inspect` 單一入口，並讓服務運行時真相（版本、exec path drift、殭屍行程）第一次變得可見（issue #89、canary F34）。

## Why

使用者要記住 `status`/`jobs`/`stat`/`list`/`work show`/`doctor` 六種不同查詢方式才能拼出完整系統圖像；dogfood canary 進一步發現 daemon 可能長期跑在已刪除的 venv 上而無人察覺（F1「pipx 快照過期指向已刪 worktree」、F3「舊 daemon 長期跑過期碼而無人察覺」）。B3 以唯讀 porcelain 收斂查詢入口並補上運行時真相，不改變任何底層資料來源。

## What Changes

- 新增 `paulsha_cortex/porcelain/_runtime_probe.py`：服務運行時探測共用函式（模式/unit 狀態/pid/exec path/版本/`stale`）。
- 新增 `paulsha_cortex/porcelain/inspect.py`：`cortex inspect status/job/ready/work/doctor/service`（唯讀、`--json`、exit code 0/1/2/3）。
- `porcelain._FAMILY_MODULES` 登記 inspect 模組。
- README 命令面補 inspect 家族段（R-16）。

## Capabilities

### New Capabilities

- `porcelain-inspect-surface`: cortex 系統狀態的統一唯讀檢視契約——status/job/ready/work/doctor/service 六子命令與 `cortex-porcelain/inspect/v1` schema，含服務運行時版本與殭屍行程偵測。
