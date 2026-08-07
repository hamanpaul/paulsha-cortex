### Added
- **Issue #203：`cortex work intake` 把 link+start 合成單一「拿到一個
  issue/task 就進件」入口，不復活低階直派**：新增 `work-action` 動作
  `intake`——帶 `--issue`／`--kind`+`--ref` 且尚未反映在受監控快照時先建立
  override link（等價 `cortex work link`），再原樣轉交既有 `start` 語意
  （`claim_key` 去重、`--combo` override 皆比照 `start`）；省略時直接沿用
  work_id 現有的 confirmed authority。Intake 不會憑空建立新 authority——
  work_id 必須已在受監控權威快照中存在，且最終仍要求 confirmed Todo 或已
  授權的 issue/openspec/path 來源，否則 fail-closed，不建立 WorkflowRun。
  `contract.py`／`work_actions.py`／`manager.py`／`manager_daemon.py`／
  `cli.py`／`porcelain/run.py` 六處同步放行 `intake`（`combo` 縱深防禦、
  job-dispatch 觸發皆與 `start` 對稱）；已停用的低階 `dispatch` 與既有
  Telegram `/dispatch <slice_id>` 維持原樣，不在本次範圍內改動。
