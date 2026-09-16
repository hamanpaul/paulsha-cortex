---
status: accepted
work_item: daemon-tick-clock-not-idle
---

# Daemon periodic clock 與 idle 配置規格

## Requirements

對應 [#819](https://github.com/hamanpaul/paulsha-cortex/issues/819)，沿用 #832 已審查 todo。只修 periodic scheduler 的熱迴圈與啟動配置；不是關閉完成檢查或把 busy 主機標為 idle。

1. **R1 Cadence**：periodic runner 回 `dispatch_skipped="not-idle"` 時推進 last_tick_monotonic，使下一次不得早於 effective_tick_interval；不推進 last_tick_at、不增加 consecutive_tick_failures、不開 circuit，idle=False、last_tick_error 無新錯誤。保留既有 failure/success/backoff 語意。
2. **R2 CPU-aware env**：default_max_load() 每次呼叫才算 `max(1.0, (os.cpu_count() or 1)*0.5)`；PSC_MANAGER_MAX_LOAD 只接受有限正浮點。unset/空白/解析失敗/NaN/±Inf/零/負數回安全預設。
3. **R3 CLI 與程式入口**：daemon --max-load 合法值優先 env；顯式非法值為 argparse error，不默默 fallback。run_loop 的非 None 顯式值也拒絕非法值；None 不向 periodic builder 加該 key，保留 strict fake/既有 caller 相容。
4. **R4 Require idle**：PSC_MANAGER_REQUIRE_IDLE 經 strip/lower 為 0/false/off/no 才 False，其餘含 unset/空白為 True；--no-require-idle 仍可關閉。保持 service-manager.sh argv，不改 installer managed_env。
5. **R5 Lane 隔離**：保留 DEFAULT_MAX_LOAD=1.0、build_request_executor/build_periodic_tick_runner 簽名預設、lib/idle.py、manual tick request 及 coordinator/cli.py tick 預設。新 run_loop 參數只流向 periodic builder；不新增 status schema 欄位，不改 idle gate 只擋 fanout 的設計。
6. **R6 文件與 residual**：明示 periodic 預設從 1.0 變 CPU-aware、PSC_MANAGER_MAX_LOAD=1.0 可保留舊門檻、env fallback 與 CLI reject 差異。os.cpu_count 不等於 cgroup quota；容器需顯式設定，後續 capacity 工作承接。不能用 bypass idle=True 證明自然 idle。
7. **R7 TDD 與真入口**：FakeClock 的 not-idle 呼叫時刻恰為 10..80 秒八次；固定 CPU 的 env/CLI/kwargs 矩陣、manual lane/backoff/idle 原斷言不改；真 parser help/invalid-argument smoke 不連 live daemon，不消耗模型。

## Boundary

Production 只改 `paulsha_cortex/coordinator/manager_daemon.py`。不修 #496/#497、registry persistence、instance 隔離、executor durable backoff，不改 #249 退避/熔斷公式或手動 tick 的時鐘。跨出此界先重新規劃/計分。

## Evidence

基底 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`：manager_daemon.py:1456 取得 skipped、:1458 只有 not skipped 才推時鐘，:1474 failure 已推時鐘；:37 常數 1.0，:1344 periodic_kwargs 無 max_load 注入，:1661 main 無該旗標。`tests/test_manager_daemon_tick_backoff.py:16` 既有 _FakeClock 與 :208 cadence 測試可重用。
