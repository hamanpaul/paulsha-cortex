# #1304 Codex 唯讀卡 sandbox 啟動相容性

用實際 `codex exec` agent loop 驗證 read-only 與 workspace-write，而不是只測內層 sandbox 命令。當 read-only 模式因 app-server socket 的 bubblewrap 限制無法啟動、workspace-write 模式可用時，唯讀 build card 走 workspace-write 相容路徑；Manager 在派工時保存完整 worktree／Git status snapshot，結案時檢查 snapshot 與 HEAD，拒絕任何持久工作區變更。若兩種模式都無法完成探針，派工診斷說明原因。
