# onboarding-documentation Specification

## Purpose
TBD - created by archiving change onboarding-docs. Update Purpose after archive.
## Requirements
### Requirement: 新手必須能依 Quickstart 文件獨立完成第一個 workflow

`docs/onboarding/quickstart.md` MUST 涵蓋 pipx install → `cortex bootstrap` → 第一個 workflow 的完整步驟，且 MUST NOT 要求讀者具備 deck/spec 內部概念的先備知識。

#### Scenario: 新手依 Quickstart 操作

- **WHEN** 一位未使用過 cortex 的使用者依序執行 Quickstart 文件列出的命令
- **THEN** 使用者可在文件描述的步驟內完成第一個可觀察的 workflow 結果
- **THEN** 過程中不需要另外查閱 Concepts 文件即可完成操作

### Requirement: 全七份文件必須以相對路徑表示，不得殘留個人識別

七份 `docs/onboarding/*.md` 與 README 導覽段內的所有範例指令 MUST 使用 `~`、`$HOME`、環境變數或相對路徑；MUST NOT 出現個人絕對路徑、使用者名或雇主／廠商識別。

#### Scenario: 路徑衛生自查

- **WHEN** 對七份文件執行機敏標記掃描（R-21 secret-scan 範疇）
- **THEN** 掃描結果不含任何個人絕對路徑或使用者名

