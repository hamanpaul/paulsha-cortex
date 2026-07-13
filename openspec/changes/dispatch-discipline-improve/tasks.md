> Detailed RED/PASS commands, exact files and commit boundaries are canonical in `docs/superpowers/plans/2026-07-12-dispatch-discipline-improve.md`. This checklist tracks OpenSpec apply progress without duplicating that execution plan.

## 1. Scoped broker cleanup

- [ ] 1.1 RED-test that periodic CLI/daemon ticks never call the reaper and manual cleanup defaults to dry-run.
- [ ] 1.2 Implement `cortex reap-brokers`, require `--cwd-root` for apply, recheck live process identity and send SIGTERM only.
- [ ] 1.3 Pass Python wiring and fake `/proc` negative fixtures, including cross-project and PID-reuse cases.

## 2. Versioned coordinator state

- [ ] 2.1 RED-test versioned `jobs+slices` state, `done → exited`, legal transitions, reloadable current/history containers and clean-start rejection.
- [ ] 2.2 Implement atomic SliceRecord CRUD/action history while retaining Job history and single-writer persistence.
- [ ] 2.3 Pass registry, dispatcher, completion, manager and restart focused tests.

## 3. Deterministic verification

- [ ] 3.1 RED-test strict `target_branch` and verification frontmatter parsing plus dispatch-time spec/plan/contract hashes.
- [ ] 3.2 Implement versioned verification evidence, Candidate fencing, required artifact/scope/task checks and base-vs-Candidate no-regression suite.
- [ ] 3.3 Replace false-green regressions so exit, rejection, timeout, exception and incomplete evidence cannot release downstream.

## 4. Foreign exact-HEAD review

- [ ] 4.1 RED-test explicit model identity mapping, different-domain selection, detached exact Candidate checkout and verdict provenance.
- [ ] 4.2 Implement reviewer Jobs, immutable GateEvaluations and cortex-owned blocking-category classification.
- [ ] 4.3 Pass same-domain, unknown identity, stale HEAD, malformed verdict, blocking and non-blocking review cases.

## 5. Artifact-aware dependency release

- [ ] 5.1 RED-test versioned CompletionRecord validation, record-first crash ordering and orphan-record restart handling.
- [ ] 5.2 Implement remote target fetch/ancestry completion and readiness checks that require matching Slice state and evidence hashes.
- [ ] 5.3 Pin downstream worktree actual base SHA and recheck every upstream Candidate before creation/launch.
- [ ] 5.4 Pass unmerged, preserving-merge, squash/cherry-pick unsupported, target mismatch and TOCTOU git fixtures.

## 6. Local recovery and status

- [ ] 6.1 RED-test persisted `retry-build`, `retry-verify`, `retry-review` and `abandon` actions with required actor.
- [ ] 6.2 Route `slice-action` through the existing control request queue so daemon/manager remains the only state writer.
- [ ] 6.3 Implement action consumption and a single status response listing all needs-human reasons, evidence and legal next actions.

## 7. Canary and documentation

- [ ] 7.1 Pass a disposable end-to-end temporary repo/remote/fake-agent canary covering false completion, foreign review, stale verdict, merge ancestry, crash recovery and reaper negatives.
- [ ] 7.2 Update README and `CHANGELOG.md [Unreleased]` with state semantics, contracts, limits, operator actions and best-effort cleanup warning.

## 8. Completion gates

- [ ] 8.1 Pass the full pytest suite, `python3 -m policy_check --repo .` and `git diff --check`.
- [ ] 8.2 Complete independent code review, re-review every fix and resolve all Critical/Important adversarial findings.
