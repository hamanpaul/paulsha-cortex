---
status: accepted
work_item: autosync-carry-forward-review
---

# autosync 後沿用舊 review 的 run 要能交付：review 與交付兩端語意一致

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1356`（從 #1345 拆出）。
- 現況：#1345 處理了「autosync 後重跑 code-review」的路徑。依 #1311 的設計，autosync 後只重跑 verify 與 ship probe，code-review 沿用舊 candidate 的結論；但 ship 時 `work_bridge._workflow_evidence_envelope`（約 2747 行）要求 review job 的 `subject_head` 等於新的 `candidate_head`，因此拋出 `delivery requires one canonical review evidence job`。
- 現場：`system-deploy-ops-defects`（另一個 operator session）、`worktree-containment-authority`（PR #1348）。
- 不放寬 exact-candidate 原則：交付的必須是 verify 過的 candidate，review 結論要能追溯到它。

## Tasks

- [ ] **T1 RED**：端到端重現 autosync 後沿用舊 review、ship 回 `delivery requires one canonical review evidence job`。
- [ ] **T2 選定語意並實作**（二選一，在 PR 中寫明理由）：(a) autosync 後一律重跑 code-review；或 (b) 交付端接受「review 針對 autosync 前的 candidate，且新 candidate 是它的 autosync 後代（candidate 自身 diff 不變，只多了 main 的內容）」，並在 evidence 記錄這條鏈。review 與交付兩端必須一致。
- [ ] **T3 端到端**：沿用 review 與重跑 review 兩種路徑都能走到 ship；#1345 的行為不退化。
- [ ] **T4 設計說明**：更新 #1311 相關文件，明確寫出 autosync 後重跑哪些卡。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
