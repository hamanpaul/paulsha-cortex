---
description: "driving cortex、派工 cortex、cortex work 導向的協作 skill（issue #177）"
---

# driving-cortex skill

## 兩視角分工（對應 B8 `docs/onboarding/*`）

- B8 onboarding docs 聚焦「人類 operator」安裝、bootstrap、日常維運；本 skill 是 agent 在 session 內的編排視角。
- 你是 agent 時，主要目標是**讓 coordinator 正確拿到可執行的 spec / plan / authority，並持續把 run 從 claim 走到 ship**；不是代替 operator 做環境安裝。

## 心智模型

- Manager 是唯一 writer：所有狀態變更、派工、run 推進都由 manager daemon 維護（透過 control queue）。
- Monitor 是唯讀投影：讀取 `.cortex/work-items.yaml`、workstream `todo.md` 以及其他 confirmed authority。
- deck 是上游契約入口：先形成 accepted planning artifact 後，依 `claim → define → plan → build → verify → review → ship` 進入生產化 workflow。
- `cortex work start`、`resume`、`retry-build`、`review-attest` 是 build 之後的人機協作邊界；`5s` 類 timeout 本身通常只是「未達結果」，不代表系統已失敗。

## 開一個 dogfood 批次

1. 從 accepted planning artifact 開始：spec/plan/frontmatter 必須有 `status: accepted`、`work_item`、必要章節與 `target_branch`。
2. 在 `.cortex/work-items.yaml` 加入對應 work item 並對齊 `issue`。
3. 把 `docs/superpowers/plans/driving-cortex-skill.md` 參考為本批次來源之一，並補齊對應 `openspec/changes/2026-07-26-driving-cortex-skill/tasks.md`。
4. 啟動或重啟 monitor snapshot，確認 work item 在 remote view 出現後再開始調度。
5. 透過 `cortex ready --specs-dir ...` 檢查已滿足條件，再進入 `cortex tick`。

## 驅動桿

- 每次 run 啟動順序：
  - `cortex work start --workflow-action`（依 manager 指示）
  - `cortex work resume --expected-run-id`（run 卡住時）
  - `cortex work retry-build --payload ...`（build 後重試）
  - `cortex work review-attest --payload ...`（有 `review-attest` evidence 時）
- 恢復桿（依序操作）：
  - [ ] run 停留在 `needs_human` 時，先看 run/facet 的 blocking reason，補齊對應 artifact 再 resume。
  - [ ] 驗證卡片 evidence 的 binding 是否可重讀，必要時用 `cortex work retry-build`。
  - [ ] review-thread 未關閉但 merge-ready 前先在 remote 解決 thread，再以 `cortex work resume` 重繼。
  - [ ] merge 後發現 run 尚未前進時，等待下一 run 合併後再 resume（避免手動強推）。

## 執行器設定

- 編輯 `~/.agents/config/paulsha/model-identities.yaml`。
- `build` 的 executor 需對齊 `identity.executor`，`model_id` 要和 CLI `--model` 完整一致。
- builder 與 reviewer `independence_domain` 不可相同，避免同域風險互相依賴。
- 直接派 `codex` 修 bug 時，請使用 `-c model_reasoning_effort="high"`（對齊 issue #148 所需效能策略）。

## 每批 merge 後部署

1. 在 target repo 以 `pipx install --force <repo>` 重新安裝，讓新 code 可被服務載入。
2. 依序 restart manager/monitor，確保新 manifest、workflow 入口與身份配置同步。
3. 開始下一批前先清點 `systemctl --user status`、`cortex status` 與 monitor snapshot。
4. 注意順序：daemon 啟動若過快，可能先看到舊 mtime；可在重啟前後比較部署時間與檔案 mtime 判斷是否已切到新版本。
5. 複核 env/unit：有些 venv 或外部注入會覆寫 `F44` 相關變數，重啟前以 `grep` 將其列出。

## 生命週期特性

- run closure 常有一批延遲：批次 N 在 N+1 合併前，可能維持在 `ongoing / review / needs_human`。
- 非故障情境下，不建議卡住 N，以免阻斷 N+1 先行 claim；優先先完成 N+1 的規劃與 merge，待前序條件滿足後再 resume N。
- 最後一批必須有獨立 PR 確認 todo 勾選完成，避免 done record 先行封裝。

## 已知坑

- [ ] `#152`：`5s` 這類短超時是預期回報節奏，不代表實際失敗，先確認 `--wait` 與 retry 方案。
- [ ] `#175`：re-claim 繼承舊 PR 綁定已於 #190 修復；仍建議避免在已開 PR 後直接 re-claim，以防邊界情境殘留。
- [ ] `#158`：archive/sources 的 Purpose 若仍留 `TBD`，會阻礙 close gate，需補齊。
- [ ] `#83`：缺少 worktree/branch GC 的情境下，偶有殘留資源；交接前回收可疑 workspace。
- [ ] `#99`：daemon 若假設目前 cwd 作為 repo root，可能讀錯目錄，務必驗證 manager/monitor 是否以 `PSC_REPO_ROOT` 類正確根路徑執行。
