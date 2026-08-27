---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Tasks

- [x] RED 測試：fix-standard、small-fix、feature-oneshot manifest 下 spec／design／plan 三檔落地；未綁定、絕對、含 `..`、非 `.md`、其他 work item 與 symlink 路徑仍拒；content needs_human 帶 hint 與 `abandon`。
- [x] 實作 `planning_kind_bound`；補 `next_step_hint` 與 next_actions，將 hint 持久化於 `needs_human_reason`，並依 detail 契約截斷超長 hint。
- [x] 既有 planning／manager／claim 測試回歸綠；補 `changelog.d/` 碎片。
- [x] focused 回歸與 full pytest gate 完成；交由 Manager 進行 pre-archive candidate 驗證與 evidence canonicalization。
