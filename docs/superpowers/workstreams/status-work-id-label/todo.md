---
status: accepted
work_item: status-work-id-label
domain_breadth: 0
state_consistency: 0
invariant_count: 3
artifact_classes:
  - source
  - tests
  - documentation
---

# cortex inspect status 摘要行以 work_id 標示（#1367）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1367`；[spec](../../specs/status-work-id-label-spec.md)、[design](../../specs/status-work-id-label-design.md)。
- 只改 `paulsha_cortex/porcelain/inspect.py` 的 `_print_status` 文字輸出；`--json` 與其他子命令不動。
- spec／design／本 todo 的文字是 pinned authority，只能勾選 checkbox；需要澄清時寫進 terminal reason。

## Tasks

- [ ] **T1 RED（tests）**：新增 `tests/test_status_work_id_label_1367.py`，涵蓋 spec R4 的 (a)～(c)，確認 (a) 在現行程式上失敗。
- [ ] **T2 實作（source）**：新增 `_entry_label` helper，四種摘要行改用它；沒有 `work_id` 的條目輸出不變。
- [ ] **T3 回歸（tests）**：跑完整 `pytest`（記得 `env -u PYTHONPATH`），`tests/test_diagnostic_invariant_family_527.py` 等既有測試全部通過。
- [ ] **T4 文件（documentation）**：新增 `changelog.d/status-work-id-label.md`，並同步 `CHANGELOG.md [Unreleased]`。
- [ ] **T5 CLI 契約（R-16 cli）**：本票只改 `cortex inspect status` 的文字輸出，不改參數；確認 CLI help 不需更動，若有更動依 R-16 同步。
