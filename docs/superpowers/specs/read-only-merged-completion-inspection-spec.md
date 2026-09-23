---
status: accepted
work_item: read-only-merged-completion-inspection
issue: 995
---

# 唯讀 merged completion closure inspection 規格

這是 issue #995 前置切片的 accepted 規劃；GitHub issue #995，已建立。它支援 #977 的 Manager finalizer，不承接 #975 proof 或 #976 registry CAS，也不改 #962/#887 aggregate acceptance。

## Problem

ShipOrchestrator.verify_remote_closure 把 GitHub closure observation、CompletionRecord validation、write 與 read-back 放在同一方法。build_production_ship_validator 另有 workspace/archive/preflight/push、registry/journal 和 _ship_action 副作用。#977 需要在 outcome 與 terminal CAS 前觀察 exact merged closure；不能把 side-effecting ship path 暫時包成 WorkflowRun view 後當成 proof。

## Requirements

### I1 單一 production module

只新增或抽取 paulsha_cortex/coordinator/delivery.py 中的 read-only inspection API。不得改 github_delivery.py、completion.py 或其他 production module；不得新增 persisted field/schema、CLI、journal/registry/outcome/workspace/evidence writer。若需要改第二 production module，停止並重新分票、重算 sizing。

### I2 明確 pure inspection contract

提供同一 durable-boundary observation、共用一份不可變 facts snapshot 的兩個唯讀 API 呼叫：第一階段只收 repo、pr_number、change、current WorkAuthority、todo_paths、expected_head、run_id、workflow_step_ids 與 proof-validated trusted_evidence_refs，GET fresh RemoteClosureFacts/default_head，保持 `completion_record_valid=False`，不呼叫 evaluator；第二階段由 Manager 以第一階段的 `default_head` 填入完整記憶體 CompletionRecord draft，再提交該 draft 與同一不可變 facts snapshot 做驗證/evaluation。兩階段結果僅在記憶體，無 cache 或持久化；任何 durable boundary 都重新從第一階段開始，不跨次重用 snapshot。

### I3 驗證 CompletionRecord draft 後才開啟 closure evaluator

`fetch_remote_closure` 回傳的原始 `RemoteClosureFacts.completion_record_valid` 預設且必須保持 `False`。第二階段先對由同次 facts.default_head 組成的記憶體 draft 呼叫 `completion.validate_completion_record`，完成結構正規化與 candidate、target_ref_sha、完整 WorkAuthority/source-revision 欄位的 exact 比對；target_ref_sha 必須等於本次 fetched facts 的 `default_head`。所有純驗證通過後，才以 `dataclasses.replace(facts, completion_record_valid=True)` 建立 evaluator 輸入並呼叫 `evaluate_remote_closure`。任何 draft 驗證或比對錯誤都在 evaluator 前拒絕；不得提前設 true、不得呼叫 evaluator，也不得把這旗標解讀為 record 已持久化。

其餘可呼叫 `GitHubDeliveryClient.fetch_remote_closure`、`_validate_work_authority` 和其他純函式。不得呼叫 completion writer/read-modify-quarantine path、atomic writer、registry/journal/outcome writer、workspace creator、preflight、push/PR mutation、archive helper、`_ship_action` 或 workflow evidence writer。所有 GitHub calls 必須是 GET/read-only。

### I4 Exact merged closure

每次第一階段都讀 fresh PR、repo default branch/ref、merge commit/parents、merge/default ancestry、required issue state、適用的 OpenSpec active/archive facts 與 remote Todo revisions。必須驗證 PR merged、PR head exact match expected_head、merge commit 是 merge commit且包含 candidate parent、merge commit 位於當次 default_head ancestry、required issue closed、OpenSpec/Todo closure 達標。任何缺項或 malformed provider data fail-closed。

### I5 Exact draft and authority binding

normalize CompletionRecord draft 後，candidate 必須等於 expected_head，target_ref_sha 必須精確等於同次 inspection 的 facts.default_head。由輸入 authority 與 closure facts 推導的 expected work_authority 必須完整包含 repo/work_id/snapshot_hash/provider ID+revision/exact source_revisions/mapped issue-PR-OpenSpec-Todo refs/PR number/change/Todo paths/merge commit/run ID/exact workflow_step_ids/trusted evidence refs。要求 normalized payload 的 WorkAuthority 完全相等；不可用 completion_records_semantically_match 作精確比對，因該 helper 忽略部分 volatile source revisions 與 optional fields。

### I6 Proof evidence responsibility

此 inspection 不替代 #975。呼叫端必須先證明所有 trusted evidence refs、內容 hashes、job/step identity 與 gates 狀態；inspection 只確認輸入的 proof-bound refs 與 CompletionRecord draft/WorkAuthority 完整一致。不從 remote closure 推導 verify/review/ship passed，不讀或建立 gate evidence，也不構造 WorkflowRun。

### I7 Existing production path compatibility

保持既有 ShipOrchestrator.verify_remote_closure 的 writer、read-back、return/error 行為相容。可抽出共用純 helper，但既有方法的語意與既有 ship flow 必須由回歸測試保護。新唯讀 API 與既有 durable API 具有不同、清晰命名與型別，不得以參數旗標模糊兩種副作用契約。

### I8 Drift and testability

每次 durable boundary 都重新執行第一階段、取得 fresh remote facts；第二階段只接受該次 snapshot。若 default_head、PR head、merge commit/parents/ancestry、Todo revisions 或 authority source revisions 變化，回傳對應 mismatch/stop result，不持久化舊 snapshot。正例測試證明 evaluator 收到的 facts 只有在 draft validation 與 exact identity compare 完成後才有 `completion_record_valid=True`；負例證明 invalid/missing/conflicting draft 不會呼叫 evaluator，也不提升旗標。提供注入 runner 的測試 seam，能記錄並斷言所有 GitHub requests 為 GET；以 mutation spy 斷言 local files/registry/journal/outcome/evidence/workspace 未改變。

### I9 Documentation, changelog, and parent aggregation

更新本模組 API documentation、docs/unified-work-lifecycle.md、changelog fragment 及 CHANGELOG.md [Unreleased]；無 CLI surface 時仍驗 cortex work --help 輸出未變。保留 #962 R1–R4/R6/R8(a–d) aggregate 和 #887 全部 AC；本 child pass 不代表 #977、#962、#887 關閉。
