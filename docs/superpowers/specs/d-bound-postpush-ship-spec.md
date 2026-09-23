---
status: accepted
work_item: d-bound-postpush-ship
---

# Push 後 exact-D delivery 規格（#973 Child C2）

## Dependencies and boundary

本票為 Red aggregate C 的 issue-backed implementation child。依賴 #972 merged、B3 採信 D、C1 ready-to-push exact-D handoff，以及 #980/#982 create/adopt contracts（只供建立新 PR，非既存 PR 更新證明）。僅修改 `work_bridge.py` 既有 journaled remote delivery pipeline；不改 merge/closeout authority。

## Requirements

### R1 Consume exact ready-to-push D
只接受 C1 明確通過、且 current Candidate/feature source SHA 仍精確等於 D 的 handoff。任何 head drift、失效 handoff 或 authority 不符都停止，不推其他 SHA。

### R2 Existing authorized PR: durable update intent and authenticated read-back
既有 PR 只有在 run 已有 durable authorized PR mapping 時才可更新。Push 前，C2 在既有 delivery journal 中持久化 typed `existing_pr_update_intent`，綁定 run/claim/repository、immutable PR identity、base repo/branch、head repo/branch、expected prior head C、target D 與 candidate/task identity。若已有 durable update result，resume 只能沿該 identity 繼續。PR 身分或 prior head 不符時，停止不 push。

Push exact D 後，以 authenticated GET 讀取該 mapping 指定的同一 PR，逐一核對 repository、PR number/REST id/node id（若 mapping/API 可得）、open state、head repo/branch/SHA D、base repo/branch。只有所有 immutable identity 與 D 都吻合，才在同一 delivery journal 寫 durable update result receipt。若 push 成功但 receipt 遺失，resume 只有在預先 durable 的 C→D update intent 存在、遠端 branch exact D、且 authenticated read-back 是同一 mapped PR exact D 時才能重建 receipt；PR identity 不同、head 仍 C、已到其他 SHA 或讀取不明均 ambiguous/needs_human，不得推別 SHA、改綁其他 PR 或 merge。

### R3 New PR: use create/adopt witness contracts
沒有 durable authorized PR mapping 時，不得把查到的相似 PR 當成可更新對象。新 PR 建立/採用只走 #982 safe create/adopt API 和 #980 same-run successful POST intent/result witness。Lost create receipt 時以 #980 witness + #982 authenticated read-back 對帳同一 PR。只有 repository/base/head 相同而沒有 producer witness 的 external PR，必須 ambiguous/needs_human，保留 PR，不 update、不 merge、不 close。#980/#982 不提供既存 PR C→D update receipt，這個更新證據由 R2 的 C2 journal intent/result 擁有。

### R4 Require post-push exact-D PR CI
只在 push D 且同一 authorized PR authenticated read-back 到 D 之後讀取 required PR CI。要求 rollup 必須成功並綁 exact PR head SHA D。pending、failed、missing、stale、wrong-head 或 ambiguous 均阻止 merge、issue closure與 run completion；若 PR head前進 E，D CI即失效。不得在 push 前要求 PR CI。

### R5 Preserve existing merge and closure authority
Exact-D CI 全綠只是必要 gate；合併仍須通過現有授權/validator。只有既有 successful merge authority 才能關 issue/run。本票不新增 merge permission 或 closeout writer。pending/fail 路徑保留現有 resumable next_actions。若既有 journal 無法承載 R2 typed intent/result，C2 必須在本票內以最小同 journal 擴充完成並驗證；不得以記憶體欄位代替 durable receipt。

## Verification

使用 fake remote、真 Git refs 與 authenticated PR API seam 測 exact D。Fault-inject push/update intent/result receipt 與 PR read-back 的各 crash point。驗 mapped PR 從 C 更新至 D：先寫 intent，push D，GET 同 PR identity/head D，再寫 result；若 result 遺失，只有同一 intent+remote D+authenticated same-PR D 可重建。測 prior head mismatch、PR identity mismatch、head仍C/到E/API ambiguous 均 stop。新 PR 測 #980/#982 witness; external matching repo/base/head without witness → ambiguous/no mutation。測 CI pending/fail/missing/stale/ambiguous、D CI green 後 PR 前進 E、exact-D CI green後走既有 validator。不准 PR CI pre-push、duplicate PR、其他 SHA push 或 unauthorized closeout。

## Sizing

Production 限 `work_bridge.py` 的既有 delivery journal/push/PR/CI pipeline；其既有 PR update 要新增 durable typed intent/result/read-back reconciliation，遠端 GitHub state 與本地 journal/CI/closeout eligibility跨 restart，state consistency=2。domain=0、acceptance=2、stability=0、orchestration=2，完整 accepted fix-standard score **6 / Yellow**。#980/#982 僅是 create/adopt contract；C2 自行擁有 mapped PR update receipt。
