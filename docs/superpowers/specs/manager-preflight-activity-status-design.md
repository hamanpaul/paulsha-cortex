---
status: draft
work_item: manager-preflight-activity-status
issue: 1028
---

# Manager 長時間 PR preflight 狀態設計（#1028）

## Decisions

### D1 — Use an owned activity lease beside status.json

新增小型 `control/activity.py` 契約，資料檔為 control root 下 `manager-activity.json`，schema `cortex-manager-activity/v1`。單一 Manager 是該 lease 的 writer；記錄含 operation_id、owner PID、repo/work_id/run_id、candidate head、operation/stage、child PID、started_at、last_progress_at。採原子 JSON 寫入；更新及清除必須比對 operation_id/owner PID，避免過期 finally 清除另一個新 operation。

這是短生命週期觀測資料，不進 JobRegistry、不改 WorkflowRun 或 gate ledger。缺檔、malformed、unknown version 一律不提供 busy 豁免。

### D2 — Instrument only the Manager PR-preflight seam

在 `work_bridge._run_exact_candidate_preflight()` 的 Manager ship 呼叫路徑建立 lease，identity 取自已驗證的 WorkflowRun、branch、candidate 與 PR metadata，不從外部 request 任意複製。透過 monitored runner 執行現有 `run_preflight()`：argv、cwd、environment、capture/text 語意與退出碼維持不變；記錄 policy/CI stage、child PID，並在 stage 轉換及子程序輸出更新 last_progress_at。完成/exception 均以 operation_id CAS 清除 lease；process crash 留下的 stale lease 交由 reader fail closed。

不改 `run_preflight()` 的 gate 順序與結果；如果 monitored runner 不能保留既有 `subprocess.run` 捕獲／回傳語意，停止並回報設計阻礙，不退化成固定 timer heartbeat。

### D3 — Derive busy only from the exact live process tree

`control.client.read_status()` 仍以既有 status age、Manager PID liveness 和 120 秒 `STATUS_STALLED_AFTER_SECONDS` 為基準。僅當 D1 lease 與 status daemon PID 一致、recorded child PID 在 Manager 子孫樹內且 lease progress 新鮮時，把 stale status 投影成 `degraded=false`/`activity.state=busy`。任何讀檔失敗或 process-tree probe 不確定都回現有 degraded 結果；不以「任一 child 還活著」當 busy。

Busy 投影的 `daemon.idle` 強制為 false，所以既有 `capacity_gate.py` 會繼續詢問 operator。`degraded=false` 在此只代表有可驗證的長工作證據，不是 ready/dispatch authorization。

### D4 — Observe job exit receipts without adopting their result

當 Manager 正忙於同步 preflight，status 可唯讀比對最新 status snapshot 的 exact `job_id`、canonical registry row 與該 row 綁定的 `control_log_path` exit receipt。若 registry 仍為 `dispatched`／`running` 但 receipt 已證明 process exit，投影需同時保留 `registry_state`、`process_exit_observed`（含可驗證的 exit code/time）與 `manager_acceptance=pending`；這是 pending observation，不是 job/card result。只有 Manager 原有 polling／terminalization owner 能更新 registry、採信 gate、分類失敗或派工。

receipt 缺失、path/root 無法安全解析、job ID 不匹配或 probe 不確定時，不從死 PID、log 尾字串、gate 檔或任意 `.exit` 推定終止；回傳 unknown／不附 exit claim。此讀取不得呼叫 `poll_headless_done()`、`update_headless_result()`、workflow terminalization 或任何 dispatch consumer。

### D5 — Keep the status API additive and operator visible

`read_status()` 在正常 idle status 回 `activity=null` 或缺省；驗證 busy 時回有限欄位的 activity object，並可附上上述 exact receipt 的 pending observation。舊 `status.json` 不含 activity 欄位仍走舊邏輯。`porcelain.inspect._print_status()` 顯示忙碌狀態、work identity、stage、started/progress time，以及 pending observation 的 job ID／registry state／process exit／acceptance state，JSON 與文字輸出一致。避免印絕對路徑、environment/token、完整 command line 或 gate payload。

### D6 — Parallel work packages and file ownership

| Candidate package | Candidate work_id | Owner files and responsibility | Dependency |
|---|---|---|---|
| Producer | `manager-preflight-activity-producer` | `control/activity.py` lease schema/writer; `work_bridge.py` exact preflight producer and monitored runner | Wait for #1024 `work_bridge.py` ship hunk to merge/stabilize; freeze activity v1 first |
| Consumer | `manager-busy-status-projection` | `control/client.py` strict lease/process-tree verification and read-only exact job-receipt projection; `porcelain/inspect.py` output; a bounded registry read adapter only if needed | Depends on frozen lease/receipt contract; may implement in parallel with producer after contract freeze |
| Aggregate | `manager-preflight-activity-status` | integration and all R1028 acceptance | Both packages complete |

These IDs remain candidates, not registered work; this planning PR creates no WorkAuthority entry or Cortex run. Before any registration, verify uniqueness and authority binding against canonical WorkAuthority and read the registration back. TDD fixtures/contract can be prepared in parallel only after the lease and receipt contracts are frozen; do not have both packages edit `work_bridge.py`, write job state, or change gate semantics independently.

## Source evidence at intake

- Current planning base: `8b26d3702c398cb8dbda337bd596e26a0d96bc5b` (`origin/main`, 2026-09-24).
- `control/constants.py` defines the 15-second stale and 120-second live-manager stalled thresholds; `control/client.py:read_status()` still marks a live PID `stalled` once the status age exceeds the latter, with no activity lease.
- `manager_daemon.py` runs request work synchronously and publishes its status snapshot after that work returns; a blocked preflight therefore leaves the prior `in_flight` view in place.
- `manager_daemon._in_flight_status()` projects only registry rows currently `dispatched`/`running`; it does not include a separate process-exit observation.
- `work_bridge._run_exact_candidate_preflight()` creates a detached exact-candidate worktree and synchronously invokes existing `run_preflight()` on the Manager ship path. The implementation must preserve its command, capture and result contract. PR #1024 currently edits this same module, so implementation waits for its ship/preflight hunk to stabilize.
- `dispatcher.poll_headless_done()` reads a registry-bound control-log exit sentinel and performs Manager-owned finalization. This is the terminal receipt source for a read-only `process_exit_observed` projection; the status reader must not call the poll/finalize path.
- `porcelain.inspect._print_status()` prints updated/degraded/readiness fields without active operation detail. `porcelain.capacity_gate.evaluate_gate()` already returns `ask` when status is degraded or `daemon.idle is False`; a valid busy projection must retain `idle=false`.
- `tests/test_control_client.py` covers live busy, dead Manager and stale live Manager behavior. Future acceptance adds lease and pending-receipt cases without weakening those existing oracles.
- Live #1024 is OPEN/unmerged at head `dac3c4b8776dbae5eff0aa0002378f725e07967a`, check rollup is empty, two Copilot findings remain open, and GitHub currently reports mergeability `UNKNOWN`. The latest #885 comment says `retry-build` was accepted on that Candidate and a repair builder was dispatched; new Candidate, verification, review, ship and merge remain unfinished. Its exact gates remain owned by #885/#1024.

## Risks and alternatives

- Raising `STATUS_STALLED_AFTER_SECONDS` globally would mask genuine stalls and omit work identity; rejected.
- Treating Manager PID alone as busy would preserve this false positive and could hide a deadlocked Manager; exact activity and child identity are required.
- Treating any child process as proof would let unrelated/orphan processes mask a stalled Manager; exact parent/child identity is required.
- A fixed timer heartbeat would claim progress during a hung subprocess; progress must come from stage transition or observed child output.
- A job exit receipt is process evidence only. Status must keep it separate from Manager acceptance, gate classification and follow-up dispatch.
- New activity/pending observation fields are diagnostic state only; they cannot change readiness, dispatch, merge, job registry, or preflight outcomes.
