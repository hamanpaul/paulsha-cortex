---
status: accepted
work_item: authorized-merged-run-completion-finalizer
issue: 977
---

# 已授權 merged run completion finalizer 設計

## Decisions

### D1 — Manager 是編排者，前置票各自擁有 primitive

#977 只在 `manager.py` 增加舊 verify-reset recovery/finalization route。#975 擁有 admission/proof oracle；#995 pure closure inspector 擁有純唯讀 remote closure inspection；#996 CompletionRecord #996 conditional-writer child 擁有 no-follow exact read 與 path-level create-if-absent CAS；#997 OutcomeStore conditional-CAS child 擁有 canonical outbox path/revision/append CAS；#976 擁有 Registry-local terminal CAS。#967 canonical Manager daemon owner lock 是單一 daemon 執行的前提，但其 registry path lock 不保護 outbox path。#961 只仲裁 WorkAuthority，不是 CompletionRecord/OutcomeStore writer serialization。#997 未合併且 consumer API shape 未凍結前不可開始 integration。

### D2 — recovery route 先於 gate，仍保留人工授權

resume_workflow_run 的 candidate route 放在 provider-backoff 短路後，現有 `needs_human` gate、planning reconcile 與普通 pending verify dispatch 前。沒有 `needs_human` 的精確 candidate 可由 periodic tick 重驗；已有人工作業 facet 時，只有 `operator_resume=True` 才進 proof/closure。retryable provider error 不新增 `needs_human`；確定性 trust mismatch 保留具體診斷。已知 merged candidate 的 proof/closure 失敗不得落回 verify dispatch。

### D3 — #975 proof consumer shape 必須 freeze

現有已知 proof 形狀只綁 run/head/authorization 與 trusted evidence，不能直接重建合法 CompletionRecord 或 terminal WorkflowRun。#977 需要在開始前由 #975 owner 凍結可用的 exact consumer contract：run/repo/work/claim era/head/auth/source revisions、逐步 workflow step/job/evidence identity、完整 verify/review/ship gate refs、record hashes/job IDs、PR identity。若 trusted proof 尚未提供某欄，不能在 Manager 或 remote inspection 拼造；先由 issue owner擴充/修訂 #975 contract。

### D4 — 只使用 pure closure inspector

`build_production_ship_validator` 會操作 workspace、archive、preflight、push、registry/journal 與 `_ship_action`；`verify_remote_closure` 會寫 CompletionRecord；`_completion_draft` 可能把輸入轉成 completion proof。#977 不呼叫這些流程，也不把暫時 WorkflowRun view 交給 side-effecting validator。pure closure inspector 先保持 fetched facts 的 `completion_record_valid=False`，驗證/normalize 記憶體 draft 和完整 authority identity 後才設 true 並呼叫 evaluator。它只做 GitHub GET 與純驗證。

### D5 — 每個 durable boundary 前重新載入 authority/proof

持有 #967 canonical daemon owner 且在 per-run finalizer critical section 中，Manager 在每次 CompletionRecord conditional create/reuse、OutcomeStore append、#976 terminal CAS 之前，都從 authoritative source fresh reload `WorkAuthority` 與完整 `source_revisions`，重新比對 run/claim key/era，重呼叫 #975 proof，再讀 fresh pure closure/default-head/Todo facts。所有 fresh binding 必須與原 candidate snapshot、待寫 record/outcome 及先前 durable rows exact match；任一漂移即停止在該 durable boundary 前。不能只重用 finalizer 入場時的 WorkAuthority object。

來源 reload API 或 #975 proof 尚未能支援此刷新時，停止 intake/integration並新增 issue-backed prerequisite；不得把 #961 arbitration result 當成每次都 fresh。

### D6 — CompletionRecord 使用 #996 conditional-writer child

Manager 先以 child no-follow exact reader 檢查既存 row並保留 `completed_at`，再建 exact normalized draft。所有建立/重用都呼叫該 child 的 conditional API：absent path 用 create-if-absent CAS；existing payload 只有全欄 exact match 才回傳。不得呼叫 legacy `write_completion_record`，因它可回傳不同 readable row或 quarantine invalid row。writer 後再 read-back/hash verify；衝突時不得 append outcome。#961/#967 不取代此 path-level CAS。

### D7 — Outcome id collision 與 outbox revision 由 conditional-CAS child 保護

OutcomeStore 現有 `append` 只按 `outcome_id` dedup，沒有 payload equality/CAS。#977 必須依賴 #997：它以實際 canonical outbox path 為 lock key，用 expected raw-file revision 做 append CAS；safe reader 以 canonical parent dirfd + final-basename `O_NOFOLLOW` descriptor 讀取並 recheck entry identity，不得使用 `Path.is_file()` / `Path.read_bytes()`；每列都通過 `validate_outcome_record`，整個 outbox 的 `outcome_id` 必須唯一。相同 ID 僅完整 normalized payload（含 `emitted_at`）完全相等才 zero-write reuse，mismatch typed conflict。所有本模組 mutation entrypoints 共用 outbox-specific lock/revision writer。Manager 每次 append 前仍重做 fresh proof/closure，從注入 registry/runtime configuration取得明確 store path，不推定 `jobs.json` owner key相同或用 default `_run_state_path` 代替。若唯一既有 outcome ID 存在，Manager 先安全取其 fixed `emitted_at`，依 fresh proof 建 expected row 並 exact compare；只有 ID 不存在才產生新 emitted_at。CAS stale/collision 時不重用舊 proof；下一次 resume 從 fresh authority/proof/outbox snapshot 重試。#967 single Manager daemon owner 是 Manager/registry 執行前提，不是 outbox CAS。

### D8 — 外部比較由 Manager，#976 只做 Registry-local CAS

Manager 在呼叫 #976 前比對 fresh WorkAuthority/source revisions、#975 proof、GitHub PR/head/merge/default ancestry、OpenSpec/Todo closure、CompletionRecord exact hash 與 OutcomeStore complete payload。#976 不跨讀 GitHub、default head、Todo 或 OutcomeStore；它只在同一 registry-local mutation 中 compare run status/phase/retry/claim era/head/auth/job generation/no-active-job 與其 API 支援的 terminal fields，再做 Registry CAS。API 支援欄位必須先 freeze；不得對 #976 提出外部驗證或整個跨系統交易保證。

### D9 — default-head 與外部來源 drift 一律 stop

每次 pure closure inspection 重新取 default head。draft `target_ref_sha` 須等於本次 default head；CRecord/Outcome/Registry 三個寫入前 refreshed snapshot 需與正在收斂的 exact identity 一致。default-head、PR/head/merge/ancestry、Todo 或 authority/source revisions 改變時，保留已寫 audit rows，不寫下一筆、不回寫/隔離舊資料、不做 terminal transition。

### D10 — crash/re-entry 採逐步精確重驗

每次 eligible resume 都重載 authority、proof 與 remote facts；不快取上一 tick 記憶體 view。CRecord exact reuse 保留原 hash/time，OutcomeStore collision 逐欄比較，#976 完全相同 local binding 才 no-op。遇 retryable provider failure 可由後續 tick 再驗；已有 `needs_human` 必須 operator resume。

### D11 — 維持 aggregate ownership

#977 只為 #962 R8(a–d) 提供 Manager integration/crash evidence；不能關閉 aggregate。#962 R1–R4/R6/R8(a–d) 與 #887 全部 AC 保持不變。#961 負責 active/archived WorkAuthority arbitration；#975 負責 proof；#976 負責 Registry-local CAS；#995 負責 pure closure inspection；#996 負責 CompletionRecord conditional writer；#997 負責 OutcomeStore conditional CAS。

## Execution sequence

1. Manager 只經 #967 owner-locked daemon 路徑進入；讀 exact ongoing verify/authority_restart candidate，取得 per-run finalizer critical section。若已 `needs_human` 且未 operator resume，沿既有 gate 返回，不執行自動 retry。
2. fresh-load 當前 WorkAuthority/source revisions，重比 run/claim identity，執行 #975 proof。若 #975 consumer shape 未 frozen 或不足以提供真實 step/evidence/gate/record fields，停在 durable step 前，不合成資料。
3. 從 exact proof refs 建記憶體 CompletionRecord draft；呼叫 pure closure inspector。Inspector 保留 raw `completion_record_valid=False`，先 validate draft、authority、candidate/default_head exact match，再設 true 並 evaluate closure。檢查 PR merged/head/merge-parent/ancestry/issues/OpenSpec/Todo。
4. 在 CompletionRecord 持久化前再 fresh-load authority/source、重跑 proof 和 inspector；若與第 2–3 步 snapshot 不同，stop。透過 conditional writer child no-follow exact read existing `completed_at`，再 create-if-absent CAS 或 exact reuse；read-back/hash exact compare。
5. 在 OutcomeStore append 前第三次 fresh-load authority/source、重跑 proof/inspector；確認 target_ref_sha 仍等於 same default head，且 CRecord hash/payload exact。由注入 registry/runtime configuration取得 explicit OutcomeStore.path，取 conditional-CAS child canonical snapshot/revision；用 `append_if_exact_or_create(expected_revision=...)`。同 ID complete payload exact match 才 zero-write reuse，mismatch stop；新 ID stale revision conflict 時不可拿舊 proof retry。
6. 在 #976 terminal CAS 前再 fresh-load authority/source、重跑 proof/inspector；讀 exact CRecord/outcome。所有外部 facts 在 Manager 比較並與前一 snapshot/rows exact match。
7. 呼叫 #976 frozen registry-local CAS，傳入 only supported local expected run/job/claim/head/auth state 與 terminal fields；#976 不負責外部 remote/outbox checks。禁止一般 registry writer/force update。
8. Read-back WorkflowRun/CRecord/outcome；驗證 legal ship/done invariants、record hash/source revisions、outcome count/payload。fault matrix 對 CRecord、outcome、registry CAS 各自注入 crash 並重入至少三次。

## Failure matrix

| 故障點 | 必要結果 |
|---|---|
| candidate / claim / fresh authority mismatch | 不寫 CRecord、outcome 或 terminal; stale input 不能進入 #976 |
| existing `needs_human` 且未 operator resume | 保持 operator-resume-required，不呼叫 proof/closure |
| #975 proof shape 未 freeze或缺必要 verified fields | 停止 integration/intake，不合成 steps/gates/evidence |
| retryable provider/transient error | 回傳 retryable stop，不新增 `needs_human`，不 dispatch known merged candidate |
| deterministic proof/evidence/identity mismatch | 留明確 needs_human diagnostic；不寫下一個 durable row，不 dispatch verify |
| closure PR/head/merge/ancestry/default/Todo/issue/archive failure | stop; 不寫下一個 durable row |
| current authority/source revision reload 在寫入前漂移 | 在該 persistence 前 stop; 保留既有稽核 rows |
| CompletionRecord conditional create/read 不 exact | typed stop；不得 legacy writer、quarantine、outcome 或 CAS |
| OutcomeStore same ID payload mismatch | conditional CAS 回 conflict；不得 #976 CAS |
| OutcomeStore stale expected revision | 不 append 舊 snapshot；保留其他 rows，要求新 tick 重新 proof/closure/outbox snapshot |
| outbox path alias / injected registry 與 default state path 分離 | 用實際 OutcomeStore canonical path lock；不得拿 #967 owner key 或 default `_run_state_path` 推定 |
| post-record / post-outcome default-head drift | 保留已寫 record/outcome；禁止下一筆 persistence/terminal CAS |
| #976 local active-job/state/head/auth/revision conflict | 保留 durable record/outcome，不 force update |
| 完全相同重入 | fresh proof/closure；exact record/outcome reuse；#976 identical local binding zero-write no-op |

## Verification design

在 `tests/test_post_merge_authority_restart_guard.py` 使用真 Manager orchestration 和可控 #975/#976/inspection/CRecord-writer/outbox-CAS ports，注入 CRecord、outbox、authority reload、remote observation、registry CAS fault。確認 exact consumer shape frozen gate、fresh reload 次數與順序；三個 durable boundaries 前都重新載入 source revisions、重跑 proof/closure；no-synthetic-pass；needs_human/operator_resume placement；transient vs deterministic diagnostics；default-head drift；CRecord writer conflicts與 immutable bytes；outbox same-ID payload/revision/path CAS；Registry-local CAS conflict。

需覆蓋每次寫入前 source revision 在讀取後改變、refresh proof mismatch、default-head 漂移、CRecord race（同/不同 payload）、outbox 同 ID 不同 payload、different-ID lost-update race、stale expected outbox revision、path alias/injected-registry mismatch、active job/head/auth/state revision CAS drift，以及各 durable crash 前後至少三次相同重入。各前置 child 的 unit tests 繼續單獨保留。

保留既有 post-merge closure、done-ship resume、retry invalidation、production wiring、work actions、work claim、provider scope 530、superseded recovery、daemon tick isolation 的全部 assertions。#962 在 #961/#975/#976/#995/#996/#997 prerequisites 合併後仍獨立跑 R1–R4/R6/R8(a–d) aggregate suite；#887 全部 AC 保持 parent gate。
