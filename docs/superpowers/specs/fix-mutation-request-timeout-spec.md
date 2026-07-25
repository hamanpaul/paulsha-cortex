---
status: accepted
work_item: fix-mutation-request-timeout
---

# fix-mutation-request-timeout Specification

#177 前置：修正 #152——`coordinator/cli.py::_submit_mutation_request` 的 `DEFAULT_REQUEST_TIMEOUT_SECONDS`（5s）小於 fanout 真實耗時，導致 daemon 背後成功但 CLI 回報失敗，operator 重試撞 `worktree already exists` 進入 slice failed 死路。

## Requirements

### R1 per-request 分級 timeout

`coordinator/cli.py::_submit_mutation_request` 的 poll timeout MUST 依 request 類型分級，不再用單一 5s 常數：

- `fanout`、`tick`：≥ 60s（建 worktree + pin inputs + headless launch 實測常 >5s）。
- `complete`、`work`、`run` 類 mutation：≥ 30s。
- 其他：5s 預設。

### R2 timeout 改 pending 語意

timeout 發生時 MUST NOT 以「daemon 未在 N s 內完成」硬錯誤收場；MUST 輸出 request id 並明示「request 仍在 daemon 處理中，勿立即重試」，附查詢指令（`cortex request list` / `cortex status`）。exit code MUST 區分「已提交但未在 timeout 內完成」（可追蹤）與「真正失敗」。

### R3 限制

- stdlib-only；TDD（mock `poll_done_fn` 控制逾時；斷言分級 timeout 取用、pending 訊息含 req_id 與追蹤指引、exit code 區別）。
- 既有成功/失敗路徑不變；不改對外 CLI `--json` envelope schema 字串。
- `test_zero_dependency_runtime` 續綠；`policy_check --repo .` 0 fail。
- 不處理 #99/#100。