---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Planning Artifact Manifest Binding Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#802`。
- Scope 限於 `paulsha_cortex/coordinator/manager.py` 的 `_publish_planning_artifacts` governed-roots／`allowed_refs` 推導、
  define 階段 needs_human 的 `next_step_hint`，與對應測試；不改 combo 卡片定義、不改 planning runtime 的 brainstorm 邏輯。

## Tasks

- [ ] RED：以 fix-standard（無 brainstorming 卡）manifest 呼叫 `_publish_planning_artifacts` 寫 `docs/superpowers/specs/<slug>-spec.md`／`-design.md`
      必須被接受；`openspec/changes/<其他 work_id>/…`、絕對路徑、`..`、非 `.md` 仍拒；先確認失敗。
- [ ] 實作 define 階段 planning kinds 的 governed 輸出恆在 `allowed_refs` 內（不依賴卡片 outputs）。
- [ ] define needs_human（content）時 `next_step_hint` 說明「補齊 accepted spec/design/plan 後 abandon＋re-intake」。
- [ ] 跑 focused／full repository gates，candidate evidence 記入 Cortex 後交付。
