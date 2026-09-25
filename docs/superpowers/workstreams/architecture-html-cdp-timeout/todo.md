---
status: accepted
work_item: architecture-html-cdp-timeout
issue: 1084
domain_breadth: 2
state_consistency: 0
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Architecture HTML Chrome CDP timeout Todo（#1084）

## Boundary

- Live authority: [issue #1084](https://github.com/hamanpaul/paulsha-cortex/issues/1084); [spec](../../specs/architecture-html-cdp-timeout-spec.md), [design](../../specs/architecture-html-cdp-timeout-design.md), [plan](../../plans/architecture-html-cdp-timeout.md).
- This PR creates the planning authority only. No product code, merge, Cortex intake, install, or deployment is performed.
- The fix crosses the upstream Archify CDP owner boundary and paulsha-cortex workflow integration. An upstream reviewed immutable commit is a prerequisite to changing the local workflow pin.
- Do not touch #966 or #987 runs, PRs, source, or journals; use their two run histories only as the incident evidence recorded in #1084.
- Preserve exact HTML verification, all Archify checks, native Playwright navigation/graph interaction/capture, and fail-closed outcomes.

## Evidence

- #1067 head 822b105adccc11950ce44e794ad95074cf8b8e1a: attempt 1 failed at 05:57:11Z and attempt 2 succeeded at 05:59:09Z; PR merged at 06:06:18Z.
- #1083 head 0a5b4a624604649a5bfe447695d82395179aa44e: attempt 1 failed at 06:11:20Z and Architecture HTML attempt 2 succeeded at 06:13:27Z. At 06:26:44Z, all PR checks, including pytest 3.13, had passed; the PR remained open.
- Pinned Archify source calls Target.getTargets with the 15000 ms CDP default. Its timeout error omits the process/stderr detail available on pipe/process errors, then cleanup closes Chrome and removes its temporary profile.

## Sizing dimensions

- domain_breadth: 2 — external Archify CDP behavior and local CI integration have distinct repository owners.
- state_consistency: 0 — no durable state or multi-store transaction is added; evidence is per run.
- acceptance_surfaces: 2 — current fix-standard has two gate-spine entries, and R-09/R-16/R-19 contribute three applicable process rules.
- spec_stability: 0 — the accepted spec, design, and plan are complete and have no blocking placeholders.
- orchestration: 2 — calculated from the current fix-standard card and persona-binding counts.
- Official `current_sizing_snapshot()` on 2026-09-25, against the accepted spec/design/plan with `fix-standard`, returned **(6, Yellow)**. Dimensions: domain_breadth 2, state_consistency 0, acceptance_surfaces 2, spec_stability 0, orchestration 2. Inputs included two gate-spine entries, R-09/R-16/R-19, nine cards, and nine persona bindings. This result does not require decomposition; recompute if scope or combo changes, and split issue-backed work if a future official result is Red.

## Tasks

- [ ] T0 freshness / owner: recheck #1084 and the linked run attempts, confirm no later same-scope resolution, and establish upstream Archify issue/PR authority before implementation.
- [ ] T1 upstream tests / source: add a typed Target.getTargets timeout diagnostic with Chrome status and bounded stderr, then exactly one same-process retry after 1 second.
- [ ] T2 upstream tests: cover recovery, exhausted timeout, delayed response, Chrome exit/pipe errors, other CDP timeouts, and content failures; only the exact startup timeout may retry.
- [ ] T3 local workflow: pin the reviewed immutable Archify commit; record the runner image/resource context, Node version, Archify revision, and selected Google Chrome identity.
- [ ] T4 local artifacts and gates: preserve JSON and logs in the always-uploaded artifact; keep the workflow step required and retain exact HTML, viewport, navigation, graph interaction, and capture gates.
- [ ] T5 verification: run the two architecture tests, upstream targeted tests, and the actual Architecture HTML workflow for recovered and non-recovered cases; confirm diagnostic artifacts and exit codes.
- [ ] T6 docs and policy: synchronize the changelog fragment/Unreleased line and pass strict OpenSpec, relevant docs gates, and PR-context policy.
- [ ] T7 delivery accounting: inspect exact head/checks and distinguish planning, implementation, merge, installation, and deployment. Keep #1084 open until implementation acceptance is verified.

## Sizing result

The official result is **(6, Yellow)**. This sizing applies to the current accepted planning packet only; it does not imply implementation readiness, an accepted upstream release, or a green product workflow.
