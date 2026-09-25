---
status: draft
work_item: todo-authority-existing-run-recovery
---

# Todo Authority 前進後既有 Run Recovery Design（#1055）

## D1 — Deliverable and authority boundary

#1055 owns only the safe recovery of an already existing Candidate and PR after a new canonical Todo authority is published. It does not own #1054 admission/source semantics or #983 delivery-journal write serialization. Before any product intake, re-read the actual merged #1054 implementation and every issue-backed prerequisite from main; require their accepted source/scanner/owner/provenance/freshness/digest contract as one hard gate. Draft PR #1057 is planning evidence only and grants no capability.

The old run remains the authority for its Candidate, job history, evidence identity, PR and journal relationship. The new Todo changes current WorkAuthority and claim digest; it does not retroactively rewrite the old run’s source vector or job bindings.

## D2 — Recovery decision ordering

The only entry is an explicit operator resume for the exact ongoing run. Periodic Monitor scans may report readiness or blockers but cannot commit a reset or dispatch recovery jobs.

1. Confirm #966 raw-revision JobRegistry CAS is merged. Confirm #1063, #1064, #1065 and #1054 are merged with their accepted contracts. Store actual issue IDs, exact merge/head revisions and accepted contracts in this workstream before implementation intake.
2. Confirm #966's raw-revision JobRegistry CAS is on main before the registry child; confirm #983's conditional journal write and identity-preserving read-back contract is on main before the delivery child. If it is not, stop the journal/delivery slice. Do not copy _save_runs or create a parallel writer.
3. Reload the current WorkAuthority from the official provider. Require one owner-published canonical Todo, valid issue/work_item binding, scanner-confirmed source, fresh Monitor observation and internally consistent source revisions. Do not derive a substitute Todo from the Superpowers plan.
4. Read the exact WorkflowRun and registry baseline, the matching JobRegistry rows, delivery journal row/revision, and authenticated remote facts for the one existing PR. Require Candidate==PR head, PR open, no active job, and all identities unique.
5. Capture an immutable recovery request identity and complete expected tuple: durable registry revision; run identity/status/phase/claim/source/Candidate/verified/gate/job/PR bindings; old and new complete authority vectors/digests; journal identity/hash/revision; PR repository/number/remote identity/state/head/base; operator request identity.
6. Re-read all locally and remotely observable facts immediately before the registry transition. Call a registry-only CAS that atomically revalidates the old expected tuple and applies the bounded same-run verify/review reset. Do not partially mutate run memory if compare or persistence fails.
7. After commit, reload WorkAuthority and the same PR/journal identities again before dispatch. If anything drifted, keep gates pending and stop. If exact, dispatch verify/review against the unchanged Candidate, each at most once under its existing job binding.
8. After new gates pass, reconcile the existing PR/journal through conditional write plus exact read-back. No push, PR create/adopt, merge, close, or external success claim occurs in this recovery path.

There is no atomic transaction across registry, WorkAuthority providers, GitHub and delivery journal. The design therefore keeps external writes out of recovery and places two fresh read boundaries around the only local state transition. Any disagreement stops progress without consuming the Candidate.

## D3 — Exact source revision and claim-era semantics

WorkAuthority identity is the complete tuple produced by the official loader: snapshot hash, provider revision, exact source revision vector, and all mapped refs. Preserve deterministic ordering and exact source IDs when recording old/new vectors. A local read-model version such as registry:1025 is not a substitute for WorkAuthority provider/source revisions.

The persisted run’s old claim_key/source_revision identify its historic era. The new source digest is computed by the official work_authority_digest implementation after #1054's source validation and Todo semantics have landed. The CAS compares both old and new eras, but historical jobs remain pinned to old values. New gate jobs bind the new digest. No caller-supplied authority digest, source revision patch, job rebind or evidence rewrite is permitted.

A Todo link override is only an override. A recovery candidate exists only after Monitor/scanner has observed and validated the canonical path and the official loader produces a fresh, consistent source vector. A digest that changes after Todo publication must be reloaded and compared before claim/reset; a stale digest cannot be repaired by copying the current value into the run.

## D4 — Registry CAS shape

A registry operation accepts the exact persisted run identity and complete expected run snapshot, expected raw registry revision, no-active-job assertion and target authority digest/vector. It performs one durable transition or returns a typed conflict. Its compare includes Candidate, PR refs, current gate states/evidence refs, phase, status, retry classification, claim key and source revision so the operation cannot reset a different run generation.

On success, mutate only the current run’s recovery metadata and verify/review gate fields; keep build evidence, Candidate and historical references intact. Set new claim_key/source_revision, clear verified_head and invalidate verify/review gate evidence, set phase=verify, advance only the documented verify attempt counter once, and append bounded audit data identifying the old/new authority era and operator request. Persistence must be atomic. On conflict/failure, durable and in-memory records remain at their original state; no fallback to the legacy digest-only reset.

The operation must be safe against concurrent resumes: the first may commit. A second request with the same request identity and exact payload reads back the same result without incrementing attempts or erasing newly produced gate evidence. Same identity with a different tuple is a conflict. A different request identity sees the changed registry revision/claim era and must require a fresh explicit decision; it cannot silently reset again.

## D5 — Evidence invalidation and re-verification

Only verify/review facts bound to the old authority are invalidated. Builder/repair jobs, the frozen Candidate commit and their evidence bytes remain historical facts. New verify/review evidence must be produced by the ordinary authorized validators at the same Candidate SHA and be bound to the new claim era. The system must never “repair” historic WorkflowJob bindings by editing their claim key.

The run does not return to builder, does not create a new Candidate and does not refreeze base. If freshness or evidence cannot be re-established, stay needs-human with the exact failed identity/freshness check.

## D6 — Existing PR and delivery journal idempotence

This path is limited to a single exact open PR whose authenticated head equals the existing Candidate. The live #1049 conflict with main is a blocker delegated to #972/#973; recovery preserves the PR unchanged. If remote facts are ambiguous or conflict, read back only the same PR identity and stop. Never send another push/create/merge request or create #1015's existing-PR C→D update intent. #1015/#1017 own main-sync exact-D push, C→D PR update witness, post-push CI and closeout; this plan only reads the unchanged Candidate/head and leaves the main conflict to #972/#973.

The journal contract comes from landed #983. Before a journal update, compare exact row identity, canonical payload digest and durable file revision using its conditional-write API. Exact duplicate payload replays return the existing durable acknowledgement with no bytes/event/time changes. Different payload under the same transaction/request ID is conflict. Unknown persistence results are resolved through the same identity’s authoritative read-back; if still unknown, no external action follows. Reconciliation never invents result rows, deletes stale rows, or treats a missing row as proof of no prior remote effect.

Crash recovery follows read-only discovery first: reload the exact run, job and PR identity; inspect whether reset, verify/review job, gate evidence or journal transaction already committed; then either continue the same idempotent step or stop. A manager crash cannot turn an ambiguous remote write into permission to repeat it.

## D7 — No abandon or implicit replacement run

Candidate and PR already exist, so this is not a pre-Candidate recovery. The existing CLI’s current abandon suggestion for the #983 fixture is not accepted evidence or an action for this plan. Neither reset failure nor stale authority permits abandon, supersede, retirement, a replacement run, or clearing the Candidate/PR. Future replacement-run adoption is a separately issue-backed decision with explicit disposition and proof of the old Candidate/PR outcome.

Merged/closed PRs use the dedicated merged-run proof/completion path (#962 and its #975/#976/#977 children), not this open-PR recovery. Main conflict resolution for PR #1049 stays with #972/#973. Post-merge Todo closure stays with #810.

## D8 — Issue-backed slices and independent owners

Whole #1055 includes a registry transition, Manager decision/reverification and delivery-journal/remote read-back integration. Those are separately reviewable and must not be combined merely to force the parent under a sizing threshold. The issue-backed children are #1068/#1069/#1070, each with a distinct owner role, independent acceptance and explicit dependencies. The repo has no CODEOWNERS or distinct named maintainers; each issue requires assignment before intake:

- #1068: Registry exact-run CAS transition, one production module, owner role: workflow registry maintainer; hard dependency: #966, #1063/#1064/#1065 and #1054 full accepted package. Expected dimension projection after complete accepted triad: 0+2+2+0+2=6 Yellow.
- #1069: Manager explicit authority restart and exact-Candidate reverify integration, one production module, owner role: Manager/work-actions maintainer; hard dependencies: #966, #1063/#1064/#1065, #1054 full accepted package and #1068. Expected projection: 0+2+2+0+2=6 Yellow.
- #1070: Existing-PR journal authority read-back after Todo adoption, production scope only work_actions.py and only after confirming existing read APIs suffice; owner role: delivery journal integration maintainer; hard dependencies: #983 accepted conditional-write contract, #966, #1063/#1064/#1065, #1054, #1068 and #1069. Expected projection: 0+2+2+0+2=6 Yellow. This excludes #1015/#1017 exact-D push/PR update/CI/closeout. If a second production module is needed, stop and re-scope before implementation.

These are projections only until each child has a complete, accepted spec/design/todo and the official helper is rerun. Before creating an issue, search existing issues for duplicate scope. If an issue duplicates a prior owner’s acceptance, narrow the new issue or do not create it. Parent #1055 retains the exact #983 fixture integration regression and cannot close because children individually passed.

## D9 — OpenSpec, changelog and policy delivery

The same frozen contract is maintained in this issue-backed Superpowers spec/design/todo and OpenSpec change. OpenSpec proposal/design/spec delta/tasks must state matching prerequisites, CAS tuple, same-run-only behavior, duplicate prevention, failure table and exact #983 fixture; tasks cannot use an empty shell or differ from the final workstream todo.

The documentation PR includes a committed changelog.d/todo-authority-run-recovery.md fragment and matching CHANGELOG.md [Unreleased] entry even though this PR changes planning artifacts only; this repo’s code_paths includes Markdown. VERSION stays 0.1.10.

The PR title/body/comments are zh-TW and conventional-commit compliant. Since it references #1055 but intentionally leaves that blocked issue open, it uses only the existing policy-exempt:issue-link label with a stated reason. The PR body contains the R-11 checklist. Before publishing, run OpenSpec validation and policy_check with exact PR title/body/labels/base/head context; no bare policy run substitutes for that result.

## D10 — Repository baseline and read-only evidence

The isolated branch is feature/1055-todo-authority-run-recovery at origin/main 6a32a3e5e0af841794f340313c11f60f2999f6ae. On 2026-09-25, the live #983 workflow projection had run workflow-52d048b72adbd5cae06f at review/needs-human, Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc, PR #1049 OPEN with DIRTY/CONFLICTING merge state and same head; its source list had #983, PR #1049, spec/design/plan and workflow, with no canonical Todo. This is evidence captured from read-only status/work/provider projections, not a claim-key or raw source revision read.

The #983 run was not changed. It is not an implementation or test target. The current CLI projection’s suggested abandon action is explicitly outside the plan.
