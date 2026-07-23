# porcelain-run-recover-verbs Specification

## Purpose
TBD - created by archiving change porcelain-run-recover. Update Purpose after archive.
## Requirements
### Requirement: run 家族必須映射既有 request types 並提供 --wait

`cortex run` 的 `tick`/`fanout`/`complete`/`work` 子命令 MUST 映射至對應既有 request type 並保持行為等價；MUST 支援 `--wait [--timeout N]`，未帶 `--wait` MUST 以 exit 3 結束並輸出 request_id。

#### Scenario: run tick 未帶 --wait

- **WHEN** 使用者執行 `cortex run tick`
- **THEN** 輸出 `request_id`/`action: tick`/`accepted: true`/`status: pending` 區塊與追蹤提示
- **THEN** exit code 為 3

#### Scenario: run complete 帶 --wait 且成功

- **WHEN** 使用者執行 `cortex run complete --wait` 且底層 request 於逾時前轉為成功 terminal
- **THEN** exit code 為 0

### Requirement: recover 家族必須要求 --actor 且映射既有復原 primitives

`cortex recover slice`/`cortex recover work` MUST 要求 `--actor` 為必填參數；四個子命令 MUST 分別映射至 `slice-action`/`work-action`/`reap-brokers`/`service restart`，行為與既有低階命令等價。

#### Scenario: 缺少 --actor

- **WHEN** 使用者執行 `cortex recover slice <slice_id> retry-build` 但未帶 `--actor`
- **THEN** exit code 為 2（用法錯誤）
- **THEN** 不產生任何 control queue 寫入

