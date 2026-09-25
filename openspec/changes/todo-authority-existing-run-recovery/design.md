---
status: draft
work_item: todo-authority-existing-run-recovery
issue: 1055
---

# Design

## D1. Consume only landed upstream authority

Before product intake, confirm #966 raw-revision JobRegistry CAS is on main and re-read #1054, #1063, #1064 and #1065 after their actual merges. #1063 defines Todo semantic qualification and existing path guard; #1064 publishes trusted correlation generation/input watermark; #1065 consumes it in the strict WorkAuthority reader. #1054 integrates those contracts, adds Manager admission/diagnostic and verifies exact run/claim digest continuity before first Builder. Freeze accepted contracts and main revisions together. A Draft PR, issue text or assumed API is not capability evidence. Require #983’s accepted conditional journal writer and read-back contract as a hard gate for the delivery slice.

## D2. Recovery is an explicit exact-tuple transaction

The operator must request resume for one exact existing run. Automatic/periodic scans do not reset it. The manager loads full current WorkAuthority and checks one canonical Todo, then compares the old/new sorted source revision vectors and formal digest against the exact persisted WorkflowRun snapshot and Candidate/PR state. CAS includes raw registry revision, run identity/status/phase, claim/source revisions, current verify/review evidence, job bindings, Candidate/verified head, PR refs, no active job, journal row identity/hash/revision, and authenticated PR repository/number/state/head/base.

Because these stores cannot share one atomic transaction, source/PR/journal facts are re-read immediately before registry CAS and after it before job dispatch. Mismatch means pending gates and stop, not compensation by rewriting historic state.

## D3. Preserve old run and invalidate only dependent gates

A successful registry CAS updates the new claim/source binding once, returns the same run to verify, invalidates verify/review gates and verified_head, and records one audit. It preserves run identity, Candidate, build history, builder evidence bytes/refs, old jobs, old claim era, and existing PR refs. New verify/review jobs are exact-Candidate jobs in the new claim era. No builder, new Candidate, rebase or base refreeze.

## D4. Delivery uses existing PR and #983 writer only

Delivery requires exactly one authenticated open PR whose head equals the original Candidate. Recovery only reads the same PR and journal identity; journal mutation uses the landed #983 conditional-write API with durable read-back. Unknown, conflict, stale revision, identity mismatch or crash ambiguity is fail closed. This path never pushes, creates a second PR, merges, closes, or says remote closure happened.

If PR #1049 is still conflicting with main, stop and leave it unchanged for #972/#973. Do not use #1015/#1017 exact-D push/C-to-D update intent or post-push closeout here. Merged/closed PRs use #962/#975/#976/#977, not this capability.

## D5. Failure and crash semantics

- Before CAS: no registry/journal/provider mutation; explicit request may be retried only after fresh complete read.
- CAS conflict: no reset or job dispatch; return typed exact expected/actual diagnostic.
- After CAS before gate dispatch: fresh operator resume reloads exact run/source/PR state and may dispatch only missing exact-Candidate gate job.
- Job creation outcome unknown: query its exact run/claim/head binding before deciding; never duplicate on timeout.
- Journal result unknown: use same transaction identity and landed conditional read-back; no external write follows unknown.
- Source/PR drift at any boundary: hold pending and preserve all rows/refs.

## D6. Read-only #983 regression

Use a fixture based on run workflow-52d048b72adbd5cae06f, Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc, PR #1049. The live PR is open and conflicting; tests inject stub GitHub reads and assert zero remote write calls. The live run/PR remain untouched.

## D7. Split points and sizing

The parent scope is Red. Created slices are #1068 registry exact-run CAS, #1069 work_actions explicit authority restart/reverify, and #1070 work_actions existing-PR journal authority read-back. Their owners, dependencies, acceptance and provisional Yellow projections are in the authoritative workstream todo. Each child must be duplicate-checked, get its own complete accepted planning packet, and rerun the official sizing helper before intake. No child closes #1055.
