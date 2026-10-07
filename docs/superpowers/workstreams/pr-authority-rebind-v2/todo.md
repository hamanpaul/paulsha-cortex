---
status: accepted
work_item: pr-authority-rebind-v2
---

# PR 已存在的 run 在正式生命週期下能 resume：先找出 WorkAuthority mismatch 的真正原因再修

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1360`（#1339 重做；第一版 PR #1358 經獨立審查判定 request-changes，不合併）。
- 現象：PR 已存在的 run，resume／review-disposition 在 `work_actions._validate_current_run_authority` 回 `persisted workflow does not match current WorkAuthority`。同一個 candidate 上執行 `retry-review` 後，resume 是唯一能派出替代 review job 的路，但這條路也被擋，於是 run 卡死。現場：PR #1318、#1324、#1316、#1358、#1354、#1355。
- 審查結論（詳見 #1360）：正式的 `claim.semantic_source_revision` 中，`github_pr:` row 只有 `identity`／`state`，review、留言、CI 都不會進入 `source_revisions`。真正原因待查，推測是 `pr_candidate` 移動，或 `work_bridge._rebase_delivery_journal_authority` 改寫 journal row 之後，self-only drift 檢查失效。
- 不得退化：既有 resume 分支（merged-delivery-closure、active-workflow、self-only drift、`_resume_existing_candidate_after_authority_change`、authority restart）的行為要維持不變，也不得增加網路呼叫。真正改變交付目標的變動（PR 改綁、issue 關閉、openspec mapping 變動）仍要 fail closed。

## Tasks

- [ ] **T1 RED：用正式格式找出原因**：以正式的 `semantic_source_revision` 格式與 journal 生命週期（claim 時沒有 PR → ship push → `_rebase_delivery_journal_authority` → 後續的 autosync／retry-build／Copilot review）重現 mismatch，在 PR 中寫明實際觸發的欄位差異。
- [ ] **T2 最小修正**：只修 T1 找到的原因，不新增優先於既有 resume 分支的檢查。
- [ ] **T3 不退化測試**：審查列出的 3 個案例（PR 已合併時的 merged-delivery-closure；head 等於 candidate 且 readback 失敗時的 active-workflow；OpenSpec revision 變動時 reason 要正確），都要有測試，結果與 base 相同。
- [ ] **T4 恢復路徑**：PR 已存在時，`regenerate-gates`→`resume` 能採信已通過的 build；同一個 candidate 上 `retry-review` 之後，resume 能派出替代的 review job。
- [ ] **T5 fail closed**：PR 改綁、issue 關閉、openspec mapping 變動時拒絕，reason 正確。
- [ ] **T6 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
