### Added
- **#622 / trust-root Phase 2b：`trust_root unit three-way --monitor`——monitor 的
  system-level unit（同帳號、同加固段，可寫面嚴格窄於 Manager）**——Phase 2b M1
  之後 `permgen` 只產生 Manager unit，實機切換後 cortex instance **完全沒有
  monitor**：舊的 `--user` unit 以操作者身分跑、`PSC_MONITOR_STATE_ROOT` 指著舊的
  `~/.agents/monitor`，起回來只會讓 `monitor-state-tree`／
  `monitor-work-items-snapshot`／`monitor-github-sync-cursor` 出現雙寫來源，而且它
  寫不進 `0700 cortex-manager` 的 `/var/lib/cortex/monitor`；`monitor-event-spool`
  因此只有 builder 的 `wx` 生產端、沒有消費端，spool 只增不減。
  新增 `permgen.build_monitor_unit()` 與 CLI 旗標 `--monitor`：`User=` 取
  `durable_state_owner`（UID 方案表寫的就是「`cortex-manager`＝Manager ＋ monitor」，
  也唯有同帳號才寫得進自己的 `0700` state 樹）、加固段與 `EnvironmentFile`
  （無 `-` 前綴＝fail-closed）與 Manager unit 共用同一份來源、`HOME`／
  `XDG_CACHE_HOME` 走 `layout.home_of()`／`cache_of()` 而非字面量。
- **`ReadWritePaths` 的 persona 過濾（`permgen.principal_needs_write()`）**——同帳號
  **不代表**同可寫面：只按帳號導出時 monitor 會拿到 Manager 的全集
  （`coordinator/`、`specs/`、`control/`、`worktree/`…），等於把 monitor 的任何 bug
  或被餵入的惡意 GitHub 內容變成對整棵 durable state 的寫入面。因此
  `required_write_targets()`／`read_write_paths()`／`read_write_path_owners()` 多一個
  `principals=` 參數（`None`＝維持既有帳號全集行為，Manager 與 job 模板 unit 的輸出
  **逐位元不變**），導出規則只有兩條、且兩條都直接讀登記表欄位：persona 是
  `writers` 之一，或是 `IngressKind.INTERPROCESS` 單向 spool 的 reader（消費＝讀完
  unlink，需要容器目錄的寫入權）。monitor unit 因此只拿到三條：
  `/var/lib/cortex/monitor`（`monitor-state-tree`＋兩個葉檔＋`monitor-event-spool`
  的消費端）、`/var/lib/cortex/run/cortex`（`runtime-run-tree`，monitor 的 unix
  socket）、以及明示 extra 的服務帳號 HOME 快取——是 Manager 十一條的**真子集**。
- **`ExecStart` 形態與 #618／PR #619 對齊**——用部署 venv 的 console script ＋ 既有
  CLI verb（`<venv>/bin/cortex monitor` → `paulsha_cortex.monitor.__main__:main`，
  不帶 `--once` 即長駐 `ProjectMonitorService.run_forever()`，符合 `Type=simple`），
  而**不是** `python -m paulsha_cortex.monitor`：後者會在部署樹裡開第二種進入點形態，
  與 `cortex service run` 各自漂移，而 R-16 的 CLI help 對齊面只看得到前者。
  新增 `tests/test_trust_root_monitor_unit_622.py`（40 測試），含比照
  `tests/test_service_run_verb.py` 的**契約鎖**——真的走一次 `cli.main`，確認
  `ExecStart` 指名的 verb 會抵達 monitor 進入點；另有加固欄位與 Manager 的**集合
  等式**（任一邊加減一項即紅，杜絕單邊漂移）與「RWP 是 Manager 真子集」的斷言。
  runbook 補第 4d 步（落檔、身分／加固／RWP 驗證、新樹確有寫入、spool 被消費、
  fail-closed 複驗、回滾）。
