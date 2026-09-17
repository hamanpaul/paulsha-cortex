- **#850 executor backoff store strict reader**：新增
  `paulsha_cortex/coordinator/executor_backoff.py` 與 package export，先交付最小
  strict reader surface：缺檔明確回 `missing`，corrupt persisted bytes／UTF-8／JSON
  ／non-object payload 與 read faults 皆回 `unknown` 並附 diagnostics，讓
  `tests/test_executor_backoff.py` focused regression 轉綠；schema v1／atomic RMW／
  bounded capacity／immutable fold／reconciliation lane 仍待後續卡片。
