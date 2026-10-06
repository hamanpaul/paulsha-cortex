---
status: accepted
work_item: system-deploy-ops-defects
---

# Trust Root system 部署的運作缺陷：Monitor fetch、新鮮度門檻、roster、quota 設定、legacy spec

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1291`。
- 目標：Trust Root system 部署裝好或升級後，就能長期當正式環境使用，不需要 operator 手動補設定
  （owner 2026-10-06：日常工作會逐步從使用者層級的 cortex 移到 system 部署）。
- 七個缺陷都在 issue 內有現場證據（唯讀盤查第一台 adoption 主機）。每項必須有修正與測試；若判斷某項
  不修，要在本 todo 的對應 task 寫明理由，並回報到 issue。
- 不在本票範圍：launcher 權威來源（#1289）、executor 執行檔來源（#1293）、命令阻擋（#1283）。
- 不放寬任何既有的 fail-closed 檢查；legacy 物件不刪除，只停放或 quarantine。

## Tasks

- [x] **T1 Monitor `github-terminal` provider**：Monitor unit 對來源樹（`<state>/repos/<repo>`）是唯讀的，
      `git fetch` 寫不了 `.git/FETCH_HEAD`，provider 一直 degraded。改成由有寫權限的身分做 fetch，或改用
      不需要寫 checkout 的查詢方式。測試涵蓋唯讀來源樹。
- [x] **T2 claim 新鮮度與刷新週期一致**：`claim.py` 的 `PROVIDER_MAX_AGE_SECONDS=900` 與 system Monitor 的
      `github_refresh_interval_seconds=1800` 互相矛盾，約一半的時間 intake 會被擋成
      `provider-degraded-or-stale`。讓刷新週期一定小於門檻，或由刷新週期推導門檻；兩種部署
      （system、使用者層級）都要一致。
- [x] **T3 model roster 隨 release 更新**：packaged roster 與 adopted overlay 都沒有 `codex/gpt-6-luna`、
      `agy/gemini-3.8-flash-high`、`copilot/gpt-5.4-mini`，builder pin 會因 identity unknown 被拒。更新
      packaged roster，並讓 system 部署有受支援的方式加入身分（不手改 cortex-manager 0600 的檔案）。
- [x] **T4 quota-pools 預設設定**：system 部署沒有 quota-pools 設定，`PSC_QUOTA_POOLS_CONFIG` 也沒設，
      `_quota_admission_context_for()` 回 `None`，#839 的 admission（連 shadow receipt）都不會產生。由
      installer 提供預設的 shadow 設定，或納入 install config。
- [x] **T5 Monitor project config 不依賴 operator HOME**：system Monitor 的 `project-cortex.yaml` workspace
      指向 operator HOME 下的路徑，但 unit 設了 `ProtectHome=yes`，那個路徑看不到。改用 system 部署自己的
      路徑（例如 `PSC_REPO_ROOT`／state root），測試涵蓋。
- [x] **T6 升級後來源樹同步**：`cortex upgrade` 以 `checkout --detach --force <candidate>` 重設來源樹，之後
      新增到 `.cortex/work-items.yaml` 的 work item 要等來源樹同步後 Monitor 才看得到。提供自動同步機制
      （例如由有權限的身分定期 fetch 並前進到預設分支），與 T1 一起設計。
- [x] **T7 adoption 停放 legacy slice spec**：adoption 接手的 `<state>/specs/` 舊 `dispatch: auto` slice spec，
      每個 tick 都會進 fanout，needs_human 也擋不住。adoption 時自動停放（例如移到 state 內的
      `specs/.parked-<日期>/`，可逆），或標示為不可派，避免 launch 修好後立刻對舊分支派真 job。
      已 adoption 的主機經升級後也要收斂。
- [x] **T8 文件**：runbook 補上各項的行為與檢查方式。新增 changelog fragment，並同步
      `CHANGELOG.md [Unreleased]`。
