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

# Main-sync recovery actions 規格

## Requirements

本 work item 的 production scope 只改 `paulsha_cortex/coordinator/work_actions.py`，為 #972 main-sync stop 提供 operator-triggered recovery action。先決條件是 #987 probe producer 與 #988 typed `DiagnosticReason` v3 contract 均已完成 implementation 和 acceptance；規劃文件或前置規劃 PR 合併不代表這兩項已交付。本票不進 Cortex intake、不派 Builder，直到兩個前置 issue 都交付。

`needs_human_reason.context.main_sync` 是 action 唯一可用的 C/M authority。它須由 #988 提供的 typed JSON-native `MainSyncContext` 讀取，至少完整保留 `candidate`、`main_head`、`conflict_paths`、`repair_kind`、`skipped_reason`、`failure.stage`、`failure.returncode`、`failure.error_kind`、`failure.main_head`。長路徑不得 stringify、截斷或合併。缺少、損壞、未知欄位或型別錯誤的 context 一律 fail closed。

若 probe 在取得合法完整 C 前失敗，typed failure context 可依 #988 修訂後的 wire contract 表達 C 不存在；action 不得從 `WorkflowRun.candidate_head`、evidence prose 或其他來源補造 context C。沒有合法完整 C 時絕不曝光或執行 `retry-build`。任何 typed probe failure 都不能授權 `retry-build`；只有有效且分類為 `main-sync-unavailable` 的 durable reason 可提供既有 `resume`，讓正式路徑重新 probe。缺少或無法解析的 typed context 不新增 recovery action。

Main-sync `retry-build` 只可由明確 operator action 啟動，且同時滿足：

- WorkflowRun 為唯一 active canonical run，`status=ongoing`、位於 `verify` 或 `review`，並有 `needs_human` facet。
- 沒有此 run 的 active job；build phase 至少有一張卡，且所有 build steps 均 passed。
- passed ship step 最多一筆；若存在，只能是有效的 Manager-owned `openspec-archive` authority，符合 registry retry-build reset 的身分判準。
- `candidate` 與 `main_head` 均為完整、符合 repo object format 且可解析為 commit object 的 SHA；context `candidate` 必須逐字等於目前 `WorkflowRun.candidate_head`。pre-valid-C failure、任何 typed probe failure、unavailable、缺 C/M、非法 SHA、C/M 不一致或不完整 context 均不得曝光或執行 `retry-build`。unavailable 只能依前述精確 typed reason 提供 `resume`。
- action 可用性與 `_manager_reset_workflow_for_retry_build` 的受理條件同源。直接呼叫 action 時也重新套用同一條 fail-closed 判準；不能只靠 UI/hint 遮住不合格 action。

Action 使用 stop 時已持久化的 `main_head=M` 作為固定 `TARGET_MAIN_SHA`，並將 C、M、`repair_kind`、完整 `conflict_paths` 帶入 operator-triggered builder repair 指示。action 不 fetch、不 probe `origin/main`，也不以後來的 M2 取代停止時的 M1。若 M 無法在該 run 的 repo object store 解析，停止並拒絕 action。

Recovery hint 與 action availability 必須一致：`main-sync-unavailable` 提供既有 `resume`；只有 `review-advance-failed` 且 reason/detail 同時包含 `merge authorization blocked:` 與 `not-mergeable` 時提供 `resume`。其他 stop 的 action 與提示保持原行為。`_claim_action` 的 `next_actions`／`next_step_hint` 使用同一 eligibility helper，讓提示能代表實際可執行結果。

本票不自動 probe、reset WorkflowRun、派 Builder 或執行修復；只有 operator 明確送出合格的 `retry-build` 才走既有 reset 及後續正常派工路徑。此票不改 Manager wrapper、durable writer、job selector、probe producer、merge candidate、真 merge 或 push；#990 負責 Manager context writer/status projection，#973／#943 保留 automatic repair、真 merge 與後續 D gates。

## Acceptance criteria

1. 可用 main-sync `retry-build` 僅在上述 WorkflowRun、job、build、ship、Candidate 與完整 C/M 條件成立時曝光；至少一個合成 WorkflowRun 以同一 registry 真正呼叫 retry-build reset 並受理。每個前置條件失敗時不曝光，直接呼叫也拒絕且不改 registry。
2. operator repair 指示固定使用 durable M1。測試在 stop 後將 main 前進至 M2，仍斷言 action target 是 M1，且 action 路徑沒有重新 probe；pre-valid-C failure、缺失／損壞 context、typed probe failure、非法 SHA、非 commit object 或 C 不等於現行 Candidate 都不曝光／拒絕 `retry-build` 並 fail closed。
3. `main-sync-unavailable` 有 `resume`；只有包含 `merge authorization blocked:` 與 `not-mergeable` 的 `review-advance-failed` 有 `resume`；相近但不符合條件的 stop 不新增 action。
4. `_claim_action` action list/hint 與直接 action admission、registry reset 受理結果一致；共用 helper 對讀取失敗採 fail-closed，不能把不確定狀態提示成可用。
5. 本票沒有 automatic main-sync reset、Builder dispatch、merge 或 push；真 merge、新 Candidate 與 D verification/review/push 留在 #973／父票驗收。

## Dependencies and boundary

- Hard dependency：#987 probe implementation／acceptance，提供可信 C/M producer 與 pre-valid-C failure classification；#988 經對抗 review 修訂後的 typed `main_sync` durable reason implementation／acceptance，明確承載 pre-valid-C failure 而不虛構 C，並提供不截斷的讀取 contract。兩者未交付前不得 intake 或實作本票。
- #989 是 #990 的 prerequisite；#990 只消費本票 recovery helper 投影 status hint，不改寫其 action gate。
- #972 的整合驗收與 #943 的真 merge／Builder re-dispatch／D gates 不因本票規劃或 action acceptance 而縮小。
