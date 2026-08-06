### Fixed
- **build 卡指引明令 pinned tasks/todo 僅可切換 checkbox**：tdd-red／subagent-build
  卡（含 retry 重派）指引補上「不得改寫、註記 pinned planning 檔文字」——W1 兩個
  修復 build 實測 builder 會註記 tasks.md 條目文字，超出 #310 checkbox 容忍造成
  planning input drift 卡死 run。
