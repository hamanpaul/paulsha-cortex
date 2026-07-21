---
status: accepted
work_item: add-cortex-version-flag
---

# add-cortex-version-flag Follow-ups

canary 實跑期間發現、不阻斷本批次但需回饋 porcelain epic 的後續事項。

## Tasks

- [ ] dispatch prompt / plan 慣例明示「build 完成必須 git commit」（completion gate 要求 commit，但 prompt 未要求）。
- [ ] policy-exempt label 需在 PR 事件觸發前就位（rerun 沿用舊 event payload，label 後掛不生效）；PR 建立時一併帶 label 或改寫 body 避免引用。
- [ ] claim key 永久唯一 + superseded run 不可重宣告的設計債：評估 claim key attempt 版本化或 tombstone 機制（歷史以 -v2 識別繞過）。
- [ ] daemon cwd=repo root 後 runtime 殘留（runtime/、.dogfood-specs/）落在 repo 內：已入 .gitignore，長期應改寫入 PSC_RUN_ROOT。
