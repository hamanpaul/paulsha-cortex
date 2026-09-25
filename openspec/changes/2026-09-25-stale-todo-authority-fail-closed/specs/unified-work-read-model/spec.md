## ADDED Requirements

### Requirement: Claim WorkAuthority requires one trusted current Monitor generation

The strict WorkAuthority reader MUST consult the trusted freshness API before selecting or returning a matching row. The latest attempt MUST have succeeded, the generation MUST cover the current correlation-input revision, and the result MUST be within the age limit defined by the producer API. Missing, legacy, malformed, unknown, expired, failed, or cross-generation evidence MUST fail closed even when a matching last-good row exists.

A confirmed Todo MUST carry the canonical path, matching `issue` and `work_item`, concrete Tasks qualification, and safe existing path result owned by #1063. The reader MUST consume this qualification rather than recreating its rules. The returned WorkAuthority MUST retain the generation, snapshot hash, correlation-input revision, and source revisions from that same successful generation.

A later generation with identical semantic source revisions MUST NOT change the semantic WorkAuthority/claim digest solely because its generation counter advanced. The explicit rate-limited last-known-good retirement path remains opt-in and MUST NOT weaken strict reads.

#### Scenario: Matching last-good row after latest refresh failure

- **WHEN** a matching Todo exists in the last-good payload and the latest Monitor attempt failed
- **THEN** the strict WorkAuthority reader returns no authority
- **AND** it reports the trusted freshness failure before accepting the row

#### Scenario: Current link override is newer than the successful generation

- **WHEN** the current work-items override revision differs from the input revision covered by the latest successful generation
- **THEN** the strict reader rejects the matching last-good Todo

#### Scenario: One current, qualified Todo is present

- **WHEN** the latest attempt succeeded, its generation covers the current correlation input, and one #1063-qualified Todo and its revisions belong to that generation
- **THEN** the reader returns WorkAuthority with coherent generation, snapshot hash, input revision, and source revisions

#### Scenario: Marker or source evidence is not trustworthy

- **WHEN** generation evidence is missing, legacy, unknown, malformed, expired, or cross-generation, or Todo qualification is missing/invalid
- **THEN** the strict reader returns no authority even if a prior row matches

#### Scenario: Retirement keeps explicit last-known-good behavior

- **WHEN** the existing retirement caller explicitly opts into rate-limited last-known-good
- **THEN** that retirement call retains its current behavior
- **AND** the default strict read remains fail-closed

#### Scenario: Unchanged sources are observed in a later generation

- **WHEN** a later successful generation has identical semantic source revisions
- **THEN** generation metadata is refreshed in returned provenance
- **AND** the semantic WorkAuthority/claim digest remains unchanged
