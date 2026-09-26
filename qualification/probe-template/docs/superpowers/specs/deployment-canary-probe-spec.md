---
status: accepted
work_item: deployment-canary-probe
---

# Deployment canary probe spec

## Requirements

- `normalize_label(value)` 去除 `value` 前後空白後回傳。
- 去除空白後為空字串時，回傳 `"unnamed"`。
- 既有行為（非空標籤只去除前後空白）不變。
