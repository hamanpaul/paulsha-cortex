---
status: accepted
work_item: add-cortex-version-flag
---

# add-cortex-version-flag Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_cli_version.py`：斷言 `cortex --version`（`main(["--version"])`）輸出版本字串且 exit 0；先確認現況 RED。

### 2. 實作

- [ ] `paulsha_cortex/cli.py` 的 `main()` 在子命令路由前處理 `--version`：`importlib.metadata.version("paulsha-cortex")`，`PackageNotFoundError` fallback `0.0.0+unknown`；輸出 `cortex <version>`。

### 3. 同步與驗證

- [ ] `changelog.d/86-cortex-version-flag.md` fragment 與 `CHANGELOG.md [Unreleased]` 同步（Added）。
- [ ] README 的 CLI 用法段落補 `--version` 一行。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
