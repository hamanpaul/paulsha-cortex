---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# fix-persona-catalog-portability Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_coordinator_candidate_verification.py` 新增 test class `PersonaCatalogPortabilityTests`，複用同檔既有 helpers（`_contract`、`_slice_row`、`_job`、`_persona_catalog`、`_git_ok`、`_git_fail`、`FakeGitRunner`、`FakeSubprocessRunner`）：
  - `test_non_cortex_repo_without_local_catalog_passes_persona_gate`：`("-C", str(root), "cat-file", "-e", dispatch_base + ":paulsha_cortex/persona/personas.yaml")` 回 `_git_fail`，且 response map **不含** `show` 該路徑的呼叫；驗證 gate 以 packaged catalog 完成 scope 判定，`payload["summary"]` 不是 `persona-catalog-unreadable`，`details["persona_catalog"]["source"] == "packaged"` 且 `commit is None`。
  - `test_repo_local_catalog_overrides_packaged`：探測回 `_git_ok`、`git show` 回 `_persona_catalog(builder_paths=[...])`（write_paths 與 packaged 內容不同以資區辨）；驗證 scope 判定採 repo-local 內容、`source == "repo-local"`、`commit == dispatch_base`。
  - `test_declared_override_unreadable_fails_closed`：探測回 `_git_ok`、`git show` 回 `_git_fail`；驗證 `status == "needs_human"`、`summary == "persona-catalog-unreadable"`，且 `details["persona_catalog"]` 的錯誤內容含 repo-local 路徑與 `dispatch_base`。
  - `test_declared_override_invalid_fails_closed`：`git show` 回壞 YAML（例如 `"roles: [broken"`）；驗證 `summary == "persona-catalog-invalid"`，不回退 packaged。
  - `test_cortex_repo_behavior_unchanged`：repo-local 存在且合法時，`details["persona_catalog"]` 的 `path == "paulsha_cortex/persona/personas.yaml"`、`commit == dispatch_base`、`hash == sha256(catalog 文字)`，與現行欄位一致。
- 驗收：`python3 -m pytest tests/test_coordinator_candidate_verification.py -q -k PersonaCatalogPortability` 五個新測試全數 FAIL（RED），其餘既有測試不動。

### 2. catalog 來源解析（GREEN）

- [ ] `paulsha_cortex/coordinator/verification.py`：改寫 `run_result_verification` 的 catalog 讀取段（現行 `:780-804`，位置維持在 checks 迴圈之前、required_artifacts 檢查之後）：
  - 先跑 `_run_git(["-C", str(resolved_repo_root), "cat-file", "-e", f"{dispatch_base}:{PERSONA_CATALOG_PATH}"], git_runner)` 探測 override。
  - 探測 `status == "ok"`：走既有 `git show` 讀取與 `_load_catalog_from_text`；show 失敗回 `persona-catalog-unreadable`、驗證失敗回 `persona-catalog-invalid`（現行為不變）。
  - 探測非 ok：`from ..persona.loader import DEFAULT_PERSONAS_PATH`，讀 `DEFAULT_PERSONAS_PATH.read_text(encoding="utf-8")` 後同樣以 `_load_catalog_from_text` 驗證；`OSError` 回 `persona-catalog-unreadable`、`ValueError` 回 `persona-catalog-invalid`。
- 驗收：Section 1 的 `test_non_cortex_repo_without_local_catalog_passes_persona_gate`、`test_repo_local_catalog_overrides_packaged`、`test_declared_override_unreadable_fails_closed`、`test_declared_override_invalid_fails_closed` 轉綠。

### 3. evidence 與錯誤訊息

- [ ] 同檔同段：成功時 `details["persona_catalog"]` 增加 `"source"`（`"repo-local"`／`"packaged"`）；repo-local 分支既有 `path`／`commit`／`hash` 欄位不變；packaged 分支 `path` 記 `str(DEFAULT_PERSONAS_PATH)`、`commit` 記 `None`、`hash` 記內容 sha256。失敗時 error payload 記錄實際嘗試過的來源（repo-local 分支：`path` 與 `commit`；packaged 分支：packaged `path`）。
- 驗收：Section 1 全部五個測試對 evidence 欄位的斷言通過（含 `test_cortex_repo_behavior_unchanged`）。

### 4. 既有測試同步

- [ ] `tests/test_coordinator_candidate_verification.py` 既有會走到 catalog 段的測試，在 `FakeGitRunner` response map 補 `("-C", str(root), "cat-file", "-e", dispatch_base + ":paulsha_cortex/persona/personas.yaml")` 項回 `_git_ok("")`，其餘 responses 與斷言不變。
- 驗收：`python3 -m pytest tests/test_coordinator_candidate_verification.py -q` 全綠。

### 5. 交付要件

- [ ] `changelog.d/fix-persona-catalog-portability.md` fragment（R-09 硬性 gate，須 commit 才進 diff）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry。
- [ ] 本次不動 CLI 介面，無 R-16 CLI help 同步需求；若實作過程動到 CLI 輸出則需同步 help 與 docs。
- [ ] 帶 PR 上下文執行 policy_check（`--pr-title`／`--pr-body`／`--pr-labels`／`--pr-base-ref`／`--pr-head-ref`），確認 fail: 0。
- [ ] `python3 -m pytest tests/ -q` 全綠。
- [ ] delivery PR body closing keywords 同時涵蓋 `Closes #295` 與 `Closes #291`（#295 為 primary）。
