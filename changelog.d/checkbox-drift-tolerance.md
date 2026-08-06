### Fixed
- **Issue #310：pinned planning input 對 task checkbox 更新的 drift 容忍**：
  卡片契約要求 builder 勾選 tasks.md checkbox，但 `_workflow_input_snapshot`
  的嚴格 raw-hash 比對使 verify 派工必然 `planning input drift` fail-closed。
  改為：authority kind=plan 且 basename 為 `tasks.md`／`todo.md` 的 ref，於
  raw-hash 不符時以 operator_root 的 baseline（hash 先驗證）做
  checkbox-insensitive 比對（`- [x]`→`- [ ]` 正規化後相等即放行）；任何其他
  差異維持 fail-closed。
