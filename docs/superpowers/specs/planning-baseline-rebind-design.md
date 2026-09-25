---
status: draft
work_item: planning-baseline-rebind
issue: 1042
---

# Planning baseline rebind and verify recovery design (#1042)

## Current code owner and failure path

`paulsha_cortex/coordinator/manager.py::_dispatch_workflow_card` evaluates the Yellow plan-review gate, then persists `plan_review_passed` and advances to build. It does not replace the earlier `planning_authority` snapshot with the exact accepted planning bytes used by that gate. Later, `_workflow_input_snapshot` compares build inputs to the frozen hashes and tolerates only the existing checkbox-marker normalization for `tasks.md`/`todo.md`; every other byte difference fails closed as `workflow planning input drift`.

`resume_workflow_run` can reconcile brainstorming evidence through `_validated_brainstorm_planning_authority`, but this is not a general accepted-source rebind for a corrected workstream todo. Existing `retry-verify` in `work_actions.py` checks the candidate SHA, has no exact run selector, and resets verification without refreshing planning authority. `recover-planning` is limited to define-phase planning failures.

## Authority owner and provenance

- **Decision owner:** Manager. Plan acceptance and any stale-baseline recovery are decided in the run lifecycle path in `manager.py`; action adapters may carry selectors but MUST NOT declare a source accepted.
- **Persistence owner:** Registry. Registry receives only a Manager-constructed transition and enforces exact run/candidate/current-state CAS. It MUST NOT read GitHub, inspect planning files, infer merge status, or choose a replacement source.
- **Action boundary:** `work_actions.py` and `control/contract.py` may expose a dedicated recovery request. The proposed request is `recover-planning-baseline` with exact `expected_run_id`, `expected_candidate`, bounded `actor`, and `reason`. Final public verb/help wording remains for adversarial review.
- **Source evidence:** Manager must bind the artifact set to a confirmed canonical WorkAuthority/source revision and to the exact plan-review result. It records each canonical ref/kind/work_id/SHA-256 plus the source revision and review identity in immutable Manager-owned evidence. A workspace file hash alone is not accepted provenance.

For the live reproduction, PR #1039 is merged at `d399e5d531abba8cf5f649d54b6b02d0c48308b2`. Its purpose was only to rename the existing T6 task to `documentation`; its Copilot review and PR body state that acceptance scope did not change. The prior todo SHA is `d5f9c6a288bee485fb0db3e259033da295c868d6872ac0c311076ba279d0735d`; the merged source todo SHA is `cd55c7ce9c3e202d9057f764c88a42b8fc294de4618e85e90433e5e1b3ce544e`. The preserved candidate commit is `8defa4ca9c8f2a310b1dc37d36d69de1580ce4d4`, and its todo SHA is `cb390f92be32801dd6c4d276a2d99ea96205ead5ece181995249fe582030016f`. The candidate differs from accepted todo only by task checkboxes; spec and design hashes match the merged source exactly (`2417677f…` and `469a63de…`).

## Decisions

### D1 — Capture the baseline at successful plan review

On the final plan-review pass, Manager computes the complete artifact snapshot from the same bytes used for completeness and plan-review checks. It verifies canonical source provenance and exact `PlanningArtifactAuthority` ownership, then persists the accepted hashes, review binding, `plan_review_passed`, phase transition, and audit receipt in one Manager-authorized transition before build can dispatch. Any source/hash disagreement stops the transition; the manager does not silently re-read a later version.

The exact provenance resolver is a pre-implementation gate: trace whether the canonical source revision and review identity already exist in the current authority/evidence contracts. If they do not, stop and return to review with the smallest explicit evidence contract needed. Never substitute the current workspace contents as proof.

### D2 — Use one narrow candidate-preserving recovery

For a run already in verify with `planning-input-drift`, the dedicated recovery validates the live WorkAuthority and source revision again, then reads the exact run, candidate, plan-review record, and candidate tree. It accepts only the original run and candidate, the same accepted source/review binding, no active job, and no prior verify job. Spec/design content must match exactly; todo/task equality may use only the existing checkbox normalizer.

After validation, Manager creates an immutable receipt containing the old/new planning hashes and source/review/candidate bindings. A Registry CAS updates the run authority and resets the pending verify step without changing `candidate_head`; the normal Manager verify dispatch consumes the same candidate. Re-entry with the same receipt and full binding is a no-op/replay of that result. Any changed source/candidate, duplicate evidence, missing accepted review, verify job, or run transition rejects before dispatch.

### D3 — Fresh-run fallback does not adopt old evidence

An abandoned or superseded run cannot be reopened through this recovery. The existing fresh `work start` path becomes the fallback: it binds current WorkAuthority and accepted plan artifacts, then runs plan/build/verify under a new `WorkflowRun`. The old candidate and gate ledger remain tied to the old run and cannot satisfy the new run's verification, review, or ship gates.

As of the latest #961 issue comment, old run `workflow-8a41b4bb942ad716b759` had been abandoned after recovery was found unavailable; its candidate ref was retained. New run `workflow-8c7a26f12cfc6d4a196c` was started from the `cd55c7ce…` todo source and had reached build. This planning packet does not claim that the old candidate completed the new run or that the new run has passed verification.

### D4 — Keep #937 and #897 out of the equivalence rule

This design changes when a Manager-reviewed baseline is frozen and how an exact old run may recover. It does not permit builder-created task decomposition, rewritten text, or new planning lines from #937. It does not move #897's planning check to harvest or adopt an ancestor candidate. Both retain their own source and recovery contracts.

### D5 — Sizing and split gate

The draft expects four production modules if candidate recovery remains in scope: `manager.py` (source decision and phase boundary), `work_actions.py` (explicit action), `control/contract.py` (exact selectors), and `registry.py` (single CAS transition). That maps to `domain_breadth=2` (four production modules) and `state_consistency=2` (durable baseline/evidence plus exact re-entry). `fix-standard` contributes two gate-spine surfaces; R-09/R-16/R-19 are applicable, giving `acceptance_surfaces=2`. `spec_stability=2` while this artifact set is draft; `orchestration=2` for nine cards with persona bindings. The draft score is 10/Red; when accepted, the expected score is 8/Red. Implementation dispatch requires an issue-backed decomposition decision or revised, evidence-based module/scope count; no acceptance item is removed to lower the score.

## Failure handling

- Missing or unverifiable source revision: keep the old run stopped; offer the fresh-run fallback.
- Source changed after plan review: reject candidate recovery; repeat acceptance on a new run or obtain new review evidence.
- Candidate hash or plan bytes differ outside checkbox marks: no rebind and no verify dispatch.
- Registry CAS conflict after receipt preparation: leave the receipt unreferenced, re-read all current evidence on the next request, and do not reuse the stale decision.
- Same request re-entry: exact stored receipt and exact current run state return the prior result without another verify job.

## Review decisions still open

- Confirm the canonical accepted-source resolver and whether current `planning_source_revision` plus WorkAuthority is sufficient, or whether a typed Manager receipt field is required.
- Confirm whether `recover-planning-baseline` is the right public action or whether an existing action can safely own this exact state transition without weakening its current contract.
- Confirm whether the four-module Red estimate should split into separate issue-backed work before implementation.
