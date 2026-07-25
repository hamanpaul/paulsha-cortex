---
status: accepted
work_item: fix-dispatch-exception-detail
---

# fix-dispatch-exception-detail Design

## Decisions

- `DispatchReadyError.__str__` 組 per-slice `type: message` 摘要並 cap 長度，避免 flood；完整 traceback 仍由 manager.log 記錄。
- tick handler 把 `errors` 轉 list[dict] 寫入 response `errors`；`jobs` 保留。
- manager.log 採前綴 ISO-8601（行尾內容不變），保持既有 parse 相容。
- 不改 `DispatchReadyError.__init__` 簽名。