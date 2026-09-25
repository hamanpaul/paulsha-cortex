---
status: proposed
work_item: stale-todo-authority-fail-closed
issue: 1065
domain_breadth: 0
state_consistency: 0
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Claim WorkAuthority fresh-generation tasks (#1065)

## Tasks

- [ ] **T0 — Freeze issue binding and upstream contracts**: Re-read #1063/#1064 and verify their published qualification/API contracts are available before implementation. Preserve the unique `work_item`, owner issue, canonical Todo, and active change mapping. If an upstream contract differs from this packet, stop and return to planning; do not create runtime registration or start Cortex intake here.
- [ ] **T1 — Tests / RED**: Add `tests/test_claim_work_authority_freshness_1065.py` using offline fixtures shaped from the real #1063/#1064 public contracts. Prove matching old Todo + latest refresh error rejects; changed override + old successful generation rejects; one healthy current qualified Todo succeeds; source revisions from another generation, invalid qualification, and missing/unknown/expired marker reject. Assert returned generation/hash/input/source provenance is coherent.
- [ ] **T2 — Claim source / freshness consumer**: In `paulsha_cortex/coordinator/claim.py`, make the strict WorkAuthority reader consult #1064's trusted freshness API for `(repo, work_id)` before matching/returning any row. Require successful latest attempt, current input coverage, bounded age, and one coherent generation. Preserve existing typed authority diagnostics where applicable; API absence/error and unknown state must fail closed.
- [ ] **T3 — Source qualification and provenance**: Consume #1063's canonical Todo qualification and validated path/revision without duplicating its parser or path checks. Return the generation, snapshot hash, correlation input revision, and source revisions from the same trusted result. Keep semantic digests stable across generation-only advancement with unchanged source revisions.
- [ ] **T4 — Retirement isolation and regressions**: Keep explicit rate-limited last-known-good behavior limited to the existing retirement path. The strict reader must still reject latest refresh failure and must not infer retirement permission. Run the new test module plus `tests/test_work_claim.py`, `tests/test_claim_provider_scope_530.py`, and `tests/test_claim_provider_rate_limit_authority.py`.
- [ ] **T5 — Documentation / changelog / CLI policy**: Update `docs/unified-work-lifecycle.md` to describe the same-generation WorkAuthority freshness requirement and failure cases. Add `changelog.d/stale-todo-authority-fail-closed.md` and a matching `CHANGELOG.md [Unreleased]` entry. No CLI surface is added; record that CLI help remains unchanged for R-16. Keep new tests under the existing CI test collection for R-19.
- [ ] **T6 — OpenSpec and local gates**: Keep this Todo, the own OpenSpec `tasks.md`, and the spec/design/delta acceptance in parity. Run `openspec validate 2026-09-25-stale-todo-authority-fail-closed --strict --no-interactive`, `openspec validate --specs`, focused/regression tests, full `python3 -m pytest tests/ -q`, `git diff --check`, and `policy_check` with the actual implementation PR title/body/labels/base/head. Record exact final lines and skipped gates.
- [ ] **T7 — Product handoff**: After implementation tests, review, policy, CI, and merge gates pass, hand the exact strict WorkAuthority contract to #1054. Do not implement or claim Manager first-Builder admission, #1055 existing Candidate/PR recovery, runtime intake/refresh, or deployment as part of #1065.
