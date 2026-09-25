## ADDED Requirements

### Requirement: Monitor correlation refresh attempts have trusted generations

Monitor SHALL assign every correlation refresh a durable, strictly increasing generation. Before scanning, it SHALL persist the new generation with outcome `running` in a marker independent of the last-good WorkSnapshot. It SHALL record terminal success or failure per repo. A process crash after the running write SHALL leave the generation untrusted. A failed refresh MAY retain last-good rows for diagnostics, but those rows SHALL NOT prove freshness.

#### Scenario: Failure follows a successful row
- **WHEN** a repo has a matching Todo row in the last successful snapshot and the next refresh fails or degrades
- **THEN** Monitor preserves the row for diagnostics and the latest generation is not trusted

#### Scenario: Interrupted attempt remains latest
- **WHEN** the process stops after persisting `running` and before a terminal outcome
- **THEN** a later reader sees that generation as incomplete and returns untrusted

### Requirement: Successful generations bind consumed inputs and sources

A successful repo marker SHALL include the SHA-256 revision of the exact `.cortex/work-items.yaml` bytes consumed by correlation, or a stable absent-input revision, plus the provider and `source_id → revision` set used by that same generation. Unknown or degraded required inputs SHALL NOT produce a successful outcome. The reader SHALL compare the recorded override revision with the current configured repo input.

#### Scenario: Link override changes after refresh
- **WHEN** the current work-items override differs from the revision recorded by the latest successful generation
- **THEN** the trusted freshness result is false until a later successful generation consumes the current revision

#### Scenario: Missing source revision
- **WHEN** a required provider or source revision is missing, unknown, degraded, or cannot be tied to the generation snapshot
- **THEN** the repo outcome is not trusted

### Requirement: Success requires durable snapshot read-back

Monitor SHALL write the candidate WorkSnapshot, reload it from the durable store, and verify canonical digest, sequence, repo/work rows, ownership, provider revisions, and source revisions against the current attempt manifest before writing a successful marker. The marker SHALL bind the exact consumed override revision and source manifest to the verified snapshot sequence and digest. A failure between snapshot write and success-marker write SHALL remain untrusted.

#### Scenario: Durable read-back differs
- **WHEN** the reloaded snapshot digest, sequence, target rows, or revisions differ from the candidate
- **THEN** the attempt is failed or incomplete and the freshness API rejects it

#### Scenario: Success marker write fails
- **WHEN** the snapshot read-back succeeds but persisting the success marker fails
- **THEN** the latest marker remains running/failed and no earlier successful generation is used as current

### Requirement: One freshness API fails closed

Monitor SHALL expose one read-only API scoped by exact repo and work ID. It SHALL resolve the unique canonical repo root from the Monitor's current `ProjectState` set; verify latest per-repo attempt success, current override revision, marker-to-snapshot generation/digest/sequence, same-generation source revisions, required provider freshness, and snapshot age no greater than configured `stale_after_seconds`; and return typed trust status, stable reason, generation, and verified revisions. Missing, ambiguous-root, legacy, malformed, unknown, stale, mismatched, or failed evidence SHALL return untrusted. Matching rows, snapshot sequence, written time, payload hash, or empty `last_refresh_error` SHALL NOT be used as fallback proof.

#### Scenario: Complete current generation
- **WHEN** the latest repo attempt succeeded and all marker, current input, source, provider, snapshot identity, and age checks pass
- **THEN** the API returns trusted with the generation and revisions it verified

#### Scenario: Legacy or unknown marker
- **WHEN** a matching row exists but the marker is missing, legacy, malformed, unknown, or unreadable
- **THEN** the API returns untrusted without upgrading the legacy snapshot

#### Scenario: Snapshot is too old
- **WHEN** the snapshot or any required provider exceeds its explicit age bound
- **THEN** the API returns untrusted with a stale reason

### Requirement: Monitor generation producer preserves adjacent work ownership

The producer SHALL consume source qualification/path facts owned by issue 1063 and SHALL NOT reimplement them. Producer implementation is ordered as issue 1077 (durable attempt-generation/failure ledger), then issue 1078 (exact source/snapshot success evidence and freshness API), then umbrella issue 1064 completion. It SHALL NOT change WorkAuthority consumer semantics owned by issue 1065 or issue 1054's pre-Builder run/claim reconciliation, first-Builder admission, stale direct-resume stop, and typed diagnostics. It SHALL NOT implement Todo qualification, claims, Manager dispatch, candidate/PR recovery, or ship semantics.

#### Scenario: Producer completes before consumer
- **WHEN** the Monitor freshness API is implemented but the WorkAuthority reader and Manager gate remain pending
- **THEN** the producer contract is complete only after issues 1077 and 1078 satisfy umbrella issue 1064, and no consumer/gate behavior is claimed
