### Fixed
- **Issue #310 補遺：reviewer frozen authority 驗證沿用 checkbox 容忍**：
  input snapshot 建立已容忍 tasks/todo 的 checkbox 勾選後，reviewer 派工的
  `verify_authority_in_input_snapshot` 仍以 baseline hash 比對而 fail-closed
  （`review input snapshot authority hash drift`）。新增
  `_authority_map_with_checkbox_tolerance`：容忍成立時以候選實際 hash 作為
  pinned 期望值；其他差異維持 baseline fail-closed。
