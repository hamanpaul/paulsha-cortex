## ADDED Requirements

### Requirement: request 必須可唯讀追蹤

`cortex request list/show/wait/logs` MUST 只讀 control root 與 job registry、不寫入任何狀態、不經 control queue；daemon degraded 時 MUST 仍可運作。

#### Scenario: timeout 後追蹤

- **WHEN** mutation CLI 因 5 秒 timeout 報錯、request 已落地 `requests/`
- **THEN** `cortex request show <id>` 顯示 pending 與參數摘要
- **THEN** daemon 處理完成後 `cortex request wait <id>` 以 exit 0（或 terminal failure exit 1）結束

#### Scenario: wait 逾時

- **WHEN** `cortex request wait <id> --timeout 1` 於 request 尚未 terminal 時執行
- **THEN** exit code 為 3 且輸出追蹤提示

### Requirement: 輸出必須雙軌且 schema 版本化

全子命令 MUST 支援 `--json`：頂層含 `"schema": "cortex-porcelain/request/v1"`、snake_case 欄位、UTC ISO-8601 時間。

#### Scenario: 機器可讀 list

- **WHEN** 腳本執行 `cortex request list --json`
- **THEN** stdout 為單一 JSON 文件、含 schema 欄位與 requests 陣列（每項含 request_id/type/state/建立時間）
