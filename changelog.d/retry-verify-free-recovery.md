#577：`retry-verify`／`retry-review` 重置前先檢查舊 exited reviewer job 是否仍符合精準 terminal recovery 判準；符合者保留 `exited`，維持免費復原路徑，其餘 job 仍沿用 failed 標記與 replacement dispatch 行為。
