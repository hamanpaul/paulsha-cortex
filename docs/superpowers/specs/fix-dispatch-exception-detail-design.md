---
status: accepted
work_item: fix-dispatch-exception-detail
---

# fix-dispatch-exception-detail Design

## Decisions

- `DispatchReadyError.__str__` 組裝 `f"dispatch_ready failed for slice(s): {ids}; details: " + "; ".join(f"{sid}: {type(e).__name__}: {str(e)[:N]}" ...)`，每則 message cap（如 200 字）避免 flood；完整 traceback 仍由 manager.log 記錄。
- tick handler 把 `DispatchReadyError.errors` 轉 `list[dict]` 寫入 response `errors`；`jobs` 保留不變。
- manager.log 加 `_ts_log(line)` helper 前置 `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ ")`；既有逐行內容不變，故既有 parse 相容。
- 不改 `DispatchReadyError` 的 `__init__` 簽名（`errors`、`jobs` 已存在）。