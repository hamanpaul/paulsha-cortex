---
status: accepted
work_item: retry-review-all-review-cards
---

# retry-review 要涵蓋所有 review 卡；舊 attempt 帶 evidence 時要能重派

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1365`。
- 現況：run 有多張 review 卡（code-review 加上 adversarial-review）時，同一個 candidate 上 `retry-review` → `resume` 只重派了 code-review。adversarial-review 最新的 job 是舊 pin 留下的，`retry-review` 只把它的 status 改成 failed，`workflow_evidence` 卻還留著。結果：
  - periodic tick 判 job-failed。
  - resume 回 `rejected-review-recovery-mismatch`（`manager.py` 約 18030 行：有 evidence，但 `_workflow_review_evidence_state` 判不出狀態）。
  - `retry-card` 與 `supersede-attempt` 都回 `refuses a card with accepted evidence`。
- 現場：`dangerous-command-blocklist`（#1283，另一個 operator session）。
- 不放寬 review evidence 的採信規則，只修重派的涵蓋範圍與狀態一致性。

## Tasks

- [ ] **T1 RED**：run 有 code-review 與 adversarial-review 兩張卡，舊 attempt 帶 evidence；`retry-review` → `resume` 之後，adversarial-review 無法重派（現行行為）。
- [ ] **T2 涵蓋所有 review 卡**：`retry-review` 重設 review phase 的**所有**卡；被改成 failed 的舊 job，其 `workflow_evidence` 要一併失效或標記為已撤換，讓 resume 能逐張重派。
- [ ] **T3 狀態一致**：job status 與 evidence 不得互相矛盾。被撤換的 attempt 不再被 `_workflow_review_evidence_state`、retry-card、supersede-attempt 當成 accepted evidence。
- [ ] **T4 恢復**：已經卡住的現有 run，升級後執行 `retry-review` → `resume` 能逐張重派並走到 ship。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
