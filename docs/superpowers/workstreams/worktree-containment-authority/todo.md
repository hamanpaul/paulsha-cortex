---
status: accepted
work_item: worktree-containment-authority
---

# 規劃依據改讀釘住的快照，builder 不得寫出自己的 worktree

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1305`。
- 現況：規劃依據重驗（planning authority reconciliation）讀的是 operator 主 checkout（`PSC_REPO_ROOT`）的工作目錄。2026-10-06 copilot builder 用絕對路徑改了主 checkout 的 todo，run 因 `artifact hash drift` 停住。直接模式下，builder 以 operator 身分執行，能寫到 worktree 以外的檔案。
- 不改規劃依據的內容規則（todo 仍只允許打勾）；不改 Trust Root 分 UID 模式的既有隔離。

## Tasks

- [ ] **T1 RED**：測試在 run 進行中改寫 operator checkout 的規劃檔，現行會讓重驗失敗。
- [ ] **T2 規劃依據改讀不可變快照**：重驗時使用 claim／define 當下釘住的不可變來源（例如 git object，或 evidence 內的副本），不讀 operator 的工作目錄；舊 run 的相容性要有測試。
- [ ] **T3 寫入範圍限制**：直接模式下，各 executor 的寫入範圍限制在自己的 worktree 與 linked git dir（依各 executor 的機制：允許目錄、sandbox、deny 規則）。寫到範圍外時拒絕並記錄。
- [ ] **T4 事後偵測**：job 結束時檢查 operator checkout 是否被改動（例如 `git status` 與規劃檔雜湊），有改動就記錄成違規事件，並在 `status`／`work show` 顯示。
- [ ] **T5 測試與文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
