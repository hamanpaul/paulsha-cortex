### Fixed
- **archive gate 不再把 R-22 advisory WARN 視為阻斷**：`policy_check` 的 doc reference gate 現在只以 return code 判定，避免既有 diff-aware R-22 advisory WARN 讓所有 archive change 永遠卡在 `doc-reference-invalid`。
