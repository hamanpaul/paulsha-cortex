---
status: accepted
work_item: copilot-headless-compat
---

# copilot builder 在 headless 模式的權限與自述可靠性

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1303`。
- 現況：`coordinator/launcher.py` 的 copilot argv builder 只在 `commit_required` 時加 `--allow-all-tools --add-dir <worktree>`，`allow_unsafe` 時才加 `--allow-all`。copilot CLI 1.0.92 在 headless 模式下，即使有 `--allow-all-tools`，絕對路徑的指令（如 `/usr/bin/git`）仍會被拒；非 commit 卡則完全沒有 shell 權限。
- copilot CLI 會自動更新、語意可能改變，修法要能偵測版本差異，不能只寫死目前的旗標組合。
- builder 不得因此取得寫入 worktree 以外 operator 檔案的權限（`worktree-containment-authority`／#1305 的範圍）。

## Tasks

- [ ] **T1 實測並記錄**：在本機以目前的 copilot 版本實測 `--allow-all-tools`、`--allow-all-paths`、`--allow-tool`、`--add-dir` 等組合，在 headless 模式下對「worktree 內的 shell、絕對路徑系統工具、linked git dir、worktree 外寫入」各自的行為。結果寫進 PR 描述與 `docs/` 的 executor 相容性說明。
- [ ] **T2 argv 修正**：依 T1 結果調整 copilot argv，讓 tdd-red／subagent-build 能在 worktree 與 linked git dir 內正常執行 git、pytest（含絕對路徑的系統工具）；唯讀卡取得足以完成檢查的唯讀 shell 權限；worktree 外的寫入仍被拒。
- [ ] **T3 相容性 probe**：新增 copilot 的派工前相容性檢查，正反兩個方向都要驗：應允許的指令能執行、應禁止的寫入被拒。語意不符時，派工前就失敗並給出明確原因。結果依 CLI 版本快取。
- [ ] **T4 自述可驗證**：card 契約要求 builder 回報 passed 時，附上實際執行的測試指令與結果摘要；Manager 收割時可以比對，缺漏時視為未驗證。
- [ ] **T5 測試與文件**：argv 與 probe 的單元測試。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
