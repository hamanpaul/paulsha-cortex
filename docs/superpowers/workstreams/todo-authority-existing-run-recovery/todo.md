---
status: draft
work_item: todo-authority-existing-run-recovery
domain_breadth: 2
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Todo Authority 前進後 Existing Candidate／PR Recovery Todo（#1055）

## Scope and hard dependencies

- Parent issue: #1055; parent #1051; source/admission predecessor #1054; regression/source case #983, run workflow-52d048b72adbd5cae06f, Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc, PR #1049.
- Hard implementation gate A: #1063, #1064, #1065 and #1054 are all merged. Freeze the actual child issue IDs, accepted contracts, and exact main revisions before intake. Do not infer these capabilities from #1057 or any other Draft.
- Hard implementation gate B: #966 exact-revision JobRegistry CAS and #983 conditional-write/serialization/read-back contracts are merged or a fresh main audit proves an accepted equivalent. If absent, stop the journal/delivery slice; do not clone its writer. Preserve the live #983 run and PR #1049.
- External delivery boundary: #972/#973 owns PR #1049 main-conflict repair. #1015/#1017 own exact-D push, existing-PR C→D update witness, post-push CI and closeout. This recovery only reads the unchanged Candidate/head and stops on the current conflict; it creates no C→D update intent.
- Merged/closed PR recovery belongs to #962/#975/#976/#977; Todo checkbox closure belongs to #810.
- This branch is planning only. No intake, product code/tests, registry/journal mutation, #983 run operation, push-to-PR branch, PR creation, merge, or live recovery.

## Read-only evidence baseline

Observed 2026-09-25 from main-origin worktree and live GitHub/provider projections:

- origin/main: 6a32a3e5e0af841794f340313c11f60f2999f6ae.
- #983 run workflow-52d048b72adbd5cae06f: ongoing review, needs-human, reason multiple-delivery-targets-unsupported; Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc.
- PR #1049: open; head exactly equals Candidate; merge state DIRTY/CONFLICTING.
- Work item sources included #983, PR #1049, accepted spec/design/plan and workflow run; no canonical Todo source. The projection revision registry:1025 is not the source authority revision.
- Installed CLI attention projection currently suggests abandon. This plan explicitly rejects that action for an existing Candidate/PR. No abandon command was run.
- PR #1057 for #1054 remains Draft (checks were green when observed); its content is not an accepted contract.

## Official five-dimension sizing

Run the repository’s actual sizing adapter against these exact three canonical artifacts and fix-standard combo:

python3 - <<'PY'
from paulsha_cortex.coordinator.work_bridge import current_sizing_snapshot
rows = [
    {"kind": "spec", "ref": "docs/superpowers/specs/todo-authority-existing-run-recovery-spec.md"},
    {"kind": "design", "ref": "docs/superpowers/specs/todo-authority-existing-run-recovery-design.md"},
    {"kind": "plan", "ref": "docs/superpowers/workstreams/todo-authority-existing-run-recovery/todo.md"},
]
print(current_sizing_snapshot(workspace_root=".", combo_name="fix-standard", artifact_rows=rows))
PY

Expected draft result is 10 / Red: domain_breadth=2 (registry CAS, Manager recovery and delivery integration exceed a single bounded production module); state_consistency=2 (run/claim era, gate evidence, delivery journal, Candidate and GitHub PR cross-store crash/idempotence); acceptance_surfaces=2 (fix-standard has 2 gate_spine and repo rules R-09/R-16/R-19, signal 5); spec_stability=2 (draft planning authority); orchestration=2 (9 cards with 9 persona bindings). The accepted-triplet counterfactual is 8 / Red. Preserve every #1055 acceptance; do not intake/dispatch at either result.

### Actual issue-backed Yellow children

Duplicate search reconciled the closest live scopes before issue creation: #966 is the generic JobRegistry raw-revision CAS consumed by #1068; #962/#887/#975/#976/#977 cover merged-PR completion; #983 is the generic delivery-journal writer; #1015/#1017 cover exact-D push, existing-PR C-to-D update witness, post-push CI and closeout. These are hard boundaries, not duplicate child acceptance.

1. **#1068 — registry exact-run CAS reset primitive.** Owner role: workflow registry maintainer. Production scope only registry.py, after #966 and #1063/#1064/#1065/#1054 land. Acceptance: compare exact durable revision and run/status/phase/claim/source/Candidate/verified/gate/job/PR tuple; atomically invalidate verify/review only; preserve Candidate, build evidence, old jobs/claim era and PR; stale/replayed/conflicting writes are unchanged/idempotent. Expected accepted-triad projection 0+2+2+0+2=6 Yellow; no parallel raw-file writer.
2. **#1069 — explicit same-run authority restart/reverify.** Owner role: Manager/work-actions maintainer, distinct from #1068. Production scope only work_actions.py; hard dependencies #966, #1063/#1064/#1065/#1054 and #1068. Acceptance: explicit operator resume, exact old/new fresh WorkAuthority vector/digest and PR/Candidate preflight, invoke #1068 CAS once, then revalidate and rerun verify/review only on the exact Candidate; zero GitHub/journal writes and no abandon/new run. Expected projection 0+2+2+0+2=6 Yellow.
3. **#1070 — existing-PR journal authority read-back.** Owner role: delivery journal integration maintainer, distinct from #1068/#1069. Production scope only work_actions.py, after confirming existing read APIs suffice; hard dependencies #983 contract, #1069, #1068, #966 and #1054 package. Acceptance: reconcile the same run/PR/Candidate row under the new fresh authority by #983 conditional write and exact read-back; same identity/payload is a no-op, unknown/drift fails closed; zero push/create/merge/close and no #1015 C-to-D update intent. Expected projection 0+2+2+0+2=6 Yellow. If a second production module is needed, stop and re-scope.

All three GitHub issues were created with separate acceptance and dependency chains; they are currently unassigned because no CODEOWNERS or distinct named maintainers are declared in this repository. Their issue bodies name distinct owner roles and require a named assignee before intake. Their sizing is a projection only; each owner must publish/accept a complete triad and rerun the official sizing helper before intake. Child completion does not close #1055; the parent retains the end-to-end #983 fixture regression.

## Five-dimension sizing table

| Dimension | Draft score | Accepted-triplet counterfactual | Basis |
|---|---:|---:|---|
| domain_breadth | 2 | 2 | End-to-end exact recovery crosses registry state, Manager decision/reverification and delivery read-back surfaces; implementation boundary depends on actual #1054 and #983 contracts. |
| state_consistency | 2 | 2 | Exact CAS spans run/claim/source era, gates, job evidence, delivery journal and remote PR facts across crashes. |
| acceptance_surfaces | 2 | 2 | fix-standard gate_spine=2 plus R-09/R-16/R-19=3, signal=5. |
| spec_stability | 2 | 0 | Draft status is current authority; complete accepted artifacts would score zero risk. |
| orchestration | 2 | 2 | fix-standard has 9 cards and 9 persona bindings. |
| Total | 10 / Red | 8 / Red | No intake/dispatch under either result. |

fix-standard and the three process-level rules are verified from the checkout; rerun the official helper before each child intake. If current source discovery changes the actual modules or dependency graph, revise declared dimensions and re-score; never shrink acceptance just to achieve Yellow.

## Tasks

- [ ] T0 — Re-read #966, #1063, #1064, #1065 and #1054 after merge. Record actual child issue IDs, accepted contracts, source scanner and freshness behavior, and exact main revision. Verify unique owner-published Todo source, source-validation path, provider freshness, and claim-after-Todo digest consistency as one integration gate.
- [ ] T1 — Confirm #983 conditional-write contract on main and inspect exact public API, conflict/unknown semantics, row identity and read-back. Do not use PR #1049 branch as implementation authority.
- [ ] T2 — Freeze the complete old/new authority vector and source digest semantics; document full CAS tuple for registry, run, candidate, PR, jobs/evidence and journal.
- [ ] T3 — Implement/accept #1068 exact run tuple CAS with one durable transition, conflict no-op, duplicate-request idempotence and no old job/evidence rewrite.
- [ ] T4 — Implement/accept #1069 explicit resume path: no automatic restart; reset verify/review only; preserve build/Candidate/PR/history; re-read exact current authority and PR facts before job dispatch.
- [ ] T5 — Implement/accept #1070 delivery read-back path on the existing exact PR and landed #983 conditional writer; unknown/conflict stops without external mutation.
- [ ] T6 — Regression using real Manager/WorkAuthority/Registry fixtures plus stub GitHub facts for the exact #983 identity. Add source missing/ambiguous/stale/digest-drift cases; old/new claim era and source revision CAS; active job and stale evidence; candidate/PR mismatch; existing PR conflict; concurrent stale registry/journal; crash/re-entry at each boundary; repeated explicit resume. Assert no duplicate job/push/PR/merge, no false closure, and no abandon/new run.
- [ ] T7 — Keep original #983 fixture read-only. No command against live run, no editing #983 issue/run/PR, and no operation of PR #1049. Report fixture-only evidence separately.
- [ ] T8 — Update lifecycle/runbook and tests documentation if actual behavior changes; add committed changelog fragment and CHANGELOG [Unreleased]; keep VERSION unchanged.
- [ ] T9 — Run the required focused/full tests for implementation, strict OpenSpec, repository policy with actual PR context, and exact-head CI. Current planning PR only runs OpenSpec/policy; do not run pytest unless requested.
- [ ] T10 — Recompute official five-dimension score after any accepted contract, module boundary, artifact completeness or PR-context change. Keep parent issue open until #1068/#1069/#1070 plus exact #983 integration acceptance pass.

## Acceptance traceability

| #1055 requirement | Task evidence |
|---|---|
| Published #1054 scanner/Todo/freshness/digest prerequisites | T0 records actual child IDs, accepted contracts and exact main revision |
| #983 conditional write, not a second journal writer | T1/T5 verify landed API and use it |
| Exact source/Candidate/PR/run/journal CAS | T2–T5 compare every identity and revision |
| Authority restart invalidates only verify/review | T3/T4 inspect durable state and preserve build/Candidate |
| Idempotent existing-PR read-back | T5 uses exact existing PR and same journal identity |
| Crash/duplicate prevention | T3/T5/T6 exercise duplicate request and boundary re-entry |
| #983 regression is fixture-only | T6/T7 |
| No abandon or new-run substitute | T3/T4/T6 assertions |
| PR/journal conflict is outside scope | T1/T5 stop without touching #1049; #972/#973 own conflict repair |
