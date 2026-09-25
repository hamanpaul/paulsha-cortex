---
status: draft
work_item: todo-authority-existing-run-recovery
---

# Todo Authority 前進後既有 Candidate／PR Run 恢復規格（#1055）

## 範圍與目前狀態

本規格只處理已存在 Candidate 與 PR 的 run，在唯一、由 owner 發布的 canonical Todo authority 到位後，如何安全地重新驗證並恢復原 run 的 delivery。主要回歸案例是 #983 的 run workflow-52d048b72adbd5cae06f、Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc、PR #1049。

本規格是 Draft。#1055 的執行仍 hard-blocked；本文件不把 #1054 Draft PR 的行為當成已存在契約。沒有 repository intake、產品實作、live run 操作或 #983 run 狀態修改授權。

## R1 — 執行前置契約必須已實際發布

正式恢復前，#1063、#1064、#1065 與 #1054 必須全部合併至 main。#1063 凍結 Todo provenance/Tasks qualification 與 path existence guard；#1064 凍結 trusted Monitor generation/input watermark；#1065 凍結 fresh-generation WorkAuthority reader；#1054 凍結 Manager admission/diagnostic 與 claim-after-Todo digest continuity。其實際 issue IDs、accepted contract、exact main revision 必須在執行前從 GitHub 和 main 重新讀取；不得沿用任何 Draft PR 的預期行為。

依已發布契約重新載入 WorkAuthority，且同一 fresh snapshot 必須證明唯一 canonical Todo path、source owner、matching work_item/issue provenance、完整 task authority 與新鮮 Monitor observation。source revision 未確認、provider degraded/stale、Todo=0 或多筆、link override 尚未反映、來源 path 無法驗證時，一律停止且不得重設 run。

registry raw-revision CAS 須先確認 #966 已合併；delivery-journal conditional-write 契約亦須先確認 #983 已合併，或 main 已具備可證明等價的 accepted API。#983 現有 PR #1049 的衝突修復不在本票範圍；不得修改、推送、合併或重建該 PR。#1015/#1017 的 exact-D push、existing-PR C→D update witness、post-push CI 與 closeout 也不在本票範圍；本票只讀回保留原 Candidate/head 的同一 PR，不建立 update intent。缺少可用的 durable conditional write/read-back 時，只能 fail closed，不得自行新增第二份 writer。

## R2 — 使用 exact source authority 與 CAS tuple

Manager 必須以正式 WorkAuthority loader 取得 old/new authority；不得由 caller 自報 digest、由檔名推 Todo、以 issue body 代替 scanner source，或直接修改 run 的 claim/source revision。

每次恢復判斷都需比較下列 tuple。tuple 中任何欄位不明、重複或變動即拒絕，不做猜測、合併或自動重試。

- Registry：WorkflowRun 所在 durable registry 的 exact raw revision；run_id、repo、work_id、status、phase、retry_classification、claim_key、source_revision、candidate_head、verified_head、PR refs、gate steps/refs、build/job/evidence bindings；同 run active job 為零。
- Source authority：old 與 new WorkAuthority 各自的 snapshot_hash、provider_revision、按 canonical source identity 排序的完整 source_revisions、mapped issue/Todo/PR/OpenSpec refs，以及正式 work_authority_digest。完整 old/new source vector 必須保留，不以 registry projection revision 代替；registry:1025 這類序號只是 run read-model revision。
- Candidate/PR：精確 Candidate commit SHA；唯一既有 PR 的 repository、number、node identity（若 provider 提供）、state、head repository/branch/SHA、base repository/branch。PR head 必須等於 Candidate，PR 必須仍 open；merged、closed、未知、duplicate 或 head drift 都不符合本路徑。
- Delivery journal：同一 run 的 row identity、canonical payload hash 與 durable revision，且由已接受的 #983 conditional-write contract 比對。不得以 reload 後立即重讀取代 CAS，也不得從缺列重新合成一筆已提交的 result。
- Authorization：本次為明確 operator resume，且來源已通過 #1054 contract 的 fresh snapshot。periodic scan/auto-claim 不得自行啟動此 recovery transaction。

同一 tuple 必須在決定與 registry transition 前重新讀取。外部 provider 與本地 registry 無共同 transaction；因此即使 registry CAS 成功，也必須在派發 verify/review job 前再次確認來源 freshness、exact Candidate、PR identity/head/state 和無 active job。第二次 readback 不一致時保持 gates pending/needs-human，不派 job、不執行遠端 side effect。

## R3 — 只允許同 run、同 Candidate 的受限恢復

只有以下條件全部成立，才可沿原 run 恢復：

1. 唯一 canonical Todo 已由授權 owner 發布並經 #1054 已發布契約驗證。
2. run 仍是同一 repo/work_id 的 ongoing run，停在 verify 或 review recovery boundary，沒有 active job。
3. run 的 persisted Candidate 完整且不變，既有 PR 唯一、open，head 完全等於 Candidate。
4. 新 WorkAuthority source vector/digest 與 run 舊 claim era 的差異可由上述 CAS 精確描述；source mapping/revision 本身已由正式 provider 確認。
5. delivery journal old row 與 binding 可按 #983 exact conditional contract讀回，沒有同 run 的 competing/stale writer。
6. 所有必要 evidence refs/hashes可讀且仍能以既有 validators 重驗。

Transition 必須透過 Registry 的受限 exact-CAS API 原子提交：只更新同一 run 的 claim_key/source_revision，將 verify/review steps 與其 gate evidence 設為 pending/invalidated，清除 verified_head，phase 回到 verify，並記錄一次 authority-restart audit。保留 Candidate、build/repair commits、builder job 與其原始 evidence bytes/refs、existing PR refs、run identity 與舊 claim era。不得改寫或重綁歷史 JobRegistry binding、舊 evidence、舊 source vector，也不得 supersede/abandon 原 run。

CAS expected tuple 不符時原子拒絕且 durable state 不變。不得先 reset 後補檢 PR/source facts，不得在 failed CAS 後靜默 retry transition。

## R4 — 重新執行的 gate 僅限 exact Candidate

Authority 前進後，舊 verify/review gate 不可直接採信。恢復後的 verify 與 review 必須重跑在同一 exact Candidate，並以新 WorkAuthority digest/claim era 綁定新 evidence。Build 不重跑；Candidate 不重建、不 rebase、不 reset base。

每個新 job/evidence 需綁相同 run_id、repo、work_id、Candidate SHA 與新 claim_key；歷史 job/evidence 保留舊 binding，仍可稽核但不得作為新 gate pass。Source revision 變動造成舊 binding mismatch 時，禁止改寫 job row 來修補相容性。

任何 gate 因 source/provider freshness、PR head/state、Candidate 或 claim era 漂移而無法確定，都停在明確 needs-human reason；不派 builder、不跳過 gate、不將舊 verified_head 移植到新 authority。

## R5 — Existing PR delivery 只讀核對，不重複遠端操作

此恢復路徑限定 PR 已存在且 PR head 精確等於原 Candidate。Manager 對 repository/PR number/identity/state/base/head 做 authenticated read-back；只可使用讀取操作。若 #1049 仍 open 但與 main 衝突，保留 stop 並交由 #972/#973 範圍處理；本票不得解衝突、push、建立第二個 PR、merge、close 或改寫 PR body。

未知的 GitHub request 結果必須讀回同一 PR identity；不得重送 push/create/merge。PR 缺失、duplicate、merged/closed、PR head 不同於 Candidate 或 provider degraded 時，回傳 typed fail-closed reason，保持原 run/Candidate/PR 可追查。

## R6 — Journal recovery 以同一 identity 冪等 read-back

對 delivery journal 的變更只可使用已 landed 的 #983 conditional-write/serialization API。所有 update 必須綁同一 run、PR、Candidate、authority digest 與 row revision；同一 transaction identity 加相同 canonical payload digest 可回傳既有 durable result，不得新增 row/event；同 identity 不同 payload、不同 row hash、stale journal revision、missing intent/result ordering 或 competing writer 一律 conflict/unknown 並停止。

每個 crash point都需定義重啟結果：CAS 前 crash → 原狀重新判斷；CAS 後、job 前 crash → exact persisted run 只可由 operator resume，重讀 authority 再決定是否派同一類 gate；verify/review job 已建立但未回應 → 先查 exact JobRegistry binding，不可另派重複 job；journal commit outcome unknown → 用同 identity conditional read-back 判定 committed/conflict/unknown，不得重送外部操作。無法證明結果時保留未完成狀態，不宣稱成功。

## R7 — 不得用 abandon 或 new run 遮蓋舊 delivery

本票明確排除 pre-Candidate abandon/recover-pre-candidate 路徑。當 run 已有 Candidate 或 PR 時，任何不符合同 run recovery 前置條件的狀態都不能改標 abandoned/superseded、清除 Candidate/PR refs、建立替代 run，或宣稱交付已處置。原 run、Candidate、PR 與已知 journal rows 必須保留，並顯示 typed stop reason。

若之後需要新 run adoption/ownership transfer，必須先建立另一張有 owner、exact disposition acceptance、source/PR CAS 與 duplicate-prevention contract 的 issue；不能在本票中把未知的舊候選清理策略混入 recovery。

## R8 — 以 #983 事實做唯讀 regression，不操作 live run

測試須用真 Manager/WorkAuthority/Registry fixture 與 stub GitHub facts，不操作正式 #983 run 或 PR。fixture 固定如下：

- repo/work_id：hamanpaul/paulsha-cortex / delivery-journal-conditional-commit。
- run：workflow-52d048b72adbd5cae06f；目前現象為 review phase、needs-human、stop reason multiple-delivery-targets-unsupported。
- Candidate：7ba7e877c94ff4eee72ba796ea9f8962953ed5cc。
- Existing PR：#1049；目前 live snapshot 為 OPEN、head=Candidate、merge state DIRTY/CONFLICTING。測試不呼叫 GitHub write，不改 PR。
- captured WorkAuthority projection at 2026-09-25T03:40:18Z included #983, PR #1049, accepted plan/spec/design refs and workflow source; no canonical Todo source. Workflow projection revision registry:1025 is not a source authority revision. The current CLI attention projection offers abandon; this recommendation is explicitly outside this acceptance path.

Positive fixture adds exactly one valid owner-published Todo source with new source revision, exact same Candidate and exact open PR, no active job, and a matching journal CAS baseline; after explicit resume, old verify/review evidence is invalidated, new gates run once on Candidate, then delivery read-back returns the existing PR without push/create/merge.

Negative fixtures include: #1054 prerequisites absent/Draft; owner/source provenance invalid; Todo missing/ambiguous; stale/degraded Monitor; digest changes between claim and Todo publication; sorted source revision vector mismatch; registry revision/run/claim/source/Candidate/PR refs CAS mismatch; active job; old/new claim-era cross-bind; verify evidence head/hash stale; PR missing/duplicate/closed/merged/head mismatch; journal stale row/hash or unknown conditional write; crash/re-entry at each boundary; repeated resume. Each negative asserts no duplicate job, push, PR creation, merge, false remote closure, rewrite of old job/evidence, abandon, or replacement run.

## R9 — issue and test ownership stay within the recovery boundary

#1055 preserves the complete existing Candidate/PR scope. If official sizing is Red, the created children #1068/#1069/#1070 separate registry exact-CAS, same-run authority restart/reverify, and existing-PR journal read-back. Each has independent acceptance, dependency and owner role; each still requires a named assignee, complete accepted triad and fresh official Yellow sizing before intake. Child completion does not close #1055; the parent integration regression remains required.

No change to #1051, #1054, #983, #1049, #972/#973, #962/#975–#977, #810 or the live #983 run is part of this artifact packet.
