---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
domain_breadth: 0
state_consistency: 0
invariant_count: 12
artifact_classes:
  - source
  - tests
  - documentation
---

# Superpowers-only Build Todo Admission Todo（#1054）

## Boundary

- Authority: live [#1054](https://github.com/hamanpaul/paulsha-cortex/issues/1054), Child A of [#1051](https://github.com/hamanpaul/paulsha-cortex/issues/1051). Unique work item: `superpowers-only-build-todo-admission`; canonical owner-published path: `docs/superpowers/workstreams/superpowers-only-build-todo-admission/todo.md`.
- Planning evidence: distinct [spec](../../specs/superpowers-only-build-todo-admission-spec.md) and [design](../../specs/superpowers-only-build-todo-admission-design.md), all three artifacts have the same `work_item` and issue provenance. This accepted packet does not start a Cortex run or authorize implementation by this PR.
- Live incident basis: #983 `workflow-52d048b72adbd5cae06f` reached build/verify/review and created PR #1049 with WorkAuthority PR=1, OpenSpec=0, Todo=0; #1051 records that the defect is missing `mapped_todo_paths`, not multiple PRs. #1053 is an open Draft parent planning PR; this packet is a separate Child A contract and binding.
- Owner: Manager owns the plan→first-Builder admission and diagnostic. WorkAuthority/Monitor owns mapped source identity and revisions; GitHub remains issue/PR fact provider.
- No implementation of existing-Candidate/PR recovery, claim-era CAS, verify/review evidence invalidation, delivery journal or PR operations; those belong to #1051's recovery child. No #1049 main conflict repair (#972/#973), no #810 merge-after-checkbox closure change, and no regression of #911 `mapped_openspec=0` support.
- No new OpenSpec change is needed: the acceptance contract is Superpowers-only and canonical `openspec validate --specs` remains a repository gate. Keep `VERSION` unchanged.

## Sizing dimensions

The official helper uses `fix-standard`, current cards/gate-spine/contract surfaces and the accepted artifact triad. This child has one production responsibility in `manager.py`, no new persistent source/claim/delivery state, and an accepted stable planning contract.

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 0 | One production module; existing WorkAuthority, Monitor scanner and DiagnosticReason contracts are reused. |
| `state_consistency` | 0 | Read current confirmed authority; write only the existing typed Manager stop. No new store, revision writer, CAS or delivery state. |
| `acceptance_surfaces` | 2 | `fix-standard` core gate spine plus applicable R-09/R-16/R-19 process rules. |
| `spec_stability` | 0 | All three planning artifacts accepted and complete, with no blocking markers. |
| `orchestration` | 2 | Current `fix-standard` has nine cards and multiple persona bindings. |
| **Total** | **4 / Yellow** | Recompute with `current_sizing_snapshot()` immediately before implementation; any new module/state surface requires re-sizing. |

## Tasks

- [ ] **T0 source and owner verification**: before implementation, re-read live #1054 and confirm the build-before-Builder scope; verify the current WorkAuthority/Monitor snapshot contract, canonical Todo parser, path/symlink guard and existing `link --kind path` behavior. Do not adopt #1053's parent draft as authority and do not mutate production work items, snapshots or runs.
- [ ] **T1 tests / admission RED** (`tests/test_manager_build_todo_admission_1054.py`): use the real Manager plan→build dispatch seam with WorkAuthority fixtures for Superpowers-only and OpenSpec=0. Cover unique Todo, no Todo, multiple Todo paths, invalid source, and link override absent from a fresh snapshot. Assert zero/multiple/invalid cases stop before Builder side effects and have zero Builder job reservations/creations, worktree creation calls and agent launches; assert the unique confirmed case reaches the normal first Builder admission path.
- [ ] **T2 source / confirmed WorkAuthority rule** (`source`): use the strict existing WorkAuthority loader keyed by `(repo, work_id)` and current successful Monitor snapshot. Count only its `mapped_todo_paths`; never treat an override, accepted Superpowers plan, `todo.md` basename, or plan checkbox as an authority mapping. Require the existing scanner's canonical `docs/superpowers/workstreams/<slug>/todo.md` source, issue provenance, exact matching `work_item` and concrete Tasks; reuse the existing parser and symlink/path guard.
- [ ] **T3 source / Manager stop before side effects** (`source`): in `manager.py`, add the first-Builder admission outcome before provider selection, job-id reservation, worktree creation, `registry.create_job()` and `launcher.launch()`. Exactly one valid fresh source continues; zero and multiple produce distinct stable typed reasons. Missing, invalid, stale or failed Monitor authority also stops fail-closed. Do not create a new authority writer or change claim/revision state.
- [ ] **T4 diagnostics / actionable zero and multiple repairs** (`documentation`): persist a typed `DiagnosticReason` with reason, count, authority reference and `next_step_hint`. Zero tells owner to publish canonical issue-backed Todo, run `cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-todo-path>` for the existing scanner source, wait for fresh Monitor correlation, then return through formal Manager `start`/`intake`; zero must not suggest `unlink`. Multiple names the extra source and gives an exact `cortex work unlink ... --kind path --ref ...` action for only that mapping, followed by a fresh snapshot and formal admission.
- [ ] **T5 tests / source and snapshot boundaries** (`tests`): using the true WorkAuthority and Monitor-source parser fixtures, reject missing/noncanonical/symlink-escaping source paths and forged path overrides; prove a successful override write alone leaves `mapped_todo_paths=0` for admission until a fresh snapshot confirms it. Test reason code, observed count, authority reference, source location and next action for both zero and multiple cases.
- [ ] **T6 source / ship and neighboring issue boundaries** (`source`): preserve `work_actions._ship_action()`'s existing PR=1, Todo=1, OpenSpec=0/1 backstop and #911's OpenSpec=0 path; do not change #810 checkbox closure, #972/#973 main-conflict handling, or #983 existing Candidate/PR recovery. Verify no new CLI surface is needed; if implementation adds one or changes documented commands, re-scope CLI help and docs tasks before coding.
- [ ] **T7 documentation / changelog** (`documentation`): update the lifecycle docs with the plan/build admission contract, exact zero/multiple next actions and the distinction between a path-link override and a fresh confirmed snapshot. Add the implementation branch's `changelog.d/<slug>.md` and `CHANGELOG.md [Unreleased]` entry; keep release `VERSION` unchanged.
- [ ] **T8 validation / policy and delivery** (`tests`, `documentation`): run focused true-Manager/WorkAuthority tests, repository-required tests, `openspec validate --specs`, PR-context `policy_check` including R-09/R-16/R-19/R-22, CLI help check if affected, and `git diff --check`. Review the exact diff and Builder side-effect counters before opening the implementation PR. Keep code, merge, runtime and loaded-service evidence separate.

## Acceptance checklist for implementation

- [ ] Zero or multiple confirmed Todo paths cause distinct typed stops before any first Builder job/worktree/agent side effect.
- [ ] One owner-published canonical Todo with issue provenance, matching work_item and concrete Tasks is the only accepted source; a `path` link only points at an existing scanner source and needs fresh Monitor confirmation.
- [ ] zero diagnostic gives publish→link→fresh snapshot→formal start/intake without unlink; multiple diagnostic gives precise extra-path unlink guidance and requires fresh snapshot.
- [ ] Existing ship backstop and #911, #810, #972/#973 and #983 boundaries remain intact.
- [ ] Focused tests, official sizing/completeness/review gates, OpenSpec, PR-context policy and diff checks pass; code is committed in an implementation PR before this issue can be called delivered.
