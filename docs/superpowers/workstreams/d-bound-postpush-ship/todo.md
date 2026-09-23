---
status: accepted
work_item: d-bound-postpush-ship
domain_breadth: 0
state_consistency: 2
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Push 後 exact-D delivery（#973 Child C2）

## Boundary

- Scope: `work_bridge.py` existing journaled push/PR/CI/merge-validator pipeline, including typed existing-PR update intent/result in the same durable journal.
- Dependencies: #972 merged, B3 adopted D, C1 ready-to-push exact-D gates; #980/#982 contracts are required only for new PR create/adopt.
- Existing mapped PR C→D update is owned here; #980/#982 do not provide its update witness.
- Push exact D first, then authenticated same-PR read-back, then exact-D PR CI before existing merge/closure authority proceeds.
- Matching repo/base/head alone without authorized mapping or same-run create witness must become ambiguous/needs_human.
- See the [issue-backed C split](../../../../issue-backed-descendants.md).

## Tasks

- [ ] T1 Validate C1 handoff, Candidate/source branch SHA D and current delivery authority before remote calls.
- [ ] T2 For a mapped authorized PR at C, persist typed update intent binding immutable PR identity, repo/base/head, prior C and target D before push.
- [ ] T3 Push exact D through existing journal; inject success-before-receipt crash and reconcile remote branch to exact D.
- [ ] T4 Authenticated GET the same mapped PR; verify identity/base/head and D before persisting update result. Recover lost result only from durable intent + remote D + same-PR read-back D; mismatch is needs_human.
- [ ] T5 For no mapped PR, use #982 safe create/adopt plus #980 same-run successful POST witness; external matching PR without witness is ambiguous/no mutation.
- [ ] T6 Assert PR head exact D before reading required PR CI; never query PR CI before push. Keep pending/fail/missing/stale/wrong-head/ambiguous CI blocked from merge/closeout.
- [ ] T7 After green exact-D CI, delegate to existing merge authority/validator; fault-inject push/update/create/read-back/receipt/CI handoffs and prove no other SHA, duplicate PR or unauthorized closeout.

## Sizing

One production module (`work_bridge.py`) gives domain_breadth=0. Remote branch/PR update or create, durable journal receipt, CI and closeout reconcile across restart, state_consistency=2. Accepted fix-standard artifacts score **6 / Yellow**.
