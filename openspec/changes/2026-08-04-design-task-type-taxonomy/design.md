---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Design

## Decisions

- `task_type` 主軸定案為 conventional-commit type 六值（`feat`／`fix`／`docs`／`test`／
  `ci`／`refactor`），次軸 `scope`；本 work item 是 taxonomy 的單一真相源（#8 叢集
  收斂裁決，2026-07-27）。
- `ambiguous`（多個互斥訊號）fail-closed 帶診斷；`absent`／`unparseable` 走 bypass 用
  現行預設並記可觀測標記——「擋」只用於不可自癒的失敗。
- taxonomy 契約落地為 `deck/data/task-types.yaml`＋loader 驗證函式（值域、ambiguous／
  absent 判定 helper）＋測試；selector（#202）、outcome ledger（#137）、cost judge
  （#138）、skill ledger（#204）只消費此契約，不在本票實作。
- 統一 log reader 與 status view 只定介面契約（來源路徑、欄位、責任邊界），實作留
  下游票；combo 值域缺口（實測最大宗 `fix` 無對應 combo）明載為 #202 的硬前提。

詳細 D1–Dn 與風險緩解見 `docs/superpowers/specs/design-task-type-taxonomy-v2-design.md`。
