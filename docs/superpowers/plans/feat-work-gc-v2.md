---
status: accepted
work_item: feat-work-gc-v2
---

# feat-work-gc Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_work_gc.py`（以 tmp git repo fixture 建構，全程不碰真 repo、不需網路）：
  - `test_squash_merged_branch_classified_reclaim`：分支內容以單一新 commit（squash）落地 default branch、原分支 commit 不在其歷史時，該分支判 `reclaim`（reason `merged-content`），不被誤判 unmerged。
  - `test_ancestor_merged_branch_classified_reclaim`：tip 為 default branch ancestor 的分支判 `reclaim`（reason `merged-ancestor`）。
  - `test_unmerged_branch_never_in_apply_list`：含未落地 commit 的分支判 `keep`（reason `unmerged-content`），且 `--apply` 執行後分支仍存在。
  - `test_dirty_worktree_kept`：有未 commit 變更（含 untracked）的 worktree 判 `keep`（reason `dirty-worktree`），`--apply` 不移除。
  - `test_closed_unmerged_pr_branch_kept_with_annotation`：注入的 PR 狀態 provider 回報 closed-unmerged 時，分支判 `keep` 且 reason 為 `pr-closed-unmerged`；provider 拋錯或缺席時退化為 `unmerged-content`，同樣 `keep`。
  - `test_default_dry_run_mutates_nothing`：未帶 `--apply` 時，所有 worktree、分支與 `jobs.json` 內容皆不變。
  - `test_git_error_fails_safe`：注入會失敗的 git runner 時，該 artifact 判 `keep`（reason `verification-error`），且不中斷其餘項目的判定。

### 2. 偵測與分類核心

- [ ] 新增 `paulsha_cortex/coordinator/gc.py`：掃描 worktree pool（用 `paulsha_cortex/config/paths.py` 的 `worktree_root_for()`，`PSC_WORKTREE_ROOT` 可覆寫）與 local branch，產出逐項分類（`reclaim`／`keep`＋reason code）。git 執行以可注入 runner 抽象，比照 `paulsha_cortex/coordinator/verification.py` 的 `_run_git` 慣例。
- [ ] merged 驗證鏈：`git merge-base --is-ancestor <tip> <default>` → 不成立再 `git cherry <default> <branch>` 檢查無 `+` 行 → 皆不成立判 unmerged。default branch 依 `origin/HEAD` 解析、缺席退回 `main`；不主動 fetch。禁止使用 `git branch -d`／`--merged` 作判定。
- [ ] 保護清單：default branch、目前 checked-out branch、掛在 `keep` worktree 上的 branch 一律 `keep`（reason `protected`）。
- 驗收：第 1 節分類相關測試轉綠；分類函式對注入 runner 可完全離線測試。

### 3. `--apply` 執行與 fail-safe

- [ ] `paulsha_cortex/coordinator/gc.py`：`--apply` 只處理 `reclaim` 清單；clean worktree 用 `git worktree remove`，merged branch 於刪除前重跑一次驗證鏈再 `git branch -D`；狀態已變（TOCTOU）或單項失敗記 `keep`（reason `apply-error`）並續行。
- [ ] gc 模組不 import 任何 registry 寫入 API，不開檔寫 `jobs.json`。
- 驗收：`test_unmerged_branch_never_in_apply_list`、`test_dirty_worktree_kept`、`test_default_dry_run_mutates_nothing` 轉綠。

### 4. CLI 接線與 help 同步

- [ ] `paulsha_cortex/cli.py`：在 `work` 分支比照 `work show` 攔截 `work gc`，路由至 `paulsha_cortex/coordinator/gc.py` 的 `main()`（不透傳 coordinator mutation 路徑、不動 control queue `WORK_ACTIONS`）；`_WORK_HELP` 加入 `gc` 一行說明。
- [ ] gc argparse：`cortex work gc [--repo-root PATH] [--apply] [--json]`，`--repo-root` 預設用 `paulsha_cortex/config/paths.py` 的 `repo_root()`。
- [ ] `tests/test_cli_help_alignment.py`：加斷言確認 `_WORK_HELP` 列出 `gc`（R-16 CLI help 同步）。
- 驗收：`cortex work gc --help` 可執行；help 對齊測試轉綠。

### 5. 報告輸出

- [ ] 文字報告：每 artifact 一行（種類／路徑或分支名／`reclaim`|`keep`／reason）；`--json` 輸出 `cortex-work-gc/v1` schema（命名比照 `cortex-porcelain/run/v1`），含 `schema`、`repo_root`、`applied`、逐項 `artifacts[]`。
- [ ] closed-unmerged PR 註記：PR 狀態 provider 介面可注入，預設實作走 `gh`（typed argv、失敗吞回退化），僅升級 keep 理由、不參與回收判定。
- 驗收：`test_closed_unmerged_pr_branch_kept_with_annotation` 轉綠；`--json` 輸出可被 `json.loads` 解析且含上述欄位。

### 6. 交付要件

- [ ] `changelog.d/feat-work-gc.md` fragment（R-09 硬性 gate，須 commit 才進 diff）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry。
- [ ] CLI help 同步（R-16）：`_WORK_HELP` 與 gc argparse help 覆蓋新命令。
- [ ] 帶 PR 上下文執行 policy_check（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`），確認 fail: 0。
- [ ] `python3 -m pytest tests/ -q` 全綠。
