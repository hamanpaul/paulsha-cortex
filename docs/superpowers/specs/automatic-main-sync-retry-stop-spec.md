---
status: accepted
work_item: automatic-main-sync-retry-stop
---

# Automatic main-sync retry 與 durable stop 規格（#973 Child B2）

## Requirements

本票是 #973 子工作，runtime integration blocked by #972 live chain #987→#988→#989→#990。另依賴 A、B0 durable budget owner 與 B1a/B1b/B1c；B1 aggregate 是 tracking-only，不作 implementation prerequisite。B0 硬依賴 #966 whole-registry revision CAS。B1b 和 B1c 在 B1a 後可平行完成，且都必須先於本票。#972 supplies typed tuple only; it does not supply a counter. B0 is the sole limit/used/remaining owner. #990 supplies only the durable needs_human stop API.

### R1 Automatic entry and exact tuple
Manager resume/periodic caller considers repair only for the same ongoing WorkflowRun, valid #972 context, and Candidate CAS exact C. Accept only clean-behind or the unique CHANGELOG conflict classification. Use exact M; no floating main lookup.

### R2 Consume B0 reservation
Ask B0 registry API to reserve the exact run/C/M/repair_kind/path tuple. Its frozen limit is one automatic dispatch reservation per tuple; used and remaining are from the persisted WorkflowRun attempts namespace. B0 atomically persists the increment and resets the final Builder card before dispatch authorization. The reservation ID is deterministic from the exact tuple and ordinal 1; B2 does not add a counter or consume a second unit. Re-entry recomputes that same ID and reuses the exact-C job. Dispatch retries must use the same idempotency/job-binding key; if outcome cannot be safely deduplicated or reconciled, stop instead of making another non-idempotent call.

### R3 Dispatch only with object/task readiness
For an eligible exact tuple, first have B1a verify and pin M in the Manager source clone, and B1c construct the task plus deterministic private-ref name before reset. B0 then commits the #966-protected budget reservation, immutable typed snapshot and final Builder-card reset atomically. Only after that commit may B1b provision the independent Builder clone, explicit-fetch exact M from the Manager source path, and verify its private ref equals M while HEAD/feature/base remain C. Launch/dispatch is allowed only after this post-reset import gate succeeds. Use A's exact-C selector/provenance and the deterministic reservation idempotency key; new job `dispatch_head` is C, task input binds exact C/M/private-ref token/reservation id, and successful output subject/result becomes D. If B0 reports #966 CAS conflict, there is no reservation and no dispatch; if B1b import fails after commit, use B0's immutable snapshot for #990 and do not refund.

### R4 Durable stop
Budget exhausted, missing/corrupt budget, invalid tuple, Candidate mismatch, M unavailable before reset, disallowed classification, B0 reset refusal, #966 CAS conflict, post-reset B1b import refusal or ambiguous reservation/job binding calls #990 `_persist_main_sync_stop` with unchanged typed C/M context (from the live #972 reason before commit, or the B0 immutable snapshot after commit) and a specific skipped reason, preserving existing next_actions. No Builder launch, preflight, push, PR or Copilot effect on refusal. A #966 CAS conflict is not a reservation; reset refusal before B0 commit leaves `used` unchanged; failed/uncertain dispatch or provisioning after reservation does not refund or permit a second automatic dispatch.

### R5 Writer and ownership boundary
#987 owns probe/gate, #988 typed serialization, #989 operator recovery/reset eligibility, #990 durable stop writer/status projection, B0 budget reservation, B1 object/task handoff, A selectors/provenance. B2 is the first real automatic caller. It creates no probe/context/writer/counter and does not make #972 depend on #973.

## Verification

Exercise eligible exact tuple with available pinned M: one B0 reservation and one current-era exact-C dispatch. Repeat after reservation/job creation and after each crash window: same reservation/job, no counter increment or duplicate. Exhausted `(limit=1,used=1,remaining=0)`, missing/corrupt budget, C/M mismatch, missing object, reset refused and ambiguous dispatch each yield typed durable stop via #990 and no unauthorized delivery side effect. Manual #989 retry remains separate and no latest-main fallback occurs.

## Boundary

Production scope is `manager.py` automatic caller/dispatch integration only; B0 owns the `registry.py` counter/reset atomicity and hard-depends on #966; B1a owns the Manager pin, B1b the post-reset Builder import, B1c the pre-reset task, and A owns selectors/provenance. State remains 2 because Manager reconciles tuple, reservation, run reset and job identity across resume. Score 6 / Yellow with accepted fix-standard artifacts.
