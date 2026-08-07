---
status: accepted
work_item: fix-repair-commit-recovery
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/fix-repair-commit-recovery.md` 的 TDD RED 章節新增 `tests/test_repair_commit_recovery.py`，確認全部失敗。
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/fix-repair-commit-recovery-spec.md` 的 Requirements（R1–R6）；不放寬 retry-build CAS 與既有窄化入口。
- [x] 1.3 `changelog.d/fix-repair-commit-recovery.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#260）；`docs/unified-work-lifecycle.md` 與 CLI help 同步。
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

failed repair job 留下的 descendant commit 可經 `recover-repair-commit` 以雙 CAS 確定性 bind 為新 candidate，並繼續 verify → foreign review → exact-head final；非 descendant、dirty worktree、evidence 已 bind、authority 不符全部 fail closed；第一次 operator resume 即 dispatch replacement 且重送不產生第二個 replacement job；retry-build CAS 行為與現況完全一致。
