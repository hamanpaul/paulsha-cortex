---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Planning Artifact Manifest Binding Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#802`。
- Scope：`paulsha_cortex/coordinator/manager.py`（`_publish_planning_artifacts`、define needs_human 記錄）、`claim.py`／`work_actions.py`
  的 `needs_human_next_actions`（若提示需在此加）、對應測試；不改 combo／cards、不改 planning runtime。

## Tasks

- [ ] RED 測試：fix-standard manifest 下 spec／design／plan 三檔落地；負面路徑仍拒；content needs_human 帶 hint 與 `abandon`。
- [ ] 實作 `planning_kind_bound`；補 `next_step_hint` 與 next_actions。
- [ ] 既有 planning／manager／claim 測試回歸綠；補 `changelog.d/` 碎片。
- [ ] focused／full gates，candidate evidence 記入 Cortex 後交付。
