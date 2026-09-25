---
status: draft
work_item: same-run-authority-restart
issue: 1069
---

# Same-run authority restart for an existing Candidate Design

## Decisions

### D1 Explicit operator route

Use the existing `cortex work resume <work_id> --repo <repo>` control action; do not introduce another command. Automatic scans, auto claims, start, and intake cannot trigger this recovery. The Manager must resolve one exact run from authoritative repo/work/run state rather than selecting by filename or latest timestamp.

### D2 Consume source and freshness contracts

Wait for #1054 and its #1063 semantic Todo qualification, #1064 successful Monitor generation, and #1065 strict WorkAuthority reader. Record old/new sorted source revisions, snapshot hash, provider revision, and full digest. The canonical Todo is owner-published, unique, issue/work-provenance matched, `work_item` matched, and task-bearing. No link override, fuzzy path signal, plan path, or registry sequence can stand in for authority.

### D3 Exact pre-CAS read

Capture the durable registry revision and exact run/repo/work/status/phase/retry-classification/old-claim/old-source tuple, Candidate and verified head, gates/evidence, job references, zero-active-job list, PR reference, and read-only existing journal row identity/revision. Authenticated GitHub reads must prove one unchanged open PR for the same repository with head equal to the Candidate. Unknown or conflicting reads stop before mutation.

The journal read is observational only. This feature does not write the row or expose a shared writer. #1070 owns full journal authority read-back/conditional delivery through #983. If exact identity/revision cannot be proven by an existing read surface, stop and add an explicit dependency/re-scope; do not add a writer here.

### D4 Delegate one atomic transition

Call the published #1068 exact WorkflowRun CAS once with all expected pins and the verified new authority digest/source binding. #1068 owns persistence, revision check, gate invalidation, new claim/source era, verified-head clearing, and its audit. No direct registry write, raw file update, or two-step status/reset path is allowed.

### D5 Verify the same tuple after CAS

Before dispatch, re-read fresh successful authority, exact run and new claim era, original Candidate, PR identity/head/state, and zero active jobs. Dispatch only verification/review for the unchanged Candidate after all facts still match. Any post-CAS drift leaves work stopped with a typed reason.

### D6 Preserve historical evidence

Keep run ID, Candidate/build/repair output, old job rows, claim bindings, evidence references and bytes, PR references, journal row, and prior history. Invalidate verify/review only. New jobs and evidence use the same run/repo/work/Candidate with the new claim era; old verification/review evidence cannot pass the new gate.

### D7 Idempotency and recovery after crash

Use only #1068's request identity and replay contract. Same identity/same payload can return the established transition; changed payload, stale revision, or concurrent action returns typed conflict. On restart after CAS but before dispatch, reread the exact same state before continuing; never duplicate attempts/audit or create another run.

### D8 Fixture isolation

Use issue-fixed run `workflow-52d048b72adbd5cae06f`, Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc`, and PR #1049 as test data only. Stub GitHub reads; assert journal/GitHub/delivery write, push, PR creation/update/merge/close spies stay zero. Keep fixture outcomes separate from live provider evidence.

### D9 Dependencies and scope

No intake until #1054/#1063/#1064/#1065 and #1068/#966 are accepted, merged, and published. #1070/#983 own subsequent complete journal reconciliation and writer. Production code remains only `work_actions.py`; a missing cross-writer read API or second production module requires issue-backed scope/dependency update.
