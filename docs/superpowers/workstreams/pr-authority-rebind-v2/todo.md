---
status: accepted
work_item: pr-authority-rebind-v2
---

# PR 已存在的 run 在正式生命週期下能 resume：先找出 WorkAuthority mismatch 的真正原因再修

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1360`（#1339 重做；第一版 PR #1358 經獨立審查判定 request-changes，不合併）。
- 現象：PR 已存在的 run，resume／review-disposition 在 `work_actions._validate_current_run_authority` 回 `persisted workflow does not match current WorkAuthority`。同一個 candidate 上執行 `retry-review` 後，resume 是唯一能派出替代 review job 的路，但這條路也被擋，於是 run 卡死。現場：PR #1318、#1324、#1316、#1358、#1354、#1355。
- **已確認的原因**（2026-10-08，兩個 operator session 以唯讀方式比對）：mismatch 的欄位是 `claim_key`。delivery journal（`<coordinator>/delivery-journal.json`）的 active row 中，`claim_key` 停在舊 era，WorkflowRun 卻已換成新 era；digest、`authority_digest`、`mapped_*` 都一致。
  - authority-restart，例如 autosync 之後的 resume，會更新 `WorkflowRun.claim_key`，但 delivery journal 不會跟著更新。
  - `work_actions._load_work_run`（約 1301 行）只同步 `source_revisions`、`snapshot_hash`、`provider_revision`，沒有同步 `claim_key`。
  - 因此只要 era 換代，`_validate_current_run_authority` 就一定失敗。`retry-build` 會重寫 journal row，所以只有它能 rebind。
  - 實例（run 與 journal 的 claim_key 不一致）：`pr-change-authority-rebind`、`gate-pythonpath-isolation`、`copilot-fix-round-accounting`、`qualification-test-fixed-clock`、`async-long-requests`，以及另一個 session 的 `upgrade-venv-umask`、`dangerous-command-blocklist`。
- 第一版審查也指出：正式的 `github_pr:` row 只有 `identity`／`state`，review、留言、CI 不會進入 `source_revisions`，所以「PR 變動讓 claim 失效」的前提不成立，不要沿用第一版的 rebind 設計。
- 不得退化：既有 resume 分支（merged-delivery-closure、active-workflow、self-only drift、`_resume_existing_candidate_after_authority_change`、authority restart）的行為要維持不變，也不得增加網路呼叫。真正改變交付目標的變動（PR 改綁、issue 關閉、openspec mapping 變動）仍要 fail closed。

## Tasks

- [x] **T1 RED**：重現「delivery journal 已有 active row → authority-restart 讓 `WorkflowRun.claim_key` 換代 → resume」時回 `persisted workflow does not match current WorkAuthority`。用正式的 revision 格式與 journal 生命週期，不要用假的 fixture 字串。
- [x] **T2 最小修正**：era 換代時，讓 delivery journal 的 active row 跟上 `claim_key`。可以在 authority-restart 時同步更新，或讓 `_load_work_run` 在 claim era 經過驗證後（`authority_matches_claim_era`）同步 `claim_key`。不新增優先於既有 resume 分支的檢查；不放寬其他欄位的比對。已經卡住的現有 run，升級後 resume 也要能自動恢復。
- [x] **T3 不退化測試**：審查列出的 3 個案例（PR 已合併時的 merged-delivery-closure；head 等於 candidate 且 readback 失敗時的 active-workflow；OpenSpec revision 變動時 reason 要正確），都要有測試，結果與 base 相同。
- [x] **T4 恢復路徑**：PR 已存在時，`regenerate-gates`→`resume` 能採信已通過的 build；同一個 candidate 上 `retry-review` 之後，resume 能派出替代的 review job。
- [x] **T5 fail closed**：PR 改綁、issue 關閉、openspec mapping 變動時拒絕，reason 正確。
- [x] **T6 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
