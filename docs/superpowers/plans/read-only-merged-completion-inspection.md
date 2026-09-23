---
status: accepted
work_item: read-only-merged-completion-inspection
domain_breadth: 0
state_consistency: 1
invariant_count: 9
artifact_classes:
  - source
  - tests
  - documentation
issue: 995
---

# Pure read-only merged completion inspection 工作計畫

唯一 owner：issue #995 前置切片 read-only-merged-completion-inspection（GitHub issue #995 已建立）。此票支援 #977；不承接 #975 proof、不承接 #976 CAS。#977 必須在此票、#975、#976 合併且 consumer contracts 凍結後才開始 integration；#962 R8(a–d) 與 #887 全部 AC 繼續是 aggregate gates。

Planning views：[spec](../specs/read-only-merged-completion-inspection-spec.md)、[design](../specs/read-only-merged-completion-inspection-design.md)。

## Boundary

Production diff 限 paulsha_cortex/coordinator/delivery.py 一個模組。新增獨立 pure read-only inspection API；既有 verify_remote_closure 保持相容 writer/read-back 行為。不得改 github_delivery.py、completion.py、manager.py、work_actions.py、registry.py、journal/outcome store、persisted schema、CLI 或 workspace/preflight/push/archive flow。若測試證明須改第二個 production module，停下重新分票與 sizing。

測試新增 tests/test_delivery_read_only_closure.py。PR 必須依 policy 帶 changelog.d/read-only-merged-completion-inspection.md、CHANGELOG.md [Unreleased]、docs/unified-work-lifecycle.md、CLI help compatibility、帶 PR 上下文 policy check 與 repo required tests。

## Tasks

- [ ] **T1 tests / RED exact read-only API (I2–I4)**：新增 tests/test_delivery_read_only_closure.py，使用 injected fake runner 驗收 inspection result/API shape；成功例與 PR unmerged、wrong head、missing candidate parent、non-merge commit、ancestry mismatch、open issue、archive/active OpenSpec/Todo incomplete 失敗例。
- [ ] **T2 source / delivery-only inspection (I1–I4)**：在 delivery.py 新增明確 read-only entrypoint，重用 fetch_remote_closure 與 evaluate_remote_closure。只做 GET 和純函式，不接受臨時 WorkflowRun，也不宣稱 steps/gates passed。
- [ ] **T3 tests / mutation audit (I3, I8)**：spy CompletionRecord writer/quarantine/atomic writer、registry/journal/outcome/evidence writer、workspace/preflight/push/archive/_ship_action；任一被呼叫都 fail。記錄 HTTP method，所有 GitHub calls 必須 GET，local stores/workspace bytes 保持一致。
- [ ] **T4 source / validate before evaluator (I3, I5–I7)**：fetch 後保留 `completion_record_valid=False`；純驗證/normalize in-memory CompletionRecord draft，exact compare candidate、target_ref_sha/default_head 及完整 WorkAuthority/source revisions；全數成功才 `dataclasses.replace(..., completion_record_valid=True)` 後呼叫 evaluator。無效 draft 在 evaluator 前拒絕；不得把旗標當成 record 已寫入。既有 verify_remote_closure persistence semantics 保持不變。
- [ ] **T5 tests / evaluator ordering, exact identity and drift (I3, I5, I8)**：spy evaluator 驗證 valid 正例只在 draft 驗證後帶 true 進 evaluator；invalid/missing/conflicting draft 不呼叫 evaluator且 raw flag 維持 false。測試 target_ref_sha/default_head、source revision、run/step/evidence/PR/merge/Todo 任一 drift 均 fail；malformed/degraded provider response fail-closed，且每次 call 重取最新 remote snapshot。
- [ ] **T6 regression / existing durable method (I7)**：保留舊 verify_remote_closure 的 CompletionRecord writer/read-back、error、return behavior 與 production ship validator/_ship_action regressions；新 inspector 不改舊 caller。
- [ ] **T7 documentation / policy and gates (I9)**：更新 delivery API 說明、docs/unified-work-lifecycle.md、changelog fragment 和 CHANGELOG.md [Unreleased]；驗 CLI 的 cortex work --help 輸出相同；跑 focused tests、repo required suite 及含 PR title/body/labels/base/head 的 policy check。
- [ ] **T8 parent handoff (I6, I9)**：文件明確 #975 對 evidence refs/hash/job/step/gate 狀態負責，#977 才可消費此 inspector 和 proof。回報精確 accepted API/result shape、tests與限制；不替 #977/#962/#887 宣告完成。

## 五維 sizing

用 repo work_bridge.current_sizing_snapshot 以本計畫及 accepted spec/design、fix-standard combo 和 ACCEPTANCE_SURFACE_RULES 實算：

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 0 | 唯一 product module 是 coordinator/delivery.py。 |
| state_consistency | 1 | 單次純讀取跨 PR/default ref/merge ancestry/issues/OpenSpec/Todo/authority inputs 並檢查來源一致；不寫 local durable state、不做跨-store CAS。 |
| acceptance_surfaces | 2 | fix-standard 有 2 gate_spine，adapter 固定 R-09/R-16/R-19，signal=5。 |
| spec_stability | 0 | spec/design/plan accepted 且 completeness gate 無 blocker。 |
| orchestration | 2 | fix-standard 9 cards、9 persona bindings。 |
| **Total** | **5 / Yellow** | **0+1+2+0+2=5**。 |

若需要修改第二個 production module，domain_breadth 至少升為 1。與 #977 的 manager.py finalizer 合併為一個 scope 時 domain_breadth 至少 1、state_consistency=2，其餘維度不變，至少 7 / Red；因此先以獨立 issue-backed prerequisite 規劃。任何 combo、contract rules、production scope 或 completeness 變動都要重跑 current_sizing_snapshot；Red 時保留全部驗收並另分票，不偷降分數。

## Completion accounting

本票完成只證明 delivery.py pure inspection 的自身 API/tests/docs/policy gates；不代表 #977 Manager integration、#962 aggregate、#887 parent closure、外部 GitHub delivery 或 installed runtime 完成。
