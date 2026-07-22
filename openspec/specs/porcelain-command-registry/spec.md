# porcelain-command-registry Specification

## Purpose
定義 `cortex` porcelain 家族的註冊表與頂層分派契約，讓各家族模組可獨立登記命令，同時維持既有 coordinator CLI 行為不被未註冊或故障家族影響。
## Requirements
### Requirement: porcelain 命令必須經註冊表登記與分派

porcelain 家族命令 MUST 以（名稱、單行說明、`run(argv) -> int`）登記於 `paulsha_cortex/porcelain` 註冊表；`cortex <name>` MUST 由頂層路由查表分派並透傳退出碼；重複名稱 MUST 於登記時立即失敗。

#### Scenario: 已登記命令分派

- **WHEN** 家族模組已 register `fake` 且使用者執行 `cortex fake --x`
- **THEN** 註冊表的 `run(["--x"])` 被呼叫且其回傳值成為行程退出碼

#### Scenario: 未登記名稱維持現行為

- **WHEN** 使用者執行未登記的 `cortex status`
- **THEN** 行為與導入註冊表前完全一致（透傳 coordinator）

### Requirement: help 必須呈現 --version 與非空 porcelain 區段

`cortex --help` 與 usage MUST 列出 `--version`；註冊表非空時 MUST 動態附加 porcelain commands 區段，為空時 MUST 不出現該區段。

#### Scenario: 空註冊表

- **WHEN** B1 狀態（無家族登記）下執行 `cortex --help`
- **THEN** 輸出含 `--version` 行且不含 porcelain 區段
