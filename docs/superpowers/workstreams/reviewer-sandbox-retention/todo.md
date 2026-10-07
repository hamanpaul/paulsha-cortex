---
status: accepted
work_item: reviewer-sandbox-retention
---

# reviewer sandbox 只在新卡派出或 terminal 採信後才丟棄；sandbox 不存在時的錯誤要明確

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1321`。
- 現況：2026-10-06，兩張已通過的 verification job（`codex-readonly-sandbox`、`adoption-review-followups`）結束後，sandbox 在採信前就被刪除。收割以 `terminalize-workflow-job-failed: workflow input snapshot file missing` 失敗，只能重跑。job 的 claim_key 與 run 相同，所以排除 era reclaim。推測是 `resume_workflow_run` 的 reviewer recovery 路徑先呼叫 `_discard_reviewer_sandbox` 才派卡，派卡擲出例外時，terminal 已失去 sandbox。
- 先以 RED 確認推測，再修正；若 RED 顯示是別的刪除路徑，修那條路徑，並在 PR 中說明。
- 不改 reviewer 採信的判準（CAS、candidate 不變、input snapshot hash）。

## Tasks

- [x] **T1 RED**：重現「reviewer job 已結束、未採信，resume 的派卡擲出例外」之後 sandbox 被刪除、下一輪收割失敗。
- [x] **T2 延後丟棄**：sandbox 只在新卡成功派出，或 terminal 已採信（evidence 落地）之後才丟棄；派卡失敗時保留，下一輪可直接採信既有的 terminal。
- [x] **T3 明確錯誤**：terminal 的 sandbox 已不存在時，needs_human 的 reason 明說（例如 `reviewer-sandbox-discarded-before-adoption`），next_actions 含 `retry-card`。
- [x] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
