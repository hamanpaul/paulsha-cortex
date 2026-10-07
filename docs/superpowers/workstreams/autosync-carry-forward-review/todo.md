---
status: accepted
work_item: autosync-carry-forward-review
---

# autosync 產生的 candidate 在所有判定點都被當成合法來源；兩條 review 路徑都能真正交付

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1356`（從 #1345 拆出）。
- 背景：#1345 在 `manager.py` 建了 `_workflow_candidate_source_job`／`_workflow_manager_candidate_source_job`，讓 builder context、review binding、repair base 都認得 `main-sync-autosync` 這個 manager job。但其他檔案仍各有一份「只認 builder 或 openspec-archive」的判定，autosync 後的 run 在交付端接連失敗：
  - **路徑 A**（autosync 後重跑 code-review；現場 `gate-pythonpath-isolation`，pin 9cb2cd4c）：review 收割通過後，ship 拋出 `delivery requires the reviewed exact-candidate builder job`，來自 `work_bridge._builder_binding`（約 1892 行）。
  - **路徑 B**（autosync 後沿用舊 review，符合 #1311「只重跑 verify」的設計；現場 `system-deploy-ops-defects`、`worktree-containment-authority`）：ship 拋出 `delivery requires one canonical review evidence job`，來自 `work_bridge._workflow_evidence_envelope`（約 2660、2677 行）。
- 2026-10-07 的 pin 中，`"openspec-archive"` 作為來源判定共出現 14 處：`work_bridge.py`（約 1892、2660、2677）、`work_actions.py`（約 5045、5152）、`registry.py`（約 5870、6086）、`manager.py`（約 4143、4195、5736、9930、9941、9957、9967）。
- 不放寬 exact-candidate 原則：交付的必須是 verify 過的 candidate，review 結論要能追溯到它。

## Tasks

- [x] **T1 RED**：端到端重現兩條路徑在交付端的失敗：A 的 `delivery requires the reviewed exact-candidate builder job`、B 的 `delivery requires one canonical review evidence job`。
- [x] **T2 全面稽核**：逐一檢查上列 14 處（以及任何以 `workflow_phase == "build"`／`persona == "builder"` 判定 candidate 來源的地方），凡是語意屬於「candidate 的來源 job」者，一律改用 #1345 的共用 helper；不屬於的，在 PR 中逐處說明理由。
- [x] **T3 選定路徑 B 的語意**（二選一，在 PR 中寫明理由）：(a) autosync 後一律重跑 code-review；或 (b) 交付端接受「review 針對 autosync 前的 candidate，且新 candidate 是它的 autosync 後代（candidate 自身 diff 不變）」，並在 evidence 記錄這條鏈。review 與交付兩端必須一致。
- [x] **T4 端到端到合併**：A 與 B 兩條路徑都要有測試，一路走到 ship 的 merge 判定（不能只到 review）；#1345 的行為不退化。
- [x] **T5 設計說明**：更新 #1311 相關文件，寫明 autosync 後重跑哪些卡，以及「candidate 來源 job」的單一定義。
- [x] **T6 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
