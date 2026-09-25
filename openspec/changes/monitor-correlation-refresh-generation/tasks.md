---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
domain_breadth: 1
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# monitor-correlation-refresh-generation Tasks

All unchecked tasks are future product implementation work for issue 1064. This planning PR does not complete them.

## Tasks

## 1. Attempt generation and marker

- [ ] 1.1 Define the versioned independent attempt-marker record/store with strict parsing, monotonic generation allocation, and an atomic `running` write before source scanning.
- [ ] 1.2 Persist terminal per-repo success/failure outcomes and diagnostics; preserve last-good rows while ensuring exceptions and degraded providers cannot leave an older success trusted.

## 2. Input and snapshot binding

- [ ] 2.1 Capture `.cortex/work-items.yaml` revision from the exact bytes parsed by correlation; represent an absent file with a stable explicit revision.
- [ ] 2.2 Record the provider revisions and `source_id → revision` set consumed for each repo outcome in the same generation.
- [ ] 2.3 Write a successful candidate snapshot, reload the durable snapshot, verify digest/sequence/rows/ownership/source revisions against the attempt manifest, and only then persist a success marker binding its input/source manifest to that read-back.

## 3. Trusted freshness API

- [ ] 3.1 Add one read-only repo/work freshness API on `WorkModelRefresher`, using the unique canonical repo root from its latest service `ProjectState` set and the Monitor `stale_after_seconds` bound.
- [ ] 3.2 Return a typed result with stable fail-closed reason codes and verified generation/input/source/snapshot evidence; reject legacy, unknown, missing, stale, failed, or inconsistent records without last-good fallback.

## 4. Tests and delivery

- [ ] 4.1 Add isolated tests for success, failure after a matching last-good row, degraded provider, changed-but-unrefreshed override, refreshed override, unknown marker, age expiry, mixed source generation, read-back mismatch, marker-write failure, and interrupted `running` marker.
- [ ] 4.2 Test exact durable bytes and API outcomes with temporary paths, fake providers, and fake time; ensure no GitHub/model/live state is touched.
- [ ] 4.3 Update Monitor docs, changelog fragment and Unreleased entry; run focused/full tests, strict OpenSpec, CI-parity preflight, actual PR-context policy, and diff checks.
- [ ] 4.4 Preserve dependency order: #1063 producer input contract → #1077 attempt/failure ledger → #1078 source/snapshot-bound success and freshness API → #1064 umbrella completion → #1065 WorkAuthority consumer → #1054 pre-Builder Manager admission, run/claim reconciliation, stale direct-resume stop, and diagnostics. Do not add consumer or gate changes to this issue.
