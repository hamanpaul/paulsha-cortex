### Fixed

- **#716 gate job 起不來（226/NAMESPACE）**：gate 模板 unit 的 `ReadWritePaths=<gate-worktree>/%i` 要求那一格在 namespace 設定前存在，但 pool 屬於 gate、Manager 進不去，原設計由 gate 自己在 unit 內複製快照時才建立，因此每個 gate job 都以 `Failed to set up mount namespacing` 起不來、Manager 回報 `gate-spool-empty`。gate unit 改以 `StateDirectory=`（`StateDirectoryMode=0700`）由 systemd 以 `User=` 身分預建那一格；gate 快照改為就地清空再複製、不移除這一格本身（它是 bind mount 點）。gate pool 不在 `/var/lib` 底下時拒絕產生 gate unit（#716）。
