---
status: accepted
work_item: quarantined-harvest-ref-cas
---

# Quarantined bundle import and expected-ref CAS 規格（#973 Child B4）

## Requirements

本票提供 B3 所需的 Git primitive，僅改 `job_workspace.py`。它不判斷 job authority、不驗 D 的完整 merge semantics、不更新 WorkflowRun/Candidate；Manager 先用 quarantine refs 完成所有 B3 proof，再呼叫 ref promotion CAS。

### R1 Quarantine import leaves feature untouched
Import an exact advertised bundle branch into a unique job-scoped `refs/cortex/quarantine/<token>` using non-forced object transfer. Verify advertised ref and commit SHA. Import may add objects and that quarantine ref only; feature branch must remain unchanged.

### R2 Compare-and-swap promotion
Expose a promotion operation that requires exact target `refs/heads/<branch>`, exact old SHA C, exact new commit SHA D already present in source object store, and atomically executes Git ref compare-and-swap (`update-ref target D C`). Return success only if the ref changed from exactly C to D. Do not use non-FF ancestry as a substitute for expected-old CAS.

### R3 Refusal and idempotent retry
If feature ref is E, even when E is an ancestor of D (for example C→E on main lineage, then D has parents [C,M] and M descends from E), CAS refuses and leaves E unchanged. If ref is already D, accept only as idempotent retry when B3 presents the same job-scoped proof identity; otherwise refuse. Never reset/rollback E or force-update a branch.

### R4 Preserve existing harvest callers
Existing `harvest_branch` behavior remains for non-B3 callers. B3 uses the new explicit quarantine-import and promotion-CAS functions so it can complete proof between import and promotion.

## Verification

Use a bare origin and real bundles. Verify quarantine import does not change feature ref; C→D promotion succeeds when old ref is C; C→E concurrent movement followed by attempted promotion to D is rejected atomically even if E is ancestor of D; E remains intact. Test wrong expected C, absent D object, mismatched quarantine ref, duplicate token and exact idempotent retry. Mutation traps fail if any operation uses force or moves a ref before expected-old validation.

## Boundary and sizing

Production scope is one module, `coordinator/job_workspace.py`; state=1 for a single atomic Git ref CAS and quarantine ref. Complete accepted fix-standard score is 5 / Yellow.
