---
status: accepted
work_item: yaml-inline-list-quotes
---

# Tasks

- [x] [RED] 新增 `tests/test_yaml_inline_list.py`，覆蓋引號內逗號與 `]`、跳脫引號、尾逗號、中間空元素、空引號字串、空 list、未閉合引號及未引號 scalar 轉型。
- [x] [RED] 新增 `_format_inline_list`／`_format_scalar` round-trip regression tests，並覆蓋 frontmatter `tests[].argv`／`full_suite.argv` 的 quoted-comma 端到端路徑。
- [x] [RED] 執行聚焦 pytest，確認現行 parser 對新增 regression tests 維持預期 RED。
- [x] [GREEN] 以 quote-aware flow-sequence tokenizer 修正 `_yaml._parse_scalar`，並使單／雙引號內的反斜線跳脫、聚焦與既有 consumer suite 通過。
- [x] [REPAIR] 補上單引號內 `\\'` 後接逗號的 inline-list regression，修正其不得被拆成多個 argv 元素的解析路徑。
- [x] [DOC] 新增 changelog fragment，並同步 `CHANGELOG.md` 與 README 的 YAML／argv 引號說明，包含單／雙引號內的反斜線跳脫。
- [x] [CLI] 執行 `python3 -m paulsha_cortex.cli --help` 與 `python3 -m paulsha_cortex.cli deck --help`。
- [x] [DOC-REPAIR] 修正三份交付前文件對單／雙引號反斜線跳脫的遺漏，未宣稱 archive、ship、merge 或 issue closure。
- [x] [VERIFY] 完成本卡修補的 pre-archive focused／consumer pytest；canonical gate 重跑與 ship／archive 由 Manager 後續處理。
- [x] [REPAIR-2] 補齊前導／中間空元素的拒絕說明，並新增空引號字串與其他元素的單／雙引號 regression；本卡僅完成 pre-archive Candidate 修補，不宣稱 archive／merge／issue closure／done。
