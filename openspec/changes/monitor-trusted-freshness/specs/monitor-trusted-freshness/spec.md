## ADDED Requirements

### Requirement: Correlation records the exact input revision

Monitor MUST compute the revision of `.cortex/work-items.yaml` from the same raw bytes it parses for correlation. A missing file MUST use a stable absent revision. Read, decode, or parse failure MUST NOT produce successful evidence.

#### Scenario: Override changes before refresh

- **WHEN** correlation reads an existing override file
- **THEN** its manifest records the SHA-256 of those exact parsed bytes

#### Scenario: Override is absent

- **WHEN** the configured repo has no override file
- **THEN** the manifest records the stable absent revision and the empty override is the parsed input

#### Scenario: Override cannot be read or parsed

- **WHEN** reading, decoding, or parsing the current override fails
- **THEN** that repo has no successful generation evidence

### Requirement: Success evidence uses one generation's provider and source snapshot

A per-repo success manifest MUST contain the provider revisions and complete `source_id → revision` map actually consumed by that generation. It MUST NOT mix retained last-good source data, another generation, or a second scan. Missing, unknown, degraded, stale, or mismatched required provider/source evidence MUST prevent success.

#### Scenario: Provider has only last-good data

- **WHEN** a required provider refresh is degraded and retains earlier sources
- **THEN** those retained sources remain diagnostic and the current repo outcome is not successful

### Requirement: Durable snapshot read-back precedes success publication

Monitor MUST write the candidate WorkSnapshot, reload it from the durable store, and verify canonical digest, sequence, target repo/work rows, ownership, provider revisions, and source revisions against the candidate manifest. Only after verification MAY it publish succeeded for the same expected running generation through the #1077 marker contract.

#### Scenario: Snapshot read-back differs

- **WHEN** the reloaded snapshot differs in digest, sequence, target rows, ownership, or revisions
- **THEN** no success marker is published for that generation

#### Scenario: Attempt generation changed before publication

- **WHEN** the marker store no longer reports the expected generation as running
- **THEN** publication is rejected and the attempt is not trusted

### Requirement: One read-only freshness API verifies current repo/work evidence

Monitor MUST expose one repo/work freshness API that resolves the canonical root from Monitor `ProjectState`, reads current input, latest per-repo marker, success manifest, and durable snapshot, and checks generation, revisions, provider freshness, target ownership, and configured `stale_after_seconds`. The caller MUST NOT provide the root, hashes, revisions, or trust result.

#### Scenario: Current complete generation

- **WHEN** current input, latest successful marker, same-generation source manifest, snapshot read-back, target row, required providers, and age all agree
- **THEN** the API returns a typed trusted result with the verified evidence

#### Scenario: Override changed but not refreshed

- **WHEN** the current input revision differs from the latest successful generation's input revision
- **THEN** the API returns untrusted with an input-mismatch reason

#### Scenario: Snapshot or provider exceeds the configured age

- **WHEN** snapshot or a required provider is older than `stale_after_seconds`
- **THEN** the API returns untrusted with a stale reason

### Requirement: Missing and inconsistent evidence fails closed

Missing, legacy, unknown, malformed, running, failed, stale, mismatched, ambiguous-root, or unreadable marker/input/snapshot MUST return a typed untrusted result with a stable reason code. Last-good rows, sequence, in-memory candidate, and empty `last_refresh_error` MUST NOT be used as fallback proof.

#### Scenario: Legacy snapshot has no success evidence

- **WHEN** a matching row exists but its marker or manifest is missing or legacy
- **THEN** the API returns untrusted and does not backfill evidence

#### Scenario: Generation and snapshot do not match

- **WHEN** marker and durable snapshot identify different successful candidates
- **THEN** the API returns untrusted with a generation or snapshot mismatch reason

### Requirement: Producer ownership remains ordered

#1078 MUST consume the durable generation/marker contract owned by #1077 and the source/path contract owned by #1063. It MUST NOT implement generation allocation/failure writer, #1065 WorkAuthority consumption, or #1054 Manager admission.

#### Scenario: Consumer and gate remain separate

- **WHEN** #1078 publishes a trusted freshness API
- **THEN** WorkAuthority and Manager behavior remain owned by #1065 and #1054, respectively
