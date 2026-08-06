---
status: accepted
work_item: feat-work-gc-v2
---

## Goals

提煉 v0.1.0 收尾的爛尾清理邏輯為 `cortex work gc` 命令：operator 以單一 proposal-first 命令安全回收殘留 build worktree 與已 merge 的 local branch，絕不誤刪任何內容尚未落地的產物。

## Why

cortex 每批次派工產生的 build 產物沒有生命週期回收，v0.1.0 收尾全靠手動考古式清理，且標準 git 訊號會騙人：squash merge 後 ref-ancestry 失真讓已落地分支顯示 unmerged、`git branch -d` 對 upstream 判定會誤拒已合併分支、closed-unmerged PR 對應分支的內容去向必須人工考證。安全清理必須做內容層驗證並對疑義 fail-safe。

## What Changes

- 新增 `paulsha_cortex/coordinator/gc.py` 與 CLI 子命令 `cortex work gc`（umbrella 攔截路由，不經 manager daemon、不動 control queue 白名單）。
- proposal-first：預設 dry-run 只輸出候選清單與逐項理由（`reclaim`／`keep`＋reason code），`--apply` 才執行且僅處理 `reclaim` 項目。
- merged 判定採內容層驗證鏈：`git merge-base --is-ancestor` → `git cherry` patch 等價比對 default branch；squash-merge 分支正確判 merged；禁止以 `git branch -d`／`--merged` 的 ref-ancestry 為準。
- fail-safe：unmerged content、dirty worktree、git 命令失敗、protected refs 一律 `keep`＋理由；closed-unmerged PR 分支保留（PR 查詢為 best-effort 註記，不可用時安全退化）。
- 範圍收斂：僅回收殘留 worktree 與已 merge local branch 並報告（文字＋`cortex-work-gc/v1` JSON）；registry（`jobs.json`）唯讀不 mutate，remote branch 刪除列非目標。
- CLI help 同步：`_WORK_HELP` 加入 `gc`（R-16）。

## Capabilities

### Modified Capabilities
- `governed-delivery-closure`：新增交付後產物回收（proposal-first、內容層 merged 驗證、fail-safe 保留、registry 唯讀）要求；詳見 `docs/superpowers/specs/feat-work-gc-v2-spec.md` 的 Requirements 與 `docs/superpowers/specs/feat-work-gc-v2-design.md` 的 Decisions。
