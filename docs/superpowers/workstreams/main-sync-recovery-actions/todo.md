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

# Main-sync recovery actions todo

## Tasks

- [ ] 在 `work_actions.py` 使用 child 02 typed `MainSyncContext`，實作 complete-field/CAS parser、registry reset eligibility mirror、固定 M 的 `_retry_build_action` 和 resume branches；paths 不得由字串還原。
- [ ] 為所有 reset 前置加入 positive/negative test；positive run 實際受 registry reset 接受但不接續 Builder dispatch。
- [ ] 測 M1 stop 後 main 移至 M2 action 仍固定 M1；missing/corrupt/invalid SHA/C mismatch 不曝光也不執行；其他 repair prompt 完全不變。
- [ ] 更新 claim action hint；保留 status projection 由後續 Manager descendant 接線。

## Sizing inputs

單一 production module。C/M + CAS/reset availability 是跨重試狀態一致性問題，state consistency 2。以本 issue 自身 complete accepted planning triplet 取得 stability 0；fix-standard acceptance=2/orchestration=2，helper 結果 6／Yellow。

## Dependencies

Blocked by descendants 01 and 02. Manager wrapper/status projection is descendant 04 and is not covered by this child.
