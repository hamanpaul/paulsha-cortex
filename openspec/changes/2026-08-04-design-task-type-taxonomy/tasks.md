---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/design-task-type-taxonomy-v2.md` 的 TDD RED 章節新增 `tests/test_deck_task_types.py`，確認失敗。（`tests/test_deck_task_types.py` 與其實作已隨 #202 的 PR #335 提前落地，先於本票收斂；本票補齊 plan 列出但缺漏的四項覆蓋：值域漂移拒載、空描述拒載、未知 combo 引用拒載、`test_disposition_mapping_is_total`。）
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/design-task-type-taxonomy-v2-spec.md` 的 Requirements（契約檔 `paulsha_cortex/deck/data/task-types.yaml` 與 `paulsha_cortex/deck/task_types.py`）。（同上，已由 #202 落地並經本票逐條核對 R1–R6 皆滿足；R5 的 `fix` → `fix-standard` 映射是 #202 openspec change 自身核可的顯式決策，屬 spec 非目標段落已預留的「叢集另案」，非本票偏離。）
- [x] 1.3 `changelog.d/design-task-type-taxonomy-v2.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#139）。（檔名依實際分支 `feature/139-design-task-type-taxonomy-v2` 的 slug 命名為 `-v2`；plan／此檔原文的 `design-task-type-taxonomy.md` 為重識別前的舊稱。）
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

`task-types.yaml` 載入成功且值域凍結六值、任何結構或值域錯誤 fail-closed 拒載；分類 helper 五類判定正確且處置映射完備（`unknown_type`／`ambiguous` fail-closed、`absent`／`unparseable` bypass）；combo 缺口以 null 明示；文件明載本票為 taxonomy 單一真相源與下游引用邊界；既有 deck 測試不受影響。
