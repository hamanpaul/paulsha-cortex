---
status: accepted
work_item: ship-main-trusted-merge-candidate
---

# Trusted main merge candidate 設計（#973 Child B）

## Decisions

### D1 Separate tuple owner, budget owner and stop writer
#972 supplies same-run C/M/classification/conflict paths and fail-closed recovery context; its typed MainSyncContext has no retry counter. B0 owns the bounded automatic budget: one durable reservation per exact `(run,C,M,kind,paths)` tuple, with limit/used stored in existing WorkflowRun `attempts` and remaining derived. B0 hard-depends on #966 whole-registry revision CAS; a CAS conflict is no reservation and no dispatch. #990 remains the only typed `needs_human` stop writer. #989 keeps operator recovery authority. No downstream child probes main or rewrites the #972 tuple.

### D2 Resolve provenance and wire the real selectors
A verifies direct Builder producer evidence and archive lineage (`archive C`, real `C^1=B`, unique successful Builder producer for B). B is historical lineage only. A wires the resolver into `manager.resume_workflow_run` `jobs[-1]`, `_dispatch_workflow_card` reusable-job selection and `work_bridge._builder_binding`; `_review_builder_job_binding` is only review-supplied-id validation. A also fixes the dispatch recorder so a new post-archive job's clone base and immutable `dispatch_head` are C, never B. `subject_head` may be absent until successful output, then records D; task input binds C/M.

### D3 Pin and transfer the exact M object
B1a pins exact M in the Manager source clone using a stable pre-reset token derived from run/C/M/classification/paths/target build ordinal. B1c constructs the task and deterministic private-ref name before reset; B1b imports exact M from the local source path only after reset and before Builder launch, while HEAD/feature/base remain C. B1b and B1c are independent after B1a and both precede B2; B1b binds the allocated job id only in its import receipt. The Builder does not fetch main. Builder alone authors D; Manager independently proves the Git objects.

### D4 Reserve once and stop durably
B2 considers only same-run exact C/M eligible classification. B0 atomically consumes its one-unit reservation with the final Builder-card reset before dispatch authorization. B2 binds a deterministic reservation identity into the task/job; re-entry reuses that identity and the matching exact-C job. Any exhausted/corrupt/missing budget, reset refusal, unavailable M or ambiguous job mapping goes through #990 and creates no delivery effect. A reservation is never refunded after an uncertain external dispatch.

### D5 Quarantine before Manager proof; CAS before adoption
Manager cannot read the Builder clone in a three-UID deployment. B4 imports the exact bundle into a unique quarantine ref without changing the feature branch. B3 validates the job/evidence, task C/M, output D, ordered parents `[C,M]`, conflict classification, full tree and CHANGELOG invariants there. Only then does it call B4's atomic expected-old `C→D` ref update; the `C→E→D` race must reject even if E is an ancestor of D. Candidate advances by its existing `C→D` CAS after the ref CAS. Retain the quarantine ref through a Candidate persistence interruption; accept already-D only as replay of the same proof/job.

### D6 Preserve real merge and tree semantics
The Builder's Git operation must make a true two-parent merge. In the allowed CHANGELOG top-insertion case, keep every complete `[Unreleased]` entry once with C entries first. Retain all clean, non-conflicting M tree changes; all other conflicts or unexplained tree edits fail closed. Neither Manager prompt/evidence metadata nor a mock can substitute for Git parents/tree proof.

### D7 Keep local gates before push and PR CI after push
C1 reruns all required local D evidence and exact-head preflight before remote effects. C2 pushes exact D, then creates/updates the authorized PR, then reads PR CI bound to exact D. For a new PR, #980/#982 prove create/adopt identity; matching repo/base/head without their witness is ambiguous. For an existing authorized PR update C→D, C2 owns a durable typed update intent/result in the existing delivery journal and authenticated read-back of the same immutable PR identity; #980/#982 do not prove that update. Existing merge validator and lifecycle authority still perform closeout.

### D8 Preserve full parent requirements and serialize shared-file owners
No #943/#973 acceptance is removed. #885's archive apply/Aborted/coexistence scope remains separate. Runtime work is blocked until #972 merges. Within #973, all changes to `manager.py` are integrated in serial order **A → B1a → B2 → B3 → C1**; B1b (`seams.py`) and B1c (`work_actions.py`) both follow B1a independently and complete before B2. C2 follows C1; #980/#982 are required only for its new-PR create/adopt path. Existing mapped PR C→D update intent/result is owned by C2. B0 (`registry.py`) hard-depends on #966 and can otherwise progress independently; B4 (`job_workspace.py`) can progress independently; B2 waits for B0 and B1a/b/c, and B3 waits for B4. The Red aggregates B1 and C track their accepted Yellow children; they are not implementation prerequisites that create dependency cycles.

## Acceptance crosswalk

| Acceptance | Owner |
|---|---|
| push-time probe, typed C/M/classification, fail-closed context/recovery | #972 (#987–#990) |
| durable numeric automatic budget | B0 |
| direct/archive producer lineage, real reuse selectors, exact-C dispatch record | A |
| exact M object transfer and shared manual/automatic task | B1a/B1b/B1c |
| automatic eligibility, reservation use, durable refusal | B2 with B0 and #990 |
| Builder real merge; Manager quarantine proof and Candidate adoption | B3 with B4 CAS primitive |
| local D evidence before push | C1 |
| exact-D push, new-PR witnessed create/adopt or existing-PR C→D durable update receipt, post-push exact-D CI and existing closeout | C2 (#980/#982 only for new PR) |
| complete parent AC and final integration | #943 owner |

## Real-Git and lifecycle evidence

The implementation gate must test:
- Independent Manager/Builder clones where M is present only in Manager; exact private-ref transfer; absent M refusal; M→N movement does not change pinned M.
- True Builder merge D with ordered `[C,M]` parents; CHANGELOG complete entries preserved once in C-first order; other clean M paths retained; README conflict rejected.
- Bundle import to quarantine leaves the feature ref at C; every invalid job/evidence/parent/tree/classification case leaves both source ref and Candidate at C.
- Valid proof promotes with expected-old `C→D`; concurrent `C→E` rejects even if E is a D ancestor; Candidate persistence interruption replays only the same proof.
- B0 one-unit reservation is atomic with reset; concurrent reservations have one winner; repeat uses same identity; exhaustion and reset refusal persist #990 stop without dispatch or delivery effect.
- C evidence cannot satisfy D. Local D gates precede push. Exact D is pushed before PR mutation and PR CI; lost POST with durable witness recovers the same PR, while an external matching PR without witness becomes ambiguous. CI must be green for exact D before closeout.

## Sizing

The full #973 umbrella touches six production modules (`manager.py`, `work_bridge.py`, `registry.py`, `work_actions.py`, `seams.py`, `job_workspace.py`): domain=2. State consistency=2 across C/M/budget, job/task/provenance, quarantine/refs/Candidate and remote delivery/PR/CI. Complete accepted artifacts give acceptance=2, stability=0, orchestration=2. Total is **8 / Red**. The issue-backed children below are each independently rescored; do not represent the umbrella as Yellow.
