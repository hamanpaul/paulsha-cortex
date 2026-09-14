---
status: accepted
work_item: fix-agy-reviewer-json-schema
---

# Design: fix-agy-reviewer-json-schema

## Decisions

### D1. schema 單一來源

不新增第二份 schema。`build_agy_argv` 在 `review_only and json_envelope` 時呼叫既有
`_claude_review_json_schema(review_terminal_kind)`（函式名稱不改，避免無關 diff；可在 docstring 註明
「claude／agy reviewer 共用」）並 `argv.extend(["--json-schema", schema_str])`，插在 `--output-format json`
之後、`--model` 之前。

### D2. 參數與呼叫端

`build_agy_argv(..., review_terminal_kind: str | None = None)`：
- `review_only` 且 `review_terminal_kind is None` → `ValueError("agy reviewer terminal contract kind missing")`
- 非 `review_only` 且 `review_terminal_kind is not None` → `ValueError("agy terminal contract requires reviewer mode")`

`SubprocessLauncher` 現行 `if self._executor == "claude": builder_kwargs["review_terminal_kind"] = …`
改為 `if self._executor in {"claude", "agy"}:`（stdin prompt 仍只有 claude）。

### D3. probe 與其他 lane 不變

`json_envelope=False`（capability probe，`#670`）與 planner／builder lane 完全不碰：以既有測試的 argv 期望值
作為回歸鎖。

### D4. Manager 端防禦性正規化

`terminalize_workflow_job` 在 verification 分支、非通過狀態檢查之後、schema 檢查之前：
```python
details = raw.get("details")
if isinstance(details, str) and details.strip():
    raw = {**raw, "details": {"text": details}}
    logger.warning("verification terminal details was a string; normalized to object (#880)")
```
只針對 verification terminal；review terminal 不動（其 findings 結構由 schema 嚴格把關）。

### D5. 不在範圍

reviewer 獨立性候選為空時的 needs_human 訊息（另開票）、gemini 重複輸出 JSON 的解析寬鬆化（`--json-schema`
生效後即不再發生）、runtime pin 升版與 #879 run 的後續 retry（operator 手動）。
