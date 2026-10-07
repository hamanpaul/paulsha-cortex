---
status: accepted
work_item: pr-change-authority-rebind
---

# PR 建立後，PR 本身的變動不得讓 resume 失效

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1339`。
- 現況：`work_actions._validate_current_run_authority` 用 delivery journal 保存的 `active` ship 綁定，比對目前 WorkAuthority 的 `claim_key`、`source_revisions`、`authority_digest`、`mapped_*`。`source_revisions` 含 `github_pr:` 的版本，所以 PR 只要有任何變動（Copilot review 留言、新 head、review thread），`resume`／`review-disposition` 都會回 `persisted workflow does not match current WorkAuthority`，只能 `retry-build` 整輪重建。
- 現場：PR #1318、#1324、#1316（`regenerate-gates` 之後 resume 一樣被拒）。
- 不放寬真正改變交付目標的檢查：PR 改綁其他 issue、todo 改寫、issue 關閉時仍要 fail closed。

## Tasks

- [ ] **T1 RED**：run 已有 PR，PR 新增一則 review 留言後，`resume` 回 mismatch（現行行為）。
- [ ] **T2 重新綁定**：PR 本身的變動（review、留言、CI 狀態，以及 Manager 自己 push 的 head）不讓 claim 失效。在 resume／review-disposition 時，以 Manager 自產的交付事實（PR head 等於 candidate 或其 autosync 後代）重新綁定 `active`，並留下 receipt。
- [ ] **T3 仍要 fail closed**：PR 改綁、todo 改寫、issue 關閉時拒絕，並給出明確的 reason。
- [ ] **T4 恢復路徑**：`regenerate-gates`→`resume` 在 PR 已存在時能採信已通過的 build。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
