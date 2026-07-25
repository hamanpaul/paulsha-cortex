---
status: accepted
work_item: dispatch-reliability-batch
---

# dispatch-reliability Design

## Decisions

- 採分級 timeout + pending 語意，不重構為全非同步 submit（保留 `--wait` 同步契約）。
- `DispatchReadyError` 訊息組 per-slice `type: message` 摘要並 cap 長度，避免 flood；完整 traceback 仍由 manager.log 記錄。
- manager.log 採前綴 ISO-8601（行尾內容不變），保持既有 parse 相容。
- git runner 採 `git -C repo_root` 根因修復 + installer `WorkingDirectory` 並行（defense in depth）。
- 不引入新對外 CLI 子命令；不改 `--json` envelope schema 字串。