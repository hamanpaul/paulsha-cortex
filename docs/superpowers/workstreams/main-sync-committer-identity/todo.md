---
status: accepted
work_item: main-sync-committer-identity
---

# clean-behind 自動同步的 merge 要明確帶 committer 身分；ship worktree 要繼承實際生效的身分

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1334`，含 issue 留言（三份 evidence、`seams.py` 的身分複製問題、已驗證的暫時解法）。
- 現況：#1311 的自動同步在 ship workspace 用 `_main_sync_isolated_git_env()`（剝掉 `GIT_*`，`GIT_CONFIG_GLOBAL=/dev/null`、`GIT_CONFIG_NOSYSTEM=1`）執行 `git merge --no-edit --no-ff --no-stat <main_head>`。ship workspace 是獨立 clone，建立時 `seams.py` 只複製來源 repo 的 `--local` 身分，而主機的身分設在 global，所以 merge commit 以「Committer identity unknown」失敗，autosync 永遠 `merge-failed`。
- 暫時解法：operator 在當前 candidate 的 ship workspace 設 `git config --local user.name/user.email` 後 resume。但 autosync 一旦成功，candidate 會變，產生新的 ship workspace，下一次又要重設。
- 保留 #1142 的隔離設計：不得讓 merge 讀取 global／system config 的其他內容。

## Tasks

- [ ] **T1 RED**：以真實 git（不 mock merge）建立沒有 global／local 身分的 ship workspace，在隔離環境下執行 autosync，現行實作會以 `merge-failed`（Committer identity unknown）失敗。
- [ ] **T2 明確身分**：autosync 的 merge 以 `-c user.name=… -c user.email=…` 明確帶入身分。身分來源依序為：設定值（例如 `PSC_MAIN_SYNC_GIT_IDENTITY`），其次是在隔離環境之外，從來源 checkout 解析出的實際生效身分（`git config user.name/user.email`，含 global）。取不到身分時 fail closed，reason 為 `main-sync-identity-missing`。
- [ ] **T3 ship worktree 身分**：`seams.py` 建立 ship worktree 時，複製來源 checkout 實際生效的身分（不只看 `--local`），讓後續產生 commit 的步驟有一致的身分。
- [ ] **T4 上限計數**：確認 `merge-failed` 是否計入 autosync 的連續上限（`PSC_MAIN_SYNC_AUTOSYNC_MAX`，預設 3），並寫測試固定行為。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
