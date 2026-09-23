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
  "probe_failure": {
    "stage": "<stage>",
    "returncode": 1,
    "error_kind": "<kind>",
    "main_head": "<full object id or null>"
  }
}
```

`probe_failure` 可為 null。`candidate` 必須是 40 或 64 位 hex full object id；`main_head` 與 failure `main_head` 可為 null，否則同為 full object id 且彼此相等。傳入 valid M 後的 failure 必須保留 M。`conflict_paths` 是 JSON string array，逐項完整保存，不設 200 字欄位上限、不合併成一個字串。Git object format 與是否為 commit object 由 probe/action 層按 repository 驗證；schema layer 只驗完整 hash 語法及欄位一致性。

`DiagnosticReason` schema version 3 在原 `context` 下序列化這個 reserved object，保留其餘 legacy string 欄位與既有上限。Reader 接受 v1/v2，正規化為 v3；v1/v2 或舊 caller 的 `context.main_sync` string 仍視為 legacy string，不能被誤讀為 typed object；新 typed writer 必須使用專用參數 `main_sync_context`，同一筆 reason 不可同時帶 legacy string 與 typed object。任何無法辨識或不完整的 main_sync object fail closed；不可透過 `**context` 將 arbitrary mapping stringified。

此票只定義 reason model 與 round-trip；Manager 在同一 WorkflowRun update 寫入 reason/context、registry read-back、actions 使用 context、budget/reset refusal writer 由後續 descendants 驗收。
