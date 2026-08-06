---
status: accepted
work_item: feat-work-gc-v2
---

# feat-work-gc Specification

#178：提煉 v0.1.0 收尾的爛尾清理邏輯為 `cortex work gc` 命令，讓 operator 以單一 proposal-first 命令安全回收殘留 build worktree 與已 merge 的 local branch，且絕不誤刪任何內容尚未落地的產物。

## 背景

cortex 每批次派工會產生大量 build 產物（worktree、交付分支、run、PR、journal、correlation），但沒有生命週期回收；v0.1.0 收尾時 operator 手動清了 8 個批次 worktree 與約 19 個分支，並暴露三個反直覺陷阱：

- **squash / rebase merge 後 ref-ancestry 失真**：PR squash 後原分支 commit 不在 default branch 歷史，`git branch --merged`／`rev-list main..branch` 顯示 unmerged，但內容其實已落地。經另一分支併入、fragment 收攏、跨 repo 搬移也都會造成同樣假象。
- **`git branch -d` 誤拒**：`-d` 對「配置的 upstream」判定而非對 default branch，導致確定已合併（`main..branch` 為 +0）的分支仍被拒刪。
- **closed-unmerged PR 的 correlation 凍結**：已關閉未合併的 PR 其 `closingIssuesReferences` 被 GitHub 凍結，對應分支的內容去向必須人工考證，絕不可自動回收。

因此安全清理必須做內容層驗證（deliverable 是否已存在於 default branch），不能只信 ref-ancestry；任何疑義都必須保留並回報，讓 operator 審閱後決定。

## Goals

- operator 以單一命令列出並回收一個 repo 的殘留 build worktree 與已 merge local branch。
- squash-merge 後的分支被正確判為 merged，不因 ref-ancestry 失真而漏收。
- 任何內容尚未落地的分支、dirty worktree 或判定疑義一律保留並附理由，絕不誤刪。

## Requirements

### R1 proposal-first 回收命令

`cortex work gc` SHALL 提供 repo 層的產物回收命令。

預設模式 MUST 為 dry-run：只輸出候選清單，逐項標明 artifact 種類（worktree／branch）、判定結果（`reclaim`／`keep`）與 reason code，MUST NOT 改變任何 git 狀態。

只有帶 `--apply` 時 MAY 執行回收，且 MUST 只處理判定為 `reclaim` 的項目。

### R2 merged 判定必須內容層驗證

分支是否 merged 的判定 MUST 依序採用以下驗證鏈，任一成立即視為 merged：

1. **ancestor 判定**：`git merge-base --is-ancestor <branch-tip> <default-branch>` 成立。
2. **內容等價判定**：`git cherry <default-branch> <branch>` 輸出不含 `+` 行（所有 patch 皆已以等價內容存在於 default branch）。

兩者皆不成立時 MUST 視為 unmerged。

判定 MUST NOT 以 `git branch -d`、`git branch --merged` 或 upstream ref-ancestry 為準。squash-merge 後內容已落地 default branch 的分支 MUST 被正確判為 merged。

default branch MUST 依 `origin/HEAD` 解析，無法解析時退回 `main`；GC MUST NOT 主動 fetch（比對基準過時只會導致多保留，不會導致誤刪）。

### R3 fail-safe：疑義一律保留

任何疑義 MUST 判 `keep` 並附具體 reason code，MUST NOT 進入 `--apply` 執行清單：

- 內容比對不吻合（unmerged content）。
- worktree 有未 commit 變更或 untracked 檔案（dirty worktree）。
- 判定過程任何 git 命令失敗（verification error）。
- default branch、目前 checked-out branch、掛在 keep worktree 上的 branch（protected）。

對應 PR 為 closed-unmerged 的分支 MUST 保留；PR 狀態查詢屬 best-effort 註記，查詢不可用時 MUST 安全退化為內容層判定結果（同樣保留）。

`--apply` 執行中單項失敗 MUST 記為 `keep` 並附 reason，且 MUST 續行其餘項目，MUST NOT 因單項失敗中斷整批。

### R4 回收範圍與報告

候選範圍 SHALL 為：worktree pool（repo 同層 `<repo>-worktrees`，`PSC_WORKTREE_ROOT` 可覆寫）內的殘留 build worktree，與 repo 的 local branch。

`--apply` 對 clean 且 merged 的 worktree MUST 以 `git worktree remove` 移除；對通過 R2 驗證鏈的 merged branch MUST 於刪除前重新驗證一次，再以 `git branch -D` 刪除。

報告 MUST 逐項列出 artifact、判定與 reason；`--json` MUST 輸出 versioned schema（`cortex-work-gc/v1`），含 dry-run／apply 模式標記與逐項結果。

### R5 registry 唯讀與範圍邊界

GC MUST NOT 寫入 `jobs.json` 或任何 manager 狀態檔（manager 是單一 writer，GC 僅讀）。

GC MUST NOT 刪除 remote branch、MUST NOT 操作 PR、MUST NOT 清理 delivery journal 或 correlation（皆屬非目標）。

## 非目標

- remote branch 刪除（本命令僅處理 local 產物）。
- registry（`jobs.json`）的壓縮、歸檔或任何 mutate（manager 單一 writer；GC 僅讀）。
- 死 PR／closed-PR correlation 凍結的退場規則（#175 範圍）。
- delivery journal 孤兒 row 清理（#175 範圍）。
- 跨 repo 搬移的自動偵測（此類分支經 R2 判 unmerged 而保留，由 operator 依 keep 清單人工處置）。

## 驗收面

- squash-merge 後的分支被正確判 merged 並可回收，不被誤判 unmerged。
- unmerged 分支絕不出現在 `--apply` 執行清單，`--apply` 後仍存在。
- dirty worktree 保留並附 `dirty-worktree` 理由。
- closed-unmerged PR 對應分支保留；PR 查詢不可用時安全退化仍保留。
- 預設 dry-run 對 worktree、分支與 `jobs.json` 皆零變更。
- git 命令失敗時該項判 `keep` 並附理由，不中斷整批。
