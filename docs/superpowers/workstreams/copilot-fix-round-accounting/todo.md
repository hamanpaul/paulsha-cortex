---
status: accepted
work_item: copilot-fix-round-accounting
---

# Copilot 修正輪數不計入同步 main；用完後 run 仍有出路

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1342`。
- 現況：`work_actions._ship_action` 只要 `previous_head != preflight.head`，`repair_rounds` 就 +1，同步 main（retry-build 的 main merge、#1311 autosync）產生的新 head 也算一輪。超過 `MAX_FIX_ROUNDS`（=2）後，ship 直接回 needs_human（`copilot-finding-budget-exhausted`），不再對新 head 要求 Copilot review；而 `review-disposition` 要求針對 exact head 的 Copilot review，`repair_rounds` 也沒有重設路徑，因此卡死。
- 現場：`intake-model-flags`（PR #1326）、`installer-launch-authorities`（PR #1316）。
- 不放寬 Copilot finding 的處置規則：真正回應 finding 的修正仍受上限約束。

## Tasks

- [ ] **T1 RED**：修正 1 次、同步 main 3 次之後，現行 ship 判 `copilot-finding-budget-exhausted`。
- [ ] **T2 只計算修正**：`repair_rounds` 只計算回應 Copilot finding 的修正；能辨識 autosync 與 retry-build main merge 產生的 head，不計入。
- [ ] **T3 用完後的出路**：上限用完時，仍對新 head 要求一次 Copilot review，讓 operator 能以 `review-disposition` 裁決；或讓 `review-disposition` 接受「最後一次 review 之後只有已確認的修正與 main 同步」的 head。
- [ ] **T4 next_actions**：此狀態下列出實際可行的動作。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
