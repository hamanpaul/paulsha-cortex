---
type: added
scope: coordinator
---
新增 `paulsha_cortex/coordinator/quota_observation.py` 的完整 quota observation
pure-data core：standalone `UnitDefinition`、descriptor inline units、profile/group
binding、多 constraint／coverage、known/unknown scope resolution、exact+bounds amount、
source method matrix、fresh/stale/unknown freshness、available/unavailable event
identity，以及 immutable parser-sealed records / redacted `QuotaContractError`。module
維持 stdlib-only、bounded validation、無 I/O、無 consumer 接線，既有
`registry.update_headless_result() → extract_usage()`／`StreamEvidence` 路徑不變。

同步補齊 `tests/test_quota_observation.py` 的 grammar／resource／immutability／frozen
profile framing／reuse seam coverage，更新 `README.md`、`docs/unified-work-lifecycle.md`
說明 schema-only boundary，並納入 `#866` 的 changelog entry、local validation receipt
與 pre-archive 本地驗證。
