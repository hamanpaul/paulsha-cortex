### Added

- **#1224 rollback 後的 loaded runtime**：RC 對已核可 prior receipt 執行隔離 upgrade rollback，立即比對 Manager／Monitor 的 loaded artifact 與 receipt，並將列舉結果納入 qualification evidence；文件明示同 artifact 情境、證據界線與 RC/live 驗收分工（#1224）

### Fixed

- **#1225 Monitor 不再覆寫 run dir 權限**：Monitor 只在自己建立 run dir 時設 0700；既有目錄只在 group／other 可寫時才收緊，不再清掉 installer 設定的 ACL mask。服務跑過之後的 `--prior-receipt` 升級，不再因 `asset:runtime-run-tree` provenance 不符被拒（#1225）。
