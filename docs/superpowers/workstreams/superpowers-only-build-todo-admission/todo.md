---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
domain_breadth: 0
state_consistency: 0
invariant_count: 16
artifact_classes:
  - source
  - tests
  - documentation
---

# Superpowers-only Build Todo Admission Todo（#1054）

## Boundary

- Authority: live [#1054](https://github.com/hamanpaul/paulsha-cortex/issues/1054), Child A of [#1051](https://github.com/hamanpaul/paulsha-cortex/issues/1051). Unique work item: `superpowers-only-build-todo-admission`; canonical Todo path is this owner-published workstream artifact.
- Distinct planning authority: [spec](../../specs/superpowers-only-build-todo-admission-spec.md) and [design](../../specs/superpowers-only-build-todo-admission-design.md); all three artifacts share issue #1054 and this work_item. #1053 is Draft parent context only and is not accepted authority.
- Live incident: #983 run `workflow-52d048b72adbd5cae06f` reached build/verify/review and PR #1049 with PR=1, OpenSpec=0, Todo=0. #1051 says Child A adds first-Builder admission; #1055 owns existing Candidate/PR recovery.
- Hard prerequisites before #1054 intake: #1063 Todo metadata/source qualification and existing-path link guard; #1064 latest Monitor correlation generation/input watermark; #1065 strict WorkAuthority freshness consumer (blocked by #1064). #1055 remains blocked by #1054. This packet does not authorize implementation while those prerequisites are open.
- Manager owns first-Builder admission, stale pre-Builder claim stop and operator diagnostics. #1063 owns provenance/work_item/Tasks/path qualification; #1064 owns generation producer; #1065 owns trusted WorkAuthority reader.
- Existing official exact-run abandon is only a documented operator release for an undelivered pre-Builder run, under the strict predicates in R5. #1054 does not implement abandon, mutate old claim/evidence, or treat abandon as completion.
- Preserve #1055 for Candidate/PR recovery, #810 merge-after-checkbox, #972/#973 PR #1049 conflict, and #911 OpenSpec=0 support. Keep ship's unique-Todo backstop unchanged.
- No new OpenSpec change is needed. Keep `VERSION` unchanged. Planning only: no work intake or formal state mutation.

## Sizing dimensions

Official input is the `fix-standard` combo, its current gate spine/persona bindings, and the accepted artifact triad. The #1054 implementation slice consumes the three hard prerequisites; it does not implement their Monitor, parser, link, or WorkAuthority state changes.

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 0 | One production responsibility/module: Manager dispatch admission and stale pre-Builder stop. |
| `state_consistency` | 0 | Compare the trusted authority to existing run claim fields; no new state or claim mutation. |
| `acceptance_surfaces` | 2 | Current `fix-standard` gate spine plus repo-wide R-09/R-16/R-19 policy surfaces. |
| `spec_stability` | 0 | Accepted complete spec/design/Todo with no blocking marker. |
| `orchestration` | 2 | Current `fix-standard` combo has nine cards and multiple persona bindings. |
| **Total** | **4 / Yellow** | Recompute with official `current_sizing_snapshot()` before implementation intake; do not include prerequisite implementation in this child sizing. |

## Tasks

- [ ] **T0 revalidate issue and prerequisites**: before code intake, re-read #1054, #1051, #1055, #983, #911, #810, #972/#973 and Draft #1053. Require #1063, #1064 and #1065 to be complete and accepted first. Verify #1063 returns qualified issue/work_item/Tasks/path facts and #1065 proves latest successful generation observes current override input; do not rely on existing scanner/link behavior without its new gates.
- [ ] **T1 tests / direct resume stale authority** (`tests/test_manager_build_todo_admission_1054.py`): with a true Manager and exact WorkflowRun, cover source/claim match, Todo mapping/revision drift, non-Todo plan-output-only drift, wrong run claim key and strict WorkAuthority error. A stale direct resume before any Builder job must return typed `stale-pre-builder-claim` before reservation, worktree or launch; persisted old claim/evidence must be byte-for-byte unchanged.
- [ ] **T2 tests / zero, multiple and trusted source admission**: using #1063/#1065 fixtures, cover Superpowers-only/OpenSpec=0 with zero, one and multiple qualified Todo; issue/work_item/Tasks/path rejection; successful link override not yet seen by successful generation; old success plus latest refresh failure; stale/unknown generation. Assert all stops produce zero Builder job reservations/creations, worktree calls and agent launches.
- [ ] **T3 source / Manager gate and exact run reconciliation**: in `manager.py`, call the shared first-Builder decision at the actual Builder dispatch seam and from direct resume when no Builder job exists. Consume only strict trusted WorkAuthority and #1063 qualification. Require exactly one Todo and compare `authority_digest_without_planning_outputs(authority)` to `run.source_revision`; recompute claim key from exact run repo/work_id/source_revision and compare it to `run.claim_key`. Planning-owned spec/plan output revisions may be excluded; Todo/source revision changes may not. Fail closed on mismatch without resetting or rewriting the old claim.
- [ ] **T4 diagnostics / accurate repairs for all stops**: zero says owner publish -> link existing qualified path -> await a matching latest successful generation. Once ready, do not resume the stale old claim: if exact old run has no Builder job, Candidate, PR or active job, explicitly abandon that undelivered run with `cortex work abandon <work_id> --repo <owner/repo> --actor <operator> --expected-run-id <exact-run-id> --reason <single-line-reason>`, then formal start/intake a new generation. Explain that this abandons unfinished work and is not completion. Active job means wait/fail closed; Candidate/PR routes to #1055. Multiple names each extra path and gives exact `cortex work unlink <work_id> --repo <owner/repo> --kind path --ref <repo-relative-extra-path>`; only after successful fresh generation may formal admission continue. Do not recommend unlink for zero or direct resume for stale claim.
- [ ] **T5 acceptance / dispatch side-effect ordering**: put the gate after plan-final phase transition but before any first Builder provider selection/preflight, ID reservation, worktree, job or launcher side effect; make direct resume pass through the same helper. Prove exact current run identity and all counters in tests.
- [ ] **T6 acceptance / recovery and ship boundaries**: preserve `work_actions._ship_action()` unique PR=1/Todo=1/OpenSpec=0-or-1 backstop; keep #911 OpenSpec=0 lane. Do not alter #1055 Candidate/PR recovery, #810 checkbox closure, #972/#973 main conflict, claim keys, delivery journal, existing evidence or CLI commands. Test/verify abandon instruction only applies before any Builder job/Candidate/PR/active job, and canonical owner Todo is not removed by planning-artifact GC.
- [ ] **T7 documentation and changelog (R-09, R-22)**: update lifecycle docs with first-Builder timing, exact run/claim comparison, stale resume stop, and the explicit abandon→new-start procedure. Preserve #1055 boundary and source/generation ownership; add the implementation PR changelog fragment and Unreleased entry.
- [ ] **T8 tests / policy gates (R-16, R-19)**: run named focused Manager tests and relevant claim/Monitor fixture suites, full required tests, official sizing/completeness/review, `openspec validate --specs --no-interactive`, CLI help check if commands/docs alter help, PR-context `policy_check`, and diff check. Test with local fixtures only; report every skipped gate.

## Acceptance checklist

- [ ] First Builder admission consumes one qualified Todo from the latest successful, current-input trusted Monitor generation; 0/multiple/invalid/stale all stop before job/worktree/agent side effects.
- [ ] Direct resume of a no-Builder run recomputes the filtered authority digest and exact claim key; stale Todo/source generation fails closed without mutating old run state.
- [ ] Only spec/plan planning-output revision drift is filtered; issue/Todo/source drift remains claim drift.
- [ ] Diagnostics identify reason/count/run/work/authority and provide correct publish/link/fresh generation or exact unlink repair. Stale claims are never continued by resume; any new generation follows exact abandon of the undelivered pre-Builder run and formal start/intake.
- [ ] #1055/#983 Candidate/PR recovery, #810 checkbox closure, #911 OpenSpec=0 and #972/#973 PR-conflict ownership remain separate; ship unique-Todo backstop remains.
- [ ] Official sizing, accepted completeness, review, OpenSpec, PR-context policy and diff checks are all green before claiming planning ready. Implementation code/merge/runtime remain later states.
