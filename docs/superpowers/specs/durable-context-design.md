---
status: accepted
work_item: main-sync-durable-context
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Main-sync durable context 設計

## Decisions

### D1. WorkflowRun 是 recovery 的 canonical record

Manager `_persist_main_sync_stop` 把 typed stop reason 與 JSON-native `context.main_sync` 在單次 registry update 寫入同一 WorkflowRun，不以 delivery-adapter evidence 做 retry input。沿用 child 02 的 `MainSyncContext` schema，更新其他 context 時 merge-preserve `delivery_reason` 與 detail。Production 接線必須涵蓋 `apply_workflow_action` 的兩個 ship-validator `needs_human` writer branch：已通過 review card 的 `advance-ship` replay branch，以及最後一張 review evidence 使 `advance-phase` transition 觸發 branch；不可只包裝其中一個 call site。

### D2. Read-back before exposure

持久化後用 registry read-back 核對必要欄位、C/M full SHA 與 C/current-candidate。兩個 Manager ship-validator writer branch 均需從 WorkflowRun store read-back 驗收。任何欄位缺失或與 validator result 不一致時保守隱藏 retry-build；不另建 recovery row，不猜測 M。

### D3. Status uses the shared recovery helper

`workflow_status_entry` 呼叫 `work_actions.py` 的 action/hint helper，避免 status hint 與 claim `next_actions` 漂移。只修改 Manager projection/persistence，不在這張 issue 複製 action eligibility 規則。

### D4. Budget/refusal context is ready before #973

writer API 接受 `repair_kind` 與 `skipped_reason`，包含後續 #973 會產生的 budget-exhausted 和 reset-refused stop。Child A 用 synthetic stop payload 證明這些 C/M/context 欄位可耐久 read-back；不把實際 auto-repair/reset/Builder dispatch 移入 #972。#973 blocked by #972 並只在此 contract 落地後呼叫該 API。

### D5. Sizing (#208)

僅 `manager.py` 一個 production module（domain 0）；同一 WorkflowRun facet/reason/context atomic update 與 read-back（state 2）；fix-standard acceptance=2、spec stability=0、orchestration=2；正式 helper result 為 6／Yellow。
