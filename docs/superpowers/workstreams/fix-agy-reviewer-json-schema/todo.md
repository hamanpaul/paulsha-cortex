---
status: accepted
work_item: fix-agy-reviewer-json-schema
---

# fix-agy-reviewer-json-schema Todo

`#880`：**agy reviewer 沒有 `--json-schema` 強制輸出**——claude reviewer lane 用
`_claude_review_json_schema(kind)` 綁定 StructuredOutput，agy lane 只有 `--output-format json`，靠 prompt 文字
要求 `details` 為 object。gemini-3.1-pro-high 三輪（run `workflow-889dd8023badb98bf1e2` job 455／457／458）
全部輸出字串，Manager `terminalize_workflow_job` 一律 `workflow verification terminal schema invalid`，
正確的 verified 判詞被整份丟掉；獨立性約束下 codex builder 的 run 又只剩 agy 可當 verifier，等於無法通過 verify。

## 現況查核（0914，對 main `739cde17`）

1. `paulsha_cortex/coordinator/launcher.py:26`（`_claude_review_json_schema`）、`:894-896`＋`:938-939`
   （claude 取 schema 並傳 `--json-schema`）、`:1900-1910`（`SubprocessLauncher` 只對 claude 傳
   `review_terminal_kind`）、`:1179-`（`build_agy_argv` 無此參數、`json_envelope` 只加 `--output-format json`）。
2. `paulsha_cortex/coordinator/manager.py:6366-6375`：`details` 非 dict 即 schema invalid，與其他欄位錯誤不可區分。
3. agy 1.2.0 支援 `--json-schema <schema|path>`，`--output-format json` 下對最終結果生效。

## Current Sprint

- [ ] **RED**：`tests/test_agy_reviewer_json_schema_880.py` 依 spec R6 五項落地，對現行程式碼 1／2／4／5 為紅
- [ ] **實作**：`build_agy_argv` 加 `review_terminal_kind`＋`--json-schema`（與 claude 同源 schema；probe／builder
      lane 不變）；`SubprocessLauncher` 分岔改 `{"claude","agy"}`；`terminalize_workflow_job` 對 verification
      `details` 非空字串正規化為 `{"text": …}` 並記一行 warning
- [ ] **GREEN＋交付**：focused 四檔全綠；`changelog.d/fix-agy-reviewer-json-schema.md`；四件套入候選；
      PR `Closes #880`；新增檔無家目錄絕對路徑

## Handoff

- 驗收指令（worktree 內 PATH 裸命令）：
  `python3 -m pytest tests/test_agy_reviewer_json_schema_880.py tests/test_coordinator_agy_launcher.py tests/test_coordinator_launcher.py tests/test_trust_root_agy_builder_grant_805.py -q`
- 全套 gate 由 Manager 採信後執行；builder 不要去打 `.venv` 絕對路徑的命令。
- merge 後 operator 要把 runtime pin 升到含本修正的 main，再對 #879 run 下
  `work resume` → `work retry-card --card verification`。
