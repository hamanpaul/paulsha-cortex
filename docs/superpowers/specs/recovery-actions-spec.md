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

本 work item 只改 `work_actions.py`，依 child 02 `diagnostics.py` 定義並已 durable 的 WorkflowRun `needs_human_reason.context.main_sync` typed object 曝光 main-sync operator recovery actions。path array 與 nested fields 必須由結構化 JSON 讀取，不從 detail/evidence 還原。

- 只有 clean-behind / conflict stop 的 context 完整、C/M 均合法 full commit SHA 且 context C 等於目前 Candidate 時，才考慮 `retry-build`。
- `retry-build` availability 必須 mirror registry reset 的 ongoing/phase/needs_human/active-job/build-step/passed-ship-step/archive-authority/CAS 全部條件；同一 run 實際呼叫 reset 證明曝光可受理。
- `_retry_build_action` 只讀 durable M，輸出固定 `TARGET_MAIN_SHA: M`；main 後來前進也不可換 M。missing/corrupt/invalid/CAS mismatch 一律 fail closed。
- `main-sync-unavailable` 提供 resume；`review-advance-failed` 只在 detail 同含 merge-authorization-blocked 與 not-mergeable 時提供 resume。其他 stop action 不變。
- 不寫 Manager durable context、不自動呼叫 reset/派 Builder、不改 job selector、不 merge/push。沒有 durable context 時不得曝光 retry-build。
- 對應 #972 action reachability、C/M safety；#943 的 auto-repair、trusted D、實際 push 仍留給 #973。
