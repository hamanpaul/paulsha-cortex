### Fixed

- **#1270 `cortex upgrade` 延後的加固**（#1263 後續；`--recover` 對已驗證 receipt 的語意仍待 owner 裁決，未動）：
  - 憑證落點（`validate_credentials`、`credentials inherit` 共用的 `_observe_credential`，以及 `rollback_credentials`）改以 `O_NONBLOCK` 開檔、讀取前先以 `fstat` 確認是一般檔案，雜湊最多讀 16 MiB（`_CREDENTIAL_MAX_BYTES`）：落點被換成 FIFO 時 root installer 不會在持有 transaction lock 時卡住，非一般檔案一律不讀、回報 `unavailable`／drift。
  - `backend._run` 新增選用的 `stdin`、`start_new_session`、`cwd`、`timeout`（預設維持原行為）。非 root 的 plan 子程序改為 `stdin=DEVNULL`、獨立 session、cwd 為 attempt 的 plan 目錄，不再繼承 root 的 stdin／controlling tty／cwd；`upgrade._READ_FLAGS` 加 `O_NONBLOCK`，plan 帳號把 `install-plan.json` 換成 FIFO 時直接拒絕；durable plan 既有檔案也先確認一般檔案才讀。
  - candidate 子程序（apply／credentials inherit／activate／verify／rollback）改在獨立 session、`stdin=DEVNULL` 執行：終端機的 INT／HUP 只送到 coordinator。maintenance window 內第一個 INT／TERM／HUP 會等正在執行的那一步結束後才觸發 rollback，不再由 `subprocess.run` 對 mutation 中的 root 子程序送 SIGKILL；同一步執行中再收到第二個訊號則立即中止（與先前相同，依 journal crash recovery）。
  - `cortex service status` 探測加 60 秒 timeout（`_STATUS_TIMEOUT_SECONDS`），逾時視為 `service_status=unavailable (TimeoutExpired …)`。
  - release ingress：metadata 與 asset 兩條通道各自的 urllib redirect handler 只跟隨 HTTPS、預設 port、無 userinfo、且 host 在允許清單內的 redirect（metadata：`api.github.com`；asset：`github.com`、`release-assets.githubusercontent.com`（2026-10-06 實測 GitHub 的 302 目標）、`objects.githubusercontent.com`），其餘在連線前拒絕；`get_json` 與 asset 下載都再核對最終 URL；`assert_private_chain` 拒絕相對路徑與 `..`；install-input archive 加成員數（10,000）與解壓總量（8 GiB）上限（v0.1.13 實際 18 個成員、約 0.9 GB）。
- **#1270 `cortex upgrade` 的診斷與文件**：
  - 子程序沒有輸出時，錯誤訊息不再重複結束碼（原本 `exit 3: exit 3`、`verify did not PASS (exit 1): exit 1`、`rollback exited 1: exit 1`），改為 `exit N with no output`。
  - `report["result"] = "upgraded"` 改在 lease 釋放後、`_signals_raise` 還原 handler 之前寫入；之後才落地的訊號不會再把已完成的升級標成 `halted`，`perform_upgrade` 對已記錄為 `upgraded` 的結果維持 exit 0。
  - RC `upgrade_diagnostics` 比對 `--json` 輸出與 durable report 的 `started_at`：完整升級在發布 report 之前就被拒（例如 preflight）時，標示 durable report「possibly stale」（那是 activate 前失敗演練留下的同版本 report），verify evidence 也只取這次的輸出。
  - `trust-root-transactional-install.md`：§6「手動恢復 `cortex upgrade` 中斷的升級」對應表註明 SIGKILL 後 durable report 停在 `in-progress`、`receipt` 為 null 時只依 snapshot 的 `receipt_path`；「一般升級」補上中斷訊號等該步結束才 rollback、再送一次才立即中止，以及 `attempt-*` 目錄不會自動清理、何時可以安全刪除（工具不自動刪：plan 與 receipt 記錄其中的路徑、halted 的升級還要用它恢復）。
  - `trust-root-legacy-adoption.md` §8 改為指向 `cortex upgrade`，不再描述手動 `--prior-receipt` 升級。
