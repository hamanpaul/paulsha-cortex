---
status: proposed
work_item: stale-todo-authority-fail-closed
issue: 1065
---

# Claim WorkAuthority fresh-generation design (#1065)

## Decisions

### D1 — Dependency and ownership order

The implementation order is **#1063 → #1064 → #1065 → #1054**. #1063 first publishes canonical Todo qualification and path validity. #1064 then publishes each correlation attempt outcome, generation, input revision, same-generation source revisions, and one trusted freshness API. #1065 consumes those two contracts in WorkAuthority loading. #1054 later consumes the strict WorkAuthority result for Manager first-Builder admission. #1055 remains the separate owner for existing Candidate/PR recovery after #1054.

If either #1063's qualification or #1064's public freshness contract is unavailable or incomplete when #1065 implementation begins, stop before source changes and return to planning. Do not invent a private marker or a duplicate freshness API.

### D2 — Guard before matching-row return

The current `load_work_authority()` flow calls `_load_work_authorities_with_diagnostics()`, selects the matching row, and returns it when exactly one exists. Only the no-match path reloads the payload and inspects `last_refresh_error`. The strict reader will instead request trusted freshness for the target `(repo, work_id)` first. Only a trusted result can authorize parsing and returning a candidate Todo row.

All failures in the trusted API, including unknown or legacy markers, unsuccessful latest attempt, current-input mismatch, generation mismatch, source revision inconsistency, and age expiry, reject the strict read even if the old row is a perfect match. The reader uses the API's authoritative reason/evidence and does not reconstruct freshness from timestamps or the old snapshot payload.

### D3 — Consume, do not recreate, #1063 qualification

The reader accepts only the qualified canonical Todo source represented in the trusted generation. It carries the validated path/revision and WorkAuthority identity through the existing source aggregation. It does not reimplement issue provenance, `work_item`, `Tasks`, canonical-path, or path-existence validation in `claim.py`.

### D4 — Preserve same-generation provenance without attempt churn

The returned immutable `WorkAuthority` records the exact generation, snapshot hash, current correlation input revision, and source revisions proved by the trusted result. These fields are observational provenance. Semantic identity remains based on source revisions and mapped authority; advancing a generation with identical sources alone does not change `work_authority_digest()` or `claim_identity_digest()`.

### D5 — Keep last-known-good retirement isolated

The existing explicit rate-limit option remains opt-in for the existing retirement action path. The ordinary strict reader continues to reject a failed latest attempt. No call-site changes to Manager, ship, or recovery behavior are planned; tests prove that strict and retirement outcomes remain distinct.

### D6 — Keep production ownership narrow

Expected production scope is one existing module, `paulsha_cortex/coordinator/claim.py`, plus tests and lifecycle documentation. This change does not edit `monitor/work_snapshot.py`, `monitor/work_api.py`, providers, `work_actions.py`, Manager dispatch, or recovery code; those responsibilities are either upstream (#1063/#1064) or downstream (#1054/#1055). If the published producer contract requires another production consumer module, stop and re-size before expanding.

## Observed baseline

At the planning base (`origin/main` `6a32a3e5`), `WorkAuthority` already carries source revisions and a snapshot hash, but no Monitor correlation generation or current work-items input revision. In `claim.py`, `load_work_authority()` returns a unique matching row before the later no-match-only `last_refresh_error` check. `WorkSnapshot` persists `sequence`, `written_at`, rows, and `last_refresh_error`; a failed refresh preserves prior rows. Those fields alone do not prove that a link override was observed by the latest successful correlation.

`work_authority_digest()` is based on mapped authority and semantic source revisions, not `snapshot_hash`, provider attempt counters, or last-success time. D4 preserves this behavior to avoid treating every successful unchanged refresh as an authority change.

## Validation strategy

Use offline fixtures that exercise the public WorkAuthority reader and the real trusted-freshness/qualification result shapes published by #1063/#1064. Add the stale-last-good, changed-override, healthy-current-generation, mixed-revision, unknown/absent/expired-marker, invalid-source, and retirement-control cases. Keep existing `tests/test_work_claim.py`, `tests/test_claim_provider_scope_530.py`, and `tests/test_claim_provider_rate_limit_authority.py` as regression coverage. Do not add Manager first-Builder or Candidate/PR recovery tests to this child.

## Rejected alternatives

- Checking `last_refresh_error` only when no row matches preserves the current bypass.
- Hashing the last-good payload cannot prove latest attempt success or current correlation-input coverage.
- Reconstructing age/generation rules inside `claim.py` can drift from #1064 and can mix generation evidence.
- Revalidating Todo metadata in the claim reader duplicates #1063 and risks two qualification standards.
- Adding the first-Builder gate here crosses #1054 ownership; repairing an existing Candidate/PR crosses #1055 ownership.
- Applying last-known-good to all readers would let a retirement accommodation authorize a new claim.

## Open Questions

- None. Any required change to the #1063 or #1064 contracts returns to the owning issue before implementation.
