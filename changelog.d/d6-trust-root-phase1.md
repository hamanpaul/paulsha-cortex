### Added
- **R0.5 D6 / trust-root 隔離 Phase 1（純程式碼、不需 root、含降級安全網）**
  ——依 spec `docs/superpowers/specs/trust-root-isolation-spec.md` 的 Phase 1 條列與
  operator 0816 裁決（路線 A 為 0.2.0 基礎；Manager 專屬 UID；headless 二分
  builder／非 builder；舊 state 以 `legacy-import` 重入帳；降級運轉預設逐案核可），
  新增 `paulsha_cortex/trust_root/` 子套件，交付 join gate 未達成期間的契約層地基：
  - **R1 資產登記表**（`trust_root/registry.py`）：把全部 security-relevant durable
    state 與 mutation ingress 固化為單一機器可讀真相（`TrustRootAsset` 常數），對每項
    宣告 `asset_id`／`tier`（0/1/2）／`tree`（Manager-owned vs job-visible，裁決 10-1／
    10-2）／`path_resolver`／`writers`／`readers`／`ingress_kind`，涵蓋 builder／reviewer／
    planner 三個 headless persona。以反射對 `config/paths.py`＋`control/constants.py`
    的每一個回傳 `Path` 的函式釘住**雙向等式**——新增未登記的 path 函式即 FAIL 並指名；
    已知重複推導（`delivery-journal` 六處、`jobs-registry` 四處、
    `work-items.snapshot` 兩處 fallback 分歧）固化為單一 canonical asset。
  - **R3 啟動自檢（WARN-only）**（`trust_root/selfcheck.py`）：用登記表對照現行部署
    實況，把帶 group／other 寫入位的 Manager-owned 路徑標為 `job-writable` 並輸出結構化
    診斷；掛在 `manager_daemon.run_loop` 啟動點（受 `PSC_TRUST_ROOT_SELFCHECK` 閘控、
    fire-and-forget、永不 raise）。Phase 1 **只 WARN、不 fail-closed**（fail-closed 是
    Phase 2 R3 切換）。自檢在現行部署上獨立重現 spec 背景段盤點（`control`／
    `coordinator`／`specs` 為 `drwxrwxr-x`），反證盤點正確。
  - **R7 capability 通道 + 降級運轉**（`trust_root/capability.py`）：敏感 action
    （`headless-acceptance`／`outbox-mutation`／`ship`／`merge` 封閉清單）在無 capability
    時 100% 被拒；capability 為 action-bound（綁 `(action, work_id, run_id,
    subject_hash, authority_revision)`）＋single-use（in-process nonce ledger）＋短效
    （TTL ≤300s）＋不可經 durable state 重放（本體不落地，附常設掃描 helper）。降級
    運轉開關（`PSC_DEGRADED_OPERATION`）預設 `per-case-approval`（裁決 10-5），提供
    `disabled`（完全停用）切換。
  - on-demand 診斷入口 `python -m paulsha_cortex.trust_root {selfcheck,registry,equation}`
    （不動 `cortex` CLI，避開 R-16 help 對齊面）。
  - **Phase 1 不提供（需 Phase 2 OS 邊界才完整）**：真正的不可寫強制（同 UID 下 mode
    對 owner 無效）、跨 process／防重啟的持久 nonce ledger、capability 通道的 Unix
    socket OS 隔離、自檢 fail-closed。Phase 1 建立契約與 fail-closed 語意，Phase 2 以
    獨立 UID／目錄 owner 把它變成 kernel 強制。**不做** Phase 2（建 UID／分樹／部署遷移／
    降權啟動器）與 Phase 3（簽章）。
  - 新增 `tests/test_trust_root_registry_r1.py`、`tests/test_trust_root_selfcheck_r3.py`、
    `tests/test_trust_root_capability_r7.py`（43 測試）。
