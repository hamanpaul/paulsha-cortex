---
status: accepted
work_item: main-sync-recovery-actions
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Main-sync recovery actions 設計

## Decisions

### D1. One read-only eligibility helper is the shared authority

在 `work_actions.py` 內建立 main-sync recovery eligibility helper，讀取目前 WorkflowRun、registry jobs/steps、`needs_human_reason.context.main_sync` 與該 run repo 的 Git object truth。`_claim_action` 與 `_retry_build_action` 共用這個 helper；`_retry_build_action` 在任何副作用前再次判斷。helper 只計算 availability，不做 reset、probe、Builder dispatch 或其他 write。`_manager_reset_workflow_for_retry_build` 仍是最後的原子 admission gate；helper 複製其 build/ship/job 前置條件，不能以提示取代 registry acceptance。

### D2. The persisted typed C/M object is the only main-sync authority

等 #987/#988 交付後，從正式 `DiagnosticReason` reader 取得 typed `MainSyncContext`，不在 action 層自行解析 evidence prose、`str(dict)` 或一份平行 JSON schema。若 probe 在 valid-C 前失敗，依 #988 對抗 review 修訂後 contract 保留「沒有合法 C」；action 不得從 run Candidate 或其他來源補造 C。對 retry-build 要求可分類的 repair context、無 typed probe failure、完整 C 與 M；驗證兩個 full SHA 符合該 repo object format 且可解析為 commit。C 必須與 run 當下 `candidate_head` 完全相等；任何 stale/missing/corrupt/mismatched context、pre-valid-C failure 或 typed failure 都不能授權 retry-build。唯一 failure recovery 是有效 durable reason 精確分類為 `main-sync-unavailable` 時沿既有 `resume` 重新 probe；缺失或無法解析 context 不新增 recovery action。

### D3. Preserve M1 through the explicit repair action

`retry-build` prompt/action payload 明確帶出 `TARGET_MAIN_SHA: M1`、原 Candidate C、repair kind 與每條完整 conflict path；不得重新解析 `origin/main` 或用 stop 後的 M2 改寫 M1。若 probe 尚無合法 C、M1 不在目前 repo object store、context 中 C/M 不合法，helper 不曝光 action，direct call 也在 registry mutation 前拒絕。現有 `expected_candidate` operator CAS 仍必須精確等於 C。

### D4. Eligibility mirrors the existing reset contract

適用 main-sync repair 的 run 必須唯一、canonical、ongoing、verify/review、needs_human，沒有 active job；build steps 非空且全部 passed；passed ship steps 至多一筆，若存在為 registry 接受的 Manager-owned `openspec-archive`。helper 需從同一份 step/job 判準算 availability，再由實際 `_manager_reset_workflow_for_retry_build` 原子重驗。用單一 synthetic WorkflowRun 真執行 reset 驗成功受理；負例逐項確認沒有 action、沒有 reset side effect。不能先呼叫 reset 試探可否，再回滾當作曝光計算。

### D5. Keep resume predicates narrow and reuse the official path

`main-sync-unavailable` 直接提供既有 `resume`，讓已修復的環境回到正式 ship validator 並由 #987 probe producer 重新取樣。`review-advance-failed` 只有在其現有 reason/detail 同時含 `merge authorization blocked:` 和 `not-mergeable` 時加上 `resume`；用完整 reason predicate，不把其他 review/authorization failure 一起放寬。兩條都不 reset builder、不派 repair job。其他 next action 順序與提示維持不變。

### D6. Keep `next_actions` and the executable action in lockstep

`_claim_action` 對 main-sync stop 的 actions/hint 與 direct `retry-build` admission 使用 D1 同一 helper；對讀取錯誤、未知 context/schema 或 registry 不確定結果，不曝光 retry-build。#990 的 `workflow_status_entry` 之後呼叫同一 helper，保持 claim/status/action 三個投影一致；status writer 和 durable context persistence 不在本票。

### D7. Scope, tests, and dependency freeze

Production 只動 `paulsha_cortex/coordinator/work_actions.py`。測試放在 work-actions recovery suite，使用合成 WorkflowRun、Fake/JobRegistry 以及專用 repo fixture 驗 SHA/object-format 與 M1 固定；reset acceptance 測試停止於 registry reset，不接續 Builder dispatch。依賴 #987/#988 的實作與驗收完成後，先核對實際 producer/schema exact fields；若兩者與本設計不相容，先更新 issue-backed artifacts 再開始，不以猜測補一層 adapter。

### D8. Official sizing (#208)

使用 `fix-standard` 9-card manifest：production 只有 `work_actions.py`，`domain_breadth=0`；durable C/M 與 current Candidate CAS、registry reset admission 跨一致性邊界，`state_consistency=2`；2 個 gate-spine 加 R-09/R-16/R-19 得 `acceptance_surfaces=2`；accepted 三件套、沒有 planning blocker 得 `spec_stability=0`；9 cards 且 9 個 persona bindings 得 `orchestration=2`。合計 `0+2+2+0+2=6 / Yellow`。這個規劃估算必須用 `current_sizing_snapshot()` 對實際 artifacts 重算；若 production scope 或 helper 結果不同，依官方實算修正或另立 issue 拆分。
