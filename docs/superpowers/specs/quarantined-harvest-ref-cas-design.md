---
status: accepted
work_item: quarantined-harvest-ref-cas
---

# Git quarantine and expected-ref CAS 設計（#973 Child B4）

## Decisions

### D1 Separate object import from branch promotion
`import_bundle_to_quarantine()` fetches the bundle's advertised branch to a unique quarantine ref, never to `refs/heads/<feature>`. Manager/B3 reads and proves the real objects there.

### D2 Promote with Git's expected-old transaction
After B3 proof, `promote_quarantined_candidate()` validates the target ref name and exact OIDs, then invokes `git update-ref <feature-ref> D C`. Git locks the ref and updates only if it still equals C. A check followed by ordinary fetch is insufficient because another writer can move it between the check and fetch.

### D3 Preserve concurrent work
Any current value other than C fails without mutation, even if it is an ancestor of D. An already-D ref is an idempotent replay only with B3's same proof token. Never roll back E.

## Sequence

1. Import exact bundle branch into job quarantine ref.
2. Return D object identity without changing feature branch.
3. Let B3 validate job/evidence/C/M/D/tree.
4. B3 calls the promotion CAS with expected C and proved D.
5. On success, B3 writes WorkflowRun Candidate C→D; on failure, it persists needs_human and leaves current ref as-is.

## Sizing boundary

Only `job_workspace.py`; one Git ref CAS/quarantine primitive, domain=0/state=1, 5 / Yellow under accepted fix-standard artifacts.
