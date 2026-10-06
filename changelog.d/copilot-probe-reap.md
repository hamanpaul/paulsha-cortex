### Fixed

- **#1297 copilot 健康檢查程序逾時後連同子程序一起回收**：`paulsha_cortex/coordinator/executor_auth.py` 與 `paulsha_cortex/porcelain/bootstrap.py` 的 probe 改共用 `run_probe` runner；一律在獨立 session／process group 執行（`start_new_session=True`），逾時或發生例外時對整個 process group 先送 SIGTERM、短暫等待後送 SIGKILL 並等待回收，防止 node wrapper 終止後 native `copilot-linux-x64` 子程序殘留（#1297）。
