# 已載入 runtime 身分證據（#841）

## 證據來源

長駐 Manager 在取得既有 `manager.lock` 後寫 startup receipt；Monitor 僅在長駐模式啟動時寫入，`--once` 不建立 service receipt。每次 Manager／Monitor restart 建立新 receipt；前次檔案不覆寫。receipt 以 `process_id`、PID、instance、instance root digest、process start time、artifact 身分與有效 config revision 綁定實際啟動程序。

receipt 位於各自解析後的 state root 下 `runtime-attestations/`，目錄 mode 為 `0700`，單檔為 `0600`。讀寫逐層拒絕 symlink；新增目錄不改寫既有 status、registry 或 Trust Root receipt schema，因此舊版只讀既有檔名的程式不會被新資料干擾。新版 reader 忽略未知的 additive 欄位；未知 schema、壞檔、錯誤 instance root 或斷裂的 reload chain 都回 unknown。

Trust Root 部分只讀既有 `PSC_TRUST_ROOT_INSTALL_RECEIPT`，摘要其 receipt digest、plan digest、activation journal revision、verification evidence digest 與 rollback revision。receipt 無法驗證或未設定時標 unknown；此摘要不建立第二個 installer，也不替代既有 Trust Root preflight。

摘要的 `status` 以 installer 寫進 install receipt 的 `state` 為準，不自行由欄位推論：

| install receipt `state` | 摘要 `status` | `reason` |
|---|---|---|
| `applied`，且 `qualified`、verification evidence `pass`、activation journal 全部 completed | `verified` | — |
| `rolled-back`（rollback 完成） | `rolled-back` | — |
| `rollback-blocked`（retained drift／unknown durable state，例如 Manager 事後寫入的 `jobs.json`） | `unknown` | `install-rollback-blocked` |
| `rolling-back`（rollback 中斷，`qualified` 尚未清掉） | `unknown` | `install-rollback-incomplete` |
| 其他（`planned`、`applying`、未驗證） | `unknown` | — |

rollback 只撤 receipt 自己建立或改動的東西：durable job registry 與 loaded receipt 不會被清除；本 receipt 建立的目錄裡若已有這類資料，目錄保留並列入 `retained_unknown`，receipt 停在 `rollback-blocked`。

System scope 的 unit 不把 root-only install receipt 暴露給 Manager／Monitor。`cortex service status --system` 讀取 system manager 的有效 unit 屬性與兩個 state root 下的 loaded receipt；要得到 Trust Root 判定，操作者必須以可讀取 `/var/lib/cortex-install-receipts/` 的身分執行（一般部署使用 `sudo`）。CLI 只載入可由 `InstallReceipt.load()` 驗證、且 plan 的 state root 與目前 Manager root 相符的 receipt；verified 結果還必須以 receipt 的 wheel hash 對上目前 system unit 選取的 installed wheel。服務帳號仍不需要 receipt 權限，也不要求在 activation/verify 前啟動的服務重啟。

## 查詢與判讀

```bash
cortex service status --instance cortex --json
cortex service status --system --instance cortex --json
cortex doctor --instance cortex --json
```

預設 `service status` 與 doctor 維持 user scope。`--system` 改查 `systemctl show` 的 system units，取得有效 `FragmentPath`、`DropInPaths`、`EnvironmentFiles`、`ExecStart` 與 `MainPID`；辨識 permgen 產生的 `<venv>/bin/cortex service run` 與 `<venv>/bin/cortex monitor`，並依對應 venv 的 `.cortex-wheel.sha256` 記錄 installed wheel 身分。unit 宣告不完整、receipt 不可讀或無法驗證時，狀態維持 unknown。

`loaded_runtime` 把三種觀測分開：

- `operator_cli`：目前這次 CLI 的 package/source digest、instance、安全環境 revision、PID 與觀測時間。從 checkout 執行時會標 `source-override`，不會偽裝成已安裝 wheel。
- `service_declaration`：磁碟 unit 的狀態、PID、有效 ExecStart path digest、unit file digest，可由 unit 宣告定位到的 Manager／Monitor 套件 artifact，以及該 service 目前有效環境的來源標示 `environment_source` 與其摘要 `environment_digest`（對已剔除機敏欄位、僅 `PSC_*`／`PAULSHACLAW_*` 的安全投影取 canonical SHA-256）；不輸出 unit 內容或環境原值——真正的值只在程序內部（`service_environment_overlay`）用於重建 root／config，絕不序列化進這份輸出。

probe 以 `systemctl --user show` 取得 systemd 已合併的 `ExecStart`、`Environment`、`EnvironmentFiles`、`DropInPaths`、`FragmentPath` 與 `WorkingDirectory`；不自行重建 systemd 的 drop-in 搜尋與合併規則。`disk_unit_sha256` 讀取 systemd 回報的 fragment 與完整 drop-in 清單，以檔名和內容摘要計算。任何列出的檔案不可安全讀取、是 symlink、超過大小上限，或屬性集合不完整時，artifact 維持 `unknown`，不退回主 unit。systemctl 不可用且無法證明檔案退路涵蓋完整 unit 搜尋路徑時也維持 `unknown`。

最後必須恰有一個有效 `ExecStart` 才能辨識 artifact。Python module 的 `PYTHONPATH` 依有效來源定位套件：`/usr/bin/env KEY=VALUE` 前綴優先於 `Environment=`；`EnvironmentFiles` 會在安全讀取並解析後納入比對，無法解析、讀取失敗或與其他來源的 `PYTHONPATH` 衝突時一律 unknown。`PYTHONPATH` 本身可能有多段（如 `/opt/helpers:/srv/pin`）：判定依 Python 實際匯入順序逐段搜尋，取第一個真正提供 `paulsha_cortex` 套件的段落，不是只看第一段；命中的段落若是 symlink 或非目錄視為不安全，直接回 unknown，不再往後找。沒有 `PYTHONPATH` 時以有效 `WorkingDirectory` 檢查是否有遮蔽套件，再依 Python executable 的 prefix 定位。環境值與檔案內容只在記憶體內解析，不會輸出或寫入摘要。

Manager 的 `service-manager.sh` 與直接宣告 `<python> -m paulsha_cortex.monitor` 維持可辨識。service status 與 doctor 共用同一份安全投影（`service_declaration_projection`），避免再次從主 unit 推算。輸出僅包含摘要與 artifact 欄位，不包含環境值或 EnvironmentFile 內容。

`PSC_COORDINATOR_ROOT`／`PSC_MONITOR_STATE_ROOT` 與 config revision 的判定同樣以這份有效宣告為準，不再固定讀 `~/.agents/core/runtime/*.env`：`environment_source` 為 `systemd-effective` 時，採用 probe 取得的有效環境（優先序 env 前綴 ＞ `Environment=` ＞ `EnvironmentFiles=`；任一來源無法安全解析時該 service 標 `unknown`，不靜默退回舊值）；只有 `environment_source` 為 `unavailable`（systemd 本身不可用的 direct 模式）才退回既有的 fallback 讀檔，並在輸出以 `unavailable`／`direct-fallback` 標示來源。drop-in 改了 root 或 config 時，`cortex service status` 與 `cortex doctor` 都會照新值判定，不會因為讀到舊 fallback 檔而對錯 state root 或誤報 config drift／match。manager／monitor 的 root／config 一律逐 service 各自解析，`cortex doctor` 不會把兩邊合併成同一份環境後共用：manager 已知有效時就用 manager 自己的值算 `PSC_COORDINATOR_ROOT`，monitor 已知有效時就用 monitor 自己的值算 `PSC_MONITOR_STATE_ROOT`，任一邊退回 direct-fallback 時也只讀那一個 service 自己的 `EnvironmentFile=`，不會拿另一邊的值頂替。

manager／monitor 各自獨立判定 `environment_source`，不互相牽連：只要其中一邊確認是 `systemd-effective`，`cortex doctor` 就會採用那一邊已知的有效值，不會因為另一邊當下無法判定（`unavailable`）就連可信的一邊也一起退回粗略的主 unit 檔 fallback；只有兩邊都 `systemd-effective` 時才要求兩者完全相同，不同即視為設定不一致而失敗。但只要任一邊判定為 `unknown`（宣告存在卻無法安全解析，例如 `Environment=`／`EnvironmentFiles=` 宣告了互相衝突的 `PYTHONPATH`），即使另一邊已經確認是 `systemd-effective`，整體判定仍會失敗（`cortex service status` 的 `service-paths` 對應改為 `fail`，不會蓋章 pass）——`unknown` 是真資料完整性問題，不是 `unavailable` 那種可以正常退回檔案讀取的預期過渡態，不能因為另一邊看起來正常就被略過。`cortex doctor` 與 `cortex service status` 對同一個 service 呼叫的是同一個判定函式（`runtime_attestation.declared_service_environment`，其分支規則與底層的 `_environment_source_and_overlay` 共用），因此對同一個 live service 的結論保證一致；兩者唯一的差異只在 `unavailable` 時各自提供的 direct-fallback 讀法——`cortex service status` 讀真實 `os.environ` 下既有的 `~/.agents/core/runtime/*.env`，`cortex doctor` 讀注入的 `home`／環境以維持可重現的 hermetic 測試，不影響判定規則本身。這條路徑的有效環境只由 unit 宣告本身＋systemd 對 user service 的既知預設（例如 `HOME`）組成，不會以呼叫者（操作 CLI）當時的殼層環境為底──操作者殼層自己的環境只用於描述 `operator_cli` 這次呼叫本身，不會混進任何 service 的判定。
- `manager`／`monitor`：各 service 啟動 receipt、由 service declaration 定位的安裝 artifact、effective config 比對、目前 unit PID 比對，以及前次 process start 與 Trust Root receipt 摘要。

已知 unit 目前的 MainPID 時，`loaded` 取「屬於該 PID 的最新 startup receipt」；沒有任何 receipt 屬於它才退回牆鐘最新者（此時 process 比對必然不符）。這讓 NTP step 或 VM 時間同步造成的牆鐘回跳不會把已結束程序的舊 receipt 當成目前載入的身分。`match` 要求 receipt 存在、目前 unit PID 與 receipt 相同，且 service 宣告 artifact/config 可比較。套件或 Monitor 有效配置與 receipt 不同時為 `drift`。Manager invocation 參數不能由目前 service declaration 確認時，該配置維持 unknown。checkout source override、缺漏／損壞 receipt、未知 schema、instance-root mismatch、缺少目前 PID 或未知 revision 都不會升成 match。status 命令本身只是唯讀 consumer，不會用磁碟 `VERSION` 重寫 loaded identity。

Installed wheel 的 package tree digest 保留在 `sha256`；有 installer wheel marker 時另輸出 `wheel_sha256`，並以已驗證 install receipt 的 `repo_identity.commit` 輸出 `candidate_commit`。wheel 安裝的 Python distribution 通常沒有 VCS `direct_url.json`，所以 `source_revision` 仍為 `unknown`；candidate commit 是 installer/bundle 聲明的來源 revision，與 wheel hash 一起綁定，不會偽稱成 distribution 自己提供的 `source_revision`。receipt 為 rolled back 時 status 保留 `rolled-back` 及 rollback revision，不把被回滾候選 commit 冒充目前載入來源。

`match` 要求 receipt 存在、目前 unit PID 與 receipt 相同，且 service 宣告 artifact/config 可比較。套件或 Monitor 有效配置與 receipt 不同時為 `drift`。Manager invocation 參數不能由目前 service declaration 確認時，該配置維持 unknown。checkout source override、缺漏／損壞 receipt、未知 schema、instance-root mismatch、缺少目前 PID 或未知 revision 都不會升成 match。status 命令本身只是唯讀 consumer，不會用磁碟 `VERSION` 重寫 loaded identity。

`transition_safe` 固定為 `false`。只有已知 in-flight 數量會呈現具體 disposition：非零為 `blocked-in-flight`，零為 `clear-not-authorized`；未知數量為 `unknown-in-flight-state`。這些診斷不放寬 installer 的 process／durable job preflight，也不清理 job。

## config reload（#841 AC4 的 reload 條目：N/A）

owner 於 2026-09-29 裁決：AC4「reload 明確區分初始／有效 config」不適用，理由是 Cortex 沒有任何 hot config reload 路徑，也就沒有需要 reload receipt 的程序狀態。

- **Manager**：程序設定＝實際收到的 argv token，加上啟動當下 `PSC_*`／`PAULSHACLAW_*`／`PY` 的環境投影（`manager_configuration_snapshot`，分成 `invocation_revision` 與 `environment_revision`）。兩者在程序生命週期內不變；沒有 SIGHUP handler、沒有 reload request type。
- **Monitor**：程序設定＝啟動時 `load_config()` 的結果（`--config`、`PSC_MONITOR_CONFIG` 或 `project-cortex.yaml`，加上同目錄 `project-hippo.yaml` 與 `PSC_REPO_ROOT`），之後不再重讀。
- 讓設定變更生效的唯一方式是重啟（`cortex service restart` 或 systemd restart）。重啟寫新的 startup receipt，前一次的 receipt 不改寫；重啟前 `service status` 對已改的設定檔報 `config-drift`，不會把「檔案改了」當成「程序已載入」。
- **執行期逐次讀取的設定不是程序設定**：例如 `quota-pools.json`（`PSC_QUOTA_POOLS_CONFIG`）在每次 dispatch／periodic tick 重新讀取（以內容 digest 快取），每筆准入決策 receipt 自己記下 `policy_config_revision`；model identity overlay（`load_model_identities()`）也在每次 request／tick 重新讀取。這類檔案的版本由使用它的決策證據記錄，loaded receipt 不涵蓋它們，改這些檔也不會讓 `service status` 報 drift。
- `record_config_reload()` 在 production 沒有呼叫端，只保留 v1 receipt chain 的 `config-reload` 事件格式，讓 reader 的 chain 驗證（拒絕 stale parent、initial／effective revision 分開）維持可測。`tests/test_runtime_attestation.py` 有 guard 測試釘住「沒有 production 呼叫端、Manager／Monitor 沒有 SIGHUP 入口」；日後真的加入 reload owner 時，該測試會失敗，須一併更新本段與 AC4 分帳。

## 驗收分帳

本地測試與 RC 隔離 qualification 涵蓋（皆不代表共享服務已安裝新 wheel 或完成 live 驗收）：

- `tests/test_loaded_runtime_installed_process_841.py`：checkout `pip install` 進隔離 venv，從 checkout 外、無 `PYTHONPATH` 的乾淨環境啟動**已安裝**的長駐 Manager／Monitor，receipt 由它們自己寫下；同一 prefix 的已安裝 `cortex service status --json`（PATH 上的假 `systemctl` 回報 MainPID 與有效宣告）比對為 `match`。同一組真程序再驗磁碟 artifact／Monitor 設定更新但未重啟時為 drift、receipt 不增不改，受控重啟後新 receipt 對齊新 identity、舊 receipt bytes 不變。從目錄安裝的 wheel 沒有 VCS commit，`source_revision` 維持 `unknown`。
- `tests/test_loaded_runtime_rollback_receipt_841.py`：走正式 `apply`→`activate`→`verify`→`rollback` 產生的真 install receipt，loaded receipt 串接 prior（`verified`）與 rollback 後（`rolled-back`／`install-rollback-blocked`）兩次啟動；durable `jobs.json` 與 installer 讀到的 in-flight 事實在 rollback 前後不變，比對只標 `blocked-in-flight`。
- RC release／deployment-canary qualification：在隔離容器內以 `cortex upgrade` 實跑三次。先用不同 wheel SHA 的同版 synthetic prior 做一次成功暖機，於 operator `umask 077` 下 fresh-create candidate slot；這次升出的 candidate receipt 隨後成為記錄用的 same-artifact qualified prior。第二次演練 activate 前的 credential handoff 失敗——先把這個 candidate prior 已記錄的 builder／codex 憑證落點權限改成 `0640`，升級預期回報 `failed_step=credentials`，並以 `restore_safe=true` 的 rollback 回到同一個 candidate prior；`rollback-loaded-runtime` 必須證明 Manager／Monitor 的 loaded wheel、installed wheel、candidate commit 與 prior receipt ID 全部相符，原始狀態存於 `qualification-output/evidence/rollback-loaded-runtime-status.json`，並再由 `install-semantic-checks.json` 的 `prior_receipt_id` 當獨立 anchor 重驗，情境仍明示為 `same-artifact-qualified-prior-to-candidate-rollback`。第三次把憑證落點權限復原，再以 root 在 builder／codex 憑證檔尾就地附加一個換行，模擬 executor 自行刷新登入檔（inode、owner、mode、nlink 不變，只有 sha256 改變；不讀出內容），然後做一次完整的 same-artifact 升級，並核對新 receipt 的 builder／codex 列記錄改寫後的 sha256、`inherited_from` 等於 prior receipt ID（#1275）；evidence 存於 `qualification-output/evidence/one-command-upgrade.json`（情境 `one-command-upgrade-same-artifact`），同樣由 validator 重驗，且 `install-semantic-checks.json.receipt_id` 必須對上這次最終 upgraded receipt。輸入只有 candidate wheel，故 rollback/full-upgrade evidence 都不宣稱比較了不同歷史版本。

`cortex service status` 預設與 `cortex doctor` 維持 user scope；`service status --system` 可另外探測 Trust Root system units、installed artifact 與 receipt。release／deployment-canary RC qualification 會在隔離 systemd 容器驗 rollback 後的 loaded↔installed↔receipt 一致性，不依賴 9900X 實機。以下步驟仍用於另外驗收受控 live target 上的程序與外部狀態：

```bash
sudo /opt/cortex/venv/bin/cortex service status --system --instance cortex --json
```

在 9900X transactional system install 完成 activation 與 verify 後，成功判準是 JSON 的 `service.loaded_runtime.manager` 與 `.monitor` 均有 `comparison.artifact_status`、`config_status`、`process_status` = `match`，`trust_root.status` = `verified`，並且各自 loaded PID 等於 systemd MainPID。輸出的 wheel hash 須等於 install receipt 的 candidate wheel hash；`candidate_commit` 須等於 receipt/bundle 的 repo commit。配置或 wheel 已變更但程序未 restart 時，對應 config/artifact 應回報 `drift`；rollback 後應回報 `trust_root.status=rolled-back`、rollback revision，且 loaded wheel 只與實際回復的 candidate 對齊。每次狀態擷取保存原始 JSON 作比較證據。

1. 比對 Manager／Monitor 的實際 MainPID 與 `loaded_runtime.*.loaded.pid`、process start time、artifact digest、config revision，以及 `service_declaration`。
2. 先用既有 Trust Root process 與 durable-job in-flight facts 確認可否進入維護；非零不得更新或回滾，unknown 也不得當作零。
3. 由正式 Trust Root 流程完成隔離安裝與 activation/inventory/verify，核對 startup receipt；在受控短生命週期程序驗證重啟與 rollback，確認 prior/current receipt 都保留且舊檔 hash 不變。
4. 只在再次確認 owner 與 active-job gate 後，升 pin／重啟受控 service；重新讀 status/doctor，確認 PID 與新 loaded identity 對齊。source tests、merge 或磁碟 unit 更新都不代表此步完成。
