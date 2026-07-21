## ADDED Requirements

### Requirement: cortex CLI 必須提供頂層 --version

`cortex --version` MUST 輸出目前安裝的套件版本並以 exit 0 結束，不得要求子命令，且不得改變任何既有子命令行為。

#### Scenario: 已安裝環境查詢版本

- **WHEN** 使用者在已安裝 paulsha-cortex 的環境執行 `cortex --version`
- **THEN** stdout 輸出單行 `cortex <version>`（version 來自套件 metadata）
- **THEN** exit code 為 0

#### Scenario: 套件 metadata 不可得

- **WHEN** 於未安裝套件 metadata 的開發環境執行 `cortex --version`
- **THEN** stdout 輸出 fallback 版本字串 `cortex 0.0.0+unknown`
- **THEN** exit code 為 0
