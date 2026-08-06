---
status: accepted
work_item: design-task-type-taxonomy-v2
---

## Goals

定案 `task_type` taxonomy 契約（主軸 conventional-commit `type` 六值、次軸 `scope` 受控詞典、fail-closed vs bypass 處置語意），並落地輕量契約骨架（契約檔＋loader／分類 helper＋測試），作為 #202／#137／#138／#204 的單一真相源。

## Why

repo 內存在至少四種互不相干的 task 分類軸，且無 issue 明確擁有 taxonomy 定義權，使 #139／#202／#137／#138 循環等待。使用者已於 2026-07-27 裁決主軸採 conventional-commit `type` 且 #139 為 taxonomy 所有者；deck combo 現況只有 `feature`／`mcu-feature`，實測最大宗的 `fix` 無 combo，缺口需以契約明示而非讓下游猜測。

## What Changes

- 新增契約檔 `paulsha_cortex/deck/data/task-types.yaml`：六值主軸（含描述與 combo 映射，`feat` → `feature-oneshot`、其餘為 null 明示缺口）＋ scope 受控詞典七值。
- 新增 `paulsha_cortex/deck/task_types.py`：凍結常數與契約檔雙鎖、fail-closed loader（比照 `DeckSchemaError` 慣例）、標題分類 helper（`matched`／`unknown_type`／`ambiguous`／`absent`／`unparseable` 五類與 proceed／fail_closed／bypass 處置映射）。
- 新增 `tests/test_deck_task_types.py` 覆蓋載入、值域漂移、combo 引用、五類分類與處置映射。
- spec／design 文件定案下游消費契約邊界（#202 selector／#137 ledger／#138 judge／#204 skill ledger）與統一 log reader／status view 的介面契約草案（只定契約，不實作）。
- 不實作 selector／ledger／judge／reader／view，不新增 combo，不動 CLI。

## Capabilities

### Modified Capabilities
- `persona-workflow-orchestration`：詳見 `docs/superpowers/specs/design-task-type-taxonomy-v2-spec.md` 的 Requirements 與 `docs/superpowers/specs/design-task-type-taxonomy-v2-design.md` 的 Decisions。
