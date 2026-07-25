---
status: accepted
work_item: fix-git-runner-cwd
---

## Goals

讓 cortex 派工鏈的 git 呼叫與 manager daemon 的行程 cwd 無關，使從 systemd 啟動的 manager daemon（cwd 非 repo root）能成功 fanout，而非僅靠 operator 手動 `WorkingDirectory` override workaround。

## Why

dogfood 實機驗收（#177）發現：`dispatcher._default_git_runner` 直接 `subprocess.run(["git", ...])` 不帶 `-C`，依賴行程 cwd；`cortex install service` render 的 `cortex-manager.service` 無 `WorkingDirectory` → systemd daemon cwd=`$HOME` → dispatch 鏈上所有 git 呼叫失呼叫失敗。歷史 dispatch 從未從 systemd daemon 成功（僅手動/腳本 cwd 正確時跑成）。這是後續 driving-cortex skill（#177）可信操作的前置。

## What Changes

- `coordinator/dispatcher.py`：`_default_git_runner` 改 `git -C <repo_root>`（`paths.repo_root()`），消除 cwd 依賴。
- installer：`cortex-manager.service` 與 `cortex-monitor.service` 模板加 `WorkingDirectory=<repo_root>`，defense in depth。

## Capabilities

### Modified Capabilities

- `coordinator-dispatch`: 派工鏈 git 執行 cwd 無關契約——顯式 `git -C repo_root` + installer `WorkingDirectory`。