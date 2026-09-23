---
status: accepted
work_item: builder-main-merge-candidate-proof
domain_breadth: 0
state_consistency: 2
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Manager 驗證並採信真 merge Candidate D（#973 Child B3）

## Boundary

- Scope: manager.py proof of Builder result, quarantine proof and Candidate adoption; B4 job_workspace.py provides quarantine import and atomic feature-ref CAS.
- Dependencies: #972, A, B1a/b/c, B2 and B4. Same-file manager.py merge order is A → B1a → B2 → B3 → C1.
- Builder authors D; Manager validates/adopts. C1/C2 own D gates and delivery.
- Do not access Builder clone, alter #972 writer, add schema, or mutate feature ref before proof.
- B3 must use B4 expected-old `C→D` ref CAS after proof; legacy `harvest_branch` is not the promotion mechanism.
- See the [descendant issue drafts](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Bind exact current-era Builder job, immutable dispatch base C, B1 task exact C/M/private ref, successful evidence and exact output D; result `subject_head` may be absent pre-output and, if written after success, must equal D.
- [ ] T2 Execute real non-fast-forward merge cases in isolated Builder clones: clean-behind, CHANGELOG-only conflict and out-of-scope conflict.
- [ ] T3 Import exact bundle head to collision-safe job-scoped quarantine; prove this does not move `refs/heads/<feature>`.
- [ ] T4 Verify D^1=C, D^2=M, conflict classification, full tree, complete unique CHANGELOG entries in C-first order, and all non-conflicting M changes.
- [ ] T5 Reject bad binding/proof while feature ref and Candidate remain C; dispatch no D gates or push.
- [ ] T6 After proof, use B4 atomic expected-ref CAS to promote feature ref only from C to D; reject concurrent C→E even when E is a D ancestor; then CAS Candidate C→D.
- [ ] T7 Inject Candidate writer failure after valid ref CAS; retain quarantine and allow idempotent retry only for the same fully revalidated job/C/M/D proof.
- [ ] T8 Cover concurrent ref race, main M→N and preserve next-probe ownership in #972.

## Sizing

fix-standard mechanics: acceptance=2, stability=0, orchestration=2. One production module gives domain=0; proof crosses job/evidence/bundle/Git refs/Candidate (state=2); total **6 / Yellow**.
