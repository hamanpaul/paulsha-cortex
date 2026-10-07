---
status: accepted
work_item: delivery-authority-refresh
---

# ship 在 preflight 後重新取得 WorkAuthority；stale 失敗不得讓每輪 tick 重跑 preflight

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1322`，含 issue 留言（stale 迴圈的嚴重度）。
- 現況：ship 在 preflight **之前**載入 confirmed `WorkAuthority`（含 `github_last_success_epoch`）。preflight（daemon 內同步跑完整 pytest，15～19 分鐘）結束後，`delivery.merge_if_ready` → `_validate_work_authority` 以「現在 − 載入時的 epoch」對照 `claim.PROVIDER_MAX_AGE_SECONDS = 900`，所以必然以 `provider degraded or stale` 失敗。這段期間 Monitor 其實持續新鮮。
- stale 失敗不會設 needs_human，run 停在 review→ship 等待推進，於是每輪 periodic tick 又重跑一次 preflight → stale。2026-10-06 daemon 因此超過 1 小時沒有更新 status，佇列中的 control request 都沒被處理。
- 目前暫由 operator 合併「只因 stale 失敗、且仍與 main 同步」的 PR（owner 同意，本票落地後停用）。
- 不放寬 authority 的一致性檢查（mapped PR／issue／todo、source revision、exact head/tree）。

## Tasks

- [ ] **T1 RED**：以假的耗時 preflight（超過 900 秒）、期間持續更新的 snapshot，測試現行 ship 以 stale 失敗；並測試 stale 失敗後下一輪 tick 會重跑 preflight。
- [ ] **T2 preflight 後重新取得 authority**：preflight 結束後重新載入 confirmed `WorkAuthority`，確認交付目標與 preflight 前一致（mapped PR、issue、todo、source revision 相同，PR head 與 tree 和 preflight 一致），再用新的那份做新鮮度檢查與 merge。不一致時 fail closed，並給出明確的 reason。
- [ ] **T3 不重跑已通過的 preflight**：同一 head/tree 的 preflight 已通過（已有 merge-authorization evidence）時，後續 tick 只重新取得 authority 後再試 merge，不重跑 preflight。stale 屬於暫時性錯誤，依 backoff 重試；超過上限才轉 needs_human，並寫出明確的 reason。
- [ ] **T4 full-suite 證據的時間競賽**：檢查 `DEFAULT_FULL_SUITE_MAX_AGE_SECONDS`（900）與 preflight 實際耗時之間有沒有同樣的問題，有的話一併修正。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
