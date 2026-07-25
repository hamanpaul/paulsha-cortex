---
status: accepted
work_item: dispatch-reliability-batch
---

## Goals

讓 cortex 從 systemd 啟動的 manager daemon 能穩定派工，並消除三個已實證的派工鏈可靠性缺口：CLI 5s timeout 過短導致成功派工被誤報失敗（#152）、dispatch 失敗時底層例外被吞且 manager.log 無時間戳（#100）、git runner 依賴行程 cwd 使 systemd daemon 派工從未成功（#99）。

## Why

dogfood 實機驗收（#177）暴露：操作者被 5s timeout 誤導重試而撞 `worktree already exists`、連鎖進入 slice failed 死路；dispatch 失敗只剩 slice id 清單，底層例外與時間資訊全失，operator 須離線重演 debug；systemd daemon 因 cwd 耦合使 spec-based fanout 本機從未成功（僅靠手動 override workaround）。這三項是後續 driving-cortex skill（#177）可信操作的前置。

## What Changes

- `coordinator/cli.py`：mutation request poll timeout 依 req_type 分級（fanout/tick ≥60s、complete/work ≥30s、其他 5s）；逾時改回傳 pending 結果含 req_id 與追蹤指引，exit code 區別 pending/失敗。
- `coordinator/autonomy.py`：`DispatchReadyError` 訊息含 per-slice 例外摘要（cap 長度）；`jobs` 保留。
- `coordinator/manager_daemon.py`：tick handler 把 `DispatchReadyError.errors` 寫入 response `errors`；log 每行加 ISO-8601 前綴。
- `coordinator/dispatcher.py`：`_default_git_runner` 改 `git -C <repo_root>`。
- installer：`cortex-manager.service` 模板加 `WorkingDirectory=<repo_root>`。

## Capabilities

### Modified Capabilities

- `coordinator-dispatch`: 派工鏈可靠性契約——分級 timeout 與 pending 語意、失敗例外透傳與時間戳、cwd 無關 git 執行。