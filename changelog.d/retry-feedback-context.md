# retry-feedback-context

**#606：重派 prompt 機械附上前次採信失敗證據——無回饋的重試不再是決定論的重複。**

## 現場

run `workflow-7812abefede9d9b5d601`（#501 dogfooding）的 subagent-build，job 492 與
493：builder（codex／gpt-5.6-luna）兩次自稱 `pytest: passed`，Manager 的 gate ledger
兩次獨立重跑抓到**同一個**失敗
（`tests/test_workflow_registry.py::test_v1_migration_creates_immutable_backup_and_isolates_legacy_records`），
兩次 `GateContradictionError` 逐字相同。

根因不是重派錯誤。`retry-card`（#545／#569）刻意用**原卡 prompt** 重派——契約不可
竄改是對的——但 prompt 沒有任何通道攜帶「上一次為什麼被拒」。builder 每次都跑
focused tests 看到綠、自稱全套 passed，ledger 每次抓包；沒有回饋，重試就只是燒 job。

## 修法（比照 #521／#540「prompt 由判準／證據機械生成」）

### 1. `retry_context`：重派 prompt 的證據回饋通道

新增 `manager._workflow_retry_context()`，輸入就是 `_dispatch_workflow_card` 既有的
`matching`（同一張卡的先前 job；verify／review 另以 candidate 定錨），輸出機械組出
一個 `retry_context` 區塊塞進 dispatch contract：

- **採信錯誤類別＋canonical 訊息**（`acceptance_error`）：不讀任何敘事欄位
  （`run.needs_human_reason` 在 `retry-card` 重置時依診斷 invariant 已被清空），而是
  對舊 job **重跑既有採信路徑的前兩段**——同一份 log、同一份 gate ledger、同一組判準
  函式，因此拿到的錯誤與當初 harvest 擲出的逐字相同。兩型都覆蓋：`GateContradictionError`
  （#606 現場）與「log 完全沒有 JSON envelope」（#569 現場）。
- **gate ledger 的 failed gates**（`failed_gates`）：名稱＋exit code＋截尾輸出。
  「哪些算 failed」複用 `terminal_contract._ledger_outcomes`（採信端判定矛盾用的就是
  它，exit_code 非 0 覆寫自述 status）——retry-context 不得另立第二份判準，否則 prompt
  會告訴 builder 一組跟採信不同的失敗集合。
- **明示語句**：prompt 散文段落多一句「attempt N was rejected by the Manager's own
  independent evidence … Reproduce that failure first, fix it, and only then complete
  this card」，並點明那份證據是 Manager 自產、不是前一次嘗試的自述。

內容**一個字都不取自模型輸出**（與 #540 的不可竄改性同一條紀律）；來源全是 Manager
在模型行程結束**之後**於自己掌控的 wrapper 內產生的 ledger 與自己的採信判準。

`retry-card` 與 daemon 的 forced retry 都收斂在 `_dispatch_workflow_card` 這唯一一個
prompt 組裝點，因此兩條路徑同時拿到回饋，不需要第二份實作。

### 2. 首派逐字不變

`_workflow_job_prompt(retry_context=None)` 是預設值；首派時 `matching` 為空 →
`_workflow_retry_context` 回 `None` → contract 沒有 `retry_context` 鍵、散文也不多一
個字，prompt 與本票之前**逐字相同**（有測試釘住，含走真正 dispatch 路徑的那條）。

### 3. 截斷上限（prompt 不得被 gate 輸出撐爆）

- `RETRY_CONTEXT_EVIDENCE_LIMIT = 2000`：**全體** failed gate `detail` 的合計預算，
  保留尾段（pytest 的 short summary 在最後，那才是可重現的線索；與
  `gate_ledger.run_gates` 自己的 `[-2000:]` 同向）。被截的項目帶 `detail_truncated`
  明示，不假裝完整。
- `RETRY_CONTEXT_MESSAGE_LIMIT = 600`：`GateContradictionError` 的訊息會把 ledger
  detail 內嵌進去，同樣要有上限（`message_truncated`）。

fail-soft：log／ledger 讀不到或壞掉時回空證據＋計數，**不得讓「讀不到舊證據」害死
一次合法重派**。

### 4. status 語意補上範圍紀律（issue 附帶觀察）

`terminal_schema.status_policy` 末段接上新的 `gate_ledger.gate_scope_honesty_hint()`：
明說「focused 綠 **不是** 宣告的 gate 綠」的推定不成立，而且**實際會被 Manager 重跑的
命令逐字出現在 prompt 裡**——與 #541 的 `allowed_names` 同一條機械生成紀律（值由
operator 的 `PSC_GATE_CMD_*` 宣告導出，不手寫第二份真實來源）。現場的行為模式
（run `084f...` 的 job 488、run `7812...` 的 492／493）過去在 prompt 裡隻字未提。

### 5. 與 #555 的接口

`retry_context` 帶 `attempt`（本次是這張卡的第 N 次派工）與 `redispatch_count`
（已重派過幾次），由這張卡已燒掉的 job 數機械導出。**本票不實作熔斷**，只把計數落到
prompt 與 retry-context 上，讓 #555 的 per-card 熔斷判準之後有一個既有的、機械的來源
可接。

## 變更檔案

- `paulsha_cortex/coordinator/manager.py`：新增 `RETRY_CONTEXT_EVIDENCE_LIMIT`／
  `RETRY_CONTEXT_MESSAGE_LIMIT`／`_retry_context_error_row`／
  `_prior_card_acceptance_error`／`_prior_card_failed_gates`／`_workflow_retry_context`；
  `_workflow_job_prompt` 新增 `retry_context` 參數（預設 `None`）與散文段落；
  `_dispatch_workflow_card` 在唯一的 prompt 組裝點注入。
- `paulsha_cortex/coordinator/gate_ledger.py`：新增 `gate_scope_honesty_hint()`。
- `tests/test_retry_feedback_context_606.py`：14 個測試（現場形狀 fixture、首派逐字
  不變、截斷上限、兩型採信錯誤、計數、status 範圍紀律、真正 dispatch 路徑、fail-soft）。
