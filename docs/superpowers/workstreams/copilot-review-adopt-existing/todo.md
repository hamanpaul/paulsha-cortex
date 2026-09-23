---
status: accepted
work_item: copilot-review-adopt-existing
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Ship 段採信既有 exact-HEAD Copilot review（#948）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#948`；[spec](../../specs/copilot-review-adopt-existing-spec.md)、[design](../../specs/copilot-review-adopt-existing-design.md)。
- 觸及模組（2 個 production 模組 → `domain_breadth: 1`）：`paulsha_cortex/coordinator/work_actions.py`（`_ship_action` 採信分支、`copilot-*` stop 的 next_actions）、`paulsha_cortex/coordinator/delivery.py`（`ReviewLoop` 採信時間基準）；`github_delivery.py` 只在需要投影 `adopted_*` 時加法修改，`evaluate_delivery_gate` 判式不改。`state_consistency: 1`：ship 狀態新增 `adopted_review_id`／`adopted_at_epoch`，與 head 同生同滅。
- 不做 GitHub re-request fresh assessment、不改 15 分鐘 timeout 值、不改 maintainer-review 路徑、不改 #943（CHANGELOG merge main）與 #810（todo-incomplete）行為、不放寬 `review-thread-open`／`checks-not-terminal-green`／`not-mergeable` 任何 gate。
- spec／design／本 todo 文字是 pinned authority，只准翻 checkbox；澄清寫進 terminal reason。

## 現場證據

- 2026-09-22 #819 PR #947：Copilot 於 PR 建立當下 COMMENTED（1 finding）；#847 restart 後 Manager 40 分鐘後才 `request_copilot` → 15 分鐘 `copilot-review-timeout`，`next_actions` 只給 `abandon`。
- 現行 main：`_ship_action` 只接受 `submitted_at_epoch >= requested_at`；`ReviewLoop.record_review` 對早於 request 的 review 回 `copilot-review-outside-request-epoch`、對 `elapsed > REVIEW_TIMEOUT_SECONDS` 回 `copilot-review-timeout`；`_recoverable_maintainer_ship_stop` 已認 `copilot-*` 可由 `review-attest` 重入。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_copilot_review_adopt_existing.py`（沿既有 ship 段測試的 fake GitHub adapter／`_ship_action` fixture 樣板，`request_copilot` 計數），斷言逐條對應 spec R6 (a)–(f)；現行必須 RED（(a) 走 request 並最終 timeout；(f) 無 `review-attest`）。
- [x] **T2 source／採信分支（R1、R3、D1、D3）**：`_ship_action` 在 `request_copilot` 前篩 exact-HEAD Copilot COMMENTED／APPROVED 非 error review（多筆取最大 `(submitted_at_epoch, review_id)`）；命中 → 寫 ship 狀態 `requested_at_epoch = review.submitted_at_epoch`、`adopted_review_id`、`adopted_at_epoch = now`，同 tick 進 review 判定、不 request、不回 `awaiting-copilot`；未命中 → 現行路徑、狀態形狀不變；head 前進整份覆寫。
- [x] **T3 source／ReviewLoop 時間基準（R2、D2）**：`ReviewLoop` 加可選 `adopted_at`；timeout 的 `elapsed` 以 `adopted_at`（有）或 `requested_at` 計；outside-request-epoch 判定對採信 review 成立；`adopted_at is None` 時行為與現行逐字相同。
- [x] **T4 source／stop 出口與證據（R4、R5、D4）**：`copilot-*` stop 的 `next_actions` 補 `review-attest`（既有值在前）、`next_step_hint` 給指令形式；採信時 `logger.info` 一行並在 delivery journal／`evidence/delivery-adapter` 帶 `adopted_review_id`。
- [x] **T5 tests／回歸**：`tests/test_delivery*.py`、`tests/test_github_delivery*.py`、`tests/test_work_actions_ship*.py`、`tests/test_work_actions_review_attest*.py` 全綠、不改斷言；補「無既有 review 時 `request_copilot` 恰一次」「舊 head review 不採信」斷言。
- [x] **T6 documentation／changelog／CLI help**：新增 `changelog.d/copilot-review-adopt-existing.md` 並同步 `CHANGELOG.md [Unreleased]`；本票不新增 CLI，`cortex work --help` 輸出不變並以 help smoke 驗證；`docs/unified-work-lifecycle.md` ship 段補「既有 exact-HEAD Copilot review 直接採信、`copilot-*` stop 可 `review-attest` 重入」；README 的 ship 段說明若列 timeout 行為則同步一句。
