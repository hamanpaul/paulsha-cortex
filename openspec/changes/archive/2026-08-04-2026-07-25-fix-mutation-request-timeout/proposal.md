---
status: accepted
work_item: fix-mutation-request-timeout
---

## Goals

讓 cortex CLI 的 mutation request poll timeout 依 request 類型分級，並在逾時改回傳 pending 結果含追蹤指引，消除「成功派工被誤報失敗→重試→撞 worktree already exists→slice failed 死路」的連鎖。

## Why

dogfood 實機驗收（#177）發現：`_submit_mutation_request` 的 5s timeout 小於 fanout 真實耗時（建 worktree + pin inputs + headless launch 常 >5s）；daemon 背後成功但 CLI 回報失敗，operator 重試撞 `worktree target already exists` 並連鎖進入 slice failed 死路，且成功時 CLI 拿不到 dispatched job 資訊。

## What Changes

- `coordinator/cli.py`：mutation request poll timeout 依 req_type 分級（fanout/tick ≥60s、complete/work/run ≥30s、其他 5s）；逾時改回傳 pending 結果含 req_id 與追蹤指引，exit code 區別 pending/失敗。

## Capabilities

### Modified Capabilities

- `coordinator-dispatch`: 派工 request timeout 契約——分級 timeout 與 pending 語意。