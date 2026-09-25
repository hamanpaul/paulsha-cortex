---
status: accepted
work_item: diagnostic-main-sync-context
domain_breadth: 0
state_consistency: 1
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 5
sizing: yellow
---

# DiagnosticReason main-sync structured context 規格

## Requirements

本 work item 僅改 `paulsha_cortex/coordinator/diagnostics.py`，提供 durable WorkflowRun reason 可承載 main-sync recovery object 的 schema/API。現有一般 `DiagnosticReason.context` 保持 string-to-string；只有 reserved key `context.main_sync` 可放 typed nested value，禁止 `str(value)`、單行化或 200 字截斷。

`MainSyncContext` 的 JSON shape：

```json
{
  "candidate": "<full object id>",
  "main_head": "<full object id or null>",
  "conflict_paths": ["<full path>", "..."],
  "repair_kind": "clean-behind | changelog-top-insert | other-conflict | unavailable",
  "skipped_reason": "<stable reason or null>",
  "failure": {
    "stage": "<stage>",
    "returncode": 1,
    "error_kind": "<kind>",
    "main_head": "<full object id or null>"
  }
}
```

`failure` 可為 null。`candidate` 必須是 40 或 64 位 hex full object id；`main_head` 與 `failure.main_head` 可為 null，否則同為 full object id 且彼此相等。傳入 valid M 後的 failure 必須保留 M。`conflict_paths` 是 JSON string array，逐項完整保存，不設 200 字欄位上限、不合併成一個字串。Git object format 與是否為 commit object 由 probe/action 層按 repository 驗證；schema layer 只驗完整 hash 語法及欄位一致性。

Wire contract 的巢狀失敗欄位固定使用 `context.main_sync.failure`，並與 live issue AC 的 `failure.stage`、`failure.returncode`、`failure.error_kind`、`failure.main_head` 路徑一致；其值使用 #987 producer `MainSyncProbeFailure` 的 typed shape。`returncode` 為整數或 null；`stage`、`error_kind` 是 #987 producer vocabulary 的非空字串。`repair_kind` 是 `clean-behind`、`changelog-top-insert`、`other-conflict` 或 `unavailable` 之一；`skipped_reason` 為 null 或非空穩定分類碼，包含後續 writer 會使用的 `repair-budget-exhausted`、`registry-reset-refused`。`conflict_paths` 每項是非空字串，保留空白及換行原樣。

`DiagnosticReason` schema version 3 在原 `context` 下序列化這個 reserved object，保留其餘 legacy string 欄位與既有上限。Reader 接受 v1/v2，正規化為 v3；v1/v2 或舊 caller 的 `context.main_sync` string 仍視為 legacy string，不能被誤讀為 typed object；新 typed writer 必須使用專用參數 `main_sync_context`，同一筆 reason 不可同時帶 legacy string 與 typed object。任何無法辨識或不完整的 main_sync object fail closed；不可透過 `**context` 將 arbitrary mapping stringified。

此模組現況為 schema v2：`context` 是 string-to-string，且 `diagnostic_reason()` 的一般值處理會單行化、轉字串並截斷至 200 字。v3 只替 reserved `main_sync` 增加 JSON-native typed shape；一般 context 的 string-to-string、200 字值上限、16 key 上限均保留。一般入口收到 mapping/list 時拒絕，不能把它們 stringify 成一般 context。舊 v1/v2 的 `main_sync` string 繼續作為一般字串讀取；舊版 payload 若在該位置出現 nested object 則 fail closed。

此票只定義 reason model 與 round-trip；Manager 在同一 WorkflowRun update 寫入 reason/context、registry read-back、actions 使用 context、budget/reset refusal writer 由後續 descendants 驗收。
