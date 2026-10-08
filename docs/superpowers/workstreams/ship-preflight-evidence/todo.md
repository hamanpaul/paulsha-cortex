---
status: accepted
work_item: ship-preflight-evidence
domain_breadth: 0
state_consistency: 0
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# ship 階段 preflight 失敗留下 evidence（#1366）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1366`；[spec](../../specs/ship-preflight-evidence-spec.md)、[design](../../specs/ship-preflight-evidence-design.md)。
- 只改 `paulsha_cortex/coordinator/work_actions.py` 的 `_ship_action` preflight 失敗出口；`work_bridge.py` 只允許純加法的抽取。
- 不改 preflight 判定、tick 例外處理、pr-preflight 階段。
- spec／design／本 todo 的文字是 pinned authority，只能勾選 checkbox；需要澄清時寫進 terminal reason。

## Tasks

- [ ] **T1 RED**：新增 `tests/test_ship_preflight_evidence_1366.py`，涵蓋 spec R5 的 (a)～(d)，確認在現行程式上 (a)～(c) 失敗。
- [ ] **T2 實作**：`_ship_action` 在 preflight 失敗時先寫 `cortex-pr-preflight/v1` evidence（`stage: "ship"`），再拋出帶 evidence 路徑的 `ship preflight failed: <stage>`；寫入失敗時依 design D3 處理。
- [ ] **T3 回歸**：跑完整 `pytest`（記得 `env -u PYTHONPATH`），既有 `tests/test_ship_*.py` 全部通過。
- [ ] **T4 文件**：新增 `changelog.d/ship-preflight-evidence.md`，並同步 `CHANGELOG.md [Unreleased]`。
