---
status: accepted
work_item: porcelain-skeleton
---

# Design

## Decisions

- **註冊表下放**：家族模組於 import 時 `register()`；`load_commands()` 依 `_FAMILY_MODULES` 清單匯入（B1 空 tuple），B2+ 每家族只改自己模組與清單一行。
- **路由插點**：`main()` 在既有命令分派之後、coordinator 透傳之前查 `COMMANDS`——未註冊名稱行為與現行完全一致（fail-open 到 coordinator）。
- **help 動態區段**：靜態 `_HELP` 保持可讀，porcelain 區段僅於非空時附加；`--version` 進靜態字串（#120）。
- **重名 fail-fast**：`register()` 對重複名稱 raise，防兩家族搶同名。
