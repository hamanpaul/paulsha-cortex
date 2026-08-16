---
status: accepted
work_item: fix-dirty-recheck-idempotency
---

# fix-dirty-recheck-idempotency Todo

`#496`：slice 因 `candidate-worktree-dirty` 掛上 `needs_human` 後，manager 每個
tick 都會重跑同一份 verification，並**無條件**寫入一筆新的 `verification-failed`
action ＋ 一筆 evidence_history——即使 worktree、candidate、結果、證據位址
四者全部沒變。實測 5 秒 timer 下約 2 分鐘累積 **33 筆**重複紀錄，`jobs.json`
在等待 operator 期間無界成長。

## 現況查核（0816，對 main `48b0205`）

**缺陷仍完整成立。** 兩段程式碼構成迴圈：

1. `manager.py:1815-1853` —— `complete_tick` 開頭掃 `list_slices()`，對
   `state == "needs_human"` 且 current evidence summary 落在
   `{"candidate-worktree-dirty", "candidate-worktree-dirty-after-verification"}`
   的 slice **刻意**重跑 `verification_runner`。這個 recheck 行為本身是設計意圖
   （讓 operator 清乾淨 worktree 後能自動脫困），**不是缺陷、不要移除**。
2. `manager.py:1845-1851` —— 重跑結果經 `_validate_result_evidence()` 後，
   **無條件**呼叫 `_apply_verification_result(registry, slice_item["slice_id"], validated_ev)`。
   沒有任何「與現況比對」的閘門。
3. `manager.py:386-409` —— `_apply_verification_result()` 一律
   `registry.record_action(...)`（`:395`）＋ `registry.update_slice(...)`（`:404`），
   本身不具冪等性，也不該由它自己承擔冪等責任（它是「套用一個真實轉換」的原語）。

因此「沒變化」與「有變化」走完全相同的寫入路徑，每 tick 各記一筆。

**放大效應**：`_apply_verification_result()` 同時會把證據 hash 寫進
`slice_row["verification"]["hash"]`（見 `#501`），所以這個迴圈**每 tick 還會
重覆污染 contract hash**。`#501` 0812-18:05 留言即由此現場觸發。本張不修那條，
但實作者需知道兩者共用同一個原語。

### 既有測試覆蓋

`tests/test_pre_candidate_recovery.py:218`
`test_candidate_worktree_dirty_reevaluation_on_tick` **只覆蓋「結果有變」**
（dirty → verified，candidate 換新）並斷言轉換發生。**沒有任何測試斷言
「結果沒變時不得寫入」**——這正是本張要補的缺口。修復不得讓這個既有測試轉紅。

## Scope（明確邊界）

**本 work item 的主體是「recheck 結果未變時的寫入冪等」，不是「recheck 該不該存在」，
也不是「terminal job 重播治理」。**

- 要做：在套用 recheck 結果**之前**，與當前狀態比對——至少涵蓋 verification
  evidence hash、`state`、`gate_state`、summary、candidate、
  `current_evidence_refs` 六者。完全相同就直接 return，**不 `record_action`、
  不 append `evidence_history`、不 `update_slice`**。
- 要做：worktree 真的變乾淨、verification 結果真的改變時，**恰好記錄一次**
  真實轉換（不得因為新增比對而漏記，也不得記兩次）。
- 要做：保持 fail-closed——證據不可讀、schema 不合法、內容不一致時，
  維持現行的保守行為，**不得**因為「比對不出差異」就把壞證據當成「沒變化」放行。
- 可做：若確有觀測需求，另立 `last_rechecked_at` 之類的欄位承載「我有在輪詢」
  這個事實，**與 lifecycle history 分離**（`#496` 建議驗收最後一條）。
  這是 optional，不做也可驗收。
- **不要做**：移除或停用 `manager.py:1815-1853` 的 dirty recheck。它是 operator
  清理後的自動脫困路徑，移掉會製造新的人工卡點。
- **不要做**：把冪等閘門塞進 `_apply_verification_result()` 內部去「一次修好所有
  呼叫點」。該函式另有五個呼叫點（`manager.py:1851`、`1960`、`2046`、`2057`，
  以及 `1623`／`1638` 的 slice action 路徑），它們的語意是「一個真實轉換剛發生」，
  在原語層加靜默 skip 會改變那些路徑的行為並可能遮蔽真實轉換。
  **閘門應加在 recheck 呼叫點（本張現場）**；若實作者評估後仍主張下沉到原語層，
  必須為上述每個呼叫點補測試證明語意不變。
- **不要做**：修 `#501` 的 contract／evidence hash 欄位混用、或 `#497` 的
  superseded terminal job 重播。本張驗收**不得依賴**那兩張是否已落地。

## Tasks

- [ ] **冪等閘門**：dirty recheck 套用前與當前 verification hash／`state`／
      `gate_state`／summary／candidate／evidence refs 比對；一致即 no-op 返回
- [ ] **真實轉換恰好一次**：worktree 轉乾淨後，脫離 dirty 狀態只產生**一筆**
      action ＋ 一筆 evidence_history
- [ ] **fail-closed 保留**：證據不可讀／schema 不合法／與 registry 現況不一致時，
      不得被冪等閘門誤判為「沒變化」而靜默略過
- [ ] **（optional）觀測分離**：如需呈現「仍在輪詢」，以獨立欄位承載，
      不寫進 lifecycle action／evidence_history
- [ ] **測試**：
      - 對未變的 dirty worktree 連續呼叫 `complete_tick` N 次（N ≥ 5），
        斷言 action 數與 evidence_history 數**完全不變**（`#496` 建議驗收第 5 條）
      - tick 之間把 worktree 清乾淨，斷言**恰好一次**脫離 dirty 的轉換
        （`#496` 建議驗收第 6 條）
      - 既有 `test_candidate_worktree_dirty_reevaluation_on_tick` 維持綠燈
      - 證據檔被破壞／不可讀時仍 fail-closed，不因冪等閘門而放行

## 現場紀錄（供實作者參考）

- issue `#496` 首則：slice `task-3-private-repo-and-forbidden-documentation-scan-build`、
  candidate `0c9faff9…`，5 秒 timer 下
  `2026-08-12T14:12:06.381861Z` → `14:14:02.820132Z` 約 **116 秒累積 33 筆**
  `verification-failed` action ＋ 33 筆 evidence_history，worktree／candidate／
  結果／證據位址全程未變
- issue 首則的 root cause 段已精準點出
  「`_apply_verification_result` always record_action and update_slice.
  There is no comparison against the current verification hash, status, summary,
  candidate, or refs」——0816 複查確認這段描述與 main 現況逐字相符
- 本張與 `#501`／`#497` 同屬「桶C slice 迴圈家族」。三者的關係：
  `#497` 是重播**來源**、`#496` 是 recheck **迴圈**、`#501` 是兩者共用的
  **污染原語**。三張各自獨立可驗收，不得合併修
