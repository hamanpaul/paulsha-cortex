---
status: accepted
work_item: quota-admission-enforce-runbook
---

# quota admission enforce 營運前提與操作手冊

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1198`。
- 範圍：新增 `docs/superpowers/runbooks/quota-admission-enforce.md`；`README.md` 的 quota 段落加一行指向它；`changelog.d/<branch-slug>.md` 與 `CHANGELOG.md [Unreleased]`。
- 不動：任何 `paulsha_cortex/` 程式碼、測試、`.cortex/`、openspec、既有 runbook 與設定檔。
- 本 work item 同時是 quota enforce live canary 的派工卡；內容是 docs-only，與 quota 程式碼無關。

## 手冊必須涵蓋的事實（撰寫時逐條回到程式碼核對，出處以 `檔案:符號` 標注）

1. **開關與生效時機**：`PSC_QUOTA_ADMISSION_ENFORCE=on` 在呼叫當下讀 daemon 的環境（`paulsha_cortex/coordinator/quota_admission.py` 的 enforce 判定函式），改變它必須重啟 Manager；`quota-pools.json` 每次派工／tick 重讀（`paulsha_cortex/coordinator/manager_daemon.py` 的 quota context 建立），不需重啟。`PSC_QUOTA_RESERVATION_ENFORCE` 已移除、沒有作用。重啟 user unit 會連帶終止在跑的 job（unit 的 `KillMode=control-group`；`docs/unified-work-lifecycle.md` 的 restart 段），開關只能在沒有 job 時切換。
2. **設定原則**：binding 以 #1116 的 identity subject（`{"kind":"identity","executor":…,"model_id":…}`）撰寫，不用會隨卡片變動的 resolved profile key；沒有 collector 的 provider 若綁池，enforce 下永遠 `unknown-remaining-quota`，因此只在 shadow 綁、或刻意不綁（unmanaged 照常放行）。`cortex quota bindings --report` 檢查覆蓋。
3. **營運前提**：enforce 下綁池的候選需要 fresh observation；觀測 TTL 取 collector 設定的 `lease_ms`（未設為 300000 ms，`paulsha_cortex/porcelain/quota.py`），periodic tick 預設 600 s，TTL 必須大於 tick 間隔，並需要週期性 `cortex quota observe`（本機目前沒有 timer）。
4. **判定與 wait**：demand 為 `dispatch-unit:v1`（每個 pool/window 門檻 1 個原生單位）；候選迴圈依序排除不可行者（`paulsha_cortex/coordinator/manager.py` 的 `_dispatch_workflow_card` 候選迴圈）；全部不可行時 run 標 `needs_human`、reason `quota-admission-insufficient`，寫一筆 `enforced/wait` receipt（`retry_eligible`）。設定無效為 `quota-config-invalid`，不自動續派。
5. **恢復**：periodic tick 只在 wait receipt `retry_eligible` 且當下重新評估有可行候選時自動續派（`manager_daemon.py` 的 quota wait 挑選、`manager.py` 的 `_quota_wait_has_recovered_candidate`）；光有 reset 時間不解除等待；不成立時 `status.json` 的 `workflow_waits` 顯示 `operator-resume-required`；手動 `cortex work resume` 會重新評估。
6. **rollback 回 shadow**：移除 env 後重啟，**保留**設定檔（刪設定檔會讓 reconcile 失去 context，bound reservation 無人收斂）；decisions／reservations／observation 三個 store 只增不改。
7. **證據指令**：`cortex quota reservations --json`、`cortex quota bindings --report --config … --json`、`cortex inspect status --json`、`cortex work show <work> --repo <repo> --json`，以及 coordinator root 下 `quota-admission-decisions/decisions.jsonl`、`quota-reservations/reservations.jsonl`、`quota-observations/events.jsonl` 的位置。
8. **判讀注意**：job 終局後 periodic reconcile 把 reservation 收斂成 `released`（不是 `settled`）；#1196（agy fraction 與 demand 1 不匹配）與 #1197（`selected_observation_state` 取總體狀態）修正前的讀法；brainstorm／planning runtime 不經 quota admission。

## Tasks

- [ ] **T1 docs**：依上列八點撰寫 `docs/superpowers/runbooks/quota-admission-enforce.md`（zh-tw；不得出現個人絕對路徑或使用者名，路徑一律用 `$HOME`、`$PSC_COORDINATOR_ROOT` 等變數）。
- [ ] **T2 docs**：`README.md` 的 quota 相關段落加一行指向手冊。
- [ ] **T3 changelog**：`changelog.d/<branch-slug>.md` 與 `CHANGELOG.md [Unreleased]`。

## 驗收

- 手冊每一點都有程式碼出處，且與 main 一致；repo 全套測試通過；policy_check（帶 PR 上下文）fail 0。
