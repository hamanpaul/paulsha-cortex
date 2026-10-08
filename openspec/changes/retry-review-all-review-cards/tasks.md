## Tasks

- [x] **T1 RED**：run 有 code-review 與 adversarial-review 兩張卡，舊 attempt 帶 evidence；`retry-review` → `resume` 之後，adversarial-review 無法重派（現行行為）。
- [x] **T2 涵蓋所有 review 卡**：`retry-review` 重設 review phase 的**所有**卡；被改成 failed 的舊 job，其 `workflow_evidence` 要一併失效或標記為已撤換，讓 resume 能逐張重派。
- [x] **T3 狀態一致**：job status 與 evidence 不得互相矛盾。被撤換的 attempt 不再被 `_workflow_review_evidence_state`、retry-card、supersede-attempt 當成 accepted evidence。
- [x] **T4 恢復**：已經卡住的現有 run，升級後執行 `retry-review` → `resume` 能逐張重派並走到 ship。
- [x] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
