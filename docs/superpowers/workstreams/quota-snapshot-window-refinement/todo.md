---
status: accepted
work_item: quota-snapshot-window-refinement
---

# quota ledger 同值 snapshot 應採用 window 資訊較完整的觀測

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1099`。
- 範圍：`paulsha_cortex/coordinator/quota_ledger.py` 的 `_same_snapshot_value()` 與其兩個呼叫端
  （file ledger `QuotaEventLedger.append_observation` 的 duplicate 判定，以及
  `quota_shadow._MemoryLedger` 的同名判定），加上 `quota_shadow` 投影中 terminal usage 依 window
  epoch 扣減的既有邏輯（只讀不改語意）。
- 不動：snapshot 量值不同時的 conflict 判定、`usage_delta` 等累加型事件的完整 digest 判定、
  observation／binding／descriptor 的 schema、reservation 與 admission 模組。
- 不新增 durable schema 版本；若需要新的 ledger 事件種類，只能是加法，且舊 reader 讀到時的行為要
  在 changelog 寫明。

## 現場證據

#836 對抗審查第九輪（copilot gpt-5.4 唯讀）：同一語意 key（pool／window／observed_at／metric）
下，先記一筆沒有 `resetsAt`（`window_instance` unknown）的 remaining snapshot，再收到同值但帶
`resetsAt`（可推導 window epoch）的 snapshot，第二筆被 `_same_snapshot_value()` 當成 duplicate 丟掉；
之後同窗口的 terminal usage 仍卡在 `window-epoch-unknown`，投影餘額不會更新。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_quota_snapshot_window_refinement_1099.py`，file ledger 與
      `_MemoryLedger` 各一組：（a）先無 window instance、後同值有 window instance 的同時點 snapshot，
      之後同窗口的 terminal usage 必須能扣減（現行 RED：仍為 `window-epoch-unknown`）；（b）同時點
      量值不同仍為 conflict；（c）順序相反（先完整、後缺 window）時不得以較不完整者覆蓋；（d）完全相同
      的重送仍為 duplicate、不重複扣減。
- [ ] **T2 source**：duplicate 判定納入 window 資訊完整度——同值但新紀錄提供了既有紀錄缺的 window
      instance 時，視為 refinement 並被採納（寫入 ledger 或以加法事件記錄），投影改用較完整者；
      不得因此產生 conflict，也不得讓較不完整的後到紀錄覆蓋較完整者。
- [ ] **T3 docs**：`changelog.d/<branch-slug>.md` 與 `CHANGELOG.md [Unreleased]`，README 若有描述
      duplicate 規則一併更新。

## 驗收

- [ ] 先無 `resetsAt`、後同值有 `resetsAt` 的同時點 snapshot，之後同窗口的終局 usage 能正確扣減。
- [ ] 同時點值不同仍為衝突。
- [ ] 既有 `tests/test_quota_observation_refine_836.py`、`tests/test_quota_collectors_836.py` 與全套
      pytest 通過。
