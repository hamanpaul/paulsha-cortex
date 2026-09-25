---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
domain_breadth: 1
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor refresh attempt ledger tasks

All unchecked tasks describe future product work for issue #1077. This planning PR does not implement them.

## Tasks

### 1. Durable attempt marker

- [ ] 1.1 Define a strict, versioned sidecar record and injectable marker store independent from `WorkSnapshotStore` payloads.
- [ ] 1.2 Read/validate the current marker, allocate a strictly greater generation, and atomically durable-write `running` before any provider scan or correlation.
- [ ] 1.3 Define first-use empty-store initialization; reject missing marker with existing snapshot, unknown/malformed marker, invalid generation, and marker read/write failures without resetting to zero.

### 2. Per-repo outcomes and refresh wiring

- [ ] 2.1 Wire `WorkModelRefresher.refresh()` to allocate the attempt before scanning and persist each repo's failure/degraded/exception outcome independently.
- [ ] 2.2 Preserve last-good `WorkSnapshot` rows for diagnostics while ensuring latest failure cannot be interpreted as latest success.
- [ ] 2.3 Keep completed scans without #1078 exact input/source manifest and durable snapshot read-back in an explicit untrusted state; do not publish `succeeded`.
- [ ] 2.4 Guarantee one writer per durable marker path; use durable lock/CAS or reject a second process if the instance path can be shared.

### 3. Isolated verification and documentation

- [ ] 3.1 Add isolated tests for generation increment, scan-after-running ordering, failure after last-good row, partial multi-repo outcome, crash/restart, unknown/corrupt marker, generation non-reset, and marker read/write errors.
- [ ] 3.2 Use temp snapshot/marker stores, fake providers, and fake time only; do not access live Monitor state, GitHub, or model CLIs.
- [ ] 3.3 Update Monitor documentation and changelog fragment/Unreleased entry, then run focused/full tests, strict OpenSpec, repo preflight, and actual PR-context policy.

### 4. Ordered issue boundary

- [ ] 4.1 Keep the implementation order #1063 → #1077 → #1078 → #1064 → #1065 → #1054; do not add source qualification, freshness API, WorkAuthority consumption, or Manager admission to this change.
