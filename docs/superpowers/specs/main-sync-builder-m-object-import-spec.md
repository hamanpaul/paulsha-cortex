---
status: accepted
work_item: main-sync-builder-m-object-import
---

# Provision exact M into the isolated Builder clone 規格（#973 B1b）

## Requirements

This B1 child consumes B1a's immutable attempt-scoped exact-M source pin while the existing `ScriptWorktreeCreator` provisions an independent per-job Builder clone. It exposes M as a private local ref so B1c's task can merge exact M without any Builder main fetch.

### R1 Consume the matching source pin only
Accept a pre-reset handoff bound to run/C/M and stable B1a attempt token/target build ordinal; job id is not yet part of the token. The source ref must exist and resolve to M. After clone/job allocation, bind the actual job id in the import receipt. Missing, mismatched, duplicate or stale ref identity fails provisioning and rolls back the new clone.

### R2 Transfer objects into private ref
During clone provisioning, explicitly fetch the exact source ref from the local Manager source repository (`--no-tags`, exact source refspec) into a job-scoped `refs/cortex/main-sync/<token>` pointing exactly M. Verify source pin and destination object SHA. Do not rely on a default clone refspec, clone all branches as proof of transfer, or fetch from network/main. Verify it resolves to a commit and the clone's feature branch and base remain C. Do not update `origin/main`, fetch a newer main, checkout M, or merge in this child.

### R3 Acknowledge before source cleanup
Return exact import receipt `(job_id, run_id, attempt_token, target_build_ordinal, C, M, private_ref)` to Manager only after clone verification. If any step fails, the private ref and new clone are rolled back and B1a retains/reconciles its pin. Repeated provisioning cannot accept a different M under the same token.

## Verification

With a bare origin, start Manager source clone at C and Builder clone at C without M. Before a job id exists, derive/pin the B1a attempt token from run/C/M/classification/paths/next build ordinal; provision Builder clone after reset, bind the allocated job id in the receipt, and assert `cat-file -e M^{commit}` plus private ref exact M while HEAD/feature remain C. Advance `origin/main` to N after probe; imported ref stays M and no fetch occurs. Missing source object/pin, inaccessible source path, wrong SHA, wrong job token, source ref drift and clone provisioning failure must reject and leave no partial clone/ref. No mocked object transfer.

## Boundary and sizing

Production scope is `coordinator/seams.py::ScriptWorktreeCreator` provision/import only. Source pin and Manager handoff belong to B1a; task construction belongs to B1c. One module and one clone-local private ref yield domain=0/state=1; accepted fix-standard artifacts score 5 / Yellow.
