---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness Tasks

All unchecked tasks are future implementation work for issue #1078. This planning change does not complete any product task.

## 1. Attempt-ledger integration and input capture

- [ ] 1.1 Verify the merged #1077 marker schema/store and freeze a narrow expected-generation `running → succeeded` publication contract; do not implement its allocator, running/failure writes, or parallel store here.
- [ ] 1.2 Read `.cortex/work-items.yaml` once per repo refresh, derive the exact-byte SHA-256 or stable absent revision, and parse the same bytes; read/decode/parse errors cannot yield success.
- [ ] 1.3 Preserve #1063 canonical input/source qualification and path-admission ownership; do not reimplement it.

## 2. Same-generation evidence and durable read-back

- [ ] 2.1 Build each per-repo manifest only from that refresh candidate: exact input revision, consumed provider revisions, and complete `source_id → revision` map.
- [ ] 2.2 Reject missing/unknown/degraded/stale required sources and any provider/source retained from another generation as success evidence.
- [ ] 2.3 Write candidate `WorkSnapshot`, reload from canonical durable path, and verify digest, sequence, target rows, source ownership, provider/source revisions, and expected generation before publishing success through #1077's store.
- [ ] 2.4 On write/read-back/expected-generation failure, preserve #1077 fail-closed latest-attempt semantics; never fall back to an older success.

## 3. Read-only freshness API

- [ ] 3.1 Add one repo/work API resolving root from Monitor's latest canonical `ProjectState` set, not from caller input.
- [ ] 3.2 Verify current input revision, latest attempt and same-generation manifest, durable snapshot identity/read-back, target repo/work row and ownership, required provider/source revisions, and configured `stale_after_seconds`.
- [ ] 3.3 Return immutable typed trust verdict and stable reason codes for missing/legacy/unknown/running/failed/malformed/stale/mismatch/ambiguous-root/I/O evidence; do not refresh, write, repair, or backfill.

## 4. Isolated tests and ownership boundary

- [ ] 4.1 Cover successful binding, override drift before refresh, refreshed override, read-back drift, marker/snapshot generation mismatch, missing/legacy/unknown/malformed/running/failed marker, degraded/missing provider/source, stale snapshot/provider, ambiguous root, and marker/snapshot I/O failure.
- [ ] 4.2 Use temporary stores, fake providers and fake time; verify no GitHub/model/formal Monitor state access.
- [ ] 4.3 Keep #1065 WorkAuthority, #1054 Manager gate, #1063 source/path qualification, CLI, recovery, and ship behavior outside the diff.

## 5. Documentation and validation

- [ ] 5.1 Update Monitor documentation and this issue's spec/design/Todo/OpenSpec views consistently; add and commit the product changelog fragment and `CHANGELOG.md [Unreleased]` entry.
- [ ] 5.2 Run focused and full tests, `openspec validate monitor-trusted-freshness --strict --no-interactive`, the repository canonical spec validation, actual PR-context policy, and `git diff --check`.
- [ ] 5.3 Complete only this issue's own OpenSpec lifecycle after implementation; #1064 umbrella closure, #1065 consumer, and #1054 gate remain separate deliverables.
