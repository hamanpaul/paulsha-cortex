---
status: accepted
work_item: merged-run-completion-finalizer
---

# 已合併 verify-reset run 的 Manager completion finalizer 規格（#977）

## Requirements

### Authority、依賴及範圍

本規格以 [#977](https://github.com/hamanpaul/paulsha-cortex/issues/977) 為直接需求，承接 [#962](https://github.com/hamanpaul/paulsha-cortex/issues/962) R1–R4、R6、R8(a–d) 的 Manager 整合切片及 [#887](https://github.com/hamanpaul/paulsha-cortex/issues/887) Child B。先後順序是 #961 WorkAuthority arbitration → #966/#967 registry/Manager owner lock → #975 admission/proof → #976 restricted registry transition、#995 read-only closure inspector、#996 conditional CompletionRecord writer、#997 conditional OutcomeStore CAS → #977 → #962 aggregate → #887 全部 AC。所有前置切片均已合併、#975 consumer proof shape 與其餘 API shape 已凍結後才開始產品整合。PR #985 的 #975/#976 accepted 文件目前仍在未合併 PR；規劃引用其契約，不宣稱 runtime 已可消費。

唯一預期 production diff 是 `paulsha_cortex/coordinator/manager.py`。不得呼叫 `build_production_ship_validator`、`ShipOrchestrator.verify_remote_closure`、`_completion_draft`、`_ship_action`，或任何會觸發 workspace、preflight、push、archive、journal、registry、outcome 寫入的舊路徑。若需第二個 production module，停止整合並以新 issue-backed split 重算 sizing。

### R977.1 路由與人工 gate

在 provider-backoff 短路之後、`needs_human` gate／planning reconcile／一般 pending verify dispatch 之前，辨識 ongoing、phase=verify、`retry_classification=authority_restart`、claim key 等於最新 WorkAuthority key、完整同 run/head journal merged binding、無 active job 的候選。無 `needs_human` 可 periodic tick 重驗；已有該 facet 時僅 `operator_resume=true` 才重驗，且不可清除或繞過 gate。短暫 provider failure 不新增 `needs_human`；確定性 trust/identity mismatch 留可診斷人工原因。已知 merged candidate 的 proof/closure 失敗不得落回一般 verify dispatch。無有效 merged binding 則依 #975 既有 fallback。新的 review mismatch 繼續 #975 admission → 既有 review→ship closure，不 reset、不重派 verify、不使用 verify-reset 專用 transition。

### R977.2 原始 proof 與完整 CompletionRecord draft

每次驗證都使用 #975 不可持久化的完整 consumer proof：`run_binding`、`work_authority_binding`、`merge_authorization`、`step_bindings`、`gate_bindings`、`completion_record_inputs`，逐欄核對 WorkflowRun identity/status/phase/retry/head、latest WorkAuthority/source revisions、原 Manager authorization hash、journal/PR/head/merge commit、可信 evidence refs 及每個原始 workflow step。job-backed step 要唯一匹配 run+phase+card+claim era+head 的成功 job；Manager-only step 由原生 durable provenance 證明，job 可為 null。verify/review/ship steps、foreign-review、唯一合規 delivery-review、builder/reviewer independence 均要可重驗。reset 後 pending step、清空的 gate refs 或 journal 的 merged 字串不得合成 passed。

draft 的必填及 optional 欄位遵循 `completion.validate_completion_record`，且完整比較 WorkAuthority 正規化欄位：repo/work_id/snapshot_hash/provider_id/provider_revision/source_revisions/mapped_issues/mapped_prs/mapped_openspec/mapped_todo_paths/pr_number/change/todo_paths/merge_commit/run_id/workflow_step_ids/trusted_evidence_refs。`target_ref_sha` 僅由 #995 同次 fresh default_head 提供；#975 不得猜測它。缺欄、跨 run row、source/evidence 漂移即拒絕，不由遠端資料補造本地 step/gate。

### R977.3 兩階段只讀 closure

呼叫 #995 第一階段 GET raw RemoteClosureFacts/default_head 時，`completion_record_valid=False` 且不執行 evaluator。Manager 用該次 default_head 與 R977.2 proof 組完整記憶體 draft。第二階段先以純 validator 正規化 draft、逐欄 exact compare，才提升當次記憶體 facts 的 `completion_record_valid=True` 並呼叫 evaluator。每個 durable boundary 均重新取得第一階段 facts，不跨界重用 snapshot。無效／缺欄／衝突 draft 不得呼叫 evaluator。

closure 必須核對同一 PR 仍 merged、PR head=candidate、merge commit 真正包含 candidate 為 parent、位於本次 default branch ancestry；required issues 已 closed；適用的 OpenSpec archive/active absence 與 Todo closure 均通過；draft `target_ref_sha` 等於本次 default_head。provider degraded、malformed、facts/default-head 漂移即停止。#995 成功只表示記憶體 draft 經驗證，不代表 record 已落盤。

### R977.4 各 durable boundary 重新驗證

在 CompletionRecord conditional write、OutcomeStore append、#976 transition 各 boundary 前，重新載入 latest WorkAuthority/source revisions、重跑 #975 proof 與 #995 兩階段 closure，核對候選、既有 durable rows 與待寫 payload 的完整身份及內容。各 store 使用自己的 conditional write/CAS；不宣稱 GitHub、authority、CompletionRecord、OutcomeStore、Registry 共用 transaction。若 fresh facts 與上一 boundary 不同，不以舊 proof 繼續。

### R977.5 CompletionRecord exact create/reuse

Manager 先用 #996 no-follow reader 檢查目標。既存 record 的固定 `completed_at` 必須沿用，再建立本次 expected payload；不存在時只產生一次新 timestamp。新建與重用均使用 #996 conditional API，回傳後以 path/hash 與完整 normalized payload exact read-back。任何欄位差異、unsafe ref、read race、writer error、hash/read-back mismatch 均停止；不得覆寫/quarantine、不得 append outcome，也不得使用 legacy `write_completion_record`。完成紀錄的原始 authorization hash、current WorkAuthority/source revisions、PR、merge commit/default head、Todo revisions、proof refs 均必須一致。

### R977.6 唯一 shipped outcome 的 conditional CAS

OutcomeStore.path 必須取自 Manager 實際注入的 registry/runtime configuration，不可按 default `_run_state_path` 重新推算。#997 snapshot 安全讀 canonical final entry，逐非空 JSONL row 驗 schema 並拒絕重複 outcome_id。ID 已存在時，安全取得既存 `emitted_at`，用新 proof/closure 重建預期 shipped payload並逐欄比較；完全相同才 zero-write reuse。不存在時只產生一次新 timestamp，帶 fresh revision conditional append。不同 payload、stale revision、unsafe target 或 entry identity drift 均 fail closed。競爭 writer 建立同 ID 時本次衝突即停止；下一次 eligible tick 須重新驗 authority/proof/closure/snapshot，使用 winner 固定 timestamp 重建，不以新 `_now_iso` 誤判 crash retry。

依賴 #997 的 canonical parent dirfd + final-basename no-follow descriptor、`fstat`/entry identity recheck、每列 `validate_outcome_record` 及 duplicate-ID rejection；#967 jobs.json owner lock 不保證其他 outbox path 單 writer。依賴 API 未合併前不得以 ID dedup 宣稱 payload exact 或單 writer。

### R977.7 outcome-first restricted terminal transition

只有 CompletionRecord exact read-back 與 shipped outcome exact durable append/reuse 均通過，才可在再次執行 R977.4 後呼叫 #976。Manager 傳入 fresh #975 authorization hash、validated CompletionRecord 投影及完整已驗 terminal binding；`completion_source_revisions` 使用 #976 凍結的 record projection，不能改用另算 current authority map。#976 只 CAS Registry-local run/job/claim/head/auth/generation/terminal fields，Manager 負責外部 proof/default/Todo/record/outcome。合法 terminal WorkflowRun 必須是 ship/done、verified_head=candidate、verify/review/ship steps 與 gate refs 都由原始證據支持。不得 force update 一般 writer/phase validator；`retire-delivered` 保持 abandoned/superseded，不是成功出口。

### R977.8 crash、負例與聚合驗收

分別在 record 建立後、outcome append 後、#976 transition 前注入 crash；每次重入先重驗 R977.4，至少三次重試後 record hash/bytes 穩定、shipped outcome 恰一筆且完整 payload 相同、相同 Registry-local binding 零新增寫入。proof/record/outcome/closure 負例不得產生新的 shipped outcome 或 done binding，已知 merged candidate 不重派 verify。Registry CAS 因 run/job/state drift 失敗不覆寫，保留既有 durable evidence 並回可診斷狀態。新 review mismatch 不 reset、不重派、不重複 outcome；舊 verify-reset 不經一般 verify dispatch。

於 `tests/test_post_merge_authority_restart_guard.py` 整合 #962 R8(a–d)，保留 `test_post_merge_closure_*`、`test_done_ship_resume_*`、`test_work_actions_retry_invalidation.py`、`test_workflow_production_wiring.py`、`test_work_actions.py`、`test_work_claim.py`、`test_claim_provider_scope_530.py`、`test_recover_superseded_776.py`、`test_manager_daemon_tick_isolation.py` 原斷言；保留 #975/#976/#995/#996/#997 的單元測試。整合測試不依賴 GitHub network。

## Parent acceptance 與交付界線

| #962／#887 AC | #977 證據；後續關閉責任 |
|---|---|
| R1–R4、R6 | Manager 新 mismatch 與舊 verify-reset finalizer、proof/closure、fail-closed；#962 aggregate 保留全部驗收 |
| R8(a) | 新 mismatch 不 reset/dispatch/重複 outcome；#962 integration |
| R8(b) | 舊 reset 有有效 record、done/ship、唯一 shipped outcome；#962 integration |
| R8(c) | proof/closure/record/outbox/CAS 負例不造假、不重派；#962 integration |
| R8(d) | journal/authorization/proof 分流、crash/re-entry；#962 integration |
| #887 R5、R7、R8(e–f) | #961 authority arbitration；#887 aggregate 仍驗 |

#887 的 R1–R4、R6、R8(a–d) 對應 #975/#976/#995/#996/#997/#977 各自切片及 #962 aggregate；R5/R7/R8(e–f) 仍由 #961 與 #887 aggregate 驗。#962、#887 在完整驗收前保持 open；#977 的規劃或實作都不代表父票完成。

## Non-goals

不處理 #847、#885、#882、merge-authorized crash window、無本 run authorization 的 external merge、monitor projection、一般 verify/ship validator 放寬、schema/CLI 新增或部署。實作 PR 另需 `docs/unified-work-lifecycle.md`、changelog fragment、CHANGELOG [Unreleased]、focused/repo-required tests 與 PR-context policy check；本規劃只提交 accepted triad 與唯一 work item。
