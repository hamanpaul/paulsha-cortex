---
status: accepted
work_item: fix-agy-reviewer-json-schema
---

# Spec: fix-agy-reviewer-json-schema

## Requirements

### R1. 缺陷敘述

`#880`：reviewer 卡派給 `agy` 時，launcher 沒有像 claude 那樣把 Manager 終端契約以 `--json-schema`
綁定到 CLI 的結構化輸出，只靠 prompt 文字。gemini 連續三輪（run `workflow-889dd8023badb98bf1e2`
job 455／457／458）把 `workflow-verification-result` 的 `details` 寫成字串（契約要求 object），
457 還把終端 JSON 印了兩次；Manager `terminalize_workflow_job` 一律擲
`workflow verification terminal schema invalid` → run 進 needs_human，判詞本身（verified、75 passed）被整份丟掉。

### R2. 現況根因證據（對 `origin/main` `739cde17`）

1. `paulsha_cortex/coordinator/launcher.py:26-`（`_claude_review_json_schema(kind)`）產生
   `workflow-verification-result`／`workflow-review-result` 的 JSON schema 字串；只有
   `build_claude_argv`（`:882-`，`review_only` 時 `:894-896` 取 schema、`:938-939` 傳 `--json-schema`）用它。
2. `SubprocessLauncher`（`:1900-1910`）只對 `self._executor == "claude"` 傳入
   `builder_kwargs["review_terminal_kind"]`；`build_agy_argv`（`:1179-`）沒有 `review_terminal_kind`
   參數，`json_envelope=True` 時只加 `--output-format json`（`:1245-1246` 一帶），沒有 `--json-schema`。
3. `paulsha_cortex/coordinator/manager.py:6366-6375`：verification terminal 的 `details` 不是 dict 即
   `schema invalid`，與其他欄位錯誤不可區分。
4. agy 1.2.0 `--help`：`--json-schema  Optional JSON schema string or path to a schema file to enforce
   structured output (for stream-json, only applicable to the final result)`；`--output-format json` 下
   對最終結果生效（capability probe `#670` 因用 `--output-format text` 不能加，維持不加）。

### R3. 目標狀態

1. agy reviewer lane（`review_only=True` 且 `json_envelope=True`）的 argv 含 `--json-schema <schema>`，
   `<schema>` 與 `_claude_review_json_schema(review_terminal_kind)` **同一來源**（同一函式回傳的字串），
   verification／review 兩種 kind 都適用。
2. `build_agy_argv` 新增 `review_terminal_kind: str | None = None`，語意比照 claude：`review_only` 時必填，
   非 `review_only` 時給了即 `ValueError`；`SubprocessLauncher` 對 `agy` 也傳入 `review_terminal_kind`。
3. capability probe（`json_envelope=False`）與 planner／builder lane 的 argv **逐字元不變**。
4. Manager 防禦：`terminalize_workflow_job` 對 verification terminal 的 `details` 若為**非空字串**，
   正規化為 `{"text": <原字串>}` 後再驗（其他欄位規則不變；空字串仍 invalid）；正規化事實寫進
   evidence／log（一行 warning 即可）。
5. 既有 `tests/test_coordinator_agy_launcher.py`、`tests/test_coordinator_launcher.py`、
   `tests/test_trust_root_agy_builder_grant_805.py` 全綠。

### R4. 範圍界線與明確排除

- 只改 `paulsha_cortex/coordinator/launcher.py`（`build_agy_argv`、`SubprocessLauncher` 的 kwargs 分岔）、
  `paulsha_cortex/coordinator/manager.py`（`terminalize_workflow_job` 的 details 正規化）、測試、changelog 碎片。
- 不改：reviewer 獨立性約束、候選選擇、cg lane、claude／codex／copilot lane、prompt 模板、#874／#875／#827。
- **禁止**改動 `docs/superpowers/**` 的本 work item 四件套。

### R5. 相容性

- `build_agy_argv` 新參數有預設值，既有呼叫端（`planning_runtime.py:86`、`model_identities.py:1071`）不需改。
- details 正規化只放寬「字串 → object」一種形狀，不接受其他型別。

### R6. 驗證期待

新增 `tests/test_agy_reviewer_json_schema_880.py`（風格比照 `tests/test_coordinator_agy_launcher.py`）：

1. `build_agy_argv(review_only=True, worktree=…, review_terminal_kind="workflow-verification-result")` 的 argv
   含 `--json-schema`，其值 `json.loads` 後與 `json.loads(_claude_review_json_schema("workflow-verification-result"))`
   相等；`workflow-review-result` 同理。
2. `review_only=True` 但缺 `review_terminal_kind` → `ValueError`；`review_only=False` 給了 kind → `ValueError`。
3. `json_envelope=False`（probe）與 builder lane 的 argv 不含 `--json-schema`，且與修正前逐字元相同（以既有
   測試的期望值釘住）。
4. `SubprocessLauncher`（executor=agy、review_only）建構出的 argv 含 `--json-schema`（可 monkeypatch
   `_ARGV_BUILDERS["agy"]` 錄下 kwargs，斷言收到 `review_terminal_kind`）。
5. `terminalize_workflow_job`：`details` 為非空字串的 verified terminal → 通過，且落地 payload 的
   `details == {"text": <原字串>}`；`details` 為空字串 → 仍 `schema invalid`；`details` 為 dict → 原樣。
6. 既有三個 agy／launcher 測試檔維持綠。

focused 測試命令（builder 在 worktree 內用 PATH 裸命令執行）：
`python3 -m pytest tests/test_agy_reviewer_json_schema_880.py tests/test_coordinator_agy_launcher.py tests/test_coordinator_launcher.py tests/test_trust_root_agy_builder_grant_805.py -q`
全套 gate 由 Manager 採信後執行。

### R7. 交付形式

- 第一張 build 卡 `git add` 本 work item 的四份 pinned planning 文件原樣入候選（不含 openspec）。
- changelog 碎片檔名固定 `changelog.d/fix-agy-reviewer-json-schema.md`。
- 新增檔案內容不得出現以家目錄開頭的絕對路徑（R-21 去識別化；用 `~/` 表示）。
- PR body `Closes #880`。
