---
status: accepted
work_item: monitor-frontmatter-isolation
---

# Monitor 隔離單一格式錯誤的 planning artifact；planner 寫入 work_item 時要用 slug；stale 要告警

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1319`。
- 現況：`monitor/correlation.py` 的 `read_frontmatter_work_item` 遇到無效的 `work_item`（例如 `owner/repo#N`）時會 raise `CorrelationError`，整個 repo 的 work-model refresh 跟著失敗，所有 provider 都變成 stale，ship 的 `merge_if_ready` 也全部被擋。2026-10-06 持續約 2.5 小時，起因是一個 run 的 planner 在 operator checkout 寫出錯誤的 frontmatter。
- 不放寬 `work_item` slug 的格式規則，也不改 correlation 的歸屬判準；只改變「單一檔案錯誤」的影響範圍。

## Tasks

- [ ] **T1 RED**：測試 operator checkout 有一份 `work_item: owner/repo#N` 的 spec 時，現行 refresh 會失敗、provider 會變成 stale。
- [ ] **T2 Monitor 隔離**：frontmatter 無效的檔案略過歸屬，寫進 diagnostics（檔名與原因，`cortex status` 看得到）；其他來源照常 refresh，provider 保持新鮮。
- [ ] **T3 planner 端修正**：planner 寫出 spec、design、plan 時，`work_item` 一律填 run 的 work_id slug；harvest 時以 `read_frontmatter_work_item` 的同一套規則驗證，錯誤時當場修正為 slug（記錄事件）或判為 schema 失敗，不讓錯誤的檔案落地。
- [ ] **T4 stale 告警**：provider 的 `last_success_at` 超過門檻時，`cortex status` 標示 `monitor-refresh-failing` 與最後一次的錯誤。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
