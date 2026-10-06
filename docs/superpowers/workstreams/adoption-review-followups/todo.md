---
status: accepted
work_item: adoption-review-followups
---

# legacy adoption 修正（PR #1292）審查留下的三項

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1282`，含重新開啟時的留言。PR #1292 已依 owner 指示先 merge，本票只處理
  該 PR 審查列出、尚未修正的三個 Important 項目。
- legacy 物件只能 quarantine，不刪除、不覆寫；rollback 後 inventory digest 必須一致；真正無法判定的情況
  fail closed。
- 可能與 `installer-launch-authorities`（#1289）、`system-deploy-ops-defects`（#1291）改到同一份 runbook。
  只改本票需要的段落，不重寫其他章節。

## Tasks

- [x] **T1 runbook 檢查失敗必須中止（I-1）**：`docs/superpowers/runbooks/trust-root-legacy-adoption.md` 在 `$(...)`
      內呼叫 `cortex_root_cli`。bash 在 command substitution 內不套用 `set -e`，sealed tree 檢查失敗會被忽略，
      root CLI 照樣執行。改寫成檢查失敗一定中止（例如改成先寫入檔案再讀取，或在檢查後加上
      `|| exit 1`）。`trust-root-transactional-install.md` 約 763 行的同類寫法一併修正。擴充直接執行 runbook
      原文的測試：檢查失敗時，後續的 root 指令不得執行。
- [x] **T2 `home-top` 只套用在 plan 管理的 HOME（I-2）**：`paulsha_cortex/trust_root/install/legacy.py`（約 1698、
      3221 行）的 `home-top` 自動 quarantine 目前也套用到 cortex-egress 的 HOME。這個 HOME 不受 plan 管理、
      路徑來自 host overlay，可能是共用目錄。改成只套用在 plan 管理的 cortex 帳號 HOME；其他 HOME 的頂層
      未知項目維持 unclassified，由 plan 拒絕。新增 egress HOME 為共用目錄（例如 `/srv`）的測試。
- [x] **T3 補 RC 與 PATH（I-3）**：`trust-root-transactional-install.md` §6 recover 的 PATH 補上 `/usr/sbin`；
      `upgrade.py` 中說 `useradd`／`groupadd` 經 PATH 解析的過時註解改正。確認 RC release profile 的首次安裝
      是否實際透過 installer 執行 `useradd`／`groupadd`：有的話在 PR 寫明，沒有的話補上。RC 補「lease 前
      服務已停止」的情境，或在 PR 說明改用哪個測試涵蓋。
- [x] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
