## ADDED Requirements

### Requirement: Terminal live canary 必須留下可審查 evidence

Terminal docs-only canary MUST記錄 mapped issue、planner/builder/reviewer independence domain、verification、archive、preflight、current-HEAD review、merge commit與done projection。任一 gate 未通過時 MUST維持未完成。

#### Scenario: Canary 完整閉合

- **WHEN** 全部 strict gates與remote closure成立
- **THEN** 操作文件記錄 issue、PR、merge commit與CompletionRecord evidence
- **THEN** Monitor投影 work item為 `done`

#### Scenario: 任一 gate 未完成

- **WHEN** brainstorm、ForeignReview、archive、preflight、review或remote closure任一缺失
- **THEN** workflow維持 `on-going`並帶 attention facet
