---
status: accepted
work_item: self-publication-pr-producer
issue: 980
domain_breadth: 0
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---
# Manager PR Publication Producer Work Plan (#980)

## Boundary

Child #980 of #963/#847; owner/work item `self-publication-pr-producer`. Accepted planning artifacts:

- [spec](../specs/self-publication-pr-producer-spec.md)
- [design](../specs/self-publication-pr-producer-design.md)

Production orchestration is limited to `paulsha_cortex/coordinator/work_bridge.py`. Dependencies are #992/#993/#994/#982/#983. Finish #994 before #982 because both change `github_delivery.py`; then implement the Manager producer in #980. Do not wait for #978 aggregate merge. #980 does not implement #979 planning publication, #964 receipt consumers, #965 status/CLI/delivery, or the #965/#847 loaded-runtime canary.

## Invariants tracked (10)

- **I01 Canonical intent:** exact #992 J/H formulas and frozen manager PR intent fields determine one publication ID.
- **I02 Exact request metadata:** NFC title/labels, LF-only body normalization, digest-before-marker, and exact body rendering follow #992/#994.
- **I03 Immutable sidecar:** retained bytes equal `J(intent_core)` and transaction `intent_ref`/`intent_sha256` are exact.
- **I04 Intent-before-create:** #983 reports committed for the intent row and fresh read-back confirms it before POST.
- **I05 Creator proof:** only a successful #982 POST witness durably committed through #983 supports further publication work.
- **I06 Authenticated observation:** #994 exact GET must match the same POST repo/number/id/node_id and all expected PR facts.
- **I07 Journal serialization:** #983 conflicts/unknown/persist-then-raise never advance; concurrent stale writer cannot replace intent/result.
- **I08 Atomic append:** #993 commits the #992 receipt and coupled `pr_refs`/`source_revision` once under registry revision CAS.
- **I09 Legacy path guard:** existing `run.pr_refs` cannot reach metadata sync or merge without exact valid receipt/witness/read-back.
- **I10 Non-destructive ambiguity:** timeout/adoption/drift/persistence failure mints no receipt, preserves remote PR, and remains diagnosable.

## Tasks

- [ ] **T01 predecessor and same-module order:** Verify #992/#993/#994/#982/#983 accepted contracts and merge status. Integrate #994 before #982; #980 follows both and does not wait for #978.
- [ ] **T02 immutable core and canonical request:** Build exact manager PR intent, normalized request metadata/digest, event ID, publication ID, and final body with #992/#994 rules. Reject any marker-like sentinel in marker-free input.
- [ ] **T03 sidecar and intent journal:** Write/retain exact `J(intent_core)` at `intent_ref`, compute exact `intent_sha256`, and use #983 conditional commit/read-back for the immutable intent row before POST. No POST on conflict, unknown, mismatch, or stale state.
- [ ] **T04 structured create and durable witness:** Migrate only Manager caller to #982 structured API. `created` requires the complete confirmed successful POST tuple; persist and reload that tuple through #983 before #994. `adopted`/`ambiguous` and lost response fail closed.
- [ ] **T05 authenticated observation:** Use #994 read-only exact GET with durable expected tuple and canonical metadata; verify repo/number/id/node_id/state/head/base/marker/title/body/labels exactly.
- [ ] **T06 result journal and receipt append:** Persist/read back the #994 observation through #983. Build only #992's closed manager PR receipt; call #993 append once to commit receipt plus `pr_refs`/`source_revision`; fresh-reload and recompute evidence.
- [ ] **T07 existing reference guard:** Before metadata sync and merge-capable shipping, require an exact same-run/claim receipt, retained sidecar, committed create witness, and matching #994 observation. Keep marker in `_metadata_file`; never retrofit an adopted PR.
- [ ] **T08 crash/replay/CAS matrix:** Fault at sidecar write, each #983 conditional write, successful POST before/after witness commit, timeout/lost response, #994 mismatch, #993 persist/unknown, reload, and stale/concurrent writer boundaries. Exact replay is idempotent; conflicts reload the exact event or stop; no replacement intent/create.
- [ ] **T09 parent negative and legacy evidence:** Run formal reachable start/intake foreign-artifact negative per #978/#980 AC; preserve raw invalid legacy receipt diagnostics; verify old runs remain readable and do not backfill.
- [ ] **T10 source/tests/docs and delivery accounting:** Keep production source in `work_bridge.py`, tests focused on the real Manager path, add policy-required `changelog.d/<slug>.md` and `CHANGELOG.md` entries for implementation; cover source, tests, and documentation; assess CLI-help synchronization; run applicable local and PR-context checks, and report test/merge/install/canary evidence separately. Preserve #847 AC01–AC10 verbatim; #980 does not close aggregate AC or AC10.

## Sizing projection and measured completeness

The production scope is one module, `work_bridge.py`, so `domain_breadth=0`. `state_consistency=2` because publication spans immutable sidecar, #983 journal, remote GitHub PR, #993 registry receipt and coupled fields, and crash/restart replay. The remaining dimensions and total are the current `feature-oneshot` formal calculation against these exact accepted artifacts; no dimension is lowered to force a band.

## Measured planning checks (2026-09-24)

- `current_sizing_snapshot(workspace_root="/tmp/980", combo_name="feature-oneshot", artifact_rows=[{"kind":"spec","ref":"docs/superpowers/specs/self-publication-pr-producer-spec.md"},{"kind":"design","ref":"docs/superpowers/specs/self-publication-pr-producer-design.md"},{"kind":"plan","ref":"docs/superpowers/plans/self-publication-pr-producer.md"}])` returned `(6, "yellow")`.
- Formal dimensions: `domain_breadth=0`, `state_consistency=2`, `acceptance_surfaces=2`, `spec_stability=0`, `orchestration=2`; total `6/Yellow`. The combo contributes gate spine 4, 11 cards, 11 persona bindings, and applicable rules R-09/R-16/R-19.
- `assess_planning_completeness()` accepted spec, design, and plan with no missing kinds or blocking markers. `plan_review_gate()` passed completeness, contract compatibility, and envelope; envelope reported the expected `envelope_unavailable` bypass.
- This is the current combo projection from the repository helper. No dimension was reduced; re-run after formal work registration and before dispatch.

## Delivery states

- Planning artifacts: accepted in this intake packet; not published or bound in the repo.
- Product implementation/tests: not started.
- Commit/push/PR/merge: not performed.
- Installed runtime and loaded-runtime/live canary: not performed; #965/#847 retain those gates.
