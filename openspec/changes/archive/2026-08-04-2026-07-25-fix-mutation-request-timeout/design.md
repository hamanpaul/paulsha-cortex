---
status: accepted
work_item: fix-mutation-request-timeout
---

# fix-mutation-request-timeout Design

## Decisions

- 採分級 timeout 表 + pending 語意，不重構為全非同步 submit（保留 `--wait` 同步契約）。
- 逾時回傳 submitted-but-not-done 結果（req_id + 追蹤指引），exit code `EXIT_SUBMITTED_PENDING` 區別 `EXIT_FAILURE`。
- 既有成功/失敗路徑不變；不改 `--json` envelope schema 字串。