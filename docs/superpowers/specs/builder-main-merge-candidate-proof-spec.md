---
status: accepted
work_item: builder-main-merge-candidate-proof
---

# Builder 真 merge Candidate proof / adoption 規格（#973 Child B3）

## Requirements

本 work item 是 #973 的 issue-backed 子工作，#973 是 #943 Child B。依賴 #972 durable C/M context、A selector/provenance、B1a/B1b/B1c task/object children、B2 automatic caller，以及 B4 quarantine/ref-CAS primitive。B3 只負責 Manager 驗證並採信 Builder 真 merge Candidate D；C1/C2 擁有 gates 和 ship。

### R1 Authority split
Builder job SHALL be the only author of D's Git commit and producer of raw job output/commit bundle in its isolated workspace. Manager MUST own evidence validation/adjudication, prove actual Git objects/tree, and alone advance WorkflowRun `candidate_head` from C to D. Builder MUST NOT write WorkflowRun or gate evidence; Manager MUST NOT author or amend D in Builder's workspace.

### R2 Producer and dispatch binding
Only the exact current-era Builder job dispatched from C under B1's pinned task is eligible to produce D. Find it using A's shared resolver and selector integration. Require immutable `dispatch_head == C`, task input bound to exact C/M/private ref, exact successful output candidate SHA D and successful job evidence. `subject_head` is result metadata: it may be absent before successful output; if recorded after success it must equal D. It is not the dispatch-base witness. A historical B job, wrong claim era, B-based dispatch, missing/duplicate binding or malformed evidence MUST NOT be adopted.

### R3 Real Git merge parents
For clean-behind and the sole permitted CHANGELOG conflict, Builder output D MUST be a true non-fast-forward Git merge commit with exactly two ordered parents `[C,M]`; `D^1 == C` and `D^2 == M`. Squash, rebase, cherry-pick, one-parent fast-forward, prompt text, metadata-only parent claims or mere descendant proof do not satisfy the requirement.

### R4 Conflict and tree content
The actual merge conflict set MUST match #972's pinned classification: empty for clean-behind or exactly `{"CHANGELOG.md"}` for automatic conflict resolution. On the CHANGELOG path, D MUST preserve every complete `[Unreleased]` entry from C and M exactly once, with Candidate C entries before M-only entries. The ordinary Git merge result for every non-conflicting M path MUST remain in D. Any other conflict, classification mismatch, missing/duplicate entry, or unexpected tree mutation fails closed with no D adoption.

### R5 Quarantine proof before feature-ref mutation
In the three-UID deployment Manager cannot read the Builder clone. Before changing WorkflowRun or the source feature ref, Manager MUST use B4 to import the exact advertised bundle branch to a unique job-scoped quarantine ref. Import may add Git objects and that quarantine ref only; it MUST NOT advance `refs/heads/<feature>`. From quarantine, verify exact C/M/D, unique job/evidence binding, task input, real parent order, conflict classification and required tree/content invariants. The feature ref must still equal C, except an idempotent retry of the same fully proved D. A failed proof must leave source feature ref and Candidate at C and stop before D gates/push.

### R6 Atomic ref promotion and Candidate transition
Only after all R5 proof succeeds, call B4's atomic expected-old ref promotion with target feature ref, expected old C and new D. This MUST reject any concurrent move to E even when E is an ancestor of D; do not accept non-FF ancestry alone. After ref CAS succeeds, use the existing Candidate CAS from C to D. Preserve the quarantine ref/evidence until Candidate persistence succeeds. If Candidate persistence fails, retry only the same run/job/C/M/D proof; B4 may accept already-D only as this exact idempotent replay. Never force or roll back unrelated ref movement.

### R7 Pinned main and later movement
If origin main advances from M to N while Builder works, this D still has exact parents `[C,M]`; N is not implicitly included. The next probe belongs to #972 and may open another C/M cycle.

## Verification

Use bare-origin-backed real Git repositories and actual Builder clones. Exercise B4 expected-ref CAS including `C→E→D` race rejection. Run true merge commands for clean-behind, top-insertion CHANGELOG `[Unreleased]` conflict and out-of-scope README conflict. Verify commit parents/tree with Git queries. Import bundle to quarantine and prove source feature ref stays C before Manager proof. Every refused job/evidence/parent/tree/classification/bundle case must leave it C. After proof, exercise atomic exact `C→D` ref CAS and Candidate adoption. Inject Candidate writer failure after a valid CAS and prove an exact retry from retained quarantine is idempotent. Cover wrong dispatch base, task C/M mismatch, wrong claim era, `subject_head` absent before output and D after successful output (reject a populated mismatched result), absent M, lost/duplicate CHANGELOG entries, unexpected non-conflict content, bundle SHA mismatch, concurrent `C→E` and origin main advancing to N. No mocked merge may count as evidence.

## Boundary

Production scope is `paulsha_cortex/coordinator/manager.py` candidate result proof and adoption only; quarantine import/atomic expected-ref CAS are B4 in `job_workspace.py`. B1 owns exact task/object transfer; B2 owns automatic trigger/stop; A owns producer selector/provenance; C1/C2 own gates and delivery. Do not call legacy `harvest_branch` for B3 promotion, add schema, change commit-author authority or perform gate/push behavior here.
