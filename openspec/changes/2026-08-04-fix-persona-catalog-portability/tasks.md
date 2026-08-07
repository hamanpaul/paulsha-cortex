---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/fix-persona-catalog-portability-v2.md` Section 1 於 `tests/test_coordinator_candidate_verification.py` 新增 `PersonaCatalogPortabilityTests`，確認五個新測試失敗。
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/fix-persona-catalog-portability-v2-spec.md` 的 Requirements（`paulsha_cortex/coordinator/verification.py` catalog 讀取段與 evidence 欄位、既有測試 response map 同步）。
- [x] 1.3 `changelog.d/fix-persona-catalog-portability-v2.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#295、#291）。
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨；delivery PR body 同時帶 `Closes #295` 與 `Closes #291`。

## 驗收

非 cortex repo（無 repo-local catalog）slice verification 通過 persona gate；repo-local override 優先於 packaged 且 pin 在 `dispatch_base`；override 壞損 fail-closed 且 reason 維持 `persona-catalog-unreadable`／`persona-catalog-invalid`；cortex repo 自身現行為不變、既有測試全綠；evidence 記錄來源標記、錯誤帶實際嘗試過的來源路徑。
