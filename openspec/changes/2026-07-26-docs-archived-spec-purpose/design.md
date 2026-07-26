---
status: accepted
work_item: docs-archived-spec-purpose
---

# docs-archived-spec-purpose Design

## Decisions

### D1 從 change proposal 推導 Purpose

archive 時從 change 的 `proposal.md` `## Goals` 段取首段作為 Purpose。若 Goals 為空或不足，fallback 到 change name 的 human-readable 形式。不再產生 `Purpose: TBD` stub。

### D2 批次更新既有 specs

逐一更新 9 個受影響 specs 的 `Purpose: TBD` 行，根據各 spec 的 change proposal 或 capability 名稱推導適當 Purpose 文字。

### D3 向後相容

既有已 archived specs 不需重新 archive，僅更新 Purpose 行。新 archive 命令不再產生 TBD。

### 風險與 mitigation

- 推導的 Purpose 可能不夠精確 → 以 change proposal 的 Goals 段為主來源，人類可後續微調。
- 批次更新需確保不破壞 spec 其餘結構 → 僅替換 `Purpose: TBD ...` 行。