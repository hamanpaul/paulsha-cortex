# report-json-preferred

- **#466 實跑驗證 follow-up（二）：profile 巷道優先讀 `report.json`，subset YAML
  parser 補 indentless sequence**——真 report.yaml（PyYAML `safe_dump`）連踩兩個
  subset parser 讀不了的形狀：indentless block sequence（`rows:` 後同縮排 `- `）
  與長 quoted scalar 折行（eutb reason 實例）。`_yaml.safe_load` 補前者（只在
  「空值 key 緊接同縮排 dash」時轉序列解析，純擴大接受集，`tests/test_yaml_subset.py`
  鎖回歸）；後者不追平 PyYAML folding——改吃 patchmud#26 新落盤的 `report.json`
  機器契約，無檔時退回 YAML fallback（相容舊版 patchmud）。
