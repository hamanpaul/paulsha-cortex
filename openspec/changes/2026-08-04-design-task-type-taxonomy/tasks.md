---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# Tasks

- [ ] 1.1 RED：依 `docs/superpowers/plans/design-task-type-taxonomy-v2.md` 的 TDD RED 章節新增 `tests/test_deck_task_types.py`，確認失敗。
- [ ] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/design-task-type-taxonomy-v2-spec.md` 的 Requirements（契約檔 `paulsha_cortex/deck/data/task-types.yaml` 與 `paulsha_cortex/deck/task_types.py`）。
- [ ] 1.3 `changelog.d/design-task-type-taxonomy.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#139）。
- [ ] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

`task-types.yaml` 載入成功且值域凍結六值、任何結構或值域錯誤 fail-closed 拒載；分類 helper 五類判定正確且處置映射完備（`unknown_type`／`ambiguous` fail-closed、`absent`／`unparseable` bypass）；combo 缺口以 null 明示；文件明載本票為 taxonomy 單一真相源與下游引用邊界；既有 deck 測試不受影響。
