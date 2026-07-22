---
status: accepted
work_item: porcelain-skeleton
---

# porcelain-skeleton Rollout Specification

B1 落地後的驗收與後續批次接軌注意事項（補充規格；主規格見 porcelain-skeleton-spec.md）。

## Requirements

### 落地驗收

B1 merge 後 SHALL 以 `pipx install --force` 重裝並實測：`cortex --help` 顯示 `--version` 且（空註冊表下）無 porcelain 區段；既有命令行為不變（抽測 `status`/`list`/`doctor --json`）。

### 後續批次接軌

B2+ 各家族 SHALL 只透過 `_FAMILY_MODULES` 清單與自身模組登記，MUST NOT 直接修改 `cli.py` 的路由 if-chain 或靜態 help 主體。
