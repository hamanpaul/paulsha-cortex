---
type: docs
scope: refine
---
進件 #911：登記 work item `ship-lane-no-openspec`（issue link＋accepted todo）。範圍裁決做 A＋C、不做 B：
ship lane 對 `mapped_openspec` 為空的 run 走無 openspec 交付模式（跳過 official archive 與 archive facts，
remote closure 以 PR merged＋issue closed＋todo 全勾＋completion record 為準；`> 1` 仍
`multiple-delivery-targets-unsupported`），`review-attest` 在無 openspec／尚無 PR 時可用並可附 operator
重現證據 ref。T1–T7 逐一點名 `_ship_action`／`_ship_binding`／`_openspec_facts`／`_validate_work_authority`／
`_normalize_work_authority`／`_review_attest_action` 的改法與回歸測試檔。純進件，不含實作。
