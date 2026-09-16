---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Planning Artifact Manifest Binding Specification

## Requirements

- define 階段 planner 落檔的三種 planning kinds（spec／design／plan）MUST 恆被 governed-roots 檢查接受，路徑樣板固定為
  `docs/superpowers/specs/*<work_id>*-spec.md`、`docs/superpowers/specs/*<work_id>*-design.md`、`docs/superpowers/plans/*<work_id>*.md`，
  **不依賴** combo manifest 是否含 `brainstorming` 卡。
- 既有 governed-roots 的其他拒絕條件 MUST 維持：絕對路徑、`..`、非 `.md`、`openspec/changes/<非本 work_id>/`、symlink 一律拒。
- combo 卡片 outputs 宣告的其他路徑（`openspec/changes/<change>/{proposal,tasks}.md`、reports）行為不變。
- define 階段因 content 失敗進 needs_human 時，`needs_human_reason` MUST 帶可操作的 `next_step_hint`（補齊 accepted 三件套後
  `abandon` ＋ re-intake，或等本修正落地後直接 resume），`next_actions` 至少含 `abandon`。
- 不改 combo 定義、不改 planning runtime 的 brainstorm／question-pack 流程、不改 claim readiness。

## Acceptance

- 測試：以 fix-standard manifest（無 brainstorming 卡）呼叫 `_publish_planning_artifacts`，spec／design／plan 三檔全部落地；
  以 small-fix／feature-oneshot manifest 行為不變；負面路徑仍拒。
- 測試：define content 失敗的 `needs_human_reason` 含 `next_step_hint` 且 `next_actions` 含 `abandon`。
- 既有 planning／manager 測試維持綠；focused／full gates 綠；PR 以 `Closes #802` 交付。
- 回歸驗證（post-merge，operator）：只有 issue＋todo.md 的 work item 用 `--combo fix-standard` claim 可直達 build。
