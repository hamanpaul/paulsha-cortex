## ADDED Requirements

### Requirement: Live canary 必須留下可審查的 terminal evidence

Unified lifecycle 的 docs-only live canary MUST在操作文件記錄 mapped issue、planner/builder/reviewer independence domain、deterministic verification、archive、preflight、current-HEAD review、merge commit 與 done projection結果。任何未通過 gate MUST標示為未完成，不得宣稱 canary 通過。

#### Scenario: Canary 完整閉合

- **WHEN** docs-only canary 的所有 strict gates與遠端 closure 均成立
- **THEN** 操作文件記錄可追溯的 issue、PR、merge commit與CompletionRecord evidence
- **THEN** Monitor 將 work item 投影為 `done`

#### Scenario: 任一 gate 未完成

- **WHEN** brainstorm、ForeignReview、archive、preflight、current-HEAD review或remote closure任一缺失
- **THEN** 文件不得宣稱 canary通過
- **THEN** workflow維持 `on-going`並帶 `needs_human`或`blocked` facet
