# afunix-sunpath-hermetic

- **`#608` AF_UNIX socket 路徑不再吃 `TMPDIR` 長度，長暫存根無法再偽造出一筆 gate
  失敗**——Linux 的 `struct sockaddr_un.sun_path` 只有 108 bytes（含結尾 NUL，可用
  107）。pytest 的 `tmp_path` 與 `tempfile.mkdtemp()` 都掛在 `TMPDIR` 底下，socket
  路徑的長度因此是**環境給的**；CI／sandbox 給一個長暫存根就整批撞牆。實測
  （0817，`origin/main`）：

  | `len(TMPDIR)` | 全套 pytest |
  | --- | --- |
  | 4（`/tmp`） | 全綠 |
  | 47 | **4 failed**（`test_monitor_work_api` 三測 ＋ `test_doctor` 一測） |
  | 66 | **18 failed**（再加 `test_stage9_project_monitor_service` 十四測） |
  | 91 | 全綠，但 AF_UNIX 家族 **36 測靜默 skip** |

  這是 P1，理由與 #565／#586 同族：Manager gate ledger 對 candidate 重跑全套 pytest
  是採信的硬 gate（#540），上表的 `failed` 進 ledger 之後與「這次交付真的沒過」
  **長得一模一樣**，合格 candidate 會被 `GateContradictionError` 拒掉。最後一列更
  陰險——#586 的 `af_unix_bind_available()` 探針自己也建在 `TMPDIR` 下，探針路徑先
  超限，`bind()` 失敗被判成「sandbox 禁止 bind」，整個 AF_UNIX 家族帶著**不成立的
  理由**靜默 skip：套件綠、ledger 綠、覆蓋卻是空的。

  **測試 hermetic 化（主修法，比照 #565 的 `tests/git_fixtures.py`）**：新增
  `tests/socket_fixtures.py`——`short_socket_root()` 選一個短且與 `TMPDIR` 無關的
  固定根（`/tmp` → `/var/tmp` → `/run/user/<uid>`，各自加 per-uid 的 `0700` 容器
  目錄），`make_short_socket_dir()`／`short_socket_dir()` 在其下開短亂數名目錄，
  `assert_socket_path_fits()` 讓 fixture 自己壞掉時炸在「fixture 壞了」而不是某個
  看起來像產品缺陷的 `bind()` 失敗上。conftest 另出 `socket_dir` fixture。五個測試
  檔改用它：`sandbox_support`（#586 探針）、`test_sandbox_afunix_skip_586`（ground
  truth）、`test_monitor_work_api`、`test_doctor`、
  `test_stage9_project_monitor_service`。**只有要 bind／connect 的路徑搬家**；
  工作區、設定檔、快照沒有長度上限，照舊留在 `tmp_path`。

  **production fail-closed（次修法）**：新增
  `paulsha_cortex/monitor/socket_path.py` 收攏 107／108 這兩個常數與判定
  （`socket_path_length()` 以 **byte** 計，非字元——中文目錄名下用字元數會低估）。
  `MonitorServer.serve_forever()`、`MonitorSocketClient.request()` 在碰檔案系統前
  先驗，超限時 raise `SocketPathTooLongError`（附實際 byte 數、上限、超出量、路徑
  與該調哪個環境旋鈕），取代一句沒有出處的 `OSError: AF_UNIX path too long`；該
  例外刻意繼承 `ValueError` 而**非** `OSError`，否則會被呼叫端既有的「transport
  出事」處理吸收，重新變回無法區分的症狀。`MonitorServer.startup_error` 讓 threaded
  呼叫端不再只看到 `wait_until_ready()` 的空 `False`。`cortex doctor` 的
  `monitor-socket` probe 在 live probe **之前**先量長度，直接說出「超過 sun_path
  上限」而不是「socket is not listening」。

  **不改用抽象命名空間**（`\0` 前綴）：抽象 socket 沒有權限位，會把現行
  `chmod 0o600` 的 monitor socket 對整個 network namespace 打開；`server` 的
  stale-socket 回收、`live monitor already listening` 偵測與 teardown identity 比對
  也全建立在「socket 是一個檔案」上；而且它**仍然**吃 108 bytes 上限，只是換個地方
  踩。

  新增 `tests/test_afunix_sun_path_608.py`（16 測）四層釘死：常數與 byte 計算契約、
  fixture 對敵意 `TMPDIR` 免疫、production 超限時 fail closed 且說得出原因、以及
  **核心不變式**——以敵意長 `TMPDIR` 走 production 的 `gate_ledger.write_gate_ledger()`
  重跑那四個原本必紅的測試，ledger 必須記成 `passed`；並附**負控制**：同一條 ledger
  路徑上真正失敗的 gate 仍然記成 `failed`，證明「環境失敗與交付失敗可區分」不是靠
  「什麼都記成 passed」作弊得來的。
