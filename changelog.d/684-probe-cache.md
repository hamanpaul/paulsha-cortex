# 684-probe-cache

- **#684（#672 票 C）：planning capability probe 的跨 tick 結果快取，指紋含模板 unit 檔本身**
  ——`build_production_planning_runtime()` 對每個 planning-capable identity 各跑一次 probe
  （`_probe_identity` 每次兩次整棵 repo 的 `copytree`；`probe_agy_capability` 兩次 CLI 呼叫），
  而它由 `run_auto_claim_scan()`（periodic tick，實機 `PSC_MANAGER_INTERVAL_SECONDS=600`）
  與 `apply_work_action()` 兩條路徑呼叫。票 E（#686）把 planning 搬上降權 job 之後，**每一次
  probe 就是一個 systemd unit 實例**——沒有快取等於每 10 分鐘起一批 job 去問模型「你是誰」，
  成本不可接受。新增 `paulsha_cortex/coordinator/planning_probe_cache.py`：
  - **指紋六格**（design D-C／D5：任何會改變 probe 結論的輸入都在裡面）——
    `PSC_JOB_RUNNER` 的**解析後模式**；roster 解析結果的 canonical JSON 雜湊；以該角色的
    PATH 解析出的 executor **絕對路徑 ＋ `st_dev/st_ino/st_size/st_mtime_ns`**；憑證檔的
    `st_size/st_mtime_ns`（**不讀內容**，避免把 token 帶進雜湊的任何中間狀態）；
    `resolve_hardening_profile(executor)`；以及**模板 unit 檔本身**的
    `st_size/st_mtime_ns`。
  - **為什麼是 unit 檔而不是剖面名**：#677 落地的 `PROFILE_LOCKED_KEYS` 說明兩份剖面的
    加固鍵逐字相同，剖面名相同**不代表** unit 內容相同（operator 重新落檔、產生器升級、
    RWP 增列都會改 unit 而不改剖面名）。只認剖面名會沿用一個對新 unit 不成立的 `ready`。
    把 unit 的 stat 放進指紋，等於把**部署動作**與**快取失效**綁成同一件事——沒有人需要
    記得清快取。
  - **為什麼要含 `PSC_JOB_RUNNER`**：direct 與 job 兩種模式的執行環境（PATH／HOME／憑證／
    seccomp／MDWE）完全不同，切換一次就必須全部重探。這同時是 plan 要求「票 F 的生產切換
    一次到位、不能一半走 job」的機械保證：混用時兩種語意的結論不會並存在同一格。
  - **fail-closed**：檔案不存在／JSON 壞／payload 不是物件／schema 不符／`items` 不是物件／
    row 形狀不合／身分欄位被改／同時帶 `ready` 與失敗診斷／指紋不符／TTL 過期／`probed_at`
    落在未來（時鐘倒退）——**全部視為 miss 並重探**，沒有任何一條路徑會因為「上次是 ready」
    而在無法重探時回答 ready。壞檔另落一筆**可辨識**的
    `planning-probe-cache-unreadable`，與「probe 失敗」分開（否則會出現「快取檔壞了、
    症狀卻報成 provider 不可用」）。
  - **與 `not_claimable`（#675）刻意的一處不同**：那份 ledger 對壞檔 **raise**（不可 claim
    的項目必須查得到）；probe 快取**不得** raise——它壞掉時若把整個 planning 拖垮，等於一份
    輔助紀錄取得了它不該有的否決權。兩者是同一條原則（不得靜默產生有利答案）在不同後果下
    的兩種實作。ledger 形狀（`schema` ＋ `items` ＋ `first_observed_at`／`last_observed_at`／
    `observations` ＋ 條件解除自動清除 ＋ temp／`os.replace`／目錄 fsync 的原子寫入）則逐項
    沿用。
  - **TTL 兩段**：`ready` 預設 3600s、not-ready 預設 300s（`PSC_PLANNING_PROBE_CACHE_
    READY_TTL_SECONDS`／`..._NOT_READY_TTL_SECONDS` 可覆寫）。失敗要快速重試（暫時性服務
    錯誤、限流、模型輸出的隨機不從短時間內就會自己好），成功不需要頻繁重確認（重確認就是
    一批 job 的成本）。TTL 在**讀取時**依當下設定判定，因此調短立即生效；非法值一律當 0
    （＝永遠 miss）並落 log，**不落回預設**——落回預設會讓一個打錯的值靜默維持一小時的快取。
  - **快取內容**：除 `ready` 外存失敗側的完整診斷。`reason`／`diagnostic` **逐字沿用**
    `CapabilityProbe`（#674 的 `stdout_excerpt()`／`strip_code_fence()` 是那件事的唯一真相，
    快取層不再造一份節錄邏輯），另存三分族（票 A 的 `classify_probe_failure()`）、`unit`、
    `hardening_profile`、`resolved_binary` 與指紋分量明表（operator 直接看得出「是哪一格
    變了」）。`returncode`／`stdout_prefix`／`binary_version` 先立在 schema 上、目前恆為
    `None`——來源是票 E 的 `PlanningOutcome.diagnostics`。
  - **落點與權限**：新增登記表資產 `planning-probe-cache`
    （`<coordinator_root>/planning-probe-cache.json`，Manager-owned 0600，writers／readers
    **只有** `Principal.MANAGER`），**刻意不進任何 job 模板 unit 的 `ReadWritePaths`**：
    job 一旦寫得動快取，「這個 provider 是 ready 的」就變成模型可以自證的東西。
  - **指紋計算永不 raise**：每個分量各自守備，算不出來的那一格落
    `<unresolved:<例外型別名>>`（**只帶型別名、不帶訊息**，與票 A 依賴的是同一條邊界）。
    因此 job 模式下 `PSC_REVIEWER_PATH` 未宣告（#679 的 fail-closed）只會讓
    `executor_binary` 那一格變成標記，而 PATH 補上之後那一格會變、快取隨之失效——
    **取不到答案本身也是一個會變的答案**。
  - 測試 `tests/test_planning_probe_cache_672.py`（40 條）；**既有測試一行未改**。
