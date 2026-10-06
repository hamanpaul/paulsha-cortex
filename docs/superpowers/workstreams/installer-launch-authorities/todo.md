---
status: accepted
work_item: installer-launch-authorities
---

# Trust Root installer 備齊 launcher 需要的權威來源，system 部署裝好就能派工

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1289`，含 issue 留言「範圍擴充：installer 要備齊 launcher 需要的所有權威來源」。
- 現況：分 UID（degraded）模式下，`coordinator/launcher.py` 對每個 executor 都會呼叫
  `spool_slot.canonical_codex_controls(runtime_principal)` 與 `provision_runtime_surfaces`。
  transactional installer 只建立空的 `<state>/config/codex-controls/`，沒有任何 principal 的
  controls（`config.toml`、`hooks.json`、`plugins/`、`skills/`）。`credentials import` 把 builder/codex
  寫到 role HOME 的 `.codex/auth.json`，launcher 卻讀 `<state>/config/codex-credentials/<principal>/auth.json`。
  Manager 的 EnvironmentFile 沒有 `PSC_COPILOT_OAUTH_CONFIG`。結果 system 部署安裝或 adoption 之後，
  任何 job 都啟動不了。
- RC 測不出來，是因為 `qualification/run.sh` 在 apply 之後，用 harness 專用的 fixture 加上
  `python3 -m paulsha_cortex.trust_root scaffold | sh -eu` 種出 controls 與憑證（commit `0e9260e5`）。
  正式 runbook 與 installer 都沒有對應步驟。
- owner 原則（2026-10-06）：cortex 是高可信自主工具，不新增需要人工的步驟；legacy 物件不刪、只
  quarantine；真正無法判定的情況 fail closed。
- 不在本票範圍：executor 執行檔從哪裡來（#1293 處理，release 不再封存 agent 執行檔）。本票的
  launch authority 建立邏輯必須與 executor 執行檔的位置無關，也不要新增任何依賴
  `/opt/cortex/toolchain` 封存副本的程式。
- 相容性：v0.1.13／v0.1.14 的 plan 與 receipt 照舊可讀；prior-receipt 交接與 #1275 的
  `credentials inherit`（記錄接手當下 sha）語意不變。

## Tasks

- [ ] **T1 RED**：新增測試，證明現行 installer 在首次安裝與 legacy adoption 之後：
      `canonical_codex_controls('builder')`／`('reviewer')` 失敗；
      `<state>/config/codex-credentials/builder/auth.json` 不存在；
      已匯入 reviewer-planner/copilot 時 Manager env 缺 `PSC_COPILOT_OAUTH_CONFIG`。
- [ ] **T2 canonical codex controls 由 installer 建立**：plan 宣告 builder 與 reviewer 的
      `codex-controls/<principal>` 為 installer-owned root 物件，apply 時建立，receipt 記錄、verify 檢查、
      rollback 還原後 inventory digest 一致。內容取自 release 封存的政策：最小 `config.toml`、與 role HOME
      相同形狀的 `hooks.json` 政策（#698 root-owned／sticky 規則）、空的 `plugins/` 與 `skills/`。不得從
      operator 或 role HOME 複製。
- [ ] **T3 codex 憑證位置一致**：選定唯一的 canonical 位置，讓 `credentials import` 與 launcher 的
      `provision_runtime_surfaces(seed_credential=True)` 讀寫同一處。#1275 的 inherit 與 receipt credential
      row 模型維持一致。既有安裝經升級後自動收斂，不需手動搬檔。
- [ ] **T4 copilot OAuth authority**：匯入 reviewer-planner/copilot 憑證時，installer 推導並寫入 Manager
      EnvironmentFile 的 `PSC_COPILOT_OAUTH_CONFIG`，verify 會檢查。
- [ ] **T5 升級與 adoption 收斂**：既有安裝缺 controls 時，新 plan 自動補上。若 controls 已存在但內容與
      封存政策不同（例如 operator 手動補過），依 installer 既有的 drift／adoption 規則以
      quarantine-then-create 處理，保留舊副本，不默默覆寫、不停下來等人。legacy adoption 的舊 controls
      照舊 quarantine，再由 T2 建立新的。
- [ ] **T6 RC production parity**：
      - 拿掉 `qualification/run.sh` 用 harness fixture 與 scaffold 種 controls／憑證的步驟。
      - installer 照 runbook 走完（apply → credentials import → activate → verify）之後、harness 對
        installer 管理目錄做任何布置之前，先經 Manager launcher 路徑實際啟動一個 job。若 release profile
        做不到完整啟動，則對每個已匯入的 provider 照 `launcher.launch()` 的順序跑 authority／provisioning
        檢查。不通過即 RC 失敗。
      - harness 對 installer 管理路徑（`/opt/cortex`、`/var/lib/cortex*`、`/etc/systemd/system`、role HOME）
        剩下的寫入，必須列在有註解的允許清單內，並由單元測試解析 `qualification/run.sh` 強制執行。
      - legacy-adoption profile 證明 adoption 之後 launcher authority 檢查通過。
- [ ] **T7 文件**：`trust-root-transactional-install.md` 與 `trust-root-legacy-adoption.md` 寫明 controls、
      憑證與 copilot authority 由 installer 管理；驗收步驟加入「至少一個 job 成功啟動」。新增 changelog
      fragment，並同步 `CHANGELOG.md [Unreleased]`。
