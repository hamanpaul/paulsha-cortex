---
status: accepted
work_item: engineering-outcome-contract
---

# engineering-outcome-contract Specification

#275：發布一份 canonical engineering outcome contract，讓外部 learning systems（含
Hippo）可以在不侵入 cortex 內部狀態機的前提下，讀到「一個 work item 的工程結果最終
落在哪裡」這件事的可重播、append-only 紀錄。

## 背景

cortex 既有的終局狀態只活在 `WorkflowRun`（`registry.py`）裡，且會被 in-place 覆
寫——`_manager_update_workflow_run` 每次呼叫都用 `dataclasses.replace` 產生新
`WorkflowRun` 蓋掉舊值。外部系統若要學習「哪些 work item 出貨了、哪些被放棄了」，
只能輪詢當前快照，拿不到歷史序列，也無法區分「這是第一次觀察到 shipped」與「這是
同一個 shipped 又被重複回報一次」。

issue 原始草案假設 outcome 有六種：`shipped`／`verified`／`rejected`／`failed`／
`abandoned`／`rolled_back`。查證 `registry.py` 現況後發現這與既有 lifecycle 不對
齊：`WorkflowRun.status` 只有 `ongoing`／`done`／`superseded` 三個合法值，run 級沒
有 `rejected`／`failed`／`rolled_back` 的既有終局轉換點——這些狀態目前是靠
`needs_human` facet 卡住、由人工再 abandon 或恢復，不是一個「run 已終結且不可逆」
的自動轉換。

## Goals

- 提供一份 append-only、可重播（replay）的 outcome outbox，讓外部系統能訂閱「這個
  work item 最終怎麼結束」而不需要理解 `WorkflowRun` 的內部覆寫語意。
- outcome 必須在既有的終局轉換（`status="done"`／`status="superseded"`）**之前**
  durable 寫入，讓「outcome 已記錄」與「WorkflowRun 已終結」之間沒有可觀測的
  fail-open 窗口（後者失敗，前者仍留下可稽核紀錄；不會出現前者遺漏但後者已終結）。
- daemon crash 或 request retry 造成的重複 tick 不得產生重複 outcome record。
- 誠實表達目前拿得到的 execution provenance，不假造沒有的資料（見 R4）。

## Requirements

### R1 canonical outcome envelope

SHALL 定義單一版本化的 outcome envelope（`schema` / `schema_version`），供
`shipped`／`abandoned`（v1 有 emitter）與 `rejected`／`failed`／`rolled_back`（v1
保留值）共用同一份 schema。envelope MUST 含 `outcome_id`／`emitted_at`／`repo`／
`work_id`／`workflow_run_id`／`slice_id`／`jobs`／`candidate`／`outcome`／
`reason_code`／`verification`／`review`／`execution_provenance`／
`supersedes_outcome_id`。`jobs` MUST 展開成 per-job 物件（`job_id`／`card`／
`persona`／`workflow_phase`），不得是扁平字串陣列。

### R2 outcome 詞彙縮限與可擴張性

v1 SHALL 只在真正存在既有終局轉換點的兩處 emit：`_ship_action` 的
`status="done"` 對應 `outcome="shipped"`；`_abandon_action` 的
`status="superseded"` 對應 `outcome="abandoned"`。`rejected`／`failed`（run 級）／
`rolled_back` MUST 留在 schema 的合法值集合內（供未來擴張既有終局轉換點時沿用同一
schema），但 v1 MUST NOT 為了湊滿六種狀態去發明新的 run-level 終局狀態或改動
`WorkflowRun.status` 的既有三值域。

### R3 idempotency

SHALL 提供決定性的 `outcome_id` 推導——由 `workflow_run_id`／`outcome`／呼叫端提供
的「該次終局轉換內容位址 digest」（`attempt_digest`）組成。同一次終局轉換（例如同
一次 merge、同一份 abandon evidence）不論被重複 tick 幾次，MUST 得到相同
`outcome_id`；outbox 的寫入層 MUST 依 `outcome_id` 去重，daemon restart 或 request
retry 造成的重複呼叫 MUST NOT 產生第二筆 record。

### R4 execution provenance 的誠實邊界

`execution_provenance.session_refs` MUST NOT 宣稱與 executor 自身 session 的
exact match——Cortex job record 目前沒有存 executor session UUID（只有
`session_name`、`log_path`、`pane`）。SHALL 改以 job 的 `session_name` 集合＋
`workspace_root`＋run 的 `created_at`/`updated_at` 時間窗提供 correlation hint，
並顯式標記 `correlation_confidence: "weak"`。捕捉真正的 executor session id 屬於
`dispatcher.py` 的另一項變更，是本次的 follow-up，不在本次範圍內。

### R5 終局轉換順序

`_ship_action`／`_abandon_action` MUST 在呼叫 `_manager_update_workflow_run`／
`_manager_abandon_workflow_run` 之前完成 outcome 的 durable 寫入（`OutcomeStore.
append` 成功回傳）。若 outcome 寫入失敗，terminal transition MUST NOT 執行
（維持現有的例外傳播行為即可，不需額外補償邏輯）。

### R6 唯讀消費 surface

SHALL 提供 `list_outcomes`／`show_outcome`／`replay_outcomes` 三個唯讀函式（不改
動任何既有狀態），並透過 `cortex outcome list/show/replay` CLI 子指令暴露；子指令
MUST NOT 經過 manager daemon control queue（純讀取，不需要單一 writer 序列化）。

## 非目標

- 不捕捉 executor 自身的 session UUID（見 R4；屬 dispatcher.py 的 follow-up）。
- 不新增 `WorkflowRun.status` 的第四個合法值，也不新增 run-level `failed`
  的自動終局轉換路徑（那是另一張更大的票——需要先定義「run 級 failed」在
  lifecycle 裡實際對應哪個轉換點）。
- 不處理 outbox 的跨機器同步或推播；本次只定義本機 append-only 檔案格式與唯讀
  消費 API，訂閱／推播交給消費端（例如 Hippo）自行輪詢或 tail。

## 驗收面

- `_ship_action` 完成一次 ship 後，對應 repo 的 outcome store 有一筆
  `outcome="shipped"` record，且早於／獨立於 `WorkflowRun.status="done"` 寫入。
- `_abandon_action` 完成一次 abandon 後（含 `run.status=="superseded"` 的重入分
  支）有一筆 `outcome="abandoned"` record；同一個 run 的重入呼叫不產生第二筆。
- 非法 outcome envelope（缺欄位、非法 outcome 值、非法 id 格式）一律 fail closed
  並保留 machine-readable 的 reason／validation_path。
- Hippo 未安裝時，本模組的建構、驗證、寫入、讀取全部行為不變。
