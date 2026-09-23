---
status: accepted
work_item: d-bound-prepush-gates
---

# Push 前 D-bound local gates 規格（#973 Child C1）

## Requirements

本票為 D-bound ship aggregate C 的 issue-backed implementation child。依賴 #972 完成及 B3 已將真 merge Candidate D 採信。C1 僅負責 Manager/本機 gate path；post-push remote delivery 歸 C2。

### R1 C evidence expires at D
Candidate 從 C 推進至 D 後，subject/head 為 C 的 build、verify、test、review、maintainer-review、preflight 與 CI evidence 一律不能滿足 D。#847 same-content shortcut 不適用。每筆必需 evidence 都須綁 exact D。

### R2 Rerun authoritative local gates
以 D 重新取得 authoritative build harvest、verify/tests、cross-domain review，以及 policy-required maintainer review。Missing、failed、stale、wrong-head、ambiguous evidence 都 fail closed。

### R3 Exact-D local preflight
所有 local gates 通過後，執行 local exact-head preflight，確認 target、Candidate 和 evidence 都是 D。此為 push 前檢查，不可要求 D 尚未 push/PR 更新前就有 PR CI。

### R4 Ready-to-push handoff only
在所有上述檢查通過時，只輸出既有 delivery path 可消費的 ready-to-push exact-D decision。C1 不 push、查 PR、建立/更新 PR、合併或關閉 issue/run；任何 gate 失敗都在遠端副作用前停止。

### R5 Preserve authority
重用 Candidate/evidence 與既有 local preflight 契約；不新增 schema、delivery journal transition 或 remote authority。C2 負責消費 ready-to-push D 並執行後續 staged ship。

## Verification

以真 Git D fixture 和只對 C 成功的 evidence 證明 C evidence 被拒絕。測試 D build/verify/tests/review/required-maintainer-review/preflight 缺漏、失敗、過期、錯 head、歧義都阻止 ready-to-push，並確認 push/PR client 未呼叫。完整 local pass 只產生 exact-D handoff，未 push、未查 PR CI。測試 #847 shortcut 不得繞過 D evidence。

## Boundary

Production 限 manager.py Candidate-bound gate invalidation/rerun 與 work_bridge.py local exact-D preflight handoff。所有 remote delivery、PR CI、merge/closure 由 C2 使用既有 pipeline 負責。
