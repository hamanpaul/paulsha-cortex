---
status: accepted
work_item: manager-main-sync-context
issue: 990
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Manager main-sync stop context 設計

## Decisions

### D1. One writer owns durable stop projection

在 `manager.py` 提供共用 `_persist_main_sync_stop(run_id, context, ...)`，由 manual validator stop 與未來 automatic-repair-skipped stop 共用。它只接受 #988 更新後的 typed `MainSyncContext`，並透過既有 WorkflowRun/registry 更新邊界，在同一次 durable update 設定 `needs_human` facet、`delivery-needs-human` reason 與 `needs_human_reason.context.main_sync`。保留原 `delivery_reason`、`detail` 及 reason context 的其他 keys；不得把 evidence ref 當 nested context 的替代品。

### D2. Preserve the exact #988 wire value

Writer 不重新解讀或重新 probe main-sync 欄位。它將 typed `MainSyncContext` 透過更新後 #988 serializer 寫成 JSON-native object，完整保留已驗證 C；Candidate 驗證前則保留 wire contract 定義的原 invalid/abbreviated C observation，且只作 diagnostic evidence。其餘欄位包括 M/null、原生 string-array paths、repair/skipped 欄位及 `failure.{stage,returncode,error_kind,main_head}`。Candidate 驗證前的 failure 必須將 M 與 failure main head 寫為 null；有效 M 已取得後發生的 failure 必須保留同一 M。讀回不得將 nested values 變為字串或縮短任何 path。

### D3. Integrate both existing ship-validator wrapper branches

接線範圍只包括兩個 `manager.py` stop 路徑：`apply_workflow_action` 對已 passed review card 的 `advance-ship` replay，以及最後 review evidence 完成後的 `advance-phase` transition。兩個分支分別呼叫正式 writer；不得只測 helper、私有 serializer 或直接寫 registry 的捷徑。

### D4. Read back from the WorkflowRun store

每條整合測試都在 Manager wrapper 返回後，重新從 durable registry store 取得同一 `run_id` 的 WorkflowRun，並 exact compare nested typed fields、原 delivery reason/detail、原有 sibling context keys 與 needs_human state。測試資料包含多條長 path、含空白/換行的 path、fetch 後 failure M、M=null 的早期 probe failure，以及 Candidate 驗證前 failure 的原 invalid/abbreviated C observation。後者必須保持 diagnostic-only 並確認 retry-build 不可用。只驗 delivery evidence、mock writer arguments 或 transient run object 不足以證明本契約。

### D5. Synthetic skipped-repair stops prove the future writer contract

對 `repair-budget-exhausted` 與 `registry-reset-refused` 分別建立合法 typed synthetic context，走同一 Manager writer 和 registry read-back。測試不得呼叫 repair、reset、Builder dispatch 或其他候選產生路徑；這些 synthetic cases 只證明未來 caller 可以安全保存 stop context。

### D6. Status hints are a projection of action availability

`workflow_status_entry` 消費 #989 recovery helper 的 action availability/result，不另寫一份可用性判斷。helper 不回報 `retry-build` 時 status 不得顯示可用；Candidate 缺失、invalid、abbreviated 或 M 為 null 時也不得顯示 retry-build。`main-sync-unavailable` 以及限定為 merge-authorization-blocked 且 not-mergeable 的 `review-advance-failed` 只在 helper 提供 `resume` 時顯示相符 hint。malformed/missing context 或 durable update/read-back error 都不授權 retry hint。

### D7. Dependency and authority boundary

#987 定義 probe/failure producer；#988 必須先更新 typed `MainSyncContext`，使 Candidate 尚未通過驗證的 failure 也能保留原始 invalid/abbreviated C observation，並以 M=null 表示未成功 probe；#989 提供 recovery action helper。#990 消費更新後的精確 #988 wire contract，不自行發明另一個 payload。產品實作 hard-blocked，直到 #987、#988、#989 的更新後規劃、產品實作與驗收證據完成。此規劃可先發布，但不得 intake、dispatch 或暗示 runtime 已支援。

#990 僅寫 stop persistence/status projection，不做 probe、reset、automatic repair、Builder dispatch、merge commit、push 或 job selection。#973 和 #943 仍擁有後續真實 repair/merge/D-gate/push 驗收。

### D8. Sizing (#208)

依 `fix-standard` 計分：production scope 僅 `manager.py`，domain breadth=0；同一 WorkflowRun stop/context 必須原子寫入並 durable read-back，state consistency=2；2 個 gate_spine 加 R-09/R-16/R-19 得 acceptance surfaces=2；完整 accepted artifacts 得 spec stability=0；9 張 workflow cards 且 9 張皆有 persona binding 得 orchestration=2。預期 `(6, "yellow")`；以 repository `current_sizing_snapshot()` 實算結果為準。
