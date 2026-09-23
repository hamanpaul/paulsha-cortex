---
status: accepted
work_item: main-sync-builder-m-object-pin
---

# Manager source tree pin exact M for Builder provisioning 規格（#973 B1a）

## Requirements

This is B1's Manager-side object-retention child. The #972 probe's exact M object is present in Manager's ship/source clone but may be absent from the independent Builder clone. B1a owns the source ref that makes only exact M transferable during per-job clone provisioning.

### R1 Validate tuple and object before reset
Before automatic or manual retry-build reset, read exact same-run C/M context, verify Candidate CAS C, and require M to resolve as a commit in Manager's source clone. Read the current persisted build-attempt count `a`; compute target retry ordinal `a+1`. Missing/unreadable M stops before reset/dispatch; never fetch a replacement or use floating main.

### R2 Stable pre-reset pin identity
Derive a stable `attempt_token` from `(run_id,C,M,repair_kind,canonical_conflict_paths,target_build_attempt=a+1)`. Do not include job id or reservation id: neither exists before reset/dispatch. Manual and automatic paths use the same derivation. Derive the advertised source ref and expected Builder private-ref name from this token. If the same attempt token/ref already exists, accept only exact M; conflicting values fail closed.

### R3 Pin without moving main or feature
Create the per-attempt advertised source ref pointing exactly M with create-if-absent/compare-existing semantics. Do not change `main`, `origin/main`, feature branch, Candidate C or the current Builder job. If reset/CAS later refuses, leave the immutable pin unreferenced or clean up only that exact ref/value; never broad-delete.

### R4 Bind reset ordinal and provisioned job
Persist the target build-attempt ordinal and attempt token in the existing pending task/action during the reset. B1b provisions after reset and before Builder launch: it imports the exact source pin into the expected private ref, validates M and C, then binds actual job id to the attempt token in its import receipt. Keep source pin until B1b acknowledges the exact import; cleanup is idempotent and value-guarded.

## Verification

Use isolated local clones: Manager source has M, Builder clone initially lacks it. Before a job/reservation id exists, B1a derives a token solely from run/C/M/classification/paths/target attempt and pins M; manual and automatic paths for the same reset ordinal derive the same token. Assert main, origin/main, feature and Candidate remain fixed. If M is absent, fail before reset/job. Move origin/main M→N and prove pin stays M. Test reset/CAS refusal, same attempt re-entry, next attempt creates distinct token, conflicting ref, B1b acknowledgment, and exact-value cleanup/retry.

## Boundary and sizing

Production scope is `coordinator/manager.py` pin publication and pre-reset handoff only. B1b clone import is `seams.py`; B1c constructs task. One module with a single scoped source-ref handoff is domain=0/state=1; accepted fix-standard artifacts score **5 / Yellow**.
