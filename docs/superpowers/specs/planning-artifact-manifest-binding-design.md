---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Planning Artifact Manifest Binding Design

## Decisions

- 採 #802 的方案 A（define 階段 `allowed_refs` 恆涵蓋 planning kinds）＋ C（`next_step_hint`）；方案 B（claim 時缺件 fail-fast）
  不採——planning runtime 的設計本來就是「缺 accepted 文件就 brainstorm 落檔」，B 會把這條路整個關掉。
- 實作點：`_publish_planning_artifacts` 的 `manifest_bound` 改為 `manifest_bound or planning_kind_bound`，其中
  `planning_kind_bound` 以 `work_id` 代入三個固定樣板（`fnmatch`）；`docs_bound`／`openspec_bound` 與 symlink／`..`／絕對路徑檢查不變。
  呼叫端 `allowed_refs=tuple(ref for step in manifest.steps for ref in step.outputs)` 不動，避免影響其他消費者。
- `next_step_hint`：在 manager 記錄 `brainstorm-not-ready` needs_human 的位置（`apply_workflow_action:start-brainstorm` 路徑）補一段
  classification=content 專用提示文字；`needs_human_next_actions(phase="define", classification="content")` 回傳含 `abandon`。
- 測試以純函式為主：對 `_publish_planning_artifacts` 餵假 manifest 與假 rows，不啟動 planner；沿用既有 `tests/test_manager_planning*`
  的 fixture 風格。
