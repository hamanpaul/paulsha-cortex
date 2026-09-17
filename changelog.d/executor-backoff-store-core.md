- **#850 executor backoff store RED coverage**：新增 `tests/test_executor_backoff.py`
  focused regression，鎖定 strict reader 遇到 corrupt persisted bytes 時必須回報
  `unknown` observation 並附 diagnostics、不可降級成 `missing`；現況因
  `paulsha_cortex.coordinator.executor_backoff` 尚未實作而維持 RED，bounded
  store／immutable fold／lane 接線仍待後續卡片。
