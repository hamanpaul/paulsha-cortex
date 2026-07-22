## ADDED Requirements

### Requirement: inspect 家族必須提供統一唯讀檢視且不得 mutation

`cortex inspect` MUST 提供 `status`/`job`/`ready`/`work`/`doctor`/`service` 六個唯讀子命令，包裝既有查詢邏輯；MUST NOT 寫入任何狀態或經 control queue 提交請求。

#### Scenario: 查詢 job 狀態

- **WHEN** 使用者執行 `cortex inspect job <job_id>`
- **THEN** 輸出內容與既有 `cortex stat <job_id>` 等價
- **THEN** 不產生任何 control queue 寫入

#### Scenario: human 與 json 輸出一致

- **WHEN** 使用者對同一查詢對象分別執行 `cortex inspect status` 與 `cortex inspect status --json`
- **THEN** 兩者呈現的欄位語意內容一致，`--json` 輸出可被標準 JSON parser 解析且含 `"schema": "cortex-porcelain/inspect/v1"`

### Requirement: inspect service 必須偵測 exec path 與 venv 是否存在

`cortex inspect service` MUST 顯示運行模式、unit 狀態、pid、exec path 與版本；當 exec path 指向的 venv 已不存在時 MUST 於輸出中標示為潛在殭屍行程。

#### Scenario: unit 指向已刪除的 venv

- **WHEN** systemd unit 的 `ExecStart` 指向一個已被刪除的 venv 路徑
- **THEN** `cortex inspect service` 的輸出標示該行程為 stale/殭屍，並附下一步建議（重新 `service install` 或 `service restart`）
