---
status: accepted
work_item: automatic-main-sync-retry-stop
---

# Automatic main-sync retry/stop 設計（#973 Child B2）

## Decisions

### D1 #972 owns tuple, not budget
Read C/M/classification/path set from same-run #972 context. Its typed MainSyncContext has no attempt counter. B0 owns the only automatic budget: limit=1 per exact tuple, `used` in existing WorkflowRun attempts and remaining derived. B0 hard-depends on #966 whole-registry revision CAS; a CAS conflict authorizes neither reservation nor dispatch. #990 alone owns the durable stop API.

### D2 Reserve once before dispatch
Call B0 atomic reserve/reset. It persists the integer counter and final Builder-card task in one WorkflowRun write. Reservation ID is deterministic from tuple digest plus ordinal 1, so B2 recomputes it after restart without adding schema. B2 binds the same ID as dispatch idempotency/job key. Re-entry never increments used again; it reuses a uniquely matching job or safely reconciles the same idempotency key. An uncertain non-idempotent dispatch stops through #990 instead of being retried.

### D3 Order the pin, task, reset, and import
B1a pins exact M in the Manager source clone and B1c constructs the shared task/private-ref name before reset; B1b imports the object only after B0 reset and before Builder launch. B1b and B1c are independent children after B1a and both precede B2. Dispatch is ineligible until the exact private ref verifies M and feature/base remain C. Later main N is irrelevant to the reserved M.

### D4 Keep manual and automatic paths distinct
#989 remains operator recovery/reset authority. B2 does not impersonate its human actor or treat an operator retry as an automatic attempt. B2 coordinates its B0 reservation and calls #990 on exhaustion/refusal.

### D5 Durable stop and no delivery effect
For exhausted/corrupt/missing budget, invalid tuple, C mismatch, unavailable M, ineligible classification, reset refusal or ambiguous dispatch relation, call #990's accepted `_persist_main_sync_stop` API with typed #988 context and specific reason. Preserve existing recovery next_actions; no job/preflight/push/PR/Copilot effect after a refusal.

### D6 Manager-file owner order
A → B1a → B2 → B3 → C1 is the serial merge order for manager.py. B1b (`seams.py`) and B1c (`work_actions.py`) both depend on B1a, may proceed independently of each other, and must complete before B2. B0 depends on #966; B0 (`registry.py`) and B4 (`job_workspace.py`) can otherwise progress independently.

## State sequence

1. Read same-run typed C/M and exact current Candidate; validate classification.
2. B1a verifies/pins exact M in the Manager source clone; B1c constructs exact C/M task and deterministic private-ref name before reset.
3. Ask B0 to atomically reserve the one unit, persist immutable context, and reset the final Builder card under #966 CAS. A CAS conflict means no reservation and no dispatch.
4. After commit, B1b provisions the independent Builder clone, imports/verifies exact M into its private ref, and confirms HEAD/feature/base remain C. Only then dispatch/reconcile one exact-C job bound to the deterministic reservation ID/task.
5. Re-entry reuses the same reservation and unique current-era job; never increment or create a second non-idempotent dispatch.
6. If no unit remains or any precondition/refusal/identity is ambiguous, persist typed needs_human through #990 and dispatch nothing.

## Sizing boundary

Only `manager.py` changes here: domain=0/state=2 for reservation/job identity/resume coordination. B0 registry budget depends on #966; B1a/B1b/B1c provide exact pin/task/import as separate accepted contracts. Complete fix-standard score **6 / Yellow**.
