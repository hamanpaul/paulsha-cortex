---
status: accepted
work_item: self-publication-receipt-value
issue: 992
---

# Self-publication receipt v1 value 規格（Child A，issue #992）

## Requirements

Child A implements the common, closed receipt value contract for #978/#963/#847 AC01 and AC04 in workflow.py. It owns only value types plus WorkflowRun parsing/serialization. It does not own registry update reconstruction/CAS (Child B), authenticated GitHub read-back (Child C), producer mint call sites (#979/#980), or consumer classification (#964).

A valid receipt value means a trusted Manager producer supplied a strict value through the private internal append seam. Content/hash equality, planning_authority, pr_refs, source membership, claim key, or a public PR marker alone never proves who accepted or published it. The value parser validates shape and digests derivable from the envelope; it does not open filesystem refs. Producer and consumer code resolves relative refs only under an explicitly configured trusted coordinator state root, then verifies exact bytes and hashes. It never follows an absolute path from receipt input.

### Exact envelope and canonical encoding

The exact v1 keys are:

schema, receipt_id, producer_kind, producer_event_id, publication_id, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input, acceptance_evidence, published_object, transaction.

schema is exactly cortex-self-publication-receipt/v1. Unknown top-level and union keys invalidate the row. The parser rejects wrong types, invalid lowercase SHA-256, invalid producer kind, empty/non-canonical identity fields, duplicate JSON keys, non-finite numbers and values outside JSON. The raw-value parser rejects duplicate members; the registry boundary separately quarantines malformed receipt rows without collapsing duplicate pairs. When B presents the reserved one-key `$cortex_invalid_publication_receipt_v1` diagnostic wrapper, A treats the wrapper as an opaque invalid row and round-trips that wrapper without interpreting its encoded source token as a candidate receipt.

Define canonical JSON bytes:

    J(v) = UTF8(json.dumps(v, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False))
    H(d, v) = lowercase_hex(SHA256(ASCII(d + LF) || J(v)))
    raw_sha256(v) = lowercase_hex(SHA256(J(v)))

Reject unpaired Unicode surrogates before UTF-8 encoding. No case-folding or implicit trimming applies to repo/work/run/claim identity. Workspace ref is normalized relative POSIX form: non-empty, no leading slash, backslash, empty/dot/dot-dot segment. A URI ref is validated by its producer-specific grammar.

receipt_id is H("cortex-self-publication-receipt/v1", envelope with receipt_id omitted). pre_publication_authority_sha256 is the WorkAuthority canonical authority digest observed before this publication; it is not an artifact byte hash and does not relabel WorkflowRun.source_revision.

### Exact producer-specific union keys

The exact nested key sets, intent cores, event basis and per-output formulas are specified in the normative section below. That section replaces shorthand and is the only schema source of truth. Every transaction intent sidecar is immutable and retained after commit; the receipt stores its exact ref and hash. Mutable transaction journals are not receipt evidence.

### Stable identity rules

producer_event_id is derived from its exact producer event basis, not wall time, list row number or journal phase. A brainstorm event can generate multiple outputs; same producer_event_id with distinct publication_id values is valid. publication_id is per output and domain separated as specified. Receipt collision rules for Child B:

- same (producer_event_id, publication_id) and identical full canonical payload: exact replay, no-op;
- same pair and different payload, same publication_id under another event, or same receipt ID with different payload: conflict;
- another pair claiming the same variant-specific object key conflicts, even when bytes are equal; planning keys use artifact kind/ref and PR keys use repository/number/id/node_id. Re-publication requires a separately authorized new run/claim era, not another receipt for one object in the same era.

### Legacy value behavior

WorkflowRun missing publication_receipts loads as legacy empty. Each list element is parsed independently into a typed valid receipt or opaque invalid row with diagnostic. Non-list input is opaque invalid-container. to_dict/from_dict preserve invalid JSON values and diagnostics; B’s duplicate/non-finite reserved wrapper is round-tripped in its canonical wrapper form while its exact source token remains base64-embedded. Deep copies prevent mutation through nested raw objects. Invalid row/container stays invalid after reload and never becomes an authority fact.

A does not assert the current registry update path carries the field; Child B owns and tests that seam.

## Acceptance criteria

- [ ] Exact v1 envelope and all three exact producer unions have executable canonical golden vectors, including IDs, digests, marker grammar and rejected extra/missing fields.
- [ ] Retained accepted-input snapshots preserve brainstorm report/default/accepted pack facts and plan ordered selection/assessment facts; prepared intent cores bind those snapshots to post-write operations/evidence; PR receipt retains intent ref/hash and successful POST witness.
- [ ] Producer-specific accepted_input/ref/hash forms remain distinct; candidate commit SHA stays 40-hex; artifact and authority digests stay 64-hex.
- [ ] All hash domains and JSON rules match the formulas above; invalid JSON, duplicate keys, NaN, unknown schema/union, malformed refs, wrong types, cross-bindings and changed facts are rejected. Child B's registry loader preserves a duplicate-key receipt row as an opaque invalid diagnostic rather than bricking other runs.
- [ ] Same-event different-output receipts are valid when publication IDs differ; all defined collisions fail closed.
- [ ] Legacy/malformed row/container round-trips opaque raw values and invalid diagnostics through WorkflowRun copy/to_dict/from_dict/reload; absent legacy field remains empty only when truly absent.
- [ ] Read-back marker is only correlation. No POST-confirmed durable witness means a GET match cannot become Manager-created provenance.

## Out of scope

No registry append/CAS or ordinary registry update behavior (Child B), no API read-back (Child C), no Manager producer writes (#979/#980), no classifier/consumer (#964), and no AC10 live canary (#965/#847). #847 AC01–AC10 remains unchanged.

## Normative exact nested schemas and recomputation

The key lists below are exact. Hash-only or locator-only facts are invalid. Planning publication uses two retained immutable records because some accepted facts exist before writes and operation/evidence hashes exist only after writes: (1) an accepted-input snapshot is written and fsynced before the first workspace mutation; (2) a prepared publication-intent core is written and fsynced after all outputs/evidence are durable and before registry commit. The receipt carries refs and hashes to both records; the input snapshot retains pre-write facts and the intent core retains post-write operation/evidence facts. The mutable recovery journal may be retired and is never a permanent hash target. A producer must not mint a receipt if either record is absent or fails revalidation.

### Common transaction and operation forms

Transaction exact keys: kind, intent_ref, intent_sha256. intent_sha256 = raw_sha256(intent_core); the immutable sidecar bytes are exactly J(intent_core) with no trailing bytes. The core is retained at intent_ref after commit. Planning refs are planning-publication-intents/<encoded-run-id>/<intent-sha256>.json; PR refs are pr-publication-intents/<encoded-run-id>/<intent-sha256>.json, relative to the producer's configured state root. Encode each run-id path segment with RFC3986 percent encoding, leaving only A-Z, a-z, 0-9, hyphen, dot, underscore and tilde unescaped.

Operation proof projection exact keys: kind, path_domain, path, before_exists, before_sha256, after_sha256, mutation. kind is artifact or evidence; path_domain is workspace or coordinator_state; path is normalized relative POSIX under that typed, configured root (never absolute); before_exists and mutation are booleans; before_sha256 is null iff before_exists=false, otherwise lowercase 64-hex; after_sha256 is lowercase 64-hex. This bounded immutable projection excludes rollback journal before_content, before_mode, after_mode, created_dirs and mutable phase. Artifact receipt requires kind=artifact, path_domain=workspace and mutation=true. Evidence paths may be under coordinator_state and are separately bound by accepted_evidence ref/hash.

### Brainstorm exact subobjects

**Pre-mutation accepted-input snapshot.** `accepted_input` exact keys are `kind, ref, sha256`; kind=`brainstorm_context/v1`. `ref` is `planning-input-snapshots/<encoded-run-id>/brainstorm/<define-attempt>/<encoded-pack-id>.json`, relative to configured coordinator state root. `define_attempt` is the non-negative integer snapshot of `run.attempts["define"]` before brainstorm starts; current runs have no `brainstorm` attempt key. Before any artifact/evidence mutation, persist exact bytes `J(snapshot)` atomically and fsync the file and parent directory. Snapshot exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, define_attempt, completeness_report, default_question_pack, accepted_question_pack, input_artifacts`; schema=`cortex-brainstorm-accepted-input/v1`, producer_kind=`brainstorm_artifact`. Its `sha256` is `raw_sha256(snapshot)`.

Completeness report has exact keys `complete, missing_kinds, artifacts`; each assessment has `kind, ref, accepted, reasons, blocking_markers`; each blocker has `kind, line, text`. Each question pack has `schema_version, pack_id, questions`; each question has `question_id, kind, prompt, source_refs`. `input_artifacts` is the ordered assessment list with exact rows `kind, ref, sha256, accepted, reasons, blocking_markers`; each SHA hashes exact UTF-8 source bytes. Retain these in-memory facts in this pre-write snapshot; do not invent a source file or use source_revision as a file hash.

**Prepared post-publication intent.** `acceptance_evidence` exact keys are `kind, ref, sha256, facts`; kind=`brainstorm_peer/v1`. `ref` is `coordinator-state://` followed by the percent-encoded, normalized relative POSIX path of the exact GateEvidenceRef under configured coordinator state root; reject evidence outside that root. Encode each UTF-8 path segment with RFC3986 percent encoding, leaving only A-Z, a-z, 0-9, hyphen, dot, underscore and tilde unescaped. `sha256` is SHA-256 of exact evidence file bytes. `facts` exact keys are `schema_version, kind, scope, question_pack_id, secondary_evidence_hash, artifacts`; scope has `repo, work_id, source_revision`; each artifact has `kind, ref, sha256`. Derive `question_pack_id` from the persisted evidence payload at `payload.question_pack.pack_id` (there is no top-level question_pack_id), and require it to equal the pre-mutation snapshot accepted pack id and event_basis value. This is a bounded projection; retain/reopen the full evidence file at its trusted ref and verify its exact raw hash. Do not copy primary_integration/secondary_evidence payload into every receipt.

`published_object` exact keys are `kind, artifact_kind, ref, sha256, declared_output_pattern`; kind=`planning_artifact`; artifact_kind is `spec`, `design` or `plan`; ref is normalized workspace-relative path; sha256 hashes exact output bytes. Exactly one declared pattern must match.

Brainstorm `event_basis` exact keys are `repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input_ref, accepted_input_sha256, acceptance_evidence_ref, acceptance_evidence_sha256, question_pack_id, define_attempt`. Recompute `producer_event_id = H("cortex-self-publication-event/brainstorm/v1", intent_core.event_basis)` and cross-check every field against the receipt and retained records.

Brainstorm `intent_core` exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, define_attempt, accepted_input, acceptance_evidence, event_basis, operations`; schema=`cortex-planning-publication-intent/v1`; producer_kind=`brainstorm_artifact`. `accepted_input` repeats the outer exact `{kind, ref, sha256}` reference to the retained pre-mutation snapshot. `acceptance_evidence` is the exact bounded evidence reference/projection above. `operations` is sorted by `(path_domain, path, kind, after_sha256)`, with no duplicate `(path_domain,path)`, and each item uses the operation proof projection above. The producer creates this exact core only after artifact operations and peer evidence are finalized, persists/fsyncs it as immutable bytes `J(intent_core)` before registry commit, then uses `intent_sha256=raw_sha256(intent_core)` and ref `planning-publication-intents/<encoded-run-id>/<intent_sha256>.json` under coordinator state root. The core does not contain its own ref/hash.

For one peer evidence artifact row, exactly one operation must match semantic row ref and SHA, with `kind=artifact`, `path_domain=workspace`, `mutation=true`, operation path equal to row ref and `after_sha256` equal to row SHA. Only such mutation operations mint a receipt; unchanged/original rows do not. Recompute each `publication_id = H("cortex-self-publication-publication/brainstorm/v1", {"producer_event_id": producer_event_id, "operation": matched_operation})`. Same event may produce distinct publication IDs; event ID alone is not a duplicate. A crash before the prepared core is durably retained cannot be repaired into provenance by matching current bytes; follow the publication journal's fail-closed recovery/rollback path and mint no receipt.

### Plan materialization exact subobjects

accepted_input exact keys: kind, ref, sha256; kind=planning_artifact; ref is the exact selected source path; sha256 hashes exact selected source UTF-8 bytes.

WorkflowStep has no step_id/card_id fields. Producer identity is the exact unique pair phase=plan and WorkflowStep.card value; phase_attempt is the integer run.attempts["plan"] snapshot. If zero/multiple plan steps match that card or phase attempt is absent/invalid, do not mint; never substitute tuple position or list row index. URI ref is workflow-run://<encoded-run-id>/phases/plan/cards/<encoded-card>/attempts/<phase-attempt>/plan-acceptance.

`acceptance_evidence` exact keys are `kind, ref, sha256, facts`; kind=`manager_plan_acceptance/v1`. facts exact keys are `selection_rule, ordered_assessments, selected_source, phase, card, phase_attempt, declared_output_pattern, matching_output_pattern_count`. selection_rule is `first_accepted_kind_plan_in_assessment_order/v1`. Each ordered assessment has exact keys `kind, ref, sha256, accepted, reasons, blocking_markers`; each blocker has `kind, line, text`. selected_source exact keys are `kind, ref, sha256`, and is the first accepted kind=plan row. phase is `plan`; card is the exact unique WorkflowStep.card; matching_output_pattern_count is integer 1. `ref` is `planning-input-snapshots/<encoded-run-id>/plan/<phase-attempt>/<encoded-card>.json`, relative to configured coordinator state root; snapshot exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, phase, card, phase_attempt, accepted_input, acceptance_evidence_facts`; schema=`cortex-plan-accepted-input/v1`, producer_kind=`plan_materialization`. It retains the selected source row and exact selection/assessment facts before target mutation. Persist/fsync its canonical bytes before writing the target. `acceptance_evidence.sha256=raw_sha256(snapshot)`; the facts are validated against the exact facts retained in that snapshot and repeated in the final prepared intent core. Do not rely on a locator/hash alone or claim only one accepted plan exists.

published_object has the same exact five keys as brainstorm with kind=planning_artifact and artifact_kind=plan; ref/hash bind the newly written target, distinct from accepted_input. Exactly one step output pattern and one target match are required.

Plan event_basis exact keys are `repo, work_id, run_id, claim_key, pre_publication_authority_sha256, phase, card, phase_attempt, selected_source_ref, selected_source_sha256, acceptance_evidence_sha256`. Recompute producer_event_id=`H("cortex-self-publication-event/plan/v1", intent_core.event_basis)`. Plan `intent_core` exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input, acceptance_evidence, event_basis, output_operation`; schema=`cortex-plan-materialization-intent/v1`; producer_kind=`plan_materialization`. Selection/evidence facts are frozen in the pre-mutation plan snapshot before writing. After the target output is durable, persist/fsync exact core bytes `J(intent_core)` before registry commit; `intent_sha256=raw_sha256(intent_core)`, with ref `planning-publication-intents/<encoded-run-id>/<intent_sha256>.json` under coordinator state root. The core does not contain its own ref/hash. `output_operation` is one exact operation proof projection, `kind=artifact`, `path_domain=workspace`, `mutation=true`; its path/hash equal target ref/hash. Recompute publication_id=`H("cortex-self-publication-publication/plan/v1", {"producer_event_id": producer_event_id, "target": published_object})`. A crash before the final core is durably retained cannot be repaired into provenance by matching current bytes; mint no receipt.

### PR exact subobjects and creator witness

PR accepted_input exact keys: kind, repository, branch, commit_sha; kind=git_commit; commit_sha is 40 lowercase hex, never a SHA-256.

PR intent_core exact keys: schema, producer_kind, repo, work_id, run_id, claim_key, ship_step_card, ship_attempt, base_repository, base_branch, head_repository, head_branch, candidate_sha, request_metadata, request_metadata_digest, event_basis. schema=cortex-manager-pr-intent/v1; producer_kind=manager_pull_request; candidate_sha is 40 lowercase hex. event_basis exact keys: repo, work_id, run_id, claim_key, ship_step_card, ship_attempt, candidate_sha; recompute producer_event_id=H("cortex-self-publication-event/manager-pr/v1", intent_core.event_basis). publication_id=H("cortex-manager-pr-intent/v1", intent_core). request_metadata exact keys are title, body, labels; title and labels are NFC, body CRLF/CR becomes LF with all other characters preserved (body is not Unicode-normalized). Marker-free request body must not contain the case-insensitive sentinel text `cortex-self-publication-intent:`; only the renderer may add the one exact marker line. Labels are sorted, NFC, unique strings. request_metadata_digest=H("cortex-pr-request-metadata/v1", request_metadata). Final body rendering first maps CRLF/CR to LF and otherwise preserves code points; if body is non-empty and does not end with LF append exactly one LF; if it already ends with LF append no separator; if empty use no separator; then append marker as its own line. Marker line is exactly <!-- cortex-self-publication-intent:v1:<publication_id> -->. Intent hash excludes publication_id, marker, remote number, GET result and mutable phase.

PR transaction exact kind is manager_pr_intent/v1; ref/hash point to retained immutable core. acceptance_evidence exact keys: kind, ref, sha256, create_witness; kind=confirmed_manager_pr_create/v1; ref=workflow-run://<encoded-run-id>/ship/<encoded-card>/attempts/<ship-attempt>/confirmed-create; sha256=H("cortex-pr-confirmed-create-witness/v1", create_witness). create_witness exact keys: operation, post_succeeded, repository, number, id, node_id, response_sha256. operation is POST /repos/{repo}/pulls; post_succeeded is literal true; repo exact; number/id are positive non-bool integers; node_id non-empty; response_sha256=raw_sha256({"repository": repository, "number": number, "id": id, "node_id": node_id}). Only a successful structured POST result persisted before GET creates this witness. Timeout/lost response has no witness and a later matching GET stays ambiguous.

PR published_object exact keys: kind, repository, number, id, node_id, state, head_repository, head_branch, head_sha, base_repository, base_branch, intent_marker, request_metadata_digest. kind=github_pull_request, state=open; number/id/node_id match witness exactly; repository, head/base and SHA match intent; marker and metadata digest match intent. GET is an observation, not proof of creation. Any GET tuple differing from POST witness fails.

### Variant-specific object uniqueness key

For planning objects the append object key is (repo, work_id, run_id, claim_key, producer_kind, published_object.kind, published_object.artifact_kind, published_object.ref). For a PR it is (repo, work_id, run_id, claim_key, manager_pull_request, published_object.repository, published_object.number, published_object.id, published_object.node_id). The same key under another event/publication pair conflicts; PR schemas do not require a ref field.
