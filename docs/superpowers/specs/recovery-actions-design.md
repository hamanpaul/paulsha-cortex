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

### D1. 一個 fail-closed context parser

在 `work_actions.py` 建共用讀取 helper，使用 child 02 的 typed `MainSyncContext` parser，檢查 `context.main_sync` shape、Candidate CAS、repo object format/commit validity 與 failure-vs-retryable 分類。保留完整 conflict path array，不 stringify；無 context 時舊 run 原樣只有舊 actions。

### D2. Availability mirror reset

`_phase_recovery_actions` 使用與 registry retry-build reset 相同的條件：ongoing；verify/review；needs_human；無 active job；至少一個 build step 且全 passed；passed ship steps 不超一筆，若有則精確 Manager-owned OpenSpec archive；合法 Candidate 且 C/M context一致。測試需用同一 run 呼叫 reset，而非只比較 helper 回傳。

### D3. Actions 只在 operator 選擇後生效

Manual retry action 使用 typed context M，不 re-probe。Validator 不會調用 reset，不自動 dispatch Builder；Builder selector / merge commit / candidate re-verification 屬 #973。`main-sync-unavailable` 和限定 `not-mergeable` 各自提供安全 resume path。Manager status projection 由 child 04 負責。

### D4. Sizing (#208)

僅 `work_actions.py` 一個 production module（domain 0）；durable C/M、Candidate CAS 與 reset eligibility（state 2）；fix-standard acceptance 2、spec stability 0、orchestration 2；helper 結果 6／Yellow。
