# lifecycle-terminal-live-canary Specification

## Purpose
定義 issue #31 docs-only terminal lifecycle canary 的可審查 closure 契約，確保所有 strict gates 與 remote closure 成立後才投影為 `done`，任一 gate 缺失時維持未完成。
## Requirements
### Requirement: Terminal live canary 必須留下可審查 evidence

Terminal docs-only canary MUST記錄 mapped issue、planner/builder/reviewer independence domain、verification、archive、preflight、current-HEAD review、merge commit與done projection。任一 gate 未通過時 MUST維持未完成。

#### Scenario: Canary 完整閉合

- **WHEN** 全部 strict gates與remote closure成立
- **THEN** 操作文件記錄 issue、PR、merge commit與CompletionRecord evidence
- **THEN** Monitor投影 work item為 `done`

#### Scenario: 任一 gate 未完成

- **WHEN** brainstorm、ForeignReview、archive、preflight、review或remote closure任一缺失
- **THEN** workflow維持 `on-going`並帶 `needs_human`或`blocked` attention facet
