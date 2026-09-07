---
status: accepted
work_item: yaml-inline-list-quotes
domain_breadth: 0
state_consistency: 0
invariant_count: 5
artifact_classes: [source, tests, documentation]
---

# YAML inline list 引號修正 Todo

Sizing 依 #208：production 只改 `_yaml._parse_scalar`，為單模組／單資料流；
emitter/frontmatter 是回歸測試消費端，不另算 production 模組。原 domain=1
估計已由正式 abandon/reclaim 更正為0；state=0 與其餘機械分、Yellow 審查不變。

## Boundary（範圍）

- Issue: `hamanpaul/paulsha-cortex#822`.
- 範圍僅限 `paulsha_cortex/_yaml.py::_parse_scalar` 的 inline（flow）list
  分支：讓含 `,` 或 `]` 的引號元素能經 `safe_load` 正確讀回；不重寫 block
  parser、不把 PyYAML 引入 runtime、不加 inline mapping／巢狀 inline list
  支援、不改 `verification.normalize_argv` 的驗證語意。

## Tasks

- [ ] source（原始碼）：把 `paulsha_cortex/_yaml.py:23` 的 `inner.split(",")` 換成一個小型
      quote-aware flow-sequence tokenizer（單／雙引號、雙引號內反斜線跳脫、
      引號內的 `,` 與 `]` 視為字面值）；尾逗號容忍，中間空元素或未閉合引號
      raise `YAMLError` 且訊息含 `malformed inline list`；未引號元素沿用既有
      `_parse_scalar` 語意。
- [ ] 新增 `tests/test_yaml_inline_list.py`，覆蓋 issue #822 驗收條件 1–4：
      引號內逗號、引號內 `]`、跳脫引號、尾逗號、中間空元素、空引號字串、
      空 list、未閉合引號、未引號 scalar 轉型（`[1, true, null, ~, x]`）。
- [ ] 新增 round-trip 測試（同檔或 `tests/test_deck_compile.py`）：把含
      `,`、`]`、`'`、`"` 的 values 分別經
      `paulsha_cortex.deck.compile._format_inline_list(values)`（`tests[].argv`
      路徑，`compile.py:432`）與 `_format_scalar(values)`（`full_suite.argv`
      路徑，`compile.py:441`）再餵回 `safe_load`，斷言與原 list 相等。
- [ ] 在 `tests/test_yaml_inline_list.py` 加端到端回歸：於 `tmp_path` 先
      `git init`（或 `monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))`，
      否則 `autonomy._infer_repo_root` 回 `repo-root-unresolved`），寫一份
      `dispatch: auto` spec，形狀比照 `tests/test_coordinator_verification.py:44-63`
      （`plan:`、`target_branch`、`verification.docs_class: code`、`checks`
      含 `kind: persona-scope` 與 `kind: command`＋`name: policy`；policy
      check／`tests[]`／`full_suite` 都帶 `argv`、`cwd: .`、`timeout_seconds`，
      `full_suite` 另帶 `baseline: no-regression`），`tests[0].argv` 含
      `"a or b, c"`、`full_suite.argv` 含 `"src,lib"`，斷言
      `autonomy.parse_spec_frontmatter` 回傳 `parse_error is None`、
      `dispatch == "auto"`，且正規化後的兩組 argv 與來源逐項相等。
- [ ] 執行 `python3 -m pytest tests/test_yaml_subset.py
      tests/test_zero_dependency_runtime.py tests/test_deck_compile.py
      tests/test_persona_config_loader.py tests/test_model_profile_cli.py
      tests/test_deck_task_types.py tests/test_yaml_inline_list.py -q`，
      確保所有既有 `_yaml` consumer 維持綠燈。
- [ ] documentation（文件）：新增 `changelog.d/yaml-inline-list-quotes.md`，同步補
      `CHANGELOG.md [Unreleased]` 與 README 的支援 YAML 子集／argv 引號限制。
- [ ] CLI 契約：執行 `python3 -m paulsha_cortex.cli --help` 與 `python3 -m paulsha_cortex.cli deck --help`，
      核對既有指令解析無回歸；本票不新增旗標。
- [ ] 執行聚焦／完整關卡，經 Cortex 保存 candidate 與驗收證據。
