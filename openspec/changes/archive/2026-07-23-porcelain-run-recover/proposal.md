---
status: accepted
work_item: porcelain-run-recover
---

## Goals

用「做什麼」而非「呼叫哪個底層動作」收斂 `tick`/`fanout`/`complete`/`work`/`slice-action`/`reap-brokers` 的命令詞彙，並讓每次 mutation 都顯性回報可追蹤的 request_id（issue #92）。

## Why

現行命令面要求使用者記住內部模型名稱（`slice-action`、`reap-brokers`）才能下對命令；dogfood F19「卡片間推進需 tick / work resume 驅動」與 F23「job 收割掛在 periodic tick」都印證手動驅動時 `run`/`recover` 與 `--wait` 的必要性。B6 以任務導向動詞包裝既有 primitives，不改變其行為。

## What Changes

- 新增 `paulsha_cortex/porcelain/run.py`：`cortex run tick/fanout/complete/work`（映射既有 request types、`--wait`、`--json`）。
- 新增 `paulsha_cortex/porcelain/recover.py`：`cortex recover slice/work/brokers/service`（映射既有復原 primitives、`--actor` 必填、`--wait`、`--json`）。
- `porcelain._FAMILY_MODULES` 登記 run 與 recover 兩模組。
- README 命令面補 run／recover 家族段（R-16）。
- 不提供 `--allow-unsafe` 等危險旁路旗標；既有低階命令行為不變。

## Capabilities

### New Capabilities

- `porcelain-run-recover-verbs`: 高階工作語意的 mutation 契約——run/recover 兩家族映射既有 request/action primitives，統一 request_id 顯性化與 `--wait` 語意。
