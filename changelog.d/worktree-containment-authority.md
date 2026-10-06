# worktree-containment-authority

- **#1305 規劃依據與 workflow builder 寫入隔離**：重驗規劃依據時使用 hash-pinned brainstorm evidence 中的不可變內容；direct、commit-required builder 透過 bubblewrap 僅寫入自己的 worktree 與必要 linked Git 目錄；job 完成時比對 operator checkout 的 Git status 與 planning authority 雜湊，偵測差異即保存違規事件並將 workflow run 標記為 `needs_human`。
