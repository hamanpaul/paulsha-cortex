---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# fix-deck-emit-frontmatter Design

## Decisions

### D1 target_branch 推導策略

從 combo/work-item context 取 `target_branch`；若 combo 有 `branch` 欄位則直接用，否則 fallback `feature/<work-item-slug>` 並 emit warning 至 stderr。

### D2 verification skeleton 結構

```yaml
verification:
  docs_class: <class>
  checks:
    - persona: <reviewer-persona>
      command: policy
      name: policy
    - tests: pytest tests/ -q
    - full_suite:
        baseline: no-regression
```

從現有 dispatch contract 測試碼推斷期望結構，填入 skeleton 值。

### D3 文件位置

在 `docs/` 新增 `dispatch-contract.md`（或併入既有文件），記錄 verification 物件結構、必填欄位、範例 frontmatter。README 對應段加連結。

### 風險與 mitigation

- verification skeleton 欄位需與實際 contract 同步 → 測試同時驗證 emit 結構與 contract 一致。
- context 不足時 fallback 行為需明確 warning，避免 silent default。