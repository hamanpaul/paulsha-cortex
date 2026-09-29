### Fixed

- **#716 canary job 診斷改以 journal glob 查詢**：模板 instance 結束後就從 unit 清單卸載，run 36634548058 的 `list-units` 因此為空、沒印出任何 journal；改以 `journalctl -u <glob>` 直接查 gate／builder／reviewer 三類 job unit，每段各自截尾（#716）。
