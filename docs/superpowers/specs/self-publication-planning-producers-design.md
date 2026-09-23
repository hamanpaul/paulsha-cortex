---
status: accepted
work_item: self-publication-planning-producers
issue: 979
---

# self-publication-planning-producers Design

## Decisions

### D1 — Consume the exact #992 value and #993 append contracts

#992 owns the closed receipt envelope, typed parser, planning input/evidence unions, canonical JSON, event/output identity formulas and retained sidecar contract. #993 owns ordinary registry carry-forward, the private append seam, same-row patch, expected whole-registry revision CAS and persistence rollback. #979 constructs only the two planning producer variants. It does not fork the schema or add a registry API. Production edits wait for both exact child contracts; they do not wait for umbrella #978, which remains open for aggregate acceptance and would make a dependency cycle.

Before implementation, re-read merged #992/#993 and verify their exact transaction-kind mapping and append cardinality. If the planning transaction kind or atomic multi-receipt append behavior remains undefined, stop and request the upstream contract amendment; do not invent a value or issue repeated appends against one registry revision. #993's #966 raw-byte CAS and #968 ownership gates remain prerequisites through that dependency.

The planning producer runs after the trusted work claim and does not receive a live `WorkAuthority`. Its authority digest may therefore come from `run.source_revision` only after verifying the exact current persisted Manager-owned run row and deterministic `claim_key_for_authority_digest(repo=repo, work_id=work_id, authority_digest=source_revision) == run.claim_key`. Freeze that row's digest/claim tuple and expected full-registry revision before any sidecar or workspace mutation. The same tuple must still be current when #993 appends against that held revision; authority restart/refresh/reclaim, changed claim binding or stale CAS means no receipt. This uses the existing trusted run proof and does not add authority loading/resolution to Manager.

### D2 — Freeze brainstorm acceptance before publication

The current `run_heterogeneous_brainstorm` validates the question pack internally. Keep production changes in `manager.py`: wrap the supplied `primary_questioner` callback, validate its returned object against the same `CompletenessReport` using the planning validator, capture the validated pack's `to_dict()`, and return the original result to the existing runner. In the `artifact_writer` callback, before `_publish_planning_artifacts` mutates the workspace, create and fsync the #992 accepted-input snapshot containing the exact report, default pack, validated pack, source artifact byte hashes and define attempt. If pack capture, source reread/hash, or snapshot persistence fails, do not publish.

The pre-mutation snapshot and post-publication intent core are separate immutable records because their facts become known at different times. Persist both under the configured coordinator state root with canonical bytes `J(v)`, file and parent-directory fsync, and no-clobber identity. Keep both after commit. The expected receipt IDs may live in a separate mutable recovery marker outside either immutable digest.

### D3 — Match brainstorm receipts to real operations

Use the existing one `_PlanningPublicationTransaction` for workspace artifact and peer-evidence operations. On ready, reopen the exact `GateEvidenceRef(kind="brainstorm")`, verify its exact raw SHA and bounded #992 projection, and confirm `payload.question_pack.pack_id` equals the accepted pack snapshot. Preserve semantic artifact kind from `evidence_payload.artifacts[]`; the transaction operation kind is only `artifact` or `evidence`.

For each evidence artifact row, mint only if it has one unique exact join to a same-transaction operation with `kind=artifact`, `path_domain=workspace`, `mutation=true`, identical canonical path/ref and `after_sha256` equal to the evidence row's `sha256`. Unchanged original/report rows get no receipt. Verify the row matches exactly one declared output pattern. Once artifact and evidence operations are durable, build the exact #992 intent core, retain it, and use its `intent_ref`/raw hash in each output receipt. One brainstorm event can have several receipts; compute a distinct publication ID from the exact matched operation for each output.

### D4 — Bind plan materialization to the first selected accepted row

Preserve `_materialize_plan_card_output`'s current selection order: the first accepted `kind="plan"` assessment is selected. Do not add a uniqueness requirement over all accepted plan rows. Capture the full ordered assessment facts, selected row and exact source byte hash before writing. Require one supported declared output pattern, one safe canonical target, one unique existing `WorkflowStep` matching `(phase="plan", card=<current card>)`, and a valid non-negative `run.attempts["plan"]`. `WorkflowStep` has no `step_id`/`card_id`; never use a tuple position or row number as identity. If card/attempt identity is missing or ambiguous, stop before target mutation.

Persist the #992 plan accepted-input decision snapshot before calling the existing CAS/no-clobber file publication. Revalidate the selected source against its frozen byte hash, publish the target, then re-read/hash both source and target. The output operation, selected source, selection facts and target identity are bound into the retained prepared intent core. A pre-existing target—even with identical bytes—does not mint.

### D5 — Use one #993 append commit for all output receipts and run fields

Before external mutation, verify the trusted current run proof: exact persisted Manager-owned row, source_revision-to-claim_key deterministic binding, and exact run/work/repo/claim tuple. Retain its expected full-registry raw-byte revision and exact row snapshot required by #993; never take source_revision from caller args. If the tuple changes or a restart/refresh/reclaim makes that revision stale, abort without receipt. After all external publication evidence is durable, submit the strict receipt set plus one explicit same-row patch through #993's private append seam. For brainstorm, the receipt set may contain several eligible output receipts sharing one event; append them with gate/evidence refs, planning authority/source revision, accepted phase/attempt/step state in one registry snapshot persist. For plan materialization, append one receipt with planning authority and the existing phase/step update in one snapshot persist.

#993 validates the expected revision and current run/claim before persisting; it does not inspect workspace or sidecars. A stale revision is a visible conflict, with no merge/retry. On conflict or persist failure, #979 reloads and reconciles exact persisted state; it must not independently re-append on a newer snapshot without a newly authorized producer event.

### D6 — Recover only from retained proof and owned bytes

The durable transaction journal remains a mutable recovery mechanism, not a receipt hash target. The receipt's `transaction` points to the retained immutable prepared intent core; `accepted_input` and `acceptance_evidence` point to retained pre-write facts. A fresh reload can re-open and validate those sidecars even after the journal has been retired. A crash before the prepared core is durable cannot be repaired into provenance by matching current bytes.

Treat a publication as committed only when the fresh registry row has the exact valid receipt set and complete expected run patch, and all retained sidecars, evidence, transaction operation projections and regular-file bytes match. Otherwise use existing rollback ownership only for this transaction's after-hash output while unadopted. Leave changed/adopted targets untouched and report the existing fail-closed producer outcome. Never synthesize receipts from surviving files or expose consumer classification as completed; #964 owns that projection.

### D7 — Keep ownership separate

- #992: receipt value, identities, exact planning producer unions and legacy/unknown value parsing.
- #993: registry persistence/carry-forward, private append, row patch and CAS. Its #966/#968 gates remain effective.
- #979: canonical brainstorm and accepted-plan materialization producer paths in `manager.py`.
- #980: Manager PR producer; coordinate same-module hunks and keep PR ownership disjoint.
- #964: receipt-backed classifier and workflow consumers.
- #965/#847: status surfaces and loaded-runtime AC10 canary/aggregate evidence.

No consumer, status, CLI, PR producer or parent AC is considered complete from the existence of a #979 receipt.

## State Table

| Event | Preconditions | Durable action | Commit condition | Failure outcome |
|---|---|---|---|---|
| Brainstorm producer | validated report/pack; retained input snapshot; ready peer evidence; unique mutation/evidence join | retained accepted-input snapshot and prepared intent core; journal marker outside immutable digest | one #993 commit appends all eligible receipts plus gate/evidence/authority/run patch | rollback only owned unadopted outputs; no receipt from unchanged evidence rows |
| Plan-copy producer | first accepted source row; frozen ordered decision; unique plan card/attempt; one safe absent target | retained decision snapshot before CAS write; retained prepared core after target/evidence hash checks | one #993 commit appends receipt plus authority and phase/step patch | no receipt; rollback only owned unadopted output; changed/adopted target remains untouched |
| Exact replay | same `(producer_event_id, publication_id)` pair and identical envelope/coupled state | none | #993 returns exact prior state without another append/rewrite | payload or row mismatch is conflict |
| Foreign start/intake artifact | caller/work-action exposes matching bytes or `planning_authority` | no planning producer intent or receipt | never commits a #979 receipt | consumer classification remains #964-owned; #979 asserts no valid receipt |

## Validation Design

Drive the actual Manager producer entry paths, then reload the persisted registry. Test brainstorm callback capture and snapshot persistence before the first publication write; exact peer evidence file hash and projection; unchanged rows; duplicate/non-unique row joins; one and multiple eligible outputs; unique publication IDs; and a single #993 commit for all receipts plus the complete run patch. Test plan first-accepted selection with multiple accepted plans, ordered assessments, missing/ambiguous card or attempt, exact one pattern, source drift, same-byte pre-existing target and post-write source/target revalidation.

Inject crashes/failures before/after each accepted-input snapshot, workspace mutation, peer evidence write, prepared intent sidecar, #993 append and registry persist boundary. Include missing/untrusted run proof, malformed/mismatched source_revision/claim_key, and an authority restart between proof capture and append; each must mint no receipt. Assert exact pair replay/conflict, fresh reload, expected revision stale conflict, rollback ownership and no false receipt. Include mapped foreign start/intake paths, wrong repo/work/run/claim, changed/adopted target, sidecar hash/path tampering, unknown/malformed receipt/container and every required #992/#993 negative. Tests must call Manager production paths; private receipt helpers, hand-built valid receipts, and pre-classified success fixtures are insufficient.

## Implementation Boundary

Production source edits stay in `paulsha_cortex/coordinator/manager.py`; tests cover brainstorm transaction and deterministic plan-card materialization producers. Do not change #992's receipt value or #993's registry schema/seam under this child. If either upstream contract cannot atomically append a multi-output brainstorm receipt set with the coupled run patch under one expected revision, or cannot preserve existing fields on fresh reload, stop and report the exact gap instead of adding another registry mechanism.
