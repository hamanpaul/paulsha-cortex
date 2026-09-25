---
status: draft
work_item: existing-pr-journal-authority-readback
domain_breadth: 0
state_consistency: 2
---

## Tasks

- [ ] 1.1 重驗 #983/#966/#1054/#1063/#1064/#1065/#1068/#1069 的 exact landed heads、API 和 CI；任一硬依賴未落地即停止，不 intake。
- [ ] 1.2 指派不同於 #1068/#1069 的 delivery-journal integration maintainer，序列化與 #983/#1015 的 work_actions.py 工作。
- [ ] 2.1 驗證 #1069 只輸出唯讀 journal identity/revision；work_actions.py 僅消費其完成的 same-run gate，不重做 reset eligibility。
- [ ] 2.2 透過 #983 API authenticated-read 原 PR，核對同 repository/PR number、OPEN、head==Candidate，並 exact-load row/revision/payload hash。
- [ ] 2.3 對 exact new-authority result 做 durable read-back no-op；必要更新時只以 #983 conditional API 更新原 row 的 authority digest/vector，保留 identity/transaction result/time。
- [ ] 2.4 實作 same-identity idempotency、typed conflict/unknown stop 及 unknown conditional read-back；若 API 不足，停止並修訂 issue。
- [ ] 3.1 覆蓋 row load、conditional update、durable confirmation、fresh read-back 前後 crash/re-entry 和 fresh-process 結果。
- [ ] 3.2 使用 #983 run/Candidate/PR identity 的 isolated stubs；#1049 OPEN/DIRTY fail-closed，push/create/update/merge/close spies 全為零。
- [ ] 3.3 只改 work_actions.py production source；focused/repo tests、configured preflight/OpenSpec、PR-context policy、exact-head CI 與 review 分別記錄。
- [ ] 4.1 交付 implementation evidence 後再由 owner 評估 merge/runtime/parent closure；本 planning artifact 不宣稱產品完成。
