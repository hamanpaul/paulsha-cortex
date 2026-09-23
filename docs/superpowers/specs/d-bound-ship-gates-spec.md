---
status: accepted
work_item: d-bound-ship-gates
---

# D-bound gates 與 staged ship 規格（#973 Child C）

## Requirements

本票依賴 #972 與 #973 子票 B2、B3。B3 採信 Candidate D 後，本票負責 D-bound gates 與既有 delivery pipeline 的 staged exact-head ship。不得更改既有 PR/run/issue authority，也不採用 #847 shortcut。

### R1 C evidence expires at D
Candidate 從 C 前進至 D 後，subject/head 為 C 的 build、verify、test、review、maintainer-review、preflight 與 CI evidence 一律不能滿足 D。所有 required evidence 必須可驗證其 subject/head 精確等於 D。

### R2 Pre-push local gates
在任何 feature push、PR create/update 或其他遠端交付副作用前，必須對 D 完成 authoritative build harvest、verify/tests、cross-domain review、policy-required maintainer review，以及 local exact-head preflight。所有 local gates 必須成功且 exact-D；缺漏、失敗、過期、錯 head 或 ambiguous 都停止於 push 前。不得在 D 尚未 push/PR 更新前要求 PR CI 已存在。

### R3 Push exact D, then reconcile PR
全部 pre-push local gates 通過後，才可將 feature branch 推送到 exact D。Push 後建立新 PR，或更新／reconcile 同一 branch 已授權的既有 PR；既有 PR 是合法路徑且不得另開重複 PR。Push 結果、branch head、PR identity 與 PR head 必須在現有 delivery journal/authority 下相符；不確定結果須沿用既有 resume/reconciliation，不得重複或改推其他 SHA。

### R4 Post-push exact-D PR CI
PR CI 只能於 exact D 已 push 且 PR 建立／更新後檢查。要求的 CI rollup 必須明確屬於該 PR 的 exact head SHA D。pending、failed、missing、stale、wrong-head 或 ambiguous CI 均阻止 PR merge、issue closure 與 run completion。先前 C 的 PR CI 不算 D 的通過證據。

### R5 Preserve existing authority
只有現有授權允許的 delivery path 才能 push 或 create/update PR；merge、issue closure、run closure 仍由既有 authority/validator 決定。本票只增加 exact-D gate/order 條件，不自行授予人或 Manager 新的 merge/closure 權限。not-mergeable resume 和其他既有 recovery next_actions 保持可用。

## Verification

使用真實 Git fixture 證明 C-only evidence 被拒絕且 D 的 local gates 通過前沒有 remote side effect。驗證成功順序為 local D gates/preflight → push exact D → create/update existing or new PR → inspect exact-D PR CI。測試 existing PR 由 C 更新至 D、避免 duplicate PR；CI pending/fail/missing/stale/wrong-head/ambiguous 各自阻擋 merge/issue/run closeout。注入 push/PR reconciliation 中斷時，使用既有 journal/resume 避免重複或錯 SHA 副作用。真 PR CI 必須在 push 後確認；不可要求 push 前取得它。

## Boundary

Production scope 限 manager.py 與 work_bridge.py 現有 Candidate-bound gate 和 staged delivery pipeline。重用既有 delivery journal、PR identity、push/merge authority 和 lifecycle；不新增 schema、PR producer authority 或 closure authority。完整 #973/#943 acceptance 保留於 umbrella。
