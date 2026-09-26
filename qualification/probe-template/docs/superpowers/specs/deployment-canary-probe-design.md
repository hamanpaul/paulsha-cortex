---
status: accepted
work_item: deployment-canary-probe
---

# Deployment canary probe design

## Decisions

- 只在 `normalize_label` 內加一個空字串分支，不新增模組或相依。
- 以 `unittest` 撰寫測試，讓 `python3 -m pytest -q` 與 `python3 -m unittest` 都能收集。
