### Fixed
- **#618 / trust-root Phase 2b：補上 `cortex service run`——permgen 的 manager unit
  `ExecStart` 指向一個不存在的 verb**——`permgen` 產生的 system-level unit 寫的是
  `ExecStart=<venv>/bin/cortex service run`，但 `porcelain/service.py` 只有
  `install`／`start`／`stop`／`restart`／`status`／`logs`／`uninstall`，unit 一 start
  就以 `unsupported service command` 失敗；Phase 2b 第 4c 步（Manager 遷 system-level）
  因此 blocking。修法是加一個薄轉發 verb：`cortex service run` 之後的 argv（含
  `--help`）在 parse 前就攔截並原樣交給 `coordinator.manager_daemon.main()`，
  不在 porcelain 端複製一份會與 daemon parser 漂移的參數宣告。
  **不沿用 `scripts/service-manager.sh` 當 `ExecStart` 的理由**：該 wrapper 會
  `mkdir -p "$HOME/.agents/log"` 並把 daemon 輸出導進去，而 Phase 2b 的
  `HOME=/var/lib/cortex-manager` 為 root-owned（只有 `cache/` 可寫）且 unit 帶
  `ProtectHome=yes`——system-level 的正確形態是 daemon 前景跑、log 交給 journald，
  重啟語意由 `Restart=on-failure` 負責。新增 `tests/test_service_run_verb.py`
  五條，含一條把「產生器的 ExecStart」與「CLI 實際提供的 verb」綁在一起的迴歸鎖，
  避免同一條契約再度單邊漂移。
