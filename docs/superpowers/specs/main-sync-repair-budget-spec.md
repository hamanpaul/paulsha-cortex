---
status: accepted
work_item: main-sync-repair-budget
---

# Main-sync automatic repair budget 規格（#973 Child B0）

## Requirements

本票是 #973 的 issue-backed budget owner。#972 typed MainSyncContext supplies same-run C/M/classification/path tuple but no attempt counter; #990 owns durable `needs_human` stop persistence, not the budget. B0 owns automatic-repair limit/used/remaining. **依賴 #966 whole-registry revision CAS 必須先合併**；沒有 CAS 的 stale registry write 不得算 reservation。

### R1 Stable budget identity and limit
Budget identity is exact `(run_id, C, M, repair_kind, canonical_conflict_paths)`. Normalize path order for identity only; preserve the complete path set in evidence. Each exact tuple gets a fixed automatic dispatch-reservation limit of **1**. A new tuple, including a new M from a new #972 probe, receives its own counter. Repeated ticks do not reset it. Manual operator retry-build remains under #989 and does not consume automatic budget.

### R2 Durable owner and scalar counter storage
Reuse the existing Manager-owned `WorkflowRun.attempts: dict[str,int]`; do not change schema. For a versioned digest of the exact tuple, persist two scalar integer entries: `<prefix>:limit=1` and `<prefix>:used`. Derive `remaining=max(limit-used,0)`; never persist a second arithmetic value. Reservation ID is deterministic from tuple digest plus ordinal `1`, so it can be recomputed without adding a string field to attempts. Missing, malformed, changed-limit, negative or over-limit counters fail closed.

### R3 Preserve typed repair context across reset
The existing retry-build reset clears `needs_human_reason`, which currently holds #972's only durable C/M context. Before reset, create an immutable content-addressed repair-context snapshot under the existing coordinator evidence store. It must contain and validate `run_id`, repo/claim identity, exact C, exact M, repair kind, complete conflict paths, budget identity, limit, pre-reservation used, committed used, derived remaining, deterministic reservation ID, and target build-attempt ordinal/token. The snapshot is written/fsynced before the registry transaction. In the same #966-protected WorkflowRun CAS write that increments `used` and resets the final Builder card, persist its immutable evidence locator (including content digest) in `evidence_refs` and on the pending repair action. Resume/stop code must reconstruct the typed tuple from that locator and verify its digest; it may not depend on the cleared diagnostic or re-probe main. A losing CAS may leave an unreferenced immutable file but is not a reservation and must never dispatch.

### R4 Atomic reservation with reset
Add one registry operation that validates ongoing run, exact Candidate C, same-run typed context, repair eligibility, and all reset preconditions. In one in-memory mutation and one atomic `_persist()` under #966 revision CAS, it initializes/verifies scalar limit, increments `used` once, records the snapshot locator/action, and resets the final Builder card to a pending exact C/M task. The reservation is consumed at this successful durable commit immediately before dispatch authorization. A reset refusal/validation error before commit consumes nothing. A `RegistryRevisionConflict` must reload/retain the durable snapshot, return explicit conflict, consume no budget and authorize no dispatch; it cannot be acknowledged as a successful reservation.

### R5 Crash and replay behavior
After reservation but before a Builder job is recorded, resume reads the immutable snapshot through the WorkflowRun locator, verifies exact tuple/counter digest, and recomputes the same reservation ID. It never increments `used` again. Reuse only a unique matching current-era job bound to that ID and exact C/M task. A missing, duplicate, mismatched or uncertain job relation fails closed through B2/#990. B2 may retry an unrecorded dispatch only if the dispatcher can safely deduplicate/reconcile the same ID; otherwise it stops for operator recovery. An uncertain/failed dispatch after reservation does not refund budget or permit another automatic reservation.

### R6 Exhaustion and reset refusal
When `used >= limit`, return exhausted without resetting or dispatching; B2 calls #990 with exact typed context recovered from the live reason or immutable snapshot and `repair-budget-exhausted`. Reset refusal/error also goes through #990 and leaves `used` unchanged when no reservation was committed. WorkflowRun attempts remain the sole counter; the evidence snapshot preserves the tuple but is not a second budget owner.

## Verification

Use #966's two-process/barrier CAS seam with two stale registries attempting the same reservation: one CAS winner at most; loser gets explicit revision conflict, durable memory reload, unchanged counter if it lost, and zero dispatch. Verify immutable snapshot content+digest exists before commit and its locator, counter increment, pending action and reset appear together in the successful WorkflowRun CAS. Start with no keys and prove `(limit,used,remaining)=(1,0,1)`; successful reserve yields `(1,1,0)`. Crash after commit then resume/stop from the snapshot with #972 reason cleared, no re-probe and no second increment. Test reset refusal and persistence failure leave old state; exhausted call changes no build/job state and B2 writes #990 stop without dispatch. Test same tuple/new valid M isolation and #989 manual retry non-interference.

## Boundary and sizing

Production scope is `coordinator/registry.py` budget validation, immutable context snapshot write/read and atomic WorkflowRun reserve/reset. It depends on #966, which supplies whole-registry revision CAS. Reuse WorkflowRun attempts/evidence_refs and existing coordinator evidence storage; no new WorkflowRun schema or second stop writer. One production module gives domain=0; transaction, snapshot and crash replay give state=2. Accepted fix-standard mechanics score **6 / Yellow**.
