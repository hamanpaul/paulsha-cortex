### Fixed
- **Issue #339：run tick 對已有 needs_human 終局紀錄的 slice 不再重複 fanout**：
  `run_tick` 原本的冪等防護只排除 `dispatcher._registry` 中仍在 `dispatched`/`running`
  的 job，job 一旦 poll 到 exited 就離開這個集合，不論其 `gate_status` 是
  `needs_human`／`failed`／`passed`；`ready_units`/`default_is_satisfied` 只檢查「別人
  depends_on 我」是否滿足，從未檢查「我自己是否已經跑過」，導致下一趟 tick 把已完成
  待人工的 slice 重新判定為就緒，對同一 branch/worktree 重新 fanout，撞
  `ScriptWorktreeCreator.create` 的 `"worktree target already exists"`。現在
  `run_tick` 會在派工前掃描每個 slice 是否已有 handoff 終局紀錄（`handoff_dir/<slice_id>.json`
  存在即算），一併併入既有 in-flight 排除集合，從源頭避免重派；此掃描與 idle gate
  無關，即使 `require_idle` 擋下新工作，`needs_human` 清單仍會回報。
- **`run_tick` summary 新增 `needs_human` 欄位**：回傳 dict 新增
  `needs_human: [{slice_id, gate_reason, handoff_path}, ...]`，讓 CLI／control-plane
  能看到有多少 slice 卡在待人工，不必再自行掃 handoff 目錄猜測。
