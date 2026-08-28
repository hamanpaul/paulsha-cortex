---
status: accepted
work_item: planning-artifact-manifest-binding
---

# Tasks

- [x] RED 測試：fix-standard、small-fix、feature-oneshot manifest 下 spec／design／plan 三檔落地；未綁定、絕對、含 `..`、非 `.md`、其他 work item 與 symlink 路徑仍拒；canonical work-item slug 與合法日期前綴皆接受，任意額外前綴／後綴仍拒；content needs_human 帶 hint 與 `abandon`。
- [x] 實作 `planning_kind_bound`；以 accepted glob 對應 spec／design／plan，再限制 basename 為 work item 或 `YYYY-MM-DD-<work_id>` slug；authority revalidation 與 publication 共用此放行；補 `next_step_hint` 與 next_actions，將 hint 持久化於 `needs_human_reason`，依 detail 契約截斷超長 hint，並將三條 operator hint 分支固定為正體中文。
- [x] 既有 planning／manager／claim 測試回歸綠；補 `DiagnosticReason` schema v2 的加法欄位、v1 payload 相容讀取與 round-trip 測試；補 `changelog.d/` 碎片。
- [x] focused 回歸與 full pytest gate 完成；pre-archive candidate 驗證完成，archive 由 Manager 進行 evidence canonicalization。
