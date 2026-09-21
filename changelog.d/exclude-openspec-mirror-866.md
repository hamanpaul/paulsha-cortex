---
type: docs
scope: refine
---
#866 work item `quota-observation-schema-core` 把 `openspec` link 移到 `excludes`（#938 workaround）：openspec 鏡像與
superpowers 三件套同內容在 workflow input envelope 重複計入，7 份 ≈146 KB 超過 128 KiB 字面上限，verify 派工
必炸（run `workflow-8e5d881120b4edf4262c` 已 abandon）。只掛三件套後 ≈79 KB，ship 走 #911 無 openspec 模式；
`openspec/changes/quota-observation-schema-core/` 留在 repo 未歸檔，待 #938 修好再決定是否掛回。
