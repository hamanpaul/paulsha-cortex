---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_deck_task_types.py`，全部先紅：
  - `test_task_types_yaml_loads_frozen_six_values`：載入 `paulsha_cortex/deck/data/task-types.yaml` 成功，值域恰為 `feat`／`fix`／`docs`／`test`／`ci`／`refactor` 六值，`feat` 的 combo 為 `feature-oneshot`、其餘五值 combo 為 None。
  - `test_loader_rejects_unknown_top_level_key`：頂層出現白名單（`version`、`task_types`、`scopes`）以外的鍵時拋 `DeckSchemaError`。
  - `test_loader_rejects_value_domain_drift`：YAML 多一值（如 `perf`）或少一值（移除 `refactor`）皆拋 `DeckSchemaError`，錯誤訊息含缺漏／多出的值。
  - `test_loader_rejects_empty_description`：任一 type 描述為空字串時拒載。
  - `test_loader_rejects_unknown_combo_reference`：帶入 combo 對照表時，combo 欄位指向不存在的 combo id 即拒載。
  - `test_loader_rejects_invalid_scopes`：scopes 空清單、重複值、或不合 `^[a-z][a-z0-9-]*$` 的 token 皆拒載。
  - `test_classify_matched_with_scope`：`"fix(cli): 修正 exit code"` → kind `matched`、`("fix", "cli")`、處置 proceed。
  - `test_classify_matched_without_scope`：`"feat: 新增選牌"` → kind `matched`、scope 為 None、處置 proceed。
  - `test_classify_unknown_type_fail_closed`：`"perf(cli): 加速"` → kind `unknown_type`、處置 fail-closed。
  - `test_classify_out_of_vocab_scope_ambiguous`：`"fix(claimx): 修正"`（scope 不在受控詞典）→ kind `ambiguous`、處置 fail-closed。
  - `test_classify_absent_bypass`：`"修 monitor 掃描漏洞"`（無 prefix）→ kind `absent`、處置 bypass。
  - `test_classify_unparseable_bypass`：`"fix(: broken"`（括號未閉合）→ kind `unparseable`、處置 bypass。
  - `test_disposition_mapping_is_total`：五類 kind 皆有唯一處置（`matched`→proceed；`unknown_type`／`ambiguous`→fail_closed；`absent`／`unparseable`→bypass），無未定義分支。

### 2. taxonomy 契約檔

- [ ] 新增 `paulsha_cortex/deck/data/task-types.yaml`：
  - 頂層鍵限 `version`（0）、`task_types`、`scopes`。
  - `task_types` 恰含六值，每值含 `description`（非空 zh-tw 描述）與 `combo`（`feat: feature-oneshot`，其餘五值 `null`）。
  - `scopes` 受控詞典七值：`coordinator`、`porcelain`、`workflow`、`cli`、`deck`、`monitor`、`onboarding`。
- 驗收：task 1 中三個 loader happy-path／drift 測試對此檔為真。

### 3. loader 與資料類

- [ ] 新增 `paulsha_cortex/deck/task_types.py`：
  - 凍結常數 `TASK_TYPE_VALUES = ("feat", "fix", "docs", "test", "ci", "refactor")` 與 `DEFAULT_TASK_TYPES_PATH`（比照 `schema.py` 的 `DEFAULT_CARDS_PATH` 寫法）。
  - frozen dataclass `TaskTypeSpec`（`name`／`description`／`combo: str | None`）與 `TaskTypeTaxonomy`（`version`／`task_types: Mapping`／`scopes: tuple`）。
  - `load_task_types(path=DEFAULT_TASK_TYPES_PATH, *, combos=None) -> TaskTypeTaxonomy`：重用 `schema.DeckSchemaError` 與未知鍵檢查慣例；值域與 `TASK_TYPE_VALUES` 不一致、描述空、scopes 非法皆收集錯誤後整批拒載；`combos` 對照表非 None 時驗證 combo 引用存在。
- 驗收：task 1 的六個 loader 測試轉綠；`python3 -m pytest tests/test_deck_schema.py tests/test_deck_data.py -q` 仍全綠（不影響既有載入）。

### 4. 分類 helper 與處置映射

- [ ] 於 `paulsha_cortex/deck/task_types.py` 增加：
  - 分類常數：kind 五值（`matched`／`unknown_type`／`ambiguous`／`absent`／`unparseable`）與處置三值（`proceed`／`fail_closed`／`bypass`）。
  - frozen dataclass `TitleClassification`（`kind`／`task_type: str | None`／`scope: str | None`／`disposition`／`reason: str`）。
  - `classify_title(title: str, taxonomy: TaskTypeTaxonomy) -> TitleClassification`：以正規表示式解析 `type(scope): subject`／`type: subject` prefix；判準＝「有主張而不合法 → fail_closed；沒有主張 → bypass」；`reason` 帶具體原因（如列出合法值域）。
- 驗收：task 1 的七個 classify／disposition 測試轉綠；helper 為純函式、不做任何 dispatch 決策、不發事件。

### 5. 交付要件

- [ ] `changelog.d/design-task-type-taxonomy.md` fragment（R-09 硬性 gate，須 commit 才進 diff）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry。
- [ ] 本票不動 CLI；若實作過程確有新增 CLI 面，須同步 `cortex` CLI help（R-16）。
- [ ] 帶 PR 上下文執行 `policy_check`（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`），確認 fail: 0。
- [ ] `python3 -m pytest tests/ -q` 全綠。
