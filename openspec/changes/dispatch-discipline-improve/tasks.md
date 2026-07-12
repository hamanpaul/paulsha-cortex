> Detailed RED/PASS commands, exact files and commit boundaries are canonical in `docs/superpowers/plans/2026-07-12-dispatch-discipline-improve.md`. This checklist tracks OpenSpec apply progress without duplicating that execution plan.

## 1. Scoped broker cleanup

- [x] 1.1 RED-test that periodic CLI/daemon ticks never call the reaper and manual cleanup defaults to dry-run.
- [x] 1.2 Implement `cortex reap-brokers`, require `--cwd-root` for apply, recheck live process identity and send SIGTERM only.
- [x] 1.3 Pass Python wiring and fake `/proc` negative fixtures, including cross-project and PID-reuse cases.

## 2. Versioned coordinator state

- [x] 2.1 RED-test versioned `jobs+slices` state, `done → exited`, legal transitions, reloadable current/history containers and clean-start rejection.
- [x] 2.2 Implement atomic SliceRecord CRUD/action history while retaining Job history and single-writer persistence.
- [x] 2.3 Pass registry, dispatcher, completion, manager and restart focused tests.

## 3. Deterministic verification

- [x] 3.1 RED-test strict `target_branch` and verification frontmatter parsing plus dispatch-time spec/plan/contract hashes.
- [x] 3.2 Implement dispatch-time spec/plan/verification hash pinning plus the versioned verification evidence writer/quarantine path.
- [x] 3.3 Replace Task 3 false-greens so malformed frontmatter/evidence or pinned-input drift fail closed before downstream release.

## 4. Candidate 固定與 deterministic ResultVerification

- [x] 4.1 RED-test builder `exited` 後必須固定可信 Candidate：`dispatch_base` 必須是 Candidate ancestor、Candidate 不得等於 base，branch ref snapshot 後漂移或 force-push 非 descendant 一律 `needs_human`。
- [x] 4.2 RED-test deterministic verification runner：required artifacts、persona-scope、typed argv checks、task tests、base/candidate full-suite 比較與 fail-closed evidence。
- [x] 4.3 實作 deterministic ResultVerification：固定 Candidate、sanitized env、dispatch-base persona catalog、base detached worktree 雙跑 full-suite，並把成功結果分流到 `verified` / `reviewing`。
- [x] 4.4 取代 false-green 迴歸：builder `exited` 單獨不得滿足 DAG，focused coordinator/persona suite 維持全綠。

## 5. Foreign exact-HEAD review

- [ ] 5.1 RED-test explicit model identity mapping, different-domain selection, detached exact Candidate checkout and verdict provenance.
- [ ] 5.2 Implement reviewer Jobs, immutable GateEvaluations and cortex-owned blocking-category classification.
- [ ] 5.3 Pass same-domain, unknown identity, stale HEAD, malformed verdict, blocking and non-blocking review cases.

## 6. Artifact-aware dependency release

- [ ] 6.1 RED-test versioned CompletionRecord validation, record-first crash ordering and orphan-record restart handling.
- [ ] 6.2 Implement remote target fetch/ancestry completion and readiness checks that require matching Slice state and evidence hashes.
- [ ] 6.3 Pin downstream worktree actual base SHA and recheck every upstream Candidate before creation/launch.
- [ ] 6.4 Pass unmerged, preserving-merge, squash/cherry-pick unsupported, target mismatch and TOCTOU git fixtures.

## 7. Local recovery and status

- [ ] 7.1 RED-test persisted `retry-build`, `retry-verify`, `retry-review` and `abandon` actions with required actor.
- [ ] 7.2 Route `slice-action` through the existing control request queue so daemon/manager remains the only state writer.
- [ ] 7.3 Implement action consumption and a single status response listing all needs-human reasons, evidence and legal next actions.

## 8. Canary and documentation

- [ ] 8.1 Pass a disposable end-to-end temporary repo/remote/fake-agent canary covering false completion, foreign review, stale verdict, merge ancestry, crash recovery and reaper negatives.
- [ ] 8.2 Update README and `CHANGELOG.md [Unreleased]` with state semantics, contracts, limits, operator actions and best-effort cleanup warning.

## 9. Completion gates

- [ ] 9.1 Pass the full pytest suite, `python3 -m policy_check --repo .` and `git diff --check`.
- [ ] 9.2 Complete independent code review, re-review every fix and resolve all Critical/Important adversarial findings.
