---
status: accepted
work_item: feat-work-gc-v2
---

# feat-work-gc Design

## Decisions

- 新模組 `coordinator/gc.py`＋umbrella CLI 子命令 `cortex work gc`，不經 manager daemon、
  不動 control queue 白名單；registry（jobs.json）唯讀（manager 單一 writer 紅線）。
- proposal-first：預設 dry-run 只輸出候選清單與逐項 reason code；`--apply` 才執行且僅
  處理 reclaim 項目，執行前逐項重驗（防 TOCTOU）。
- merged 判定用兩段內容層驗證鏈：`git merge-base --is-ancestor` → `git cherry` 無 `+` 行
  即內容等價；禁止只靠 ref-ancestry（squash-merge 陷阱）；default branch 依 origin/HEAD
  解析退回 main、不主動 fetch（基準過時只多保留不誤刪）。
- fail-safe reason code 分類（merged-ancestor／merged-content／unmerged-content／
  dirty-worktree／protected／pr-closed-unmerged／verification-error／apply-error）；任何
  git 失敗或疑義一律 keep 並標註原因，絕不刪 unmerged content。
- closed-unmerged PR 偵測為 best-effort 註記，不參與回收判定；remote branch 刪除與
  registry 壓縮列為非目標。

詳細 D1–D6 與風險緩解見 `docs/superpowers/specs/feat-work-gc-v2-design.md`。
