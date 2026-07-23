## ADDED Requirements

### Requirement: bootstrap 必須在環境不滿足時明確指引而非單純報錯

`cortex bootstrap` 的 preflight 階段偵測到 Python/Git/repo-root/executor 登入態任一不滿足時 MUST 以 exit 4 結束，並 MUST 針對每一項缺失列出具體可執行的修復命令。

#### Scenario: executor 未登入

- **WHEN** 唯一可用的 executor CLI 未登入
- **THEN** `cortex bootstrap` 於 preflight 階段回報該 executor 未登入
- **THEN** exit code 為 4，輸出包含該 executor 的登入指令建議

### Requirement: --dry-run 必須只預覽不產生 mutation

`cortex bootstrap --dry-run` MUST 只執行 preflight 並預覽將呼叫的 `service install`/`service start` 參數，MUST NOT 呼叫 installer 或啟動服務。

#### Scenario: dry-run 預覽

- **WHEN** 使用者於滿足 preflight 的環境執行 `cortex bootstrap --dry-run`
- **THEN** 輸出列出將執行的 `service install`/`service start` 參數預覽
- **THEN** 系統上不存在任何新安裝的 unit 檔或啟動的行程
