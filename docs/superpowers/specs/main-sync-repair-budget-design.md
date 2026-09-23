---
status: accepted
work_item: main-sync-repair-budget
---

# Main-sync budget 設計（#973 Child B0）

## Decisions

### D1 Depend on #966 before durable reservation
#966 supplies whole-registry revision CAS under transaction lock. B0's read/modify/write must not run on stale snapshots. A `RegistryRevisionConflict` means no reservation and no dispatch; reload state and preserve the diagnostic context.

### D2 #973 owns the concrete counter
The live #972 typed context provides tuple and fail-closed stop contract but no `limit`, `used`, or `remaining`. Freeze one automatic dispatch reservation per exact `(run_id,C,M,repair_kind,canonical conflict paths)` tuple. #989 owns manual recovery; #990 remains the only typed needs-human stop writer.

### D3 Reuse integer counter persistence
Store `main-sync-budget:v1:<tuple-digest>:limit` and `...:used` as integer entries in existing `WorkflowRun.attempts: dict[str,int]`. Derive remaining. Reservation ID is deterministic from the tuple digest and ordinal 1; do not add a string counter/token field or alter schema.

### D4 Snapshot the context before clearing the stop
The current reset removes `needs_human_reason`, which is presently #972's sole durable tuple source. Build a canonical immutable snapshot containing exact run/claim/C/M/classification/path set and budget identity/limit/used/remaining/reservation id plus target build-attempt ordinal/attempt token. Write/fsync it to the existing content-addressed coordinator evidence store before registry persist. Then one #966-protected CAS writes the snapshot locator+digest into `evidence_refs` and the pending repair action, increments `used`, resets the final Builder card and clears the old needs-human stop. A crash after commit can reconstruct tuple and stop reason from the locator; no probe is repeated. If CAS loses, its snapshot is unreferenced and no budget/job is consumed.

### D5 Reserve and reset atomically before dispatch
A registry-owned `_manager_reserve_main_sync_repair` validates context, no-active-job and reset eligibility. It changes scalar counter keys and the pending task/action in one in-memory mutation and one atomic `_persist()` CAS. Only a confirmed successful write counts as the reservation. On revision conflict, registry reloads durable state and returns conflict; caller must not dispatch. B2 passes the deterministic reservation id as dispatch idempotency/job-binding key.

### D6 Resume/reconcile the same reservation
After commit, resume reads the snapshot via its durable locator and verifies its digest, then recomputes the same ID. It reuses the unique exact-C job. A known reservation without a job may be dispatched only through safe deduplication of that same ID. If the result cannot be reconciled safely, stop through #990 instead of dispatching non-idempotently. Budget is never refunded after uncertain dispatch.

## State flow

1. B2 reads exact #972 context and builds the candidate immutable snapshot.
2. Registry writes/fsyncs the content-addressed snapshot.
3. Under #966 revision CAS, registry validates reset, then atomically persists snapshot locator, scalar limit/used, deterministic-key pending action, and final-card reset.
4. On CAS conflict, no reservation/job dispatch; on success, B2 continues with that snapshot/key.
5. After Builder clone provisioning, B1b verifies private M before launch. Provision refusal uses the preserved snapshot for #990 stop; the committed reservation stays consumed.
6. Restart reads the same snapshot/key and reuses the same job or safely deduplicates the same dispatch.

## Failure matrix

| Failure point | Durable counter | Snapshot | Dispatch |
|---|---|---|---|
| Validation/reset refusal before CAS | unchanged | may be unreferenced | prohibited |
| #966 revision conflict | unchanged by loser | unreferenced or existing digest | prohibited; explicit conflict |
| Successful reservation commit | used=1 | locator+digest referenced with action | now authorized |
| Crash after commit | used=1 | readable by run locator after reason cleared | resume same ID only |
| Builder M provisioning fails | used=1 | source for typed #990 stop | no Builder launch |
| Scheduler/job relation ambiguous | used=1 | source for typed #990 stop | no second non-idempotent call |
| Same tuple after limit | used=1, remaining=0 | source for exhausted stop | no reset/dispatch |

## Sizing boundary

Only `registry.py` changes, reusing attempts/evidence_refs and existing evidence storage. #966 is a hard prerequisite. Snapshot/reset/counter/job replay is cross-state transaction work (state=2), not a new workflow schema. Complete accepted fix-standard score is **6 / Yellow**.
