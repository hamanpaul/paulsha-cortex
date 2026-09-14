---
status: accepted
work_item: fix-agy-reviewer-json-schema
---

# fix-agy-reviewer-json-schema Plan

`#880`：agy reviewer lane 缺 `--json-schema` 綁定，gemini 把 verification terminal 的 `details` 寫成字串，
Manager 一律 `schema invalid`。權威需求見 `docs/superpowers/specs/fix-agy-reviewer-json-schema-spec.md`，
設計裁決見 `-design.md`。

## Requirements（摘要，以 spec 為準）

- R3：agy reviewer argv 含 `--json-schema`（與 claude 同源）；`build_agy_argv` 新增 `review_terminal_kind`；
  probe／builder lane 不變；Manager 對 verification `details` 字串正規化為 `{"text": …}`。
- R4：只改 `launcher.py`、`manager.py`、測試、碎片；禁改四件套。
- R6：新增 `tests/test_agy_reviewer_json_schema_880.py` 六項驗收；既有三個 agy／launcher 測試檔維持綠。
- R7：碎片 `changelog.d/fix-agy-reviewer-json-schema.md`；四件套入候選；無家目錄絕對路徑；PR `Closes #880`。

## Decisions（摘要，以 design 為準）

D1 schema 單一來源；D2 參數＋`SubprocessLauncher` 分岔改 `{"claude","agy"}`；D3 probe／其他 lane 不變；
D4 Manager 防禦正規化只針對 verification；D5 範圍外。

## Scope（明確邊界）

- 允許：`paulsha_cortex/coordinator/launcher.py`、`paulsha_cortex/coordinator/manager.py`（僅
  `terminalize_workflow_job` verification 分支）、`tests/test_agy_reviewer_json_schema_880.py`（新）、
  `changelog.d/fix-agy-reviewer-json-schema.md`（新）。
- 禁止：其他 `paulsha_cortex/**`、`docs/superpowers/**`、prompt 模板、model 候選邏輯。

## Tasks

1. **RED**：新增 `tests/test_agy_reviewer_json_schema_880.py`，實作 spec R6 的 1～5 項；對現行程式碼跑
   focused 測試，第 1／2／4／5 項應為紅。
2. **launcher**：依 D1／D2 實作 `build_agy_argv` 的 `review_terminal_kind` 與 `--json-schema`；
   `SubprocessLauncher` kwargs 分岔改 `{"claude","agy"}`。
3. **manager**：依 D4 在 `terminalize_workflow_job` 加 details 字串正規化與一行 warning。
4. **GREEN**：focused 四檔全綠（`python3 -m pytest tests/test_agy_reviewer_json_schema_880.py
   tests/test_coordinator_agy_launcher.py tests/test_coordinator_launcher.py
   tests/test_trust_root_agy_builder_grant_805.py -q`，worktree 內 PATH 裸命令）。
5. **交付**：`changelog.d/fix-agy-reviewer-json-schema.md`（一段 zh-tw：症狀、根因、兩處修法）；第一張 build
   卡 `git add` 四份 pinned planning 文件；PR body `Closes #880`；新增檔以家目錄絕對路徑掃描零命中。
