---
status: accepted
work_item: daemon-tick-clock-not-idle
---

# Daemon periodic tick 時鐘與 idle 設定

## Boundary

- Issue：`hamanpaul/paulsha-cortex#819`。
- 修正 `manager_daemon.run_loop` periodic lane 在 `dispatch_skipped="not-idle"`
  時未推進時鐘的問題，並提供 periodic runner 的 max load／require idle 設定。
- 不改 dirty recheck／terminal replay（#496／#497）、registry persistence（#821）、
  instance isolation（#818）、`lib/idle.py`、#249 backoff／circuit-breaker 公式、
  manual tick request lane、`coordinator/cli.py` tick 預設、installer managed_env，
  或 `status.json` schema。
- 保留 `DEFAULT_MAX_LOAD = 1.0` 與 `build_periodic_tick_runner`／
  `build_request_executor` 簽名預設；CPU-aware 預設只用於 daemon CLI 啟動的
  periodic lane。manual lane 預設仍為 1.0，這是明確的相容性邊界。

## Tasks

- [ ] 在 periodic runner 回傳 not-idle 時推進 `last_tick_monotonic`，如同失敗分支
      會推進時鐘；不更新 `last_tick_at`、`consecutive_tick_failures`、
      `tick_circuit_open`、`last_tick_error`，且 `daemon.idle` 保持 False。
- [ ] 在 `tests/test_manager_daemon_tick_backoff.py` 新增
      `test_periodic_tick_not_idle_does_not_hot_loop`：使用既有 `_FakeClock`，
      `poll_interval=1.0`、`tick_interval=10.0`、`max_rounds=85`，runner 恆回
      not-idle，斷言呼叫時刻恰為 `[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]`；
      同時斷言上述 daemon 欄位分別為 None／0／False／None／False。
- [ ] 新增 `default_max_load()`：呼叫時以 `max(1.0, (os.cpu_count() or 1) * 0.5)`
      計算預設；讀取 `PSC_MANAGER_MAX_LOAD` 時只接受 `math.isfinite(value)` 且
      `value > 0` 的浮點數。unset／空字串／無法解析／NaN／正負 Inf／零／負數
      均安全回預設，不得使非法值進入 idle probe。
- [ ] daemon `main()` 新增 `--max-load`，CLI 與 env 共用「有限正數」驗證規則；
      CLI 的顯式非法值回 argparse error，env 非法值則採上條安全預設。CLI
      合法值優先於 env；文件須明示這項錯誤呈現差異，不得默默接受 NaN／Inf。
- [ ] `run_loop` 新增 `default_max_load: float | None = None`；只有非 None 時
      才加進 `periodic_kwargs`，且非 None 值也必須為有限正數；不要將新參數傳入
      `build_request_executor`。參數與 helper 同名，函式內不得誤呼叫遮蔽後的參數。
- [ ] 新增 `default_require_idle()`：`PSC_MANAGER_REQUIRE_IDLE` 經
      `strip().lower()` 為 `0`／`false`／`off`／`no` 時 False，其他值、unset／空字串
      都為 True；`main()` 採 `default_require_idle() and not args.no_require_idle`。
      不改 `service-manager.sh` 啟動 argv。
- [ ] 新增 `tests/test_manager_daemon_env_defaults.py`；固定 `cpu_count=20`，驗證
      unset／空字串／abc／NaN／nan／Inf／-Inf／0／-1 回 10.0、4.5 回 4.5，
      `cpu_count=None` 回 1.0。CLI 非法值均拒絕，CLI 7 覆寫 env 4.5，未帶旗標則
      使用 env 4.5；以 stub `run_loop` 捕獲 `main()` 的實際傳參。
- [ ] 同檔以 monkeypatch `build_periodic_tick_runner` 捕獲 `run_loop` 的 kwargs：
      顯式 7.0 有傳入、未提供參數時 key 不存在，非法顯式值拒絕；設定
      `PSC_CONTROL_ROOT=tmp_path`，不要傳入不存在的 `run_tick_fn` 給 `run_loop`。
      另直測 `build_periodic_tick_runner(run_tick_fn=fake, default_max_load=7.0)`
      會將 7.0 傳給 `run_tick`。
- [ ] 驗證 require-idle helper 的大小寫／空白與 main 路徑：env 0→False、unset→True、
      unset 或 env 1 加 `--no-require-idle`→False。保留既有 tick backoff、idle、
      coordinator manager daemon 測試的原始斷言；只新增必要測試，不變更 manual lane。
- [ ] README 的 daemon env 設定段記載新 env／CLI、實際優先序、非法值規則及行為變更；
      periodic 預設由 1.0 改為 CPU-aware，可設 `PSC_MANAGER_MAX_LOAD=1.0` 保留舊門檻。
      新增本 workstream 的 changelog fragment 並同步 `CHANGELOG.md [Unreleased]`。
- [ ] 透過 Cortex 記錄修前 RED、修後 focused／完整 gates、review、merge 與實際
      runtime 驗證；本 todo 的 accepted 不代表修正或測試已完成。

## 已列管限制

- `os.cpu_count()` 不保證反映 cgroup CPU 配額；本票不新增 cgroup 探測。容器環境須
  顯式設定適當門檻，後續 capacity 工作再處理。不能以 bypass 模式的 idle=True
  證明自然負載低於門檻。
- manual request 預設、installer env 清單與 status schema 保持原狀，後續統一設定／
  可觀測性工作需引用此邊界；不得把它們宣稱為本票交付。
