# worktree-containment-authority

- **#1305 規劃依據與 workflow builder 寫入隔離**：brainstorm evidence 凍結完整規劃 artifact manifest（含 primary integration 未改寫的既有檔案），重驗一律採用這份不可變內容並保留舊 run 相容路徑；direct、commit-required builder 透過 bubblewrap 僅寫入自己的 worktree 與必要 linked Git 目錄；job 完成時比對 operator checkout 的 Git status 與 planning authority 雜湊，偵測差異即保存違規事件，並在 `status`／`work show` 顯示 job、變更欄位、狀態雜湊與受影響規劃檔。
