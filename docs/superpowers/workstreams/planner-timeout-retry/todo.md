---
status: accepted
work_item: planner-timeout-retry
---

# brainstorm 呼叫 planner 的逾時與自動重試

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1313`。
- 現況：define 階段的 heterogeneous brainstorm 呼叫第二位 planner（agy）時，參數帶 `--print-timeout 2400s`，但外層 subprocess 只等 120 秒就 `TimeoutExpired`。結果被判為 `secondary-output-malformed` → `brainstorm-not-ready`（needs_human）。
- 2026-10-06 `worktree-containment-authority`、`async-long-requests` 都卡在這裡，只能手動 `recover-planning`；而且 `failure_reason` 必須和記錄裡的長字串完全相同。
- 不改 brainstorm 的 domain 獨立性要求與收斂判準。

## Tasks

- [ ] **T1 RED**：測試以假 planner 模擬「回應時間超過 120 秒、但在宣告的逾時內」，現行應判為逾時。
- [ ] **T2 逾時一致**：外層 subprocess 的逾時與 planner 自身宣告的逾時一致（由 adapter 或身分矩陣提供，預設與 `--print-timeout` 相同），逾時時結束整個 process group。
- [ ] **T3 自動重試與改派**：planner 逾時、限流這類 environment 失敗，自動有上限地重試；仍失敗時，改用另一位符合 domain 獨立性的 planner。用盡後才轉 needs_human。
- [ ] **T4 recover-planning 易用性**：`recover-planning` 允許用 failure 記錄的 id 或 digest 指定失敗，不必複製完整的 reason 字串（舊用法保持相容）。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
