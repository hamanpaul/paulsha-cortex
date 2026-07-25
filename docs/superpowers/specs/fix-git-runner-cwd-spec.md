---
status: accepted
work_item: fix-git-runner-cwd
---

# fix-git-runner-cwd Specification

#177 前置：修正 #99——`dispatcher._default_git_runner` 依賴行程 cwd，使 systemd 啟動的 manager daemon（cwd 非 repo root）派工鏈上所有 git 呼叫失敗。目標讓 git 執行與 daemon cwd 無關。

## Requirements

### R1 git runner 顯式指定 repo root

`paulsha_cortex/coordinator/dispatcher.py::_default_git_runner` MUST 以 `git -C <repo_root> <args>` 執行，`<repo_root>` 取自 `paulsha_cortex.config.paths.repo_root()`（`PSC_REPO_ROOT` 或 cwd 解析）。MUST NOT 依賴行程 cwd。既有 `GitRunner` 簽名（`Callable[[list[str]], str]`）保持不變；相對路徑參數在 `-C repo_root` 下解析行為與「cwd==repo_root」一致。

### R2 installer 模板含 WorkingDirectory

`cortex install service` render 的 `cortex-manager.service` MUST 含 `WorkingDirectory=<repo_root>`（unit 欄位或 drop-in），使 systemd 啟動的 daemon 即使無 operator 手動 override 亦 cwd 正確。此為 defense in depth，與 R1 並行（任一缺失仍可派工）。

### R3 限制

- stdlib-only；TDD（mock `subprocess.run` 斷言 argv 含 `-C` 與 `paths.repo_root()`；installer render 含 `WorkingDirectory=`）。
- 不得改變既有對外 CLI `--json` envelope schema 字串。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。
- 不處理 #152/#100（屬另兩個 work item）。