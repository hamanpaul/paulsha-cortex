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

# Claim WorkAuthority fresh-generation Todo (#1065)

## Issue and unique binding

- Owner issue: [hamanpaul/paulsha-cortex#1065](https://github.com/hamanpaul/paulsha-cortex/issues/1065).
- Unique `work_item`: `stale-todo-authority-fail-closed`.
- Canonical Todo: `docs/superpowers/workstreams/stale-todo-authority-fail-closed/todo.md`.
- Accepted planning references when approved: [spec](../../specs/stale-todo-authority-fail-closed-spec.md), [design](../../specs/stale-todo-authority-fail-closed-design.md), and active OpenSpec change `2026-09-25-stale-todo-authority-fail-closed`.
- This packet binds one work item to one issue and one active OpenSpec change. Formal `.cortex/work-items.yaml` registration and Monitor refresh are later intake actions; neither is performed by this planning PR.

## Scope and dependency

- Execution order: **#1063 → #1064 → #1065 → #1054**. #1063 owns canonical Todo source qualification/path validity; #1064 owns trusted latest-attempt generation and freshness API; this work item owns the claim-side WorkAuthority consumer; #1054 owns Manager first-Builder admission after this contract lands.
- #1055 remains the separate existing Candidate/PR recovery owner. Do not add its recovery behavior or tests here.
- This work item changes only the WorkAuthority consumer in `paulsha_cortex/coordinator/claim.py`, its focused/regression tests, and `docs/unified-work-lifecycle.md`. It does not implement #1063/#1064 producers, Manager admission, source registration/intake, claim recovery, CLI changes, or deployment.
- Implementation must wait for the public #1063 qualification and #1064 freshness API contracts. If either contract is unavailable or requires another production consumer module, stop and re-scope before changing source.

## Observed baseline

Planning base: `origin/main` at `6a32a3e5`. `load_work_authority()` currently returns a unique matching WorkAuthority before it checks top-level `last_refresh_error`; the error check is reached only when no row matches. Existing `WorkAuthority` retains source revisions and `snapshot_hash`, but not the latest Monitor correlation generation or the work-items override revision observed by that generation. The Monitor keeps last-good rows after a failed refresh, so those rows cannot alone prove current authority.

## Five-dimension sizing

Use repo `work_bridge.current_sizing_snapshot()` with the `fix-standard` combo selected by task type `fix` and this spec/design/Todo set. The adapter supplies the current process-rule set R-09/R-16/R-19, `fix-standard`'s two gate-spine items, and its nine cards/persona bindings. The current artifact status is `proposed`, so the helper includes two spec-stability points until the owner accepts the complete packet. The exact helper result is `(6, 'yellow')`. Recalculate after acceptance and again before implementation if the combo, applicability, source scope, or artifact completeness changes.

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 0 | One existing production consumer module: `coordinator/claim.py`. A second production module requires re-sizing. |
| state_consistency | 0 | Read-only freshness consumption; no new durable writes, schema, registry transition, or state migration. |
| acceptance_surfaces | 2 | `fix-standard` gate spine 2 + R-09/R-16/R-19 (3) = signal 5, which scores 2. |
| spec_stability | 2 | Current packet status is proposed; the helper assigns risk 2 until all three planning artifacts are accepted without blockers. |
| orchestration | 2 | `fix-standard` has nine cards, all with `persona_binding`. |
| **Current total / band** | **6 / Yellow** | Official `current_sizing_snapshot()` result recorded after the artifact set is complete. |

## Invariants and acceptance

1. Latest-attempt failure rejects a matching last-good Todo row.
2. A successful generation that predates the current link override rejects the matching row.
3. A healthy generation that covers current correlation input and one #1063-qualified Todo returns authority.
4. Generation, snapshot hash, correlation input revision, and source revisions all come from that same successful generation.
5. Missing, unknown, malformed, legacy, expired, or cross-generation markers fail closed.
6. The returned authority cannot gain freshness solely by hashing a last-good payload.
7. Explicit rate-limited last-known-good remains limited to the existing retirement path.
8. Generation advancement with identical source revisions does not by itself change semantic authority/claim digest.

## Tasks

- [ ] **T0 — Freeze issue binding and upstream contracts**: Re-read #1063/#1064 and verify their published qualification/API contracts are available before implementation. Preserve the unique `work_item`, owner issue, canonical Todo, and active change mapping. If an upstream contract differs from this packet, stop and return to planning; do not create runtime registration or start Cortex intake here.
- [ ] **T1 — Tests / RED**: Add `tests/test_claim_work_authority_freshness_1065.py` using offline fixtures shaped from the real #1063/#1064 public contracts. Prove matching old Todo + latest refresh error rejects; changed override + old successful generation rejects; one healthy current qualified Todo succeeds; source revisions from another generation, invalid qualification, and missing/unknown/expired marker reject. Assert returned generation/hash/input/source provenance is coherent.
- [ ] **T2 — Claim source / freshness consumer**: In `paulsha_cortex/coordinator/claim.py`, make the strict WorkAuthority reader consult #1064's trusted freshness API for `(repo, work_id)` before matching/returning any row. Require successful latest attempt, current input coverage, bounded age, and one coherent generation. Preserve existing typed authority diagnostics where applicable; API absence/error and unknown state must fail closed.
- [ ] **T3 — Source qualification and provenance**: Consume #1063's canonical Todo qualification and validated path/revision without duplicating its parser or path checks. Return the generation, snapshot hash, correlation input revision, and source revisions from the same trusted result. Keep semantic digests stable across generation-only advancement with unchanged source revisions.
- [ ] **T4 — Retirement isolation and regressions**: Keep explicit rate-limited last-known-good behavior limited to the existing retirement path. The strict reader must still reject latest refresh failure and must not infer retirement permission. Run the new test module plus `tests/test_work_claim.py`, `tests/test_claim_provider_scope_530.py`, and `tests/test_claim_provider_rate_limit_authority.py`.
- [ ] **T5 — Documentation / changelog / CLI policy**: Update `docs/unified-work-lifecycle.md` to describe the same-generation WorkAuthority freshness requirement and failure cases. Add `changelog.d/stale-todo-authority-fail-closed.md` and a matching `CHANGELOG.md [Unreleased]` entry. No CLI surface is added; record that CLI help remains unchanged for R-16. Keep new tests under the existing CI test collection for R-19.
- [ ] **T6 — OpenSpec and local gates**: Keep this Todo, the own OpenSpec `tasks.md`, and the spec/design/delta acceptance in parity. Run `openspec validate 2026-09-25-stale-todo-authority-fail-closed --strict --no-interactive`, `openspec validate --specs`, focused/regression tests, full `python3 -m pytest tests/ -q`, `git diff --check`, and `policy_check` with the actual implementation PR title/body/labels/base/head. Record exact final lines and skipped gates.
- [ ] **T7 — Product handoff**: After implementation tests, review, policy, CI, and merge gates pass, hand the exact strict WorkAuthority contract to #1054. Do not implement or claim Manager first-Builder admission, #1055 existing Candidate/PR recovery, runtime intake/refresh, or deployment as part of #1065.

## Completion boundary

This packet specifies the claim-side consumer only. Its merge can make the approved #1065 planning authority available; product behavior remains unimplemented until a separately authorized implementation passes its own tests, review, policy, CI, and merge. #1054 first-Builder admission and #1055 Candidate/PR recovery remain separate outcomes.
