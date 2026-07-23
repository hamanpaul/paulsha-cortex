# porcelain-init-sample Specification

## Purpose
TBD - created by archiving change porcelain-init-sample. Update Purpose after archive.
## Requirements
### Requirement: init-sample 產出的 spec 必須一律為 dispatch hold

`cortex init-sample` MUST 呼叫既有 `deck compile --emit` 產出 spec，且產出的 spec frontmatter 的 `dispatch` 欄位 MUST 為 `hold`；本命令 MUST NOT 提供任何將 `dispatch` 改為 `auto` 的旁路。

#### Scenario: 產出 sample spec

- **WHEN** 使用者執行 `cortex init-sample --task "示範一個 feature"`
- **THEN** 產出的 spec 檔 frontmatter 含 `dispatch: hold`
- **THEN** 輸出包含 spec 檔案路徑與必補欄位清單

### Requirement: init-sample 必須列出必補欄位清單與 deck verify 檢核命令

輸出 MUST 列出 `plan`/`target_branch`/`verification` 三項必補欄位的合法形狀說明，並 MUST 附上可直接執行的 `deck verify` 命令。

#### Scenario: 未知 combo

- **WHEN** 使用者執行 `cortex init-sample --task "..." --combo not-a-real-combo`
- **THEN** exit code 為 2
- **THEN** 不呼叫 `deck compile`、不產生任何檔案

