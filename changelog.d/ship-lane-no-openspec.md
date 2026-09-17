---
type: fix
scope: workflow
---
ship lane 現在接受 `mapped_openspec == ()` 的合法交付模式：`_ship_action`、delivery binding、GitHub
facts、remote closure 與 CompletionRecord 全面支援 `change=None`，沒有 mapped OpenSpec 時會跳過
official archive 與 archive facts，但仍維持 PR／Todo／issue／merge ancestry／CompletionRecord 的全部
fail-closed gate；`mapped_openspec > 1` 仍以 `multiple-delivery-targets-unsupported` 阻擋並提示先
`cortex work unlink` 修正 correlation 後 `resume`。`review-attest` 亦可在無 openspec、甚至尚無 PR 時先建立
immutable maintainer evidence，並支援選填 `evidence_refs` 保存 operator reproduction artifact。
