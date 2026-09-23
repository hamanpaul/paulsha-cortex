---
status: accepted
work_item: d-bound-ship-gates
---

# D-bound staged ship 設計（#973 Child C）

## Decisions

### D1 D 是所有 gate 的 exact subject
B3 推進 Candidate C→D 後，依現有 Candidate-bound evidence matching 使 C evidence 失效。逐一確認 build、verify、test、review、preflight 和 CI gate 都沒有 #847 shortcut 或其他 stale-evidence bypass。

### D2 將 local 與 PR CI 分成 push 前後兩階段
Push 前先完成 D 的 authoritative local gates、required reviews 和 exact-head preflight。PR CI 尚未能觀察新 D，因為它要等 push 及 PR create/update；所以 local phase 不查 PR CI，也不把 local preflight 冒充 PR CI。

### D3 只推已驗證的 exact D
Local phase 通過才執行既有 push journal/action，source 和 destination head 都必須是 D。Push 成功後按既有 PR identity 更新既有 PR 或建立新 PR。遇到已存在的授權 PR 必須 reconciliation/reuse，不另開重複 PR。中斷時走現有 journal/resume。

### D4 PR CI 是 push 後 merge gate
在 PR head 已指向 D 後，讀取該 PR 對 exact D 的 required check rollup。只有綠燈且無缺漏/歧義時才讓既有 merge authority 繼續；其他狀態保留 open/pending 與 resume path，不關 issue/run。

### D5 不擴張 authority
本票改進 gate ordering 與 exact binding；復用現有 journal、PR mapping、merge validator 和 closure authority。沒有新的 writer/schema/權限模型。

## Sequence

1. Confirm accepted Candidate is D and C-bound evidence cannot pass.
2. Run build harvest, verify/tests, cross-domain review, required maintainer review, and local exact-D preflight.
3. If any local gate fails or is unresolved, stop before push and PR mutation.
4. Push exact D through the existing journaled path.
5. Create a PR or update/reconcile the authorized existing PR for this branch; verify its head is D.
6. Read required PR CI after push and require exact PR head D.
7. Continue only through existing merge/issue/run authority after CI passes; otherwise preserve resumable pending state.

## Verification matrix

| Condition | Required behavior |
|---|---|
| C-only evidence | Reject as stale; no remote effect |
| Local D gate failed/missing/stale | Stop before push/PR |
| Local D gates pass | Push D, then reconcile the mapped PR |
| Existing PR on branch | Update/reuse same PR; no duplicate |
| Post-push PR head differs from D | Stop before merge/closure |
| PR CI pending, failed, missing, stale or ambiguous | Keep PR/run open and resumable |
| Exact-D required CI green | Existing authorized merge path may continue |

## Sizing boundary

Two production modules give domain_breadth=1. This scope coordinates the existing delivery journal, Git push result, PR mapping/head, remote CI rollup and lifecycle closeout across restart boundaries, so state_consistency=2. With complete accepted planning and fix-standard mechanics (acceptance=2, stability=0, orchestration=2), the actual score is 7 / Red. Do not lower state consistency to force Yellow; retain the full Child C AC and retain the full aggregate requirements in #973 and use accepted C1/C2 as the Yellow implementation intake units.
