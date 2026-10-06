---
status: accepted
work_item: codex-readonly-sandbox
---

# codex 唯讀卡在 sandbox 啟動時 panic

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1304`。
- 現況：codex CLI 0.159.3 在 cortex 的唯讀卡（worktree-isolation）回報 `filesystem-restricted execution requires bubblewrap to isolate app-server sockets`（exit 101），連 `git rev-parse HEAD` 都執行不了。主機的 `/usr/bin/bwrap` 可以正常執行；同版 codex 跑 workspace-write 卡正常。
- 唯讀卡仍然不得寫入 worktree。
- 相容性 probe 的機制與 `copilot-headless-compat`（#1303）共用，若兩張同時進行，各自實作自己 executor 的部分即可。

## Tasks

- [ ] **T1 重現與定位**：用 launcher 為唯讀卡組出的 codex argv 與環境，在本機重現 panic，找出原因（sandbox 模式旗標、app-server socket 路徑、環境變數、巢狀 sandbox 等），結果寫進 PR 描述。
- [x] **T2 修正 launcher**：讓 codex 唯讀卡能正常執行唯讀檢查。若目前版本的唯讀 sandbox 在此環境確實不可用，改用等價且安全的方式（例如 workspace-write，但由 cortex 驗證 worktree 沒有被寫入），不讓卡片失敗。
- [x] **T3 相容性 probe**：派工前驗證 codex 在唯讀與 workspace-write 兩種模式都能執行基本檢查；不符時給出明確原因。
- [x] **T4 測試與文件**：單元測試涵蓋 argv、fallback 與 probe。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
