# 已載入 runtime 身分證據（#841）

## 證據來源

長駐 Manager 在取得既有 `manager.lock` 後寫 startup receipt；Monitor 僅在長駐模式啟動時寫入，`--once` 不建立 service receipt。每次 Manager／Monitor restart 建立新 receipt；前次檔案不覆寫。receipt 以 `process_id`、PID、instance、instance root digest、process start time、artifact 身分與有效 config revision 綁定實際啟動程序。

receipt 位於各自解析後的 state root 下 `runtime-attestations/`，目錄 mode 為 `0700`，單檔為 `0600`。讀寫逐層拒絕 symlink；新增目錄不改寫既有 status、registry 或 Trust Root receipt schema，因此舊版只讀既有檔名的程式不會被新資料干擾。新版 reader 忽略未知的 additive 欄位；未知 schema、壞檔、錯誤 instance root 或斷裂的 reload chain 都回 unknown。

Trust Root 部分只讀既有 `PSC_TRUST_ROOT_INSTALL_RECEIPT`，摘要其 receipt digest、plan digest、activation journal revision、verification evidence digest 與 rollback revision。receipt 無法驗證或未設定時標 unknown；此摘要不建立第二個 installer，也不替代既有 Trust Root preflight。

## 查詢與判讀

```bash
cortex service status --instance cortex --json
cortex doctor --instance cortex --json
```

`loaded_runtime` 把三種觀測分開：

- `operator_cli`：目前這次 CLI 的 package/source digest、instance、安全環境 revision、PID 與觀測時間。從 checkout 執行時會標 `source-override`，不會偽裝成已安裝 wheel。
- `service_declaration`：磁碟 unit 的狀態、PID、有效 ExecStart path digest、unit file digest，以及可由 unit 宣告定位到的 Manager／Monitor 套件 artifact；不輸出 unit 內容或環境值。

probe 以 `systemctl --user show` 取得 systemd 已合併的 `ExecStart`、`Environment`、`EnvironmentFiles`、`DropInPaths`、`FragmentPath` 與 `WorkingDirectory`；不自行重建 systemd 的 drop-in 搜尋與合併規則。`disk_unit_sha256` 讀取 systemd 回報的 fragment 與完整 drop-in 清單，以檔名和內容摘要計算。任何列出的檔案不可安全讀取、是 symlink、超過大小上限，或屬性集合不完整時，artifact 維持 `unknown`，不退回主 unit。systemctl 不可用且無法證明檔案退路涵蓋完整 unit 搜尋路徑時也維持 `unknown`。

最後必須恰有一個有效 `ExecStart` 才能辨識 artifact。Python module 的 `PYTHONPATH` 依有效來源定位套件：`/usr/bin/env KEY=VALUE` 前綴優先於 `Environment=`；`EnvironmentFiles` 會在安全讀取並解析後納入比對，無法解析、讀取失敗或與其他來源的 `PYTHONPATH` 衝突時一律 unknown。沒有 `PYTHONPATH` 時以有效 `WorkingDirectory` 檢查是否有遮蔽套件，再依 Python executable 的 prefix 定位。環境值與檔案內容只在記憶體內解析，不會輸出或寫入摘要。

Manager 的 `service-manager.sh` 與直接宣告 `<python> -m paulsha_cortex.monitor` 維持可辨識。service status 與 doctor 共用同一份安全投影，避免再次從主 unit 推算。輸出僅包含摘要與 artifact 欄位，不包含環境值或 EnvironmentFile 內容。
- `manager`／`monitor`：各 service 啟動 receipt、由 service declaration 定位的安裝 artifact、effective config 比對、目前 unit PID 比對，以及前次 process start 與 Trust Root receipt 摘要。

`match` 要求 receipt 存在、目前 unit PID 與 receipt 相同，且 service 宣告 artifact/config 可比較。套件或 Monitor 有效配置與 receipt 不同時為 `drift`。Manager invocation 參數不能由目前 service declaration 確認時，該配置維持 unknown。checkout source override、缺漏／損壞 receipt、未知 schema、instance-root mismatch、缺少目前 PID 或未知 revision 都不會升成 match。status 命令本身只是唯讀 consumer，不會用磁碟 `VERSION` 重寫 loaded identity。

`transition_safe` 固定為 `false`。只有已知 in-flight 數量會呈現具體 disposition：非零為 `blocked-in-flight`，零為 `clear-not-authorized`；未知數量為 `unknown-in-flight-state`。這些診斷不放寬 installer 的 process／durable job preflight，也不清理 job。

目前 Manager／Monitor 沒有 hot config reload 入口。配置檔變更但程序未重新載入時，status 會報 drift；正式重啟會建立新 startup receipt。`record_config_reload()` 僅供已存在的 reload owner 在成功切換 effective in-memory config 後追加 receipt，拒絕 stale parent，保留 initial revision 與舊 receipt。

## 驗收分帳

本地測試以隔離 prefix 和 checkout 外 CLI 驗證 installed-wheel 證據；它們不代表共享服務已安裝新 wheel 或完成 live 驗收。operator 需在有權控管的 live target 上依序：

1. 比對 Manager／Monitor 的實際 MainPID 與 `loaded_runtime.*.loaded.pid`、process start time、artifact digest、config revision，以及 `service_declaration`。
2. 先用既有 Trust Root process 與 durable-job in-flight facts 確認可否進入維護；非零不得更新或回滾，unknown 也不得當作零。
3. 由正式 Trust Root 流程完成隔離安裝與 activation/inventory/verify，核對 startup receipt；在受控短生命週期程序驗證重啟與 rollback，確認 prior/current receipt 都保留且舊檔 hash 不變。
4. 只在再次確認 owner 與 active-job gate 後，升 pin／重啟受控 service；重新讀 status/doctor，確認 PID 與新 loaded identity 對齊。source tests、merge 或磁碟 unit 更新都不代表此步完成。
