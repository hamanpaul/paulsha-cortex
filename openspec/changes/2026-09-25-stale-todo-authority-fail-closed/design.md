---
status: proposed
work_item: stale-todo-authority-fail-closed
issue: 1065
---

# Design

## Decisions

### D1 — Ordered upstream dependencies

Use the implementation order #1063 → #1064 → #1065 → #1054. #1063 supplies source qualification and path validity. #1064 supplies a monotonic attempt outcome, generation and current-input watermark, same-generation source revisions, age enforcement, and one trusted freshness API. #1065 consumes both results in `claim.py`. #1054 later owns Manager first-Builder admission. #1055 remains the existing Candidate/PR recovery owner.

### D2 — Freshness check precedes candidate-row selection

`load_work_authority()` currently returns the unique matching row before checking `last_refresh_error`. The strict path must ask #1064's API about the requested repo/work item first. If the API reports a failed latest attempt, a stale/current-input mismatch, over-age state, missing/unknown marker, or inconsistent source generation, the read fails before a last-good row can be accepted.

The consumer uses the public result from #1064. It does not infer currentness from `WorkSnapshot.sequence`, `written_at`, payload hash, or a separate private field.

### D3 — Treat Todo qualification as upstream evidence

Only a Todo source that carries #1063's validated canonical path, matching `issue`/`work_item`, concrete Tasks result, and source revision can establish `confirmed_todo`. `claim.py` does not reparse the file or create a second qualification policy. An absent or invalid qualification result rejects the authority.

### D4 — Preserve coherent provenance and semantic digest

Copy the generation, snapshot hash, correlation-input revision, and source revisions from one successful trusted result into the returned immutable WorkAuthority. Keep `work_authority_digest()` content-based: generation number/time alone must not change the digest when the same semantic source revisions are observed again. Correlation-input or Todo source changes remain visible through their source revisions.

### D5 — Isolate the retirement exception

Keep the current opt-in for rate-limited last-known-good in the existing retirement caller path. The default strict read cannot fall back to it. This design does not alter retirement action membership or add a Manager/recovery call site.

### D6 — One consumer module

The expected product source diff is limited to `paulsha_cortex/coordinator/claim.py`; tests and lifecycle documentation are additional deliverables. Do not change Monitor producer or source qualification files owned by #1063/#1064, Manager first-Builder flow owned by #1054, or existing Candidate/PR recovery owned by #1055. Re-scope and recalculate sizing before accepting a second production module.

## Current evidence

At `origin/main` `6a32a3e5`, `WorkAuthority` records source revisions and a snapshot hash but no Monitor generation or current work-items input revision. `load_work_authority()` first searches parsed authorities and immediately returns a unique match; it reads `last_refresh_error` only after finding no match. `WorkSnapshotStore.record_provider_result()` retains prior WorkItems and source owners after provider failure. The reader therefore needs #1064's latest-attempt API; old row presence and payload hashing cannot serve as freshness evidence.

## Alternatives rejected

- Check `last_refresh_error` only on the no-match path: the matching last-good row still bypasses the failure.
- Hash only the prior snapshot: the hash does not prove latest success or that the current override revision was scanned.
- Implement freshness and age rules again in `claim.py`: duplicated rules can drift from #1064 and mix generations.
- Reimplement Todo parser/path qualification in the consumer: that overlaps #1063.
- Add the Manager first-Builder gate or Candidate/PR recovery: those are owned by #1054 and #1055.
- Let every caller opt into last-known-good: the existing retirement accommodation would then authorize strict new work.
