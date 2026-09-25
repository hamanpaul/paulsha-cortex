---
status: accepted
work_item: architecture-html-cdp-timeout
issue: 1084
domain_breadth: 2
state_consistency: 0
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Architecture HTML Chrome CDP timeout 計劃（#1084）

## Authority and scope

- Authority: live issue #1084. The current PR publishes this planning packet only; it does not merge or start implementation.
- Candidate source baseline: origin/main 574b4f513c013e58705f0587b3bfb7dfaec7b788.
- Exact-name/content search found no existing active OpenSpec or committed plan for this failure. The 2026-09-23 handoff records an earlier flaky Architecture HTML rerun but no diagnosis or accepted fix.
- The implementation spans the external Archify CDP seam and this repository's CI integration. Archify source changes require an upstream reviewed commit before this repository changes its pin.
- The plan only uses #1067 and #1083 run outcomes as evidence. Do not edit or act on #966/#987 runs, PRs, source, or journals.
- Do not edit Architecture HTML source/data. Keep the exact rebuild comparison, Archify visual-check, and native Playwright navigation/interaction/capture checks required.
- Do not open a Cortex intake, merge a PR, install a runtime, or deploy from this planning work.

## Observed evidence

| Candidate | Initial Architecture HTML attempt | Same-head rerun | Other status |
| --- | --- | --- | --- |
| #1067, head 822b105adccc11950ce44e794ad95074cf8b8e1a | Attempt 1 failed at 2026-09-25 05:57:11Z with Target.getTargets timeout. [Failure](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36100607645/attempts/1) | Attempt 2 passed at 05:59:09Z. [Success](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36100607645/attempts/2) | PR #1067 merged at 06:06:18Z. |
| #1083, head 0a5b4a624604649a5bfe447695d82395179aa44e | Attempt 1 failed at 06:11:20Z with Target.getTargets timeout. [Failure](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36101642910/attempts/1) | Attempt 2 passed the Architecture HTML review at 06:13:27Z. [Success](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36101642910/attempts/2) | At 06:26:44Z, all PR checks, including pytest 3.13, had passed. PR #1083 remained open. |

Both failures occurred in the same named visual-check step and neither candidate changed docs/architecture/architecture.html according to issue #1084. A previous GitHub rerun launched another hosted job, so the rerun evidence does not prove that the physical runner was unchanged or identify runner pressure as the cause.

The exact pinned upstream source sets a 15000 ms default for CDP sends and calls Target.getTargets in ChromeVisualBrowser.attach(). Its timeout timer rejects without Chrome process/stderr details. Pipe/process errors do include those details. runVisualCheck converts the error to viewer/visual-check-runtime, persists the receipt, then closes Chrome and removes its profile. The [pinned source](https://raw.githubusercontent.com/tt-a1i/archify/a07fa1d5b2a10cbea110c5a2be2817397a301cdc/archify/bin/visual-check.mjs) confirms the responsibility boundary.

## Official sizing

The official `current_sizing_snapshot()` was run on 2026-09-25 against this accepted spec/design/plan set with `fix-standard`; it returned **(6, Yellow)**. The snapshot uses two gate-spine entries, three applicable contract rules, nine combo cards, and nine persona bindings.

- domain_breadth: 2 — one upstream Archify CDP owner boundary plus this repository's CI integration boundary.
- state_consistency: 0 — no durable application state, transaction, or cross-store write is introduced; all diagnostic evidence is per workflow run.
- acceptance_surfaces: 2 — two gate-spine entries plus applicable rules R-09, R-16, and R-19.
- spec_stability: 0 — the completeness assessor accepted all three planning artifacts with no missing kinds or blockers.
- orchestration: 2 — the `fix-standard` combo has nine cards, all with persona bindings.

This Yellow result does not require decomposition. Recompute if the pinned engine ownership, changed modules, acceptance surface, or combo changes; if the result becomes Red, split upstream diagnostics/retry and local workflow integration into separately scoped issue-backed work before implementation. Do not silently reduce the accepted requirements.

## Tasks

### T0. Freshness and upstream ownership

- [ ] Before implementation, reread #1084 and the exact PR/run attempts linked above. Confirm the same timeout and verify no upstream Archify fix superseded the pinned commit.
- [ ] Find or establish a separately reviewable upstream Archify issue/PR for structured Target.getTargets timeout evidence and the one-retry contract. Stop if owner approval or an upstream candidate is missing.
- [ ] Confirm the #1084 implementation scope still excludes #966/#987 product logic and does not overlap active run, PR, or journal ownership.

### T1. Archify diagnostic and retry

- [ ] Add a typed timeout record for Target.getTargets containing method, configured timeout, elapsed duration, attempt, Archify revision, Chrome identity/process state, and a bounded stderr tail. Redact environment/credential data.
- [ ] Retry exactly once after 1 second only when the first Target.getTargets request timed out and Chrome remains alive. Keep the same browser process and CDP pipe.
- [ ] Add fake-CDP tests for first-attempt recovery, second-attempt timeout, late first response, Chrome exit/pipe failure, other CDP method timeout, and actual visual assertion failure.
- [ ] On recovery, add an informational/warning record without setting receipt failure; on exhaustion, retain a specific failure diagnostic and nonzero exit.

### T2. Cortex workflow integration

- [ ] Update .github/workflows/architecture-html.yml only after the reviewed upstream Archify commit exists; pin its exact immutable commit.
- [ ] Save allowlisted runner/image/kernel/load/memory/disk context, Node version, Archify revision, and ARCHIFY_CHROME path/version for each run.
- [ ] Save visual-check stdout as visual-check.json and command stderr as visual-check.log; retain the existing if: always() artifact upload and include both attempt records.
- [ ] Preserve the current required exit status, exact HTML rebuild comparison, Playwright browser review, and capture upload. Do not use continue-on-error, skip, or a blanket workflow retry.

### T3. Verification and delivery accounting

- [ ] Run tests/test_architecture_docs.py and tests/test_architecture_phase_dispatch.py plus upstream Archify's targeted visual-check tests.
- [ ] Run the actual Architecture HTML workflow against the exact candidate. Verify successful navigation/interaction/captures and verify persistent timeout, Chrome exit, and content failure remain red with actionable artifacts.
- [ ] Run OpenSpec strict, relevant docs/reference checks, and PR-context policy check; inspect the exact diff and all changed paths.
- [ ] Keep product implementation, CI integration, merge, installed runtime, and deployment as separate states. Close #1084 only after the required behavior is merged and verified; this planning PR leaves it open.

## Completion

Implementation is complete only when the exact transient startup timeout is diagnosed and boundedly retried, every original visual gate still runs and remains fail-closed, and the failing and successful paths produce the specified evidence. This plan is complete as a planning contract; it is not evidence of implemented or deployed behavior.
