---
status: accepted
work_item: legacy-caller-auth
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# H — operator authentication 規格

## Authority and scope

Add narrow local authorization for the explicit migration action. Current requested_by is client-supplied audit text and is not authentication.
Blocked by E contract and #971's confirmed WorkAuthority/action source. Supplies a principal to I; ordinary control requests remain unchanged.

## Requirements

- **R1** — Open the exact control request without following symlinks and derive owner UID from fstat on the opened descriptor; pin file identity/content during validation and consumption.
- **R2** — Install a versioned root-owned policy, readable but not writable by Manager, mapping allowed OS UIDs to exact repo/work_id/migrate-legacy-slice tuples; missing, malformed or stale policy denies.
- **R3** — Require currently confirmed WorkAuthority to match policy and bind policy digest, UID, request identity, request ID and authority digest to immutable action provenance.
- **R4** — Reject forged requested_by, unknown UID, replaced/symlinked request, unauthorized repo/work item, duplicate request or missing authority before proof or registry call.
- **R5** — Tests prove UID comes from the opened fd and cover TOCTOU/path replacement, permissions, policy failure, audit provenance and replay.
- **R6** — Authenticated UID authorizes the action only; it cannot replace historical slice→job→WorkflowRun lineage.

## Acceptance and verification

- [ ] Open the exact control request without following symlinks and derive owner UID from fstat on the opened descriptor; pin file identity/content during validation and consumption.
- [ ] Install a versioned root-owned policy, readable but not writable by Manager, mapping allowed OS UIDs to exact repo/work_id/migrate-legacy-slice tuples; missing, malformed or stale policy denies.
- [ ] Require currently confirmed WorkAuthority to match policy and bind policy digest, UID, request identity, request ID and authority digest to immutable action provenance.
- [ ] Reject forged requested_by, unknown UID, replaced/symlinked request, unauthorized repo/work item, duplicate request or missing authority before proof or registry call.
- [ ] Tests prove UID comes from the opened fd and cover TOCTOU/path replacement, permissions, policy failure, audit provenance and replay.
- [ ] Authenticated UID authorizes the action only; it cannot replace historical slice→job→WorkflowRun lineage.

## Non-goals

No general control authentication, human login, GitHub identity, WorkAuthority synthesis, workspace proof, registry mutation or automatic migration.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。
