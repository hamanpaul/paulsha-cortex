---
status: accepted
work_item: retry-build-pinned-main-task-contract
---

# 固定 C/M task helper 設計（#973 B1c）

## Decisions

### D1 Task planning precedes clone provisioning
B1c runs before retry-build reset: validate same-run tuple, Candidate C and B1a source-pin identity; determine the exact private-ref name/token but do not assert that a per-job Builder clone ref exists. B0 can then persist the final-card action/task. B1b provisions the new clone after reset and before Builder launch, explicitly imports M, and verifies the private ref equals M.

### D2 One helper for operator and automatic paths
Both callers use one pure task builder. Automatic caller supplies B0's deterministic reservation id; manual retry retains #989 authority. The output names exact C/M, repair classification/path set and expected private ref. It does not claim object import succeeded.

### D3 Provisioning failure blocks launch
B1b returns a typed failure if the exact M ref cannot be imported/verified. B2 must use B0's immutable tuple snapshot and #990 durable stop; a committed reservation is not refunded. Manual path stays in existing #989 recovery. No Builder process starts on a bad ref.

### D4 Keep Git truth with Builder and B3
The task tells Builder to merge the private M ref into C without moving main. B3 validates real Git parents/tree; prompt assertions cannot qualify D.

## Sizing boundary

Only `work_actions.py`, one action helper and existing WorkflowRun task/action fields. domain=0/state=1; accepted fix-standard score is **5 / Yellow**.
