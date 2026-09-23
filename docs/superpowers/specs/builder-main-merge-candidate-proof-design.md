---
status: accepted
work_item: builder-main-merge-candidate-proof
---

# Manager proof, quarantine and adoption 設計（#973 Child B3）

## Decisions

### D1 Builder authors D; Manager owns proof/adoption
Builder makes true merge commit D in its isolated workspace and emits raw result plus commit bundle. Manager owns evidence adjudication and the only accepted Candidate transition. Builder cannot write WorkflowRun/gate evidence; Manager cannot author or amend D.

### D2 Quarantine import is the three-UID proof seam
Manager cannot read the Builder clone in a three-UID deployment. Existing `job_workspace.harvest_branch` fetches directly into `refs/heads/<branch>` and mutates the feature ref, so it cannot be the first bundle import or B3's promotion primitive. B4 imports the bundle's exact advertised branch to a unique job-scoped `refs/cortex/quarantine/<token>` using a safe ref name, collision checks and non-forced object transfer. This makes Git objects readable while leaving the source feature branch at C.

### D3 Verify quarantined objects before mutation
Bind the unique current-era job, B1 task input exact C/M/private ref, immutable `dispatch_head=C`, successful evidence and output D. `subject_head` is a result field: absent before output is valid; if recorded after success it must be D. Resolve D from quarantine and verify exact output SHA, exactly ordered parents `[C,M]`, true non-fast-forward merge, permitted conflict classification, full tree invariants and CHANGELOG entry order. Confirm the source feature ref still equals C. No failed proof may mutate the source feature ref or `candidate_head`.

### D4 Promote with expected-old CAS, then adopt
After proof, call B4's atomic feature-ref update with expected old C and new D. This compare-and-swap rejects concurrent `C→E` even if E is a D ancestor. Do not call `harvest_branch` for this step. Then advance `candidate_head` with the existing `C→D` Candidate CAS. Preserve quarantine ref/evidence until Candidate persistence succeeds.

### D5 Recover only the same fully proved result
If proof and ref CAS succeed but WorkflowRun persistence fails, retain quarantine/evidence. Retry may accept the feature branch at D only after revalidating the same run/job/C/M/D proof and exact bundle. B4 permits already-D only with that proof identity. Any other ref tip is a durable stop; never reset or roll back an unrelated branch update.

### D6 Keep automatic trigger and downstream gates separate
B2 owns whether/when to dispatch and the durable stop. B3 proves/adopts D only. C1/C2 invalidate C evidence and perform staged delivery. B3 does not request verify/review/preflight/push on rejected D.

## Proof sequence

1. Validate job identity/evidence and locate its regular, non-symlink commit bundle.
2. Check the bundle advertises the recorded build branch; ask B4 to import the exact commit to quarantine only.
3. Read D from quarantine; verify output identity, exact C/M task, immutable dispatch_head, parents, conflict paths and tree.
4. Confirm source feature ref is still C; if already D, require same fully proved run/job tuple as replay.
5. Ask B4 to atomically update feature ref from expected C to D; reject every other current value, including C→E→D ancestry races.
6. CAS WorkflowRun Candidate from C to D.
7. Keep proof refs/evidence until adoption persists, then clean them under existing lifecycle policy.

## Verification matrix

| Case | Quarantine | Feature branch | Candidate |
|---|---|---|---|
| Bad bundle/job/evidence/commit | retained or safely removed | stays C | stays C |
| Correct D, proof fails on tree/classification | retained for diagnosis | stays C | stays C |
| Correct D, CAS sees concurrent E | retained | remains E | stays C |
| Correct D, Candidate writer interrupted after CAS | retained | D from proved job only | C, retryable |
| Exact retry of same proof | resolves same D | remains/sets D only under matching proof | advances once to D |
| Unrelated branch movement | retained | unchanged by Manager | C; durable stop |

## Real-Git proof

Use a bare origin and real Builder clones. Assert parents/tree from Git objects. Prove quarantine ref resolves D before `refs/heads/<feature>` moves. Mutation traps must fail on any feature-ref update before all proof checks or any B3 use of legacy `harvest_branch` as promotion. Include the atomic `C→E→D` race.

## Sizing boundary

Single production module but cross-record job/evidence/bundle/quarantine ref/source branch/Candidate transition authority: domain=0, state=2. Accepted fix-standard artifacts yield **6 / Yellow**.
