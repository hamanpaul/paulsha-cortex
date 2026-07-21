---
status: accepted
work_item: porcelain-skeleton
---

# porcelain-skeleton Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_registry.py` 與 `tests/test_cli_porcelain_routing.py`：註冊表契約（register/重名 raise/load_commands 冪等）、fake command 路由與退出碼、`--help` 含 `--version`、空註冊表不出現 porcelain 區段；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/__init__.py`：`PorcelainCommand` dataclass、`COMMANDS`、`register()`、`load_commands()`（`_FAMILY_MODULES` 空 tuple）。
- [ ] `paulsha_cortex/cli.py`：`_USAGE`/`_HELP` 補 `--version` 行；`--help` 分支動態附加非空 porcelain 區段；coordinator 透傳前查 `COMMANDS` 分派。

### 3. 同步與驗證

- [ ] 新增 `changelog.d/porcelain-skeleton.md` fragment，並在 `CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-skeleton` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠（含 `test_zero_dependency_runtime`）；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-skeleton/tasks.md` 的對應項並以 conventional commit 提交（不得改動本 plan 檔）。
