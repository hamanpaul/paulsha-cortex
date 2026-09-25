---
status: proposed
work_item: stale-todo-authority-fail-closed
issue: 1065
---

# Claim WorkAuthority fresh-generation specification (#1065)

## Problem and outcome

`load_work_authority()` currently returns a matching last-good Todo row before checking the snapshot's `last_refresh_error`. A matching row and a hash of its payload do not prove that the latest Monitor correlation succeeded after the current link override or that all source revisions belong to that successful correlation.

This child defines the claim-side consumer contract. A strict WorkAuthority read succeeds only when the latest trusted Monitor generation is successful, covers the current correlation input, is within the producer's declared age limit, and contains a Todo qualified under #1063. The returned authority records the freshness evidence from that same generation.

## Requirements

### R1 — Strict reads require the latest trusted generation

For a requested `(repo, work_id)`, the strict WorkAuthority reader MUST consult #1064's trusted freshness API before selecting or returning a matching last-good row. It MUST reject the request if the latest refresh attempt failed, even when a prior row matches exactly.

### R2 — Freshness covers current correlation input

The reader MUST require the trusted Monitor result to prove that the latest successful generation covers the current `.cortex/work-items.yaml` correlation revision and falls within the age limit defined by #1064. A successful generation that observed an older override revision is not current authority.

### R3 — Todo source validity comes from #1063

A Todo may count as confirmed authority only when the generation carries #1063's qualification result for the canonical workstream Todo: the path exists as a safe scanner source, `issue` and `work_item` match the same WorkAuthority, and `Tasks` contains concrete non-placeholder entries. The claim reader MUST consume that result; it MUST NOT duplicate or weaken qualification rules. This qualification describes repository source and metadata, not authenticated author identity.

### R4 — Returned provenance is generation-coherent

A successful WorkAuthority MUST retain the Monitor generation, snapshot hash, correlation input revision, and source revisions supplied by one trusted successful generation. The reader MUST NOT create a freshness claim by hashing only the last-good payload or by combining marker fields from different generations.

### R5 — Missing or unknown freshness fails closed

Missing, legacy, malformed, unknown, expired, or generation-inconsistent freshness evidence MUST make strict authority unavailable. Provider/API errors and absent or invalid #1063 qualification have the same fail-closed outcome.

### R6 — Retirement last-known-good stays explicitly opt-in

The existing rate-limited last-known-good option MAY remain available only through its explicit retirement call path. It MUST NOT weaken the strict WorkAuthority read consumed by new claim admission. This child does not change the retirement action set.

### R7 — Semantic authority identity stays content-based

Monitor attempt counters and freshness timestamps are provenance, not semantic source changes. A new successful generation over identical source revisions MUST NOT by itself churn `work_authority_digest()` or create a new claim identity. Current correlation and qualified source revisions remain the semantic inputs.

### R8 — Manager and recovery owners remain separate

This child provides a trusted WorkAuthority consumer result. #1054 owns Manager first-Builder admission and diagnostics. #1055 owns recovery of an existing Candidate/PR. This child MUST NOT implement either owner’s behavior.

## Acceptance evidence

1. Matching last-good Todo row plus latest refresh failure is rejected.
2. A link override changed after the latest successful generation is rejected.
3. A new successful generation that observed the current override and one qualified Todo returns a WorkAuthority with coherent generation/hash/input/source provenance.
4. Cross-generation source revisions, invalid/missing qualification, absent/unknown marker, and over-age snapshot are rejected.
5. Rate-limited last-known-good retirement tests remain valid while strict reads remain strict.
6. Identical source revisions in later successful generations do not change semantic claim digest solely because the generation advanced.

## Scope boundary

Production ownership is the WorkAuthority consumer in `paulsha_cortex/coordinator/claim.py`. Tests and `docs/unified-work-lifecycle.md` provide acceptance and documentation. #1063 owns source qualification; #1064 owns the freshness producer/API; #1054 owns Manager admission; #1055 owns existing Candidate/PR recovery. No source registration, Monitor refresh, Cortex intake, implementation, merge, or deployment is part of this planning packet.

## Open Questions

- None. The consumer uses the public API and field contract published by #1064 after #1063's qualification output is available.
