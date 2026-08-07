---
status: accepted
work_item: feat-task-type-combo-selector
---

# feat-task-type-combo-selector Design

## Decisions

- 新增 `deck/data/combos/fix-standard.yaml`（採 issue comment 已驗證可載入的草稿，
  7 cards、2 gates）。
- selector：task_type→combo 對照（`feat`→feature-oneshot、`fix`→fix-standard；`docs`／
  `test`／`ci`／`refactor` 未有專屬 combo 前 fallback 現行預設並記可觀測 bypass 標記）；
  明示 override 永遠優先。
- `ambiguous`（多個互斥訊號）fail-closed 帶診斷；`absent`／`unparseable` bypass＋
  可觀測標記（對齊 #139 taxonomy 契約與 #202 comment 相容模式議定）。
- task_type 訊號來源與掛載點錨定既有 claim／combo 決定路徑（work_bridge 的 manifest
  combo 解析、deck `load_combo`），與 #139 taxonomy 單一真相源一致。

詳細 D1–Dn 與風險緩解見 `docs/superpowers/specs/feat-task-type-combo-selector-design.md`。
