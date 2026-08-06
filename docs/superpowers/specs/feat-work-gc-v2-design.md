---
status: accepted
work_item: feat-work-gc-v2
---

# feat-work-gc Design

## Decisions

### D1 新模組 gc.py＋umbrella CLI 攔截，不經 manager daemon

實作放在新模組 `paulsha_cortex/coordinator/gc.py`；CLI 子命令 `cortex work gc` 由 `paulsha_cortex/cli.py` 比照既有 `work show` 的模式，在透傳 coordinator mutation 路徑之前攔截並路由到 gc 模組，同步更新 `_WORK_HELP` 文字。

理由：GC 是「唯讀偵測＋本機 git 操作」，不是 work lifecycle mutation——不寫 registry、不動 run 狀態，走 manager daemon 的單一 writer 佇列既無必要也增加故障面。umbrella 攔截已有 `work show` 前例（讀 Monitor 而非送 daemon），`gc` 沿用同一路由模式即可；control queue 的 `WORK_ACTIONS` 白名單完全不動。

### D2 proposal-first：預設 dry-run，`--apply` 才執行

預設只輸出候選清單與逐項理由；`--apply` 才對 `reclaim` 項目執行回收。

理由：issue #178 的核心教訓是「標準 git 訊號會騙人」——squash merge、經另一分支併入、fragment 收攏、跨 repo 搬移都會讓 ref 層訊號與內容真相背離。operator 必須先看到「GC 打算刪什麼、為什麼」才授權執行；`reap-brokers` 的 dry-run/apply 介面已是 repo 內既有慣例，沿用同一心智模型。

### D3 merged 判定用兩段驗證鏈：ancestor → `git cherry` 內容等價

先跑 `git merge-base --is-ancestor <tip> <default>`（普通 merge 的快速路徑）；不成立再跑 `git cherry <default> <branch>`，輸出無 `+` 行即為「所有 patch 已以等價內容落地」；兩者皆不成立視為 unmerged。default branch 依 `origin/HEAD` 解析、退回 `main`，且不主動 fetch。刪除一律用 `git branch -D`，但僅在驗證鏈通過後。

理由：v0.1.0 收尾實證 `git branch -d`／`--merged` 對 upstream 判定會誤拒已合併分支，而 ref-ancestry 對 squash-merge 必然誤報 unmerged；`git cherry` 以 patch-id 等價比對內容層，正好覆蓋 squash／rebase 案例。不 fetch 讓命令離線可用，且基準過時的失效方向是「多保留」——與 fail-safe 同向，不會誤刪。

### D4 fail-safe 分類輸出：每項 artifact 標 action＋reason code

每個候選 artifact 產出 `reclaim` 或 `keep`，並附機器可讀 reason code：`merged-ancestor`、`merged-content`、`unmerged-content`、`dirty-worktree`、`protected`、`pr-closed-unmerged`、`verification-error`、`apply-error`。任何 git 命令失敗一律轉 `keep`＋`verification-error`；`--apply` 單項失敗記 `apply-error` 並續行。

理由：靜默跳過與靜默刪除都是最糟行為——operator 需要能從報告直接回答「為什麼這條分支沒被收」。reason code 讓報告可 grep、可 JSON 消費，也讓測試能對每個負向案例逐一斷言。

### D5 GitHub 資訊只做 best-effort 註記，不參與回收判定

回收判定純本機 git；closed-unmerged PR 的偵測透過可注入的 PR 狀態 provider（預設走 `gh`）把 keep 理由升級為 `pr-closed-unmerged`，provider 不可用時退化為 `unmerged-content`。

理由：closed-unmerged 分支的內容必然不在 default branch，內容層判定已保證它被保留——GitHub 查詢只影響「理由標註的精確度」，不影響安全性。這讓 GC 離線可用、測試不需網路，也避免 gh 故障變成 GC 故障。

### D6 範圍收斂：worktree＋local branch，registry 唯讀，remote 非目標

本次只做殘留 worktree 與已 merge local branch 的回收與報告。registry（`jobs.json`）壓縮／歸檔、remote branch 刪除、死 PR／correlation 退場（#175）、journal 清理全部列非目標；gc 模組不 import 任何 registry 寫入 API。

理由：manager 是 registry 的單一 writer，GC 兼寫會破壞既有一致性模型；remote 刪除不可逆且跨越本機安全邊界。worktree＋branch 是 v0.1.0 收尾實際手動清理量最大、且可用純本機 git 安全判定的部分——先把這塊做到絕不誤刪，其餘留給 #175 與後續 issue。

## 風險與緩解

- **`git cherry` 對大分支較慢**：候選數量以 worktree pool 與 local branch 為界（數十級），且 ancestor 快速路徑先行；dry-run 為互動命令可接受秒級延遲。
- **內容曾落地後又被 revert**：`git cherry` 仍會判 merged 而回收——但此時內容確實曾完整進入 default branch 歷史，revert 是後續獨立決策，分支本身已無獨有內容，符合「不刪 unmerged content」的安全定義。
- **umbrella help 與實際子命令漂移（R-16）**：`_WORK_HELP` 更新納入 plan 任務，並在 `tests/test_cli_help_alignment.py` 加斷言鎖住。
- **`--apply` 與判定之間的 TOCTOU**：apply 對每個 `reclaim` 項目在執行前重跑一次驗證鏈，狀態已變時轉 `keep`＋`apply-error`。
