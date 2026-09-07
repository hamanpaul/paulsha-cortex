---
status: accepted
work_item: daemon-tick-clock-not-idle
---

# Daemon periodic cadence 與設定傳播設計

## Decisions

### D1 Monotonic 排程與 wall-clock 狀態分離

periodic due → runner → success/not-idle/failure；本票只補 not-idle 的排程 monotonic 更新，既有 success/failure 分支的時鐘與 status 語意原樣保留，不擴張其他 skipped reason 的處理。not-idle 不算 exception，不寫新的 failure/circuit 狀態；後續 exception 仍走 #249 邏輯。
本票不改 manual tick request 的 skipped 時鐘分支。FakeClock 驗證 periodic 時刻而非真 sleep；85 rounds、poll=1、tick=10 的 runner 時刻必為 [10,20,30,40,50,60,70,80]。

### D2 一套值域，三種錯誤呈現

共享純 validator 定義 finite 且 >0。env helper 捕捉缺失/無法解析/非法值後回 CPU-aware 預設；argparse type 對顯式非法 CLI raise ArgumentTypeError；run_loop 顯式非法參數 raise ValueError，且在建立背景 worker/處理 request 前拒絕。
CPU 數在 helper 呼叫時計算，不能 import-time 固化。default_max_load helper 與同名 run_loop 參數不互相誤呼叫。

| 入口 | 值來源與結果 | 不受影響者 |
|---|---|---|
| main → periodic | 合法 CLI > 合法 env > CPU-aware default | manual request executor 預設 1.0 |
| run_loop(default_max_load=None) | 不加 periodic_kwargs key | strict keyword-only fake/caller |
| run_loop(default_max_load=合法值) | 只向 periodic builder 傳值 | build_request_executor 不接新參數 |
| 直接 build_periodic_tick_runner() | 簽名預設仍 DEFAULT_MAX_LOAD=1.0 | 既有直呼測試 |
| manual tick | request max_load 或原預設 1.0 | coordinator/cli.py、lib/idle.py 不改 |

### D3 Require idle 與操作邊界

main 將 `default_require_idle() and not args.no_require_idle` 傳給 run_loop。service argv、installer managed_env、status schema不動；文件明示 instance env 要由 operator 配置，不由測試寫入真 env。CPU-aware 是預設行為變更，不宣稱 cgroup-aware；容器 operator 可用顯式門檻。

### D4 風險與測試

| Surface／風險 | Harness | Oracle |
|---|---|---|
| skipped 熱迴圈 | 既有 _FakeClock | 八個精確時刻；status None/0/False/None/False |
| NaN/Inf/非法 env | cpu_count=20 + monkeypatch env | fallback=10.0；cpu_count=None→1.0 |
| 非法 CLI／優先序 | main stub + 真 subprocess parser | 非法 exit 2；CLI 7 覆寫 env 4.5；不進 run_loop |
| kwargs 相容 | strict fake periodic builder | None 時無 key；顯式 7.0 只傳 periodic |
| require idle | 大小寫/空白參數矩陣 | env 關閉與 CLI 關閉按 R4，不改 manual |
| regression／usage | 原 daemon/idle tests、help smoke | 原斷言不變；help 含新旗標、README 說明可追溯 |

重用 pytest/monkeypatch/tmp_path；無新框架、無真 loadavg 假設，PSC_CONTROL_ROOT 僅指 fixture。測試先保存 not-idle RED，再修 source，最後跑 focused/full/CI/policy 與實際候選 parser smoke。

### D5 Sizing

單 production 模組 → domain_breadth=0；維持跨入口 backward compatibility → state_consistency=1；7 條 requirements → invariant_count=7。現行完整 fix-standard lane 預期 7/Red，不能刪宣告變 unknown；#831 實際 runtime 落地後重評，仍 Red 才真拆 cadence 與配置傳播。不可預填修後分數當現行 authority。
