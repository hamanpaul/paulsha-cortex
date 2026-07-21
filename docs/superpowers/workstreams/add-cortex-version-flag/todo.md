---
status: accepted
work_item: add-cortex-version-flag
---

# add-cortex-version-flag Todo

## Tasks

- [ ] 將 issue #86、active OpenSpec change 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] 由 coordinator 派工 copilot（gpt-5.4）在 worktree 完成 `cortex --version` build（TDD）。
- [ ] ForeignReview（claude/sonnet）、自動 push、自動開 PR、Copilot bot review、merge 與 archive 全自動閉合。
- [ ] pipx 重裝後 `cortex --version` 輸出正確版本字串，坑清單記錄至 porcelain epic #84。

### Canary follow-ups（實跑發現，不阻斷本批次）

- [ ] dispatch prompt / plan 慣例明示「build 完成必須 git commit」（completion gate 要求 commit，但 prompt 未要求）。
- [ ] policy-exempt label 需在 PR 事件觸發前就位（rerun 沿用舊 event payload）；PR 建立時一併帶 label 或改寫 body 避免引用。
- [ ] claim key 永久唯一 + superseded run 不可重宣告的設計債：評估 claim key attempt 版本化或 tombstone 機制。
- [ ] runtime 殘留（runtime/、.dogfood-specs/）長期應改寫入 PSC_RUN_ROOT。
- [ ] ship 的 single-todo 交付目標限制與「以新增 todo workstream 擴充 authority」的繞法互斥，須擇一制度化。
