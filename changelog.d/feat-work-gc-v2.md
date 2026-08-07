### Added
- **Issue #178：新增 `cortex work gc` 交付後產物回收命令**：proposal-first
  回收殘留 build worktree（worktree pool，`PSC_WORKTREE_ROOT` 可覆寫）與已
  merge 的 repo local branch；預設 dry-run 只輸出候選清單與逐項
  `reclaim`／`keep`＋reason code，帶 `--apply` 才執行且只處理 `reclaim`
  項目，執行前逐項重新驗證（TOCTOU-safe）。merged 判定改走內容層驗證鏈
  （`git merge-base --is-ancestor` → `git cherry` 內容等價），修正
  squash-merge 後 ref-ancestry 失真、`git branch -d`／`--merged` 誤拒已合併
  分支的既有陷阱；不影響本次交付範圍的 upstream ref 判定，全程不主動
  fetch。任何疑義（unmerged content、dirty worktree、git 命令失敗、
  default／目前 checked-out／掛在保留 worktree 上的 branch）一律 `keep`
  並附機器可讀 reason code；closed-unmerged PR 分支保留，PR 查詢為
  best-effort 註記、不可用時安全退化。新模組 `paulsha_cortex/coordinator/gc.py`
  由 umbrella CLI 在 `work show` 之後同一層攔截路由，不經 manager daemon、
  不動 control queue `WORK_ACTIONS` 白名單；對 `jobs.json`／manager 狀態檔
  唯讀，不刪 remote branch、不操作 PR、不清 delivery journal／correlation。
