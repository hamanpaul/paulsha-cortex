---
status: accepted
work_item: status-work-id-label
---

# cortex inspect status 摘要行以 work_id 標示設計

## Decisions

### D1 一個 helper，四個呼叫點

四種摘要行各自組標示字串，規則已經開始分歧（`provider_failure` 只看 `slice_id`，其他看 `run_id` 再看 `slice_id`）。改成同一個私有 helper `_entry_label(entry, *, fallback_keys)`，呼叫端只指定沒有 work_id 時的備援順序，這樣規則只寫一次。

### D2 保留 run_id

只印 work_id 會失去 run 的辨識：同一個 work_id 可能有多個世代的 run。所以格式是 `<work_id> (<run_id>)`，兩者都看得到。

### D3 無 work_id 時位元組相容

沒有 `work_id` 的條目，輸出必須與現行一模一樣，既有依賴這些字串的測試與 operator 腳本才不會壞。

### D4 測試做法

直接用組好的 status dict 呼叫 `_print_status`，用 `capsys` 擷取輸出比對。不需要啟動 daemon。

### D5 Sizing

只改一個 production 模組，不碰 state，`domain_breadth: 0`、`state_consistency: 0`，屬於小票。
