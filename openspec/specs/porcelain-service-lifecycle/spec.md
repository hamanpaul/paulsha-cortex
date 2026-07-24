# porcelain-service-lifecycle Specification

## Purpose
TBD - created by archiving change porcelain-service. Update Purpose after archive.
## Requirements
### Requirement: service 生命週期命令必須明確區分三種運行模式

`cortex service` 的全子命令 MUST 明確標示目前運行模式為 `systemd`／`fallback`／`未安裝` 三者之一；MUST NOT 在任一模式下回報假成功。

#### Scenario: systemd 不可用時的 install

- **WHEN** 在無 systemd 的環境執行 `cortex service install`
- **THEN** 輸出明確標示「systemd 不可用，改用 fallback」並列出 fallback 模式下的能力差異
- **THEN** fallback 安裝成功時 exit 0，不得以靜默 pass 呈現假成功

#### Scenario: stop 必須連動 timer

- **WHEN** 使用者於 systemd 模式執行 `cortex service stop`
- **THEN** `<instance>.service` 與 `<instance>.timer` 皆被停止
- **THEN** `service status` 之後不再顯示 timer 為 active

### Requirement: service status 必須顯示運行中版本並偵測 exec path drift

`cortex service status` MUST 顯示運行模式、pid、運行中版本與 exec path；exec path 指向的 venv 不存在時 MUST 標示為潛在殭屍行程。

#### Scenario: 查詢運行中版本

- **WHEN** 使用者執行 `cortex service status`
- **THEN** 輸出包含目前運行中程式的版本字串與 exec path

