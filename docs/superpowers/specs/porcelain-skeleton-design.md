---
status: accepted
work_item: porcelain-skeleton
---

# porcelain-skeleton Design

## Goals

讓 B2+ 七家族各自以獨立模組登記命令、互不修改共享路由檔，消除序列批次對 `cli.py` 的反覆衝突；同時把 `--version` 補進 usage/help（#120）。

## Decisions

### 註冊表形狀

`paulsha_cortex/porcelain/__init__.py`：`PorcelainCommand`（frozen dataclass：`name`、`summary`、`run: Callable[[list[str]], int]`）、模組層 `COMMANDS: dict[str, PorcelainCommand]`、`register(command)`（重名 raise ValueError）、`load_commands()`（依 `_FAMILY_MODULES` tuple 匯入家族模組使其自行 register；B1 為空 tuple）。

### 路由位置

`cli.py` 的 `main()`：在 `doctor` 分派之後、coordinator 透傳之前呼叫 `porcelain.load_commands()` 並查 `COMMANDS`；命中則 `return command.run(args[1:])`。lazy import porcelain 模組以維持啟動成本。

### Help 組裝

`_USAGE` 與 `_HELP` 靜態字串補 `--version` 一行；`--help` 分支在輸出 `_HELP` 前，若 `COMMANDS` 非空則附加「porcelain commands:」區段（name + summary 對齊排版）。B1 註冊表為空 → 輸出與現行加 `--version` 行後完全一致。

### 測試策略

新檔 `tests/test_porcelain_registry.py`：register/重名 raise/`load_commands` 冪等；`tests/test_cli_porcelain_routing.py`：注入 fake command 後 `main(["fake", ...])` 分派與退出碼透傳、未註冊名稱透傳 coordinator 行為不變、`--help` 含 `--version` 且空註冊表無 porcelain 區段。
