---
status: accepted
work_item: fix-slice-failed-deadend
---

# fix-slice-failed-deadend Specification

`#153`：修正 slice state machine `failed` 為終端 sink，使 failed slice 可恢復；registry daemon 可從磁碟重載。

## Requirements

### R1 failed state 有恢復路徑

`paulsha_cortex/coordinator/registry.py` 的 slice state machine MUST 讓 `failed` state 有 outgoing transition（如 `failed → needs_human`）或新增 `reset` action 清除 failed state 允許 re-dispatch。MUST NOT 使 `failed` 為無出口的終端 sink。

### R2 failed slice actions 非空

failed slice 的 `actions` MUST 非空，含恢復 action（如 `retry-build` 或 `reset`）。

### R3 registry 磁碟重載

registry daemon MUST 支援從磁碟重載 jobs（偵測 `jobs.json` 修改或提供 reload 命令），使外部修改 `jobs.json` 後 daemon 狀態可同步。

### R4 限制

- stdlib-only；TDD。
- 不得改變既有對外 CLI envelope schema。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。