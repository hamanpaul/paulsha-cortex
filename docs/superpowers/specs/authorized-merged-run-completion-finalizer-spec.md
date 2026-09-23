---
status: accepted
work_item: authorized-merged-run-completion-finalizer
issue: 977
---

# 已授權 merged run completion finalizer 規格

本規格定義 #977 的 Manager 收尾切片。依賴 #975 admission/proof、#976 registry-local transition、#995 pure closure inspector、#996 CompletionRecord conditional-writer 與 #997 OutcomeStore conditional-CAS；Manager recovery 仍須走 #967 owner-locked daemon path。#961 負責 WorkAuthority arbitration，**不**提供 CompletionRecord 或 OutcomeStore writer serialization。整合驗收仍由 #962 保留，須待 #961/#966/#967/#975/#976/#995/#996/#997 合併且 consumer API shapes 凍結後執行；父 #887 全部驗收條件仍有效。

## 背景與界線

authority restart 可把原本已 merge 的 run reset 到 verify，清空完成 gate 與 verified head；若一般 resume 直接派送 pending verify，就會重跑已 merge candidate。#977 只把「重新證明並完成」接入 Manager。它不重做 proof oracle、remote closure 規則或 registry CAS。

目前 production ship validator 會處理 workspace、archive、preflight、push、journal、registry 與 ship action，不能拿暫時 WorkflowRun view 呼叫來當成唯讀 closure proof。#977 不呼叫該 validator、ShipOrchestrator.verify_remote_closure、_ship_action、_completion_draft 或任何會產生上述副作用的流程。#995 pure closure inspector 提供只做 GitHub GET 與純驗證的 closure inspection；CompletionRecord 與 outcome 只由本票依下列精確比對規則持久化。

## Requirements

### I1 唯一 owner 與 production 範圍

只有 Manager 在舊 verify-reset recovery 流程中決定是否建立 completion。#977 production diff 限於 `paulsha_cortex/coordinator/manager.py`；沿用 #975 oracle、#976 registry-local restricted CAS、#995 pure closure inspector API、#996 CompletionRecord conditional-writer API、#997 OutcomeStore conditional-CAS API。不得改 registry.py、delivery.py、completion.py、engineering_outcome.py、work_actions.py、work_bridge.py 或增加 persisted 欄位。任何依賴 API 尚未凍結，或 fresh WorkAuthority/source reload 路徑不可用時，停止 integration；若必要修正超出 manager.py，另開 issue-backed split 並重算 sizing。

### I2 精確候選

僅考慮仍為 ongoing、current_phase=verify、retry_classification=authority_restart、claim key 精確符合目前 WorkAuthority 的舊 run。run_id、repo、work_id、candidate full SHA、原始 Manager merge authorization hash、目前 authority source revisions 均須一致。不得用 journal 中的 merged 字串單獨判定候選成功。

### I3 Recovery placement 與 human gate

候選檢查放在 resume_workflow_run 的 provider-backoff 短路之後，且在現有 needs_human gate、PlanningPublicationTransaction.reconcile 與普通 verify dispatch 之前。這個位置只讓 finalizer 有機會檢查精確候選，不得清除或繞過 human gate：

- 沒有 needs_human facet 的精確候選可由週期 tick 重驗。
- 若 run 已有 needs_human facet，只有 operator_resume=true 才呼叫 proof/closure；否則保留 operator-resume-required 行為。
- 對可重試 provider/transient 錯誤，不持久化 needs_human facet，讓下一次 tick 可重新檢查。
- 對確定性 authorization/evidence/identity mismatch，保留 needs_human 與具體診斷。沒有 operator_resume 時不自動重試。
- 候選確認為 merged 後，任何 proof/closure 失敗都回傳 stop，不得落入普通 verify dispatch。

若沒有有效 merged binding，按 #962 已接受的 fallback 繼續原 resume 流程。

### I4 #975 proof 的完整 consumer binding 與 shape-freeze gate

實作前 #975 oracle 的回傳形狀須明確 freeze，不能把「proof passed」布林值當成足夠輸入。proof 必須同時綁定：

- run_id、repo、work_id、claim key/era、candidate_head、verified_head。
- 原始 Manager merge authorization 的 canonical path/ref 與 content hash。
- exact workflow_step_ids；逐步回傳 phase/card、gate_result、candidate/head、job_id、workflow_run_id、workflow_step_id、claim era 與 evidence ref/hash。
- 建立 CompletionRecord 所需的 spec/plan/verification hashes、builder/reviewer job IDs、verification evidence ref/hash、foreign-review evidence ref/hash，以及適用的 delivery review ref/hash；每項都需有可信內容與 passed 狀態證據。
- workflow gate refs 的完整 kind/ref/hash；foreign-review 必須恰一份，delivery review 必須符合 WorkflowRun 當前契約，且 reviewer/builder independence 可核對。不得合成 verify/review/ship passed step 或 gate。
- 精確 PR number、candidate、merge authorization identity，以及 proof 使用的當前 WorkAuthority source revisions。

目前已知 #975 回傳僅綁 run/head/authorization 與 trusted evidence，尚未凍結上述完整 CompletionRecord/WorkflowRun consumer shape。這是明確 blocker：#977 不得宣稱現有 proof 足以建 record，不得由 Manager/remote closure/不相干 rows 補欄位或合成 passed steps/gates。實作前由 #975 owner 凍結並驗證能供給的 exact consumer contract；如果證據缺項，先更新 #975 的 issue-backed scope，再開始 #977 integration。

### I5 使用 pure closure inspector 純唯讀 closure inspection

在 #975 proof 完整成立後，Manager 建立本次呼叫的記憶體 CompletionRecord draft，將 proof 的 run/step/evidence identity 與重新載入的 current WorkAuthority 完整寫入 draft，再呼叫 #995 pure closure inspector API。該 API 必須先以 `completion.validate_completion_record` 驗證 draft，確認 exact identity 後才把 fetched facts 的 `completion_record_valid` 從 false 提升為 true 並呼叫 evaluator。inspection 回傳 fresh RemoteClosureFacts 和 normalized expected authority binding；Manager 必須確認：

- remote PR 為 merged，PR head 等於 candidate，merge commit 是真正 merge commit 且包含 candidate 為 parent。
- merge commit 位於剛讀到的 default branch ancestry；required issues closed；適用的 OpenSpec active/archive 與 Todo closure 條件均通過。
- CompletionRecord target_ref_sha 精確等於 inspection 回傳的 default_head。
- completion payload 的 WorkAuthority 每個欄位都與 inspection 推導值完全相同，包括 snapshot_hash、provider/source revisions、mapped refs、PR number、merge commit、run_id、workflow_step_ids 與 trusted evidence refs。

不得呼叫 build_production_ship_validator、_completion_draft、ShipOrchestrator.verify_remote_closure 或 _ship_action；不得 provision/reset workspace、跑 preflight、push、改 registry/journal、建立 gate evidence，或由暫時 WorkflowRun view 宣稱 passed。

### I6 CompletionRecord exact identity 與 conditional persistence

CompletionRecord draft 只能使用 #975 已驗證的既有證據 refs，不得重建或改寫 verification/review evidence。draft 必須綁定原 run_id、完整 steps、candidate、原 authorization hash、當前完整 WorkAuthority/source revisions、PR number、merge commit、default_head、Todo revisions 與 proof evidence refs。

呼叫任何 durable writer 前，Manager 必須重新載入 authoritative WorkAuthority 與其 source revisions、重新核對 run/claim identity，重新呼叫 #975 proof，再呼叫 pure closure inspector 取得 fresh remote facts；這次 authority/proof/default-head/Todo/closure binding 必須 exact match 上一次檢查及待寫 payload。若來源 reload API 或 #975 proof 尚未提供此 consumer contract，停止，不把進入 finalizer 時取得的舊 authority object 當 fresh。

若確定的 CompletionRecord path 已存在：

1. 以 #996 conditional-writer child 提供的 no-follow、regular-file、完整 reference 驗證 reader 讀取；讀取錯誤、格式無效或不可讀都視為衝突並停止。
2. 比較 schema、slice/run identity、spec/plan/verification hashes、builder/reviewer job IDs、dispatch_base、candidate、target branch/remote/ref/ref SHA、verification/review refs/hash、review policy/docs class、所有 optional fields 與完整 WorkAuthority。所有 identity/evidence/source revision 欄位必須 exact match。
3. 對 completed_at 只接受可解析的既有值，並將它作為該筆已建立 record 的固定值；重入時不得重寫 timestamp 來製造新 hash。
4. 任一欄位不同即停，不覆寫、不隔離、不採用 semantic-only WorkAuthority match，也不 append outcome。

不得呼叫 legacy `write_completion_record`。已存在 record 時，使用上述安全 reader 取得固定 `completed_at`，組成 exact incoming payload，再由 #996 conditional-writer child 安全 exact-reuse；不存在時也只能用該 child 的 create-if-absent CAS。回傳後按 hash/read-back 再比對完整 payload。任何 mismatch/error 都停止，不寫 outcome。#967 owner lock 只保護 single Manager daemon 的執行 owner；#961 只仲裁 authority；兩者都不取代 CompletionRecord path-level conditional writer。

### I7 Outcome collision 必須比對完整 payload

先以 exact CompletionRecord hash 建出預期 shipped outcome。每次 append 前重新載入 current WorkAuthority/source revisions、重跑 #975 proof 與 #995 pure closure inspection，並 exact 比對既存 record。OutcomeStore 必須由 Manager 當前注入 registry/runtime configuration 導出的明確 `OutcomeStore.path` 建立；不得改用 default `_run_state_path` 重算，亦不得假設 outbox lock key 等於 #967 registry owner key。取 #997 的 fresh safe canonical outbox snapshot/revision；child 必須逐列驗證 schema 並拒絕重複 `outcome_id`。若該 `outcome_id` 已存在，使用 no-follow reader 安全取得該唯一 row 的 fixed `emitted_at`，以 fresh proof/closure 重建 expected shipped payload，逐欄 exact compare 後才可 zero-write reuse。若 ID 不存在，才產生一次新的 `emitted_at` 建立 expected payload，並用該 snapshot revision 呼叫 `append_if_exact_or_create(expected_revision=...)`。同 ID 只有完整 payload（包含 fixed `emitted_at`）完全相等才接受；任何差異 stop。CAS collision 若是同時由另一 writer 建立的同 proof outcome，當次仍 fail closed；下一次 eligible tick 必須 fresh reload/proof/closure/snapshot，再取 winner row 的 timestamp 重建並完整比對，避免以新 `_now_iso` 錯判已完成的 crash retry。outbox revision CAS conflict 時不 retry 舊 proof。run/work/repo、attempt digest、candidate、PR、merge commit、Todo paths、authorization hash、record path/hash、source revisions、emitted_at 與 outcome kind 任一欄不同即停止，不呼叫 #976。不得把同 outcome_id 當成 payload 相同證明或將 #967 lock 當作 outbox serialization。

### I8 先 outcome，再 #976 exact terminal transition

CompletionRecord exact read-back 與 outcome exact durable append/compare 均通過後，Manager 再重新載入 current WorkAuthority/source revisions、重跑 #975 proof、讀 fresh remote closure/default/Todo facts，並 exact compare record/outcome；之後才呼叫 #976 restricted transition。Manager 負責所有外部 proof/remote/outcome 比較；#976 僅負責 Registry 內的 local compare-and-swap。傳給 #976 的欄位限於其凍結 API 支援的 registry-local expected run/job/claim/head/auth state 及支援的 terminal fields。Binding 的外部比較至少須覆蓋：

- run_id、預期目前 status=ongoing/current_phase=verify/retry_classification=authority_restart、claim key/era、candidate_head、verified_head、authorization hash、完整 current WorkAuthority source revisions。
- 無 active job 的預期；所有原 run workflow_step_ids、逐步 phase/card/status/job/evidence binding 與完整 gate_refs。
- 合法 terminal WorkflowRun 全步驟資料：verify/review/ship steps 全為已證明的 passed、foreign-review 與唯一合規 delivery-review gate refs、reviewer/builder independence；不可合成 gate。
- CompletionRecord path/hash、fixed completed_at、source revisions、PR number/head、merge commit/parents、inspection default_head/Todo revisions，以及 Manager 已透過 outbox CAS exact-checked 的 shipped outcome。這些外部欄位由 Manager 在 CAS 前自行精確核對；不得要求 #976 讀 GitHub、default branch、Todo 檔或 OutcomeStore，也不得把 #976 描述為跨系統 CAS。不得新增 WorkflowRun 欄位。

#976 必須在其同一受限 registry mutation 中檢查其支援的 local exact run status/phase/retry/claim era/head/auth/job generation/no-active-job 與 supported terminal fields，並以 registry-local CAS 寫出合法 ship/done WorkflowRun。完全相同 local binding 才可 no-op；任何 partial/mismatch 都拒絕。Manager 另行驗證外部 proof、WorkAuthority/source revisions、remote/default/Todo closure 與 outcome。#977 不呼叫一般 registry writer、不 force update，也不以 API 未凍結的假定 payload 形狀開始實作。

### I9 每次 eligible resume 都重驗

每次 eligible resume/tick 都重新載入 current WorkAuthority/source revisions，重新呼叫 #975 proof、pure closure inspector fresh closure inspection，再依現存 durable rows 重做 exact compare。尤其在 conditional CompletionRecord create/reuse、OutcomeStore conditional append、#976 registry CAS 前，各自再做一次 current-authority/proof/closure refresh；Outbox operation 另 compare-and-set fresh outbox revision。任何 mismatch/default-head drift/source-revision drift/outbox stale revision 都阻止該次 durable operation。proof/inspection result 僅限該次呼叫記憶體，不新增 cache 或 persisted authorization。existing needs_human 且無 operator_resume 是刻意保留的人工作業 gate，不屬於 eligible automatic retry。

### I10 Default-head drift fail-closed

每次 pure closure inspection 都重新讀 current default ref。draft 的 `target_ref_sha` 必須等於當次 `default_head`，且所有邊界的 default_head 必須與待完成 record/outcome 所綁 snapshot 相同。若 closure、Todo revisions、PR head、merge commit/parent/ancestry、WorkAuthority source revisions 或任一 proof identity 漂移，停止且不得改寫已存在 CompletionRecord/outcome。CRecord create/reuse 前、完成 record 後/outcome 前、outcome 後/#976 CAS 前各自重讀 current authority/source revisions 並重驗 proof/closure。漂移時保留已 durable rows、不跨越下一步。GitHub/authority provider 與 local stores 無共同 transaction；本票不宣稱外部 compare-and-swap，只保證每一個 local durable boundary 前都重驗並用該 local store 支援的 conditional write/CAS。

### I11 Crash/re-entry 冪等

在 CompletionRecord 寫入後、outcome append 後、#976 transition 前分別注入 crash。重入必重新驗證；exact CompletionRecord hash 穩定，shipped outcome 恰一筆且完整 payload 相同，#976 完全相同 binding 為零新增寫入，WorkflowRun/record/outcome read-back 穩定。既有 conflicting row 的 crash/re-entry 測試必證明停止且不移除/改寫 row。

### I12 Fail-closed 分流

已知 merged candidate 但 proof/closure 不足、default-head drift、record/outcome collision、active job、API shape mismatch 或 CAS drift時，回傳明確 stop reason，不 dispatch verify、不寫假 done。可重試 provider/transient 失敗不得持久化 needs_human；確定性 trust mismatch 才設 needs_human。Outcome 已先落地而後續 CAS/remote snapshot 失敗時，保留 outcome 作稽核證據並阻止 terminal transition。

### I13 其他路徑與 aggregate gate 保持完整

新 review/mismatch run 仍走 #975 admission 後的 review→ship closure；非本 run authorization 的外部 merge、PR 未 merged、identity mismatch、journal 無效、provider degraded、proof 不全均不得建立 shipped outcome/done binding。retire-delivered 仍是 abandoned/superseded，不能作成功出口。

本票不移除或縮減 #962 R1–R4、R6、R8(a–d) 聚合驗收，也不移除 #887 任一 AC。R8(a) 新 mismatch、不重派 verify；R8(b) 有效正例；R8(c) proof/CAS 負例；R8(d) journal/authorization 分流仍由 #962 整合證明。#887 R5/R7 與 R8(e–f) authority arbitration 繼續由 #961 負責。#962/#887 在各自完整 aggregate gate 通過前保持 OPEN。

## #887 與 #962 acceptance 對照

| 保留條件 | #977 責任 | 不移轉的 owner |
|---|---|---|
| #887 R1、新 review mismatch closure | 確認 finalizer 不攔截既有 review→ship 路徑 | #975 admission、#962 aggregate |
| #887 R2、舊 verify-reset completion | 產生 exact CompletionRecord、shipped outcome、合法 terminal run | #975 proof、#976 CAS、#995 closure inspector API |
| #887 R3、journal/auth/proof 缺漏 fail-closed | 不寫 outcome/done；已知 merged candidate 不重派 | #975 oracle、#976 transition |
| #887 R4、phase-free journal helper | 消費 proof，不複製判準 | #975 |
| #887 R5/R7、active/archived authority arbitration | 使用合併後 WorkAuthority，不實作 arbitration | #961 |
| #887 R6、原 Manager authorization 與 merge closure | 完成 record/outcome/terminal 編排 | #975/#976/#995 closure inspector |
| #962 R8(a–d) | 提供 Manager 收尾整合與 crash/re-entry evidence | #962 aggregate gate |
| #887 R8(e–f) | 不取代 authority 衝突測試 | #961 |

父 #887 三項期望全部保留：已授權且 merge 的 run 進合法 completion；authority restart 前辨識 delivery state；local active 與 GitHub archived 衝突依精確 merged evidence 收斂。後兩項由 #961 負責。

## Non-goals

不處理 #847 merge 前 authority-restart、#885 archive-applied invariant、#882 一般 verify operator 出口、merge-authorized crash window、無本 run authorization 的 external merge、monitor projection、retire-delivered 語意、CLI、new persisted fields、一般 verify/ship validator 放寬，或 #975/#976/#995 closure inspector 自身演算法。
