---
status: accepted
work_item: docs-archived-spec-purpose
---

# docs-archived-spec-purpose Design

## Decisions

### D1 Purpose 推導來源

從 change `proposal.md` 的 `## Goals` 段取首段文字。Goals 段通常是一句話描述目標，適合作為 Purpose。若 Goals 段不存在或為空，fallback 到 capability name 的 human-readable 形式（如 `porcelain-service-lifecycle` → `Porcelain service lifecycle`）。

### D2 批次更新策略

逐一讀取各 spec 的對應 change proposal（從 `openspec/changes/archive/` 找），推導 Purpose，替換 `Purpose: TBD ...` 行。不動 spec 其餘內容。

### 風險與 mitigation

- change proposal 可能已 archived 且路徑不明 → fallback 到 capability name human-readable。
- 推導的 Purpose 需人類 review → 更新後可在後續 PR 微調。