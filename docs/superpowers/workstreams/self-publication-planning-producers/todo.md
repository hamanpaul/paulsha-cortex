---
status: accepted
work_item: self-publication-planning-producers
issue: 979
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# self-publication-planning-producers Todo

## Boundary

- Issue: [#979](https://github.com/hamanpaul/paulsha-cortex/issues/979); parents [#963](https://github.com/hamanpaul/paulsha-cortex/issues/963) and [#847](https://github.com/hamanpaul/paulsha-cortex/issues/847).
- Hard dependencies: [#992](https://github.com/hamanpaul/paulsha-cortex/issues/992) exact value/producer-union schema and [#993](https://github.com/hamanpaul/paulsha-cortex/issues/993) carry-forward/private batch append. Do not wait for umbrella #978 merge; #993 carries the #966 full-file CAS and #968 writer-sequencing gates.
- Sizing basis is the `feature-oneshot` comparison projection until root publishes the registered work/combo binding; recompute against the selected combo before intake and after accepted artifacts change.
- Production source scope is `paulsha_cortex/coordinator/manager.py`; do not modify #992 receipt value, #993 registry schema/seam, #980 PR producer, #964 consumers or #965 AC10 canary.

## Tasks

- [ ] Verify merged #992 exact receipt envelope, planning transaction-kind mapping (`transaction.kind` equals `intent_core.schema`), derived plan logical identity versus snapshot `acceptance_evidence.ref`, canonical IDs, and retained sidecar validation.
- [ ] Verify merged #993 supports a non-empty batch of typed receipts plus one row patch and one `_persist()`; exercise full-registry revision CAS, exact replay and partial-batch conflict before production edits. Coordinate `manager.py` hunks with #980.
- [ ] Before any sidecar/workspace mutation, validate the current persisted Manager-owned run proof: exact repo/work/run/claim identity, lowercase 64-hex `run.source_revision`, and `claim_key_for_authority_digest(repo=repo, work_id=work_id, authority_digest=source_revision) == claim_key`. Hold the expected full-registry revision through #993 append; fail closed on missing/untrusted proof, caller-sourced values, mismatches, intervening authority restart/refresh/reclaim or stale CAS. Do not load/reconstruct `WorkAuthority` in `manager.py`.
- [ ] For brainstorm, capture the validated QuestionPack through a `primary_questioner` wrapper in Manager. Before `_publish_planning_artifacts` runs in `artifact_writer`, read/hash ordered input artifact bytes and persist/fsync the exact #992 accepted-input snapshot with the report, default/accepted pack and `define` attempt. Failure to snapshot means no workspace publication.
- [ ] For brainstorm, reopen and hash the exact peer evidence file; derive `question_pack_id` from `payload.question_pack.pack_id`; build bounded facts and operation projections. Mint only evidence rows uniquely joined to same-transaction `artifact` operations with `path_domain=workspace`, `mutation=true`, matching canonical path and `after_sha256`; unchanged rows never mint.
- [ ] Persist/fsync the exact brainstorm prepared intent core after outputs/evidence are durable and before append. Mint one receipt per eligible output, with one event ID and per-operation publication IDs; submit the full receipt set and one coupled run patch to #993 in one registry persist.
- [ ] For plan materialization, retain the exact first accepted `kind=plan` assessment in current order and the complete ordered assessment facts. Require one matching `(phase=plan, WorkflowStep.card)` and a non-negative `run.attempts["plan"]`; do not invent step IDs, list indices or global source uniqueness.
- [ ] Before target write, persist/fsync the exact plan acceptance snapshot with source bytes hash, selection facts, card/attempt and unique declared pattern. Then use the existing no-clobber/CAS writer, re-read source/target hashes, persist the prepared intent core and append one receipt plus authority/phase/step patch through #993.
- [ ] Add production-path tests for brainstorm callback capture, pre-mutation snapshot, peer evidence re-read, unique mutation joins, multiple eligible artifacts in one append batch, receipt identities and fresh reload.
- [ ] Add production-path tests for plan first-row selection, ordered evidence, missing/ambiguous card or attempt, one output pattern, source/target revalidation, pre-existing same-byte target and one atomic append.
- [ ] Add negative controls for foreign start/intake mapped artifacts; wrong owner/work/run/claim/kind/ref/hash; same bytes/source prefix/caller row; source drift; duplicate/conflicting event/output; unknown/malformed receipt/container; stale #993 revision; CAS conflict; and competing run/claim.
- [ ] Fault-inject restart before/after accepted-input snapshot, workspace mutation, peer evidence, prepared intent core, #993 append and registry save. Assert exact full-batch replay or visible fail-closed outcome, no duplicate mint, no deletion of adopted/changed files, and no orphan file accepted as proof.
- [ ] Preserve v2/v3 planning journal compatibility and never upgrade missing/unknown legacy receipts. Keep consumer diagnostics/status ownership in #964/#965.
- [ ] Add `changelog.d/self-publication-planning-producers.md` and the corresponding `CHANGELOG.md [Unreleased]` entry with the implementation PR, following repo policy; this planning intake itself is docs-only.
- [ ] Run the focused Manager/planning transaction tests, full repo test suite and required PR-context policy check after implementation. Report skipped/deferred gates separately; this plan does not claim tests or CI have run.
- [ ] Before intake, recompute sizing with the published registered work/combo. Do not lower any dimension to obtain a desired band; if the real helper result is Red, stop intake and report Red without deleting acceptance criteria.
- [ ] Keep all #847 AC01–AC10 intact. Close only after #979 producer evidence is accepted; #963 aggregate, #964 consumer delivery, #965 loaded-runtime canary and #847 aggregate/canary remain separate gates.

## Completion Gate

All producer C01–C08 acceptance criteria pass against the exact merged #992/#993 contracts and persisted fresh-reload state. Source tests, CI, merge, installed-runtime evidence and live canary remain distinct. No claim from this child marks #847 AC10 or the parent issue complete.
