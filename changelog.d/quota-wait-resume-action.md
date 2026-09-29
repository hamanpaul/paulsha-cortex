### Fixed

- **#1202 額度等待手動恢復建議**：只有帶 retry-eligible `quota-admission-insufficient` receipt 的 run 才投影 `resume`；claim、status 與 Monitor 提示一致，額度仍不足時沿用原等待 receipt。
