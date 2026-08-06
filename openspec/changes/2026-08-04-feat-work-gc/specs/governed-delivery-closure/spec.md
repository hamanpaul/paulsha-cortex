---
status: accepted
work_item: feat-work-gc-v2
---

## ADDED Requirements

### Requirement: 交付後產物回收必須 proposal-first

`cortex work gc` MUST 預設以 dry-run 模式執行：只輸出候選清單與逐項判定（`reclaim`／`keep`＋reason code），MUST NOT 改變任何 git 狀態。只有帶 `--apply` 時 MAY 執行回收，且 MUST 只處理判定為 `reclaim` 的項目；執行前 MUST 對每個項目重新驗證，狀態已變時 MUST 轉為保留並附理由。

#### Scenario: 預設 dry-run 零變更

- **WHEN** operator 執行 `cortex work gc` 未帶 `--apply`
- **THEN** 所有 worktree、local branch 與 `jobs.json` 內容皆不變
- **THEN** 報告逐項列出候選 artifact、判定與 reason code

### Requirement: merged 判定必須內容層驗證

分支 merged 判定 MUST 依序採用：`git merge-base --is-ancestor <tip> <default>` 成立即 merged；否則 `git cherry <default> <branch>` 輸出不含 `+` 行即 merged；兩者皆不成立 MUST 視為 unmerged。判定 MUST NOT 以 `git branch -d`、`git branch --merged` 或 upstream ref-ancestry 為準。squash-merge 後內容已落地 default branch 的分支 MUST 被正確判為 merged。default branch MUST 依 `origin/HEAD` 解析、無法解析時退回 `main`，且 MUST NOT 主動 fetch。

#### Scenario: squash-merge 分支正確判 merged

- **WHEN** 分支內容以單一 squash commit 落地 default branch，原分支 commit 不在其歷史
- **THEN** 該分支判 `reclaim`（reason `merged-content`），不被誤判 unmerged

#### Scenario: 內容比對不吻合視為 unmerged

- **WHEN** `git cherry` 輸出含 `+` 行（存在未落地 patch）
- **THEN** 該分支判 `keep`（reason `unmerged-content`）

### Requirement: 回收必須 fail-safe 保留疑義

unmerged content MUST NOT 出現在 `--apply` 執行清單。dirty worktree（未 commit 變更或 untracked 檔案）、判定過程 git 命令失敗、default branch、目前 checked-out branch 與掛在保留 worktree 上的 branch MUST 一律判 `keep` 並附具體 reason code。對應 PR 為 closed-unmerged 的分支 MUST 保留；PR 狀態查詢屬 best-effort 註記，查詢不可用時 MUST 安全退化為內容層判定結果。`--apply` 單項失敗 MUST 記為保留並續行，MUST NOT 中斷整批。

#### Scenario: unmerged 分支絕不刪除

- **WHEN** 分支含未落地 commit 且 operator 帶 `--apply` 執行
- **THEN** 該分支不在執行清單，執行後仍存在

#### Scenario: dirty worktree 保留

- **WHEN** 候選 worktree 有未 commit 變更
- **THEN** 判 `keep`（reason `dirty-worktree`），`--apply` 不移除

#### Scenario: closed-unmerged PR 分支保留

- **WHEN** 分支對應的 PR 已關閉且未合併
- **THEN** 判 `keep`，PR 查詢可用時 reason 為 `pr-closed-unmerged`，不可用時退化為 `unmerged-content`

### Requirement: GC 對 registry 唯讀且不動 remote

GC MUST NOT 寫入 `jobs.json` 或任何 manager 狀態檔（manager 是單一 writer，GC 僅讀）。GC MUST NOT 刪除 remote branch，MUST NOT 操作 PR，MUST NOT 清理 delivery journal 或 correlation。

#### Scenario: apply 後 registry 不變

- **WHEN** operator 帶 `--apply` 完成回收
- **THEN** `jobs.json` 位元組層不變，remote refs 不變
