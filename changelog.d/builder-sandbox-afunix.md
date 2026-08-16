### Fixed
- **#586（缺陷 A）：builder sandbox 無法 bind AF_UNIX socket，全套 pytest 假失敗**
  ——builder（codex executor）以 `codex exec --sandbox workspace-write` 執行，實測
  （codex-cli 0.147.0 `codex sandbox`）該沙箱**允許** `socket(AF_UNIX)` 建立與
  `socketpair()`，但用 seccomp 把 **`bind()`** 這個網路類 syscall 擋成 EPERM（errno 1）
  ——即使 socket 路徑落在 workspace-write 的可寫根內。凡是 bind 本地 unix-domain
  socket（起 `MonitorServer`／直接綁 socket）的測試在 builder 沙箱內必失敗，使 builder
  自跑的整套 `python3 -m pytest -q` 永遠有失敗，與正常環境（manager 獨立 ledger／CI）
  結果系統性不一致（envelope failed vs ledger passed 分歧的環境根因）。
  - **根因與放寬邊界**：codex 的 seccomp 是編譯進二進位、無法從 paulsha-cortex 端做
    「只放行 AF_UNIX bind」的細粒度放寬；codex 只提供 `network_access` /
    `danger-full-access` 這種「整片網路打開」的粗粒度開關，而那正是 #586 安全邊界
    明文禁止的（不放寬網路、不放寬 builder 連上 manager 既有 socket）。因此採**環境
    修復**的可行且安全路徑：測試層偵測「當前 runtime 能否 bind AF_UNIX」，凡需要
    bind 的測試在無法 bind 的沙箱語境下**明確 skip（帶原因）**，而非假失敗。
  - **效果**：builder 自跑整套 pytest 在沙箱下由「有失敗」變「skip 不失敗」（exit 0），
    與 manager 權威 ledger（正常環境 run→pass）在源頭一致，消除 envelope/ledger 分歧；
    正常環境／CI／manager 下 bind 可用，這些測試照常執行、零覆蓋損失。
  - **安全邊界**：只改變測試在「無法 bind AF_UNIX」時的判定（run vs skip），**不**放寬
    任何 syscall、不打開網路、不允許 builder 連上 manager 的 socket——與 trust-root
    隔離（#584）方向不衝突。
  - 新增 `tests/sandbox_support.py`（`af_unix_bind_available()` probe + `requires_af_unix_bind`
    marker），防護 `tests/test_stage9_project_monitor_service.py`、
    `tests/test_monitor_work_api.py`、`tests/test_doctor.py` 內會 bind socket 的測試；
    新增 `tests/test_sandbox_afunix_skip_586.py` 證明 probe 判定與真實 bind 能力一致，
    並以子行程在**模擬沙箱**（process-wide 把 `bind` 打成 EPERM）下跑防護測試檔，驗證
    需 bind 的測試 skip 而非 fail、整檔 exit 0。
