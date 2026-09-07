---
status: accepted
work_item: yaml-inline-list-quotes
---

# Tasks

- [x] [RED] 新增 `tests/test_yaml_inline_list.py`，覆蓋引號內逗號與 `]`、跳脫引號、尾逗號、中間空元素、空引號字串、空 list、未閉合引號及未引號 scalar 轉型。
- [x] [RED] 新增 `_format_inline_list`／`_format_scalar` round-trip regression tests，並覆蓋 frontmatter `tests[].argv`／`full_suite.argv` 的 quoted-comma 端到端路徑。
- [x] [RED] 執行聚焦 pytest，確認現行 parser 對新增 regression tests 維持預期 RED。
- [x] [GREEN] 以 quote-aware flow-sequence tokenizer 修正 `_yaml._parse_scalar`，並使聚焦與既有 consumer suite 通過。
- [x] [DOC] 新增 changelog fragment，並同步 `CHANGELOG.md` 與 README 的 YAML／argv 引號說明。
- [x] [CLI] 執行 `python3 -m paulsha_cortex.cli --help` 與 `python3 -m paulsha_cortex.cli deck --help`。
- [x] [VERIFY] 執行完整驗證關卡並保存 candidate 與驗收證據。
