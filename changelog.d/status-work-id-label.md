修正 `cortex inspect status` 的四種摘要行，優先顯示 `work_id` 並附帶 `run_id`，讓 operator 可直接對應工作項目。沒有 `work_id` 的舊條目與 `--json` 輸出維持不變。
