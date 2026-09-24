# Self-publication receipt v1 wire contract

**Status:** accepted schema and vector contract under issue [#1029](https://github.com/hamanpaul/paulsha-cortex/issues/1029), after scoped alignment by the #992, #993, #994, #979, and #980 owner reviewers at PR #1031 HEAD `b983ce93`. This document is not implementation evidence. The accompanying vectors are synthetic and are computed independently in `compute_vectors.py`.

This contract stays within the #1029 schema boundary. It does not change producer behavior, publication eligibility, registry write ordering, or any issue owner's production scope. The parent envelope and digest rules below are inherited from [#992](https://github.com/hamanpaul/paulsha-cortex/issues/992), not redefined.

## 1. Shared encoding and primitive rules

### 1.1 Canonical JSON and structured hashes

`J(v)` is UTF-8 bytes of exactly:

```python
json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

`H(domain, v)` is lowercase hex SHA-256 of `ASCII(domain + "\n") || J(v)`. Domain strings are ASCII and the separator is exactly one LF byte. Do not append a newline to `J(v)`.

Reject duplicate object keys before conversion to a normal mapping, `NaN`, `Infinity`, `-Infinity`, non-JSON values, unpaired Unicode surrogates, uppercase/non-64-hex digest strings, and booleans where an integer is required. All hashes in wire values are lowercase 64-hex strings. Git commit identities are lowercase 40-hex strings.

### 1.2 Raw-byte hashes, structured hashes, and the WorkAuthority digest

The following are ordinary `SHA256(bytes)` values, with no domain prefix:

- `sha256` for artifact bytes, peer evidence bytes, accepted-input snapshot bytes, and immutable intent sidecar bytes;
- `pre_publication_authority_sha256` is the WorkAuthority digest captured by the producer under #992/#979/#980. It is a separate authority value: do not derive it from this receipt, substitute `source_revision`, run it through `H`, or assume it equals a snapshot/artifact/intent raw-byte hash. The trusted producer proves its origin and equality to the captured current run before publication; a value parser can only check that it is lowercase 64-hex and that copies inside the receipt and resolved sidecars agree.

These categories are not interchangeable. Raw-byte SHA256 binds stored bytes, `H` binds a structured JSON value under a named domain, and the WorkAuthority digest binds the authority object selected by the producer's authority contract. In particular, `J(sidecar)` having a raw SHA does not make that SHA the authority digest.

All event IDs, publication IDs, receipt IDs, acceptance-facts IDs, and request-metadata IDs use `H(domain, value)`. A path/hash pair never substitutes for the content required by the respective snapshot or intent schema.

### 1.3 Strings, repo identity, and paths

- Strings are compared by exact Unicode scalar sequence. No case folding, whitespace trimming, or normalization is performed unless this section names a field-specific normalization. Reject empty/all-whitespace identity strings and non-scalar strings. Digests remain ASCII-only.
- `repo` and GitHub repository fields use exact `owner/name` spelling; each component matches `[A-Za-z0-9_.-]+`. Case is significant in receipt binding. A GitHub response must return the same spelling as the intent.
- `work_id`, `run_id`, `claim_key`, card IDs, branches, and node IDs are case-sensitive opaque strings. `claim_key` is `claim:v1:` followed by 64 lowercase hex characters. The value parser validates exact nested shape, IDs, digest formulas, and all available envelope/union cross-bindings. The producer separately proves that the `pre_publication_authority_sha256` originated from the trusted current WorkAuthority/run claim; a parser cannot establish that trusted origin from serialized values alone.
- Workspace refs and coordinator-relative sidecar refs are canonical POSIX relative paths: nonempty; no leading `/`, backslash, empty, `.` or `..` component; and exact equality with their normalized slash-joined spelling. A workspace ref is rooted at the run workspace. A coordinator ref is rooted at the coordinator state root.
- `<encoded-run-id>` and `<encoded-card>` use UTF-8 percent encoding per URI path segment: leave only ASCII letters, digits, `-`, `_`, `.`, `~` unescaped; use uppercase hex for every escaped byte. Encode a whole `.` or `..` segment as `%2E` or `%2E%2E` so it cannot become traversal.
- Every wire object below has exactly the listed keys. `null` is allowed only at fields explicitly declared nullable. Arrays preserve the producer's source order unless a sort order is explicitly specified.

### 1.4 Receipt envelope

The v1 receipt has exactly these 14 keys and no others:

```json
{
  "schema": "cortex-self-publication-receipt/v1",
  "receipt_id": "<64-lower-hex>",
  "producer_kind": "brainstorm_artifact | plan_materialization | manager_pull_request",
  "producer_event_id": "<64-lower-hex>",
  "publication_id": "<64-lower-hex>",
  "repo": "<owner/name>",
  "work_id": "<opaque nonempty string>",
  "run_id": "<opaque nonempty string>",
  "claim_key": "claim:v1:<64-lower-hex>",
  "pre_publication_authority_sha256": "<64-lower-hex>",
  "accepted_input": "<producer union below>",
  "acceptance_evidence": "<producer union below>",
  "published_object": "<producer union below>",
  "transaction": {"kind": "<intent schema literal>", "intent_ref": "<coordinator-relative ref>", "intent_sha256": "<64-lower-hex>"}
}
```

`receipt_id = H("cortex-self-publication-receipt/v1", envelope_without_receipt_id)`. `transaction` has exactly `kind`, `intent_ref`, `intent_sha256`; `kind` must equal the producer-specific intent schema literal below. The pure receipt value parser checks shape, primitive types/ranges, digest syntax, `receipt_id`, and cross-bindings whose operands are contained in the receipt itself. This includes PR `acceptance_evidence.intent_ref` and `intent_sha256` equality with `transaction.intent_ref` and `intent_sha256`, PR POST witness/read-back tuple equality, and the marker derived from `publication_id`. It does not open refs or claim to recompute event/publication IDs whose basis is in sidecars.

### 1.5 Value parsing, sidecar resolution, and reload

- **Pure value parsing:** `WorkflowRun.from_dict` and each receipt `from_dict` validate only serialized receipt values: exact keys/types, digest syntax, `receipt_id`, producer-kind/transaction-kind pairing, local producer-union joins, and any formula whose complete basis is included in the receipt. No filesystem or GitHub access occurs. Separately, after the intent sidecar has been resolved and parsed into an intent value, a pure intent-value validator can recompute `request_metadata_sha256` and the PR `publication_id` from that intent core. The receipt does not contain the intent core, so this PR validation cannot be done from the receipt alone or during `WorkflowRun.from_dict`.
- **Sidecar resolution:** a separate producer/append validation boundary resolves immutable snapshot/evidence/intent refs, hashes their exact stored bytes, parses their exact schemas, passes the parsed values to pure sidecar-value validators, rederives event and publication bases, checks receipt-to-sidecar joins, and verifies the producer-specific operation/evidence rules below. This stage reports missing, changed, or mismatched sidecars. Trusted WorkAuthority origin and current run/claim proof remain #979/#980 producer responsibilities.
- **Typed-valid reload:** a receipt that passes the pure value parser reloads as a typed-valid receipt value even when sidecars are not opened during registry load. “Typed-valid” is not a provenance verdict. Reload neither fetches sidecars nor upgrades the receipt to producer-verified; only the sidecar-resolver/producer boundary can establish that proof. Opaque invalid rows/containers stay opaque and append-conflicting.

### 1.6 Exact receipt-to-sidecar joins

These joins are checked by the producer-side resolver before the private registry append. They do not make `WorkflowRun.from_dict` a resolver. “Envelope identity” below means exact equality of `repo`, `work_id`, `run_id`, `claim_key`, and `pre_publication_authority_sha256` wherever those fields are present. The authority digest's trusted origin/current-run check remains separate from this equality check.

| Producer | Receipt and resolved sidecar joins |
|---|---|
| `brainstorm_artifact` | Resolve the exact `accepted_input.ref`, verify SHA256 over stored bytes, parse the snapshot, and bind its envelope identity and `define_attempt` to the captured event. Receipt `accepted_input` equals that snapshot ref/raw-byte hash. Receipt `acceptance_evidence.gate_ref` equals the reread `GateEvidenceRef` (`kind`, exact `ref`, raw-byte hash), and its `question_pack_id` equals the snapshot's accepted pack ID; default and accepted packs equal the production deterministic pack. Resolve the immutable planning intent at `transaction.intent_ref`, verify exact raw bytes/hash and `cortex-planning-publication-intent/v1`, then bind its envelope and `accepted_input` to the receipt/snapshot and its `acceptance_evidence` ref/hash/pack ID to the receipt. Recompute the event from captured identity, snapshot raw-byte hash, GateEvidenceRef ref/hash, and pack ID. The selected operation must equal one workspace artifact mutation and the receipt's `published_object`; its path must match exactly one declared output pattern. The persisted transaction journal must bind the same GateEvidenceRef evidence write (same ref/kind/after hash) and selected output operation. The freshly reread peer payload must have `schema_version=1`, `kind="brainstorm-peer"`, exact scope keys `{repo,work_id,source_revision}`, the captured repo/work ID, and source revision equal to `planning_source_revision` when that optional captured value is set. Its unique same-kind/ref/hash artifact row and primary-integration artifact SOURCE TEXT must bind the selected operation's after hash. |
| `plan_materialization` | Resolve the exact snapshot and intent refs and verify each raw-byte SHA. Bind both sidecar envelope identities to the receipt and each other. The receipt `accepted_input` equals the first accepted plan assessment's kind/ref/hash, with source text retained in the snapshot and SHA256 recomputed from its UTF-8 bytes. Receipt acceptance facts equal the snapshot's ordered assessments, selected index, phase/card/attempt, output pattern and one-match target facts. The intent's accepted input and acceptance evidence equal those receipt facts. Recompute event/publication IDs from the selected source, acceptance-facts digest and exact target/operation. The operation and `published_object` must identify the same workspace artifact; the target must be new (`before_exists=false`, `before_sha256=null`) and mutated. |
| `manager_pull_request` | Resolve the exact immutable intent sidecar named by `transaction.intent_ref`, verify raw-byte SHA256 against both `transaction.intent_sha256` and `acceptance_evidence.intent_sha256`, and require `acceptance_evidence.intent_ref == transaction.intent_ref`. Parse `cortex-manager-pr-intent/v1`, bind its repo/work/run/claim identity to the receipt, check its raw-byte hash, and recompute metadata digest and publication ID from the parsed intent core. Receipt `accepted_input` equals intent repo/head branch/candidate SHA. The intent's base/head and request metadata are the comparison basis for the POST target and later GET. `post_witness.repository` must equal the canonical intent/POST target repository and the successful POST response repository; it is not established by the GET. Durable delivery-journal intent confirmation precedes POST; a lost/unknown append is resolved before proceeding. A successful structured POST witness must be durably confirmed before authenticated GET. Only after complete GET equality (tuple, state, base/head, normalized title/labels, raw marker scan, and rendered body) is `request_metadata_sha256` admitted to `published_object`. A current metadata file or wrapped gate-evidence payload is not the canonical intent sidecar. |

For every immutable snapshot/intent sidecar, the canonical ref is create-once: if the ref already contains the exact same bytes, an exact-byte replay is idempotent; if it contains any different bytes, fail closed even when the decoded JSON would be semantically equal. Intent bytes are exactly `J(core)` with no final LF. Existing producer evidence files retain their own producer-defined byte format; this rule does not silently rewrite them as canonical sidecars.

### 1.7 Deterministic diagnostic selection

Apply the first applicable rule in this order, as confirmed by #992: (1) raw duplicate JSON member names are detected before mapping and preserved as `invalid-json-row` / `duplicate-json-key` by the #993 boundary; #992 does not reclassify them; (2) `unknown-schema` only when the value is an object whose `schema` is a string other than `cortex-self-publication-receipt/v1`; (3) `unknown-producer-kind` only when the schema is the known v1 literal and `producer_kind` is an explicit string outside the three allowed variants; (4) `malformed-known-v1` for a non-object, missing/non-string `schema` or `producer_kind`, missing/extra keys, wrong type, or out-of-range value; (5) `invalid-digest` for digest syntax, a contained digest formula, or resolved sidecar-byte hash mismatch; (6) `cross-binding` for producer/transaction variant mismatch, other local field disagreement, or resolved event/publication/evidence join mismatch. Append replay/conflict outcomes are #993 results, not receipt diagnostic codes.

Every row wrapper's `diagnostic.index` is the zero-based original `publication_receipts[j]` position. It must satisfy `type(index) is int and index >= 0`; booleans and floats are invalid. A duplicate-key raw row uses the same `j`, even when preceding rows are separately invalid.

## 2. Shared bounded operation proof

Brainstorm and plan intent cores use this exact operation object:

```json
{
  "path_domain": "coordinator | workspace",
  "ref": "<canonical relative path in the named domain>",
  "kind": "artifact | evidence",
  "before_exists": false,
  "before_sha256": null,
  "after_sha256": "<64-lower-hex>",
  "mutation": true
}
```

The exact keys are `path_domain`, `ref`, `kind`, `before_exists`, `before_sha256`, `after_sha256`, and `mutation`. `before_sha256` is `null` iff `before_exists` is false; otherwise it is a lowercase 64-hex digest. `mutation` is a JSON boolean recording that the publication operation wrote the target; an idempotent evidence no-op may be false. The proof omits rollback `before_content`, modes, created directories, mutable journal phase, and absolute host paths. It is a bounded projection of the operation observed by the existing planning transaction.

For an intent's `operations` array, sort ascending by `(path_domain, ref, kind, after_sha256)` using Unicode code-point order. Reject duplicate tuple keys. Every brainstorm or plan output operation must have `path_domain="workspace"`, `kind="artifact"`, and `mutation=true`; its `published_object` ref and SHA equal that operation's `ref` and `after_sha256`. The `before_exists`/`before_sha256` pair records the actual prior state. A plan output must be new (`before_exists=false`, `before_sha256=null`); a pre-existing same-byte plan target does not mint a receipt. Brainstorm may overwrite an existing authority-owned artifact: when present, its prior content hash is retained in `before_sha256`, and `before_exists=true` does not by itself disqualify the mutation. An evidence operation can be present in the same transaction but is not itself a published artifact; an idempotent evidence no-op may have `mutation=false`.

## 3. Producer unions

### 3.1 `brainstorm_artifact`

#### Accepted-input snapshot

Before any workspace mutation, write canonical `J(snapshot)` bytes (no trailing LF) to:

```text
planning-input-snapshots/<encoded-run-id>/brainstorm/<define-attempt>.json
```

`define-attempt` is a positive JSON integer. The snapshot has exactly:

```json
{
  "schema": "cortex-brainstorm-accepted-input/v1",
  "repo": "<owner/name>",
  "work_id": "<opaque nonempty string>",
  "run_id": "<opaque nonempty string>",
  "claim_key": "claim:v1:<64-lower-hex>",
  "pre_publication_authority_sha256": "<64-lower-hex>",
  "define_attempt": 1,
  "completeness_report": {
    "complete": false,
    "missing_kinds": ["<nonempty string>"],
    "artifacts": [
      {"kind": "<planning kind>", "ref": "<workspace ref>", "accepted": true, "reasons": [], "blocking_markers": []}
    ]
  },
  "default_question_pack": {
    "schema_version": 1,
    "pack_id": "<nonempty string>",
    "questions": [
      {"question_id": "<nonempty string>", "kind": "<nonempty string>", "prompt": "<string>", "source_refs": ["<workspace ref>"]}
    ]
  },
  "accepted_question_pack": "<same exact question-pack shape>",
  "artifacts": [
    {"kind": "<planning kind>", "ref": "<workspace ref>", "sha256": "<64-lower-hex>", "content_utf8": "<exact decoded artifact bytes>"}
  ]
}
```

Exact keys for a report are `complete`, `missing_kinds`, `artifacts`; each report row has `kind`, `ref`, `accepted`, `reasons`, `blocking_markers`; each blocking marker has `kind`, `line`, `text`. A question pack has `schema_version`, `pack_id`, `questions`; each question has `question_id`, `kind`, `prompt`, `source_refs`. An artifact snapshot row has `kind`, `ref`, `sha256`, `content_utf8`. Planning kinds are `spec`, `design`, and `plan`. Strings and arrays in the report, packs, and artifact facts retain the exact `CompletenessReport.to_dict()` / `QuestionPack.to_dict()` / accepted input order from the Manager invocation.

`artifacts` is one-to-one and same-order with `completeness_report.artifacts`; each ref/kind/acceptance/reason/marker set must agree, each content string must encode as UTF-8, and each `sha256` is SHA256 of those UTF-8 bytes. The default and accepted question packs must be structurally identical after `validate_question_pack` succeeds. The snapshot itself binds `repo/work/run/claim/authority` to the same producer event.

The default pack is derived from the production deterministic algorithm in `planning.py` (`_make_question` and `_build_default_question_pack`; see the source ranges recorded in `decision-log.md`). Compute `accepted_refs` in assessment order. For each missing kind, use refs of assessments with that kind, or fall back to all accepted refs when that list is empty. Append blocking-decision questions in assessment order after missing-kind questions. For a question, `identity` is exactly `{"kind": kind, "prompt": prompt, "source_refs": source_refs}`; `question_id` is `q-` plus the first 16 lowercase hex characters of ordinary `SHA256(J(identity))`. `pack_id` is `qp-` plus the first 24 lowercase hex characters of ordinary `SHA256(J([question.to_dict(), ...]))`. `validate_question_pack` trims validated string fields, rejects duplicate IDs, then requires `normalized.to_dict() == report.default_question_pack.to_dict()`; it does not accept an arbitrary syntactically valid pack.

For the positive vector below, the only accepted assessment is `plan` at `workstreams/demo/todo.md`, with no blockers; `spec` and `design` are missing in the production order. Neither has a same-kind assessment, so both questions use the accepted plan ref. The computed IDs are `q-df4aa821a3fa77ff` and `q-77064250f4775eb4`, and the pack ID is `qp-e9e6ef5058abe3c28d31a246`. The accepted pack repeats that exact value. `golden-vectors.json` records each identity object, source refs, full rows, pack body, and IDs. These values are derived by the independent stdlib calculator from the production algorithm; they are not hard-coded guesses.

The same vector uses task slug `contract-1029` and the compiled `brainstorming` card outputs from `deck/data/cards.yaml`: `docs/superpowers/specs/*contract-1029*-spec.md` and `docs/superpowers/specs/*contract-1029*-design.md`. Its two output refs match those distinct declared patterns; the calculator asserts both matches. This output-pattern fixture is scoped to the existing manifest and does not add a producer or alter its allowed outputs.

#### Receipt union

`accepted_input` exact keys: `ref`, `sha256`; the ref is the snapshot ref above and the hash is SHA256 of its exact stored bytes.

`acceptance_evidence` exact keys:

```json
{
  "gate_ref": {"kind": "brainstorm", "ref": "<absolute current GateEvidenceRef path>", "sha256": "<raw evidence-byte sha256>"},
  "question_pack_id": "<accepted pack_id>"
}
```

The gate ref object matches the existing `GateEvidenceRef.to_dict()` shape and has exactly `kind`, `ref`, `sha256`. Preserve its current absolute path spelling; the producer must independently prove it resolves within the coordinator evidence root and reread its exact bytes. Do not use an absolute path for the immutable intent sidecar or bounded operation proof.

`published_object` exact keys: `kind`, `ref`, `sha256`, `output_pattern`. `kind` is a planning kind; `ref` is a workspace-relative path; `sha256` is artifact-byte SHA256; `output_pattern` is the unique exact declared planner output pattern matching that ref.

The peer-evidence payload has `schema_version=1`, `kind="brainstorm-peer"`, and a `scope` object with exactly `repo`, `work_id`, and `source_revision`. Bind `repo` and `work_id` to the captured run; when `run.planning_source_revision` is set, require `source_revision` to equal it. The producer also proves the authority origin from the trusted current run claim. Read the evidence afresh through the captured `GateEvidenceRef` (`kind`, `ref`, and raw-byte `sha256`); the same planning transaction journal must bind that evidence write and the selected output operation. No synthetic transaction-ID field is added.

For each brainstorm receipt, the selected operation must be a workspace artifact mutation with `mutation=true`; it may create or overwrite the workspace artifact, and its before-state fields must truthfully record that transaction. In the same transaction's freshly reread peer-evidence `artifacts` array, there must be exactly one row whose planning `kind`, `ref`, and `sha256` equal the selected output. The integration resolution for that output must agree on kind/ref and the output bytes must match the evidence hash. The evidence scope and GateEvidenceRef/operation-journal binding must also match the captured run and same transaction. Zero or multiple matching evidence rows, a wrong run/authority scope, a coordinator-domain output operation, a changed/missing evidence row, or an operation with `mutation=false` yields no receipt. An existing authority-owned artifact can be replaced when the transaction records a real mutation. The operation row, peer-evidence row, and published object are one joined proof; receipt IDs are not minted from evidence rows alone.

#### Event and publication identity

`event_basis` has exactly:

```json
{
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "pre_publication_authority_sha256": "<64-hex>",
  "accepted_input_sha256": "<snapshot raw-byte sha256>",
  "brainstorm_evidence": {"ref": "<exact GateEvidenceRef ref>", "sha256": "<raw evidence-byte sha256>"},
  "question_pack_id": "<accepted pack_id>"
}
```

`producer_event_id = H("cortex-self-publication-event/brainstorm/v1", event_basis)`.

The immutable prepared intent core has exactly:

```json
{
  "schema": "cortex-planning-publication-intent/v1",
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "pre_publication_authority_sha256": "<64-hex>",
  "accepted_input": {"ref": "<snapshot ref>", "sha256": "<snapshot raw-byte sha256>"},
  "acceptance_evidence": {"ref": "<absolute GateEvidenceRef ref>", "sha256": "<raw evidence-byte sha256>", "question_pack_id": "<pack id>"},
  "operations": ["<sorted bounded operation proof rows>"]
}
```

Write the exact canonical `J(intent_core)` bytes, with no trailing LF, after all outputs and peer evidence are durable and before the registry append. `transaction.kind` is `cortex-planning-publication-intent/v1`; `intent_sha256` is SHA256 of the exact sidecar bytes. The relative intent ref is:

```text
planning-publication-intents/<encoded-run-id>/brainstorm/<producer-event-id>.json
```

`publication_basis` has exactly `producer_event_id` and `operation`; the latter is the one exact selected mutating artifact operation object from the intent core. `publication_id = H("cortex-self-publication-publication/brainstorm/v1", publication_basis)`. A single event may have multiple output receipts with different operation rows and publication IDs.

### 3.2 `plan_materialization`

#### Accepted-input snapshot

Before writing the target, write canonical `J(snapshot)` bytes (no trailing LF) to the path fixed by #992:

```text
planning-input-snapshots/<encoded-run-id>/plan/<phase-attempt>/<encoded-card>.json
```

`phase-attempt` is a positive JSON integer. The snapshot has exactly:

```json
{
  "schema": "cortex-plan-accepted-input/v1",
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "pre_publication_authority_sha256": "<64-hex>",
  "phase": "plan", "card": "<exact WorkflowStep.card>", "phase_attempt": 1,
  "selection_rule": "first_accepted_kind_plan_in_assessment_order/v1",
  "assessments": [
    {"kind": "<planning kind>", "ref": "<workspace ref>", "sha256": "<raw source-byte sha256>", "content_utf8": "<exact source bytes decoded as UTF-8>", "accepted": false, "reasons": ["<string>"], "blocking_markers": [{"kind": "<string>", "line": 1, "text": "<string>"}]}
  ],
  "selected_index": 0,
  "declared_output_patterns": ["<exact single plan output pattern>"],
  "target": {"kind": "plan", "ref": "<canonical target workspace ref>", "sha256": "<selected source-byte sha256>", "output_pattern": "<same pattern>", "match_count": 1}
}
```

Each assessment uses exact keys `kind`, `ref`, `sha256`, `content_utf8`, `accepted`, `reasons`, `blocking_markers`; marker rows use `kind`, `line`, `text`. The assessment list is the Manager's full ordered assessment list. Its selected index is the first row where `accepted=true` and `kind="plan"`; selected source hash is recomputed from `content_utf8`. `target.ref` is the one canonical path derived by the existing materializer from the sole declared output pattern, and that target must match the pattern exactly once. `target.sha256` equals selected source bytes' SHA256.

#### Receipt union

`accepted_input` exact keys: `kind`, `ref`, `sha256`; `kind` is literal `plan`, `ref` is the selected source's canonical workspace-relative ref, and `sha256` is its exact UTF-8 source-byte hash.

`acceptance_evidence` exact keys: `ref`, `sha256`, `selection_rule`, `selected_index`, `phase`, `card`, `phase_attempt`, `output_pattern`, `match_count`. The sidecar `ref` and raw-byte `sha256` point to the full snapshot above. The remaining fields must equal its selection facts. `phase` is literal `plan`; `selection_rule` is literal `first_accepted_kind_plan_in_assessment_order/v1`; `match_count` is literal integer `1`.

`published_object` exact keys: `kind`, `ref`, `sha256`, `output_pattern`; `kind` is literal `plan`; `ref` and `sha256` equal the target facts; `output_pattern` is the unique plan output pattern.

The plan intent's `operation` is the exact workspace artifact operation for `published_object`; it must have `before_exists=false`, `before_sha256=null`, and `mutation=true`. If the materializer encounters a pre-existing target with the same bytes, it does not mint a receipt: byte equality alone is not a new write.

#### Event, acceptance-facts, and publication identity

Define `acceptance_facts` with exactly `selection_rule`, `assessments`, `selected_index`, `phase`, `card`, `phase_attempt`, `declared_output_patterns`, and `target_match_count`. `assessments` is the ordered projection of each snapshot assessment to `kind`, `ref`, `sha256`, `accepted`, `reasons`, and `blocking_markers`; the exact source bytes are retained in the snapshot and their hashes are in this projection. `acceptance_facts_sha256 = H("cortex-plan-acceptance-facts/v1", acceptance_facts)`.

`event_basis` has exactly:

```json
{
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "pre_publication_authority_sha256": "<64-hex>", "phase": "plan", "card": "<card>", "phase_attempt": 1,
  "selected_source": {"kind": "plan", "ref": "<source ref>", "sha256": "<source-byte sha256>"},
  "acceptance_facts_sha256": "<64-hex H result>"
}
```

`producer_event_id = H("cortex-self-publication-event/plan/v1", event_basis)`.

The immutable prepared intent core has exactly:

```json
{
  "schema": "cortex-plan-materialization-intent/v1",
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "pre_publication_authority_sha256": "<64-hex>",
  "accepted_input": {"kind": "plan", "ref": "<source ref>", "sha256": "<source-byte sha256>"},
  "acceptance_evidence": {"ref": "<snapshot ref>", "sha256": "<snapshot raw-byte sha256>", "selection_rule": "first_accepted_kind_plan_in_assessment_order/v1", "selected_index": 0, "phase": "plan", "card": "<card>", "phase_attempt": 1, "output_pattern": "<pattern>", "match_count": 1},
  "operation": "<one bounded workspace artifact operation proof row>"
}
```

Write canonical `J(intent_core)` (no trailing LF) after target publication and before registry append. `transaction.kind` is `cortex-plan-materialization-intent/v1`; `intent_sha256` is raw SHA256 of those exact bytes. The relative intent ref is:

```text
planning-publication-intents/<encoded-run-id>/plan/<producer-event-id>.json
```

`publication_basis` has exactly `producer_event_id` and `target`; `target` has exactly `kind`, `ref`, `sha256`, `output_pattern` from `published_object`. `publication_id = H("cortex-self-publication-publication/plan/v1", publication_basis)`.

### 3.3 `manager_pull_request`

#### Request metadata and durable intent

Before the first publication-metadata write, the producer reads the exact full-registry file bytes and captures their raw-byte SHA-256 revision. This token covers the whole registry file, including every workflow and receipt row; it is not a parsed-row digest or a structured `H` value. At that boundary the producer also verifies the trusted WorkAuthority origin and the captured run/work/repository/claim binding against the registry workflow row, including the authority digest/revision. It holds both the verified binding and raw-byte revision through publication and passes that exact held revision as #993 append's expected revision. A stale registry revision at append, or any changed run/claim/authority binding after capture (including a mismatch detected before the first metadata write), fails closed without appending a receipt; the producer does not silently reread, rebase, or substitute a new revision. This full-registry raw-byte revision is separate from the #983 delivery-journal revision used for journal compare-and-swap.

With those preconditions held, the producer writes the immutable canonical intent sidecar and durably appends/confirms the matching intent record in the delivery journal before issuing the POST. If that append outcome is unknown, it resolves the journal state before POST. After POST, only a successful structured response supplies the witness; the witness is durably confirmed before GET. A timeout or lost POST response fails closed: a later matching GET cannot be used as proof that this producer created the PR. These are producer ordering constraints from #980/#983/#994, recorded here so the receipt cannot be interpreted as evidence for an unobserved order.

Normalize the request before computing any ID:

- NFC-normalize `title`;
- in `body`, replace CRLF and lone CR with LF and preserve every other code point; reject any existing marker line or marker-like line containing `cortex-self-publication-intent:` under case-fold comparison;
- NFC-normalize labels, reject empty labels, deduplicate equal normalized labels, then sort the remaining labels by Unicode code-point order.

`request_metadata` has exactly `title`, `body`, `labels`. `request_metadata_sha256 = H("cortex-manager-pr-request-metadata/v1", request_metadata)`. For this contract the immutable intent retains the normalized request values as well as that digest, allowing the digest and later read-back to be recomputed from durable facts.

The immutable PR intent core has exactly:

```json
{
  "schema": "cortex-manager-pr-intent/v1",
  "repo": "<PR repository owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "base": {"repository": "<owner/name>", "branch": "<base branch>"},
  "head": {"repository": "<owner/name>", "branch": "<head branch>", "sha": "<lowercase 40-hex candidate commit>"},
  "request_metadata": {"title": "<NFC title>", "body": "<line-ending normalized marker-free body>", "labels": ["<NFC sorted unique labels>"]},
  "request_metadata_sha256": "<64-hex H result>"
}
```

The `base` object has exactly `repository`, `branch`; `head` has exactly `repository`, `branch`, `sha`. The intent core excludes the publication ID, marker, PR number, REST ID, node ID, observation/read-back, and mutable journal state. `publication_id = H("cortex-manager-pr-intent/v1", immutable_intent_core)`. Write canonical `J(intent_core)` bytes with no trailing LF before the remote POST. The relative intent ref is:

```text
delivery-intents/<encoded-run-id>/manager-pull-request/<publication-id>.json
```

`transaction.kind` is `cortex-manager-pr-intent/v1`; `intent_sha256` is SHA256 of the exact stored canonical intent bytes. `accepted_input` has exactly `repository`, `branch`, `candidate_sha` and equals the core's `repo`, `head.branch`, and `head.sha`, respectively.

#### Marker rendering

The only marker syntax is one exact line:

```text
<!-- cortex-self-publication-intent:v1:<publication_id> -->
```

The final body is normalized marker-free body followed by exactly one LF iff it is nonempty and does not already end in LF, then the marker line. An empty body is just the marker line. The marker is excluded from request metadata and the intent digest. Existing/adopted PRs are never retrofitted with it.

The GET body is the unmodified JSON-decoded Unicode string in `pull["body"]`, before normalization or trimming; it is not the HTTP response bytes. Scan that original string first, splitting only on CRLF, CR, or LF line boundaries. A line is marker-like when its case-folded text contains `cortex-self-publication-intent:`. Exactly one marker-like line must exist, and it must equal the expected marker line by exact Unicode code-point sequence. Reject duplicate exact lines and every malformed marker-like line, including wrong version/ID, altered case, surrounding whitespace, or other text on the line. After the marker scan, replace CRLF and lone CR with LF in the entire GET string, then require exact Unicode equality with the full rendered marker-bearing body derived above. Thus line-ending style may differ; no other body code point may differ. The GET body is validation input and is not duplicated in `published_object`.

#### Receipt union and read-back

`acceptance_evidence` has exactly:

```json
{
  "intent_ref": "<relative intent ref>",
  "intent_sha256": "<64-lower-hex raw-byte hash>",
  "post_witness": {"repository": "<owner/name>", "number": 7, "id": 7007, "node_id": "<nonempty immutable node ID>"}
}
```

`post_witness` has exactly `repository`, `number`, `id`, `node_id`; `number` and `id` are positive JSON integers (booleans rejected); `node_id` is a nonempty string. This value exists only after a successful structured POST response was durably recorded. Its `repository` equals the canonical target derived from the immutable intent and the POST response's repository. Do not establish this witness from the later GET repository. Timeout/lost response is ambiguous even when a later GET finds a matching marker.

`published_object` is the authenticated #994 GET observation and has exactly:

```json
{
  "repository": "<owner/name>", "number": 7, "id": 7007, "node_id": "<immutable node ID>", "state": "open",
  "head": {"repository": "<owner/name>", "branch": "<branch>", "sha": "<lowercase 40-hex>"},
  "base": {"repository": "<owner/name>", "branch": "<branch>"},
  "marker": "<!-- cortex-self-publication-intent:v1:<publication_id> -->",
  "request_metadata_sha256": "<64-hex H result>"
}
```

`head` and `base` keys match the corresponding intent objects. State is literal `open`. The canonical repository, number, REST ID, and node ID in the GET must each equal the durable POST witness; the witness repository is independently bound to intent target/POST response. All base/head/marker fields must equal the intent. Normalize GET title and labels under the same NFC/deduplicate/sort rules and compare them with intent `request_metadata`; scan raw GET marker lines, normalize only its line endings, then compare the complete body with the exact rendered marker-bearing body as described above. Set `published_object.request_metadata_sha256` only after all of these GET equality checks pass; it is the canonical marker-free metadata digest from the resolved intent, not a digest of the marker-bearing GET body. The receipt does not duplicate PR body/title/labels there.

#### Event identity

`event_basis` has exactly:

```json
{
  "repo": "<owner/name>", "work_id": "<id>", "run_id": "<id>", "claim_key": "<claim key>",
  "ship_step": {"phase": "ship", "card": "<unique exact WorkflowStep.card>"},
  "ship_attempt": 1,
  "candidate": {"repository": "<owner/name>", "branch": "<branch>", "sha": "<lowercase 40-hex candidate commit>"}
}
```

`ship_step` keys are exactly `phase`, `card`; `phase` is literal `ship`. `candidate` keys are exactly `repository`, `branch`, `sha`. The producer proves the exact ship step and `run.attempts["ship"]` from the trusted current run. `producer_event_id = H("cortex-self-publication-event/manager-pr/v1", event_basis)`.

## 4. Invalid and legacy receipt history

### 4.1 `WorkflowRun.publication_receipts` field

- If the serialized run has no `publication_receipts` member, read it as an empty history. Do not backfill a receipt.
- A normal history is a JSON list. Each receipt is parsed independently by the pure value parser. A locally typed-valid receipt remains typed-valid on `WorkflowRun.from_dict` reload without sidecar resolution; it is not thereby provenance-verified. Unknown schema, unknown producer kind, missing/extra key, wrong type, wrong contained digest, local cross-binding failure, or another malformed object becomes a deterministic opaque invalid row; it does not prevent other WorkflowRun rows from loading.
- A malformed row already available as a JSON value is represented on the next serialization as exactly:

```json
{"kind":"cortex-publication-receipt-invalid-row/v1","raw":<original JSON value>,"diagnostic":{"code":"<closed diagnostic code>","index":j}}
```

Allowed row diagnostic codes are `unknown-schema`, `unknown-producer-kind`, `malformed-known-v1`, `invalid-digest`, and `cross-binding`. Keep `raw` by defensive deep copy. Preserve its full nested value without conversion to a valid receipt.
- A row containing duplicate JSON object keys cannot be recovered from an already-built Python dict. At the raw registry decoder boundary, retain that row's exact UTF-8 source slice as:

```json
{"kind":"cortex-publication-receipt-invalid-json-row/v1","raw_json":"<exact row source text>","diagnostic":{"code":"duplicate-json-key","index":j}}
```

The source slice includes the row's original whitespace and escapes. `raw_json` is a string, not reparsed data. At `/workflows[i]/publication_receipts[j]`, preserve only that row as an invalid-JSON-row wrapper with `diagnostic.index == j`; valid sibling workflows still load. Duplicate object member names are compared after JSON string escape decoding (for example, `"schema"` and `"sch\\u0065ma"` are duplicates). The full-registry vector below includes a loadable v2 registry with two complete WorkflowRun objects, one duplicate-key receipt row, and an unrelated workflow that must still load. Any duplicate key outside a receipt row, including a decoded duplicate registry-root name, rejects the entire registry load. The raw-aware registry decoder must identify the row before constructing its mapping; a post-`json.loads` value parser cannot meet this rule. This load behavior is accepted by #993; implementation remains in #993, not this schema-only contract.
- A non-list container is represented by exactly:

```json
{"kind":"cortex-publication-receipts-invalid-container/v1","raw":<original JSON value>,"diagnostic":{"code":"container-not-list"}}
```

Keep this object in the `publication_receipts` field across `to_dict`/`from_dict` and reload. Never coerce it to `[]`. Unknown wrapper kind/version is itself an opaque invalid value.
- `from_dict` recognizes the exact v1 invalid-row, invalid-JSON-row, and invalid-container wrapper shapes emitted by `to_dict`; a subsequent `to_dict` must reproduce the same wrapper, raw nested value/source text, diagnostic code, and index byte-for-value. It must not wrap a known wrapper again, discard its diagnostic, or reinterpret it as a receipt. An unrecognized wrapper kind/version is retained as opaque invalid data and is never valid provenance.
- Serialization preserves invalid row/container diagnostics and defensive copies. It may materialize a missing legacy field as `[]` when the run is next written; a missing field and an explicit `[]` both round-trip as empty history. Unknown or malformed history remains readable but is never valid provenance. Any invalid row, wrapper, or container that could make replay/conflict detection ambiguous causes append to fail closed without writing.
- #993 append is fail-closed if any workflow receipt history in the registry contains an invalid/opaque row, wrapper, or container. Producer-side validation resolves sidecars before invoking the private append; registry reload itself does not resolve them. The append API receives a nonempty ordered batch from one event and producer kind; it validates the whole batch before mutation and persists either all-new rows once or an exact full-batch replay with identical patch at the caller's current expected revision. A stale expected revision conflicts before replay recognition, even for an otherwise exact full-batch replay. Partial replay/conflict writes nothing; same-event distinct-output publication IDs are allowed per #993's live amendment.

### 4.2 Strict JSON boundary

The raw decoder rejects duplicate keys after decoded-name comparison for envelope and strict nested values. For duplicates inside `workflows[i].publication_receipts[j]`, #993's load boundary isolates and preserves that row as described above. Duplicate keys elsewhere in the registry fail the full registry load. `WorkflowRun.from_dict` alone cannot detect duplicate keys or resolve sidecars.

### 4.3 Object-conflict keys

The contract uses these exact keys when append checks whether a different event already claimed the same published object:

- Planning workspace object key: `(run_id, claim_key, producer_kind, canonical_workspace_ref)`, where `producer_kind` is the exact receipt union tag (`brainstorm_artifact` or `plan_materialization`) and `canonical_workspace_ref` is the exact canonical workspace-relative `published_object.ref`. This deliberately omits `repo`, `work_id`, and output kind because a run/claim/producer owns its own workspace namespace; producer variants remain separate. Same ref in a different run, a different claim era, or a different producer variant is allowed by this key. #979 withdrew its earlier broader key after reconciling with #993's issue text.
- PR resource key: `(canonical_repository, positive_number)`, where the repository is bound to the canonical intent/POST target and the structured POST response, and the positive number comes from the POST witness. The later authenticated GET must confirm this tuple; it is not the source of creator identity and REST `id`/`node_id` are not the resource key. This key is checked across valid receipt history in the registry.

An exact replay of the same publication remains governed by the receipt identity/full-batch replay rules. A distinct valid publication ID for the same object key conflicts before persistence. #993 confirms both the planning key and PR resource key; #979 confirms the narrow planning key. Vectors cover same-run/claim/producer/ref conflict, a different-run positive, a same-run/different-claim-era positive after authority restart, same-event distinct outputs, full-batch replay, partial replay, and a cross-history PR resource conflict whose run, claim, event, and publication all differ while canonical repository and positive number remain equal.

## 5. Canonical vectors and negative vectors

`golden-vectors.json` contains complete synthetic instances of all three variants, each exact snapshot/evidence/intent fixture needed by the contract, canonical `J` bytes for every hash input, and independently computed expected values for:

- raw snapshot/evidence/artifact/intent SHA256;
- structured acceptance facts, event, publication, metadata, and receipt IDs;
- PR marker and rendered body;
- generic `J/H` vectors, including literal empty-object input `{}` with canonical bytes `{}` and `H("test/v1", {}) = 24b225963d2bc670202bd0dde4768073c86ccdd932e51789a2a2ccc7d91b283f`.

The brainstorm positive is reachable under current `planning.py` deterministic defaults and has two outputs for its single event: one spec receipt and one design receipt, each with a distinct operation-derived publication ID. It is not a fabricated accepted question pack. Producer eligibility vectors include both a new brainstorm output and an overwrite of an existing authority-owned artifact; they reject no-write same-byte operations, duplicate/missing/mismatched evidence rows, wrong evidence scope, and wrong path domain. The plan vectors accept only the new-target operation and reject an existing same-byte target. The append vectors include the full brainstorm all-new batch, exact full-batch replay, stale-revision replay conflict, partial replay, duplicate pair in a batch, mixed event, same pair with a different internally valid PR witness/read-back payload, publication-ID reuse across events, and a repeated PR resource key under a different publication ID. Planning key vectors cover same-run/claim/producer/ref conflict, a different-run positive, a same-run/different-claim-era positive after authority restart, and a different producer variant. Each append scenario carries literal history, incoming batch/key values, coupled patch equality token where relevant, and expected persist count/result. The patch token is test-harness equality data, not a proposed #1029 wire member.

Negative value vectors include full receipt objects for unknown schema/kind, missing and extra keys, wrong union type, uppercase/short digests, cross-variant transaction kind, event/publication formula mismatch, published-object/intent mismatch, intent ref/hash mismatch between PR `acceptance_evidence` and `transaction`, bool-as-integer, invalid diagnostic indexes, and the confirmed deterministic multi-defect precedence. Raw boundary vectors reject duplicate keys after decoded member-name comparison, NaN, both infinities, and unpaired surrogates. Registry raw-document vectors include a complete loadable v2 document where a duplicate receipt row at `/workflows/0/publication_receipts/1` is preserved as exact `raw_json` with index `1` while a second workflow loads, and a full-registry decoded duplicate root key that fails load. Round-trip vectors give exact invalid-row, invalid-container, missing-field, and unknown-wrapper behavior. Append must not write when any loaded history is opaque or invalid; stale expected revision conflicts even with an exact replay. PR read-back vectors use literal JSON-decoded raw GET `body` Unicode strings: they independently mismatch each POST tuple field (`repository`, `number`, REST `id`, `node_id`) against the observation; accept CRLF/CR bodies after line-ending normalization; reject a wrong publication marker, duplicate exact marker lines, malformed marker-like lines (leading/trailing whitespace, altered case, and wrong version), and a body that differs from the exact rendered marker-bearing body after permitted line-ending normalization. The published metadata digest is only recorded after all GET equality checks. The normalization vector records NFC title/labels, post-NFC label deduplication and sorting, and line-ending normalization. Sidecar vectors check exact-byte idempotent replay and reject different bytes at an occupied immutable ref for each producer. PR ordering vectors include an exact full v2 registry file byte fixture and SHA-256 captured before the first metadata write, the identical held value passed to #993 append, a stale-file revision conflict without rebase, a changed verified-binding rejection before metadata write, and an explicitly separate #983 journal revision domain.

One `#993` negative case is deliberately pending: a pair of distinct valid receipt preimages with one SHA-256 `receipt_id` cannot be produced as a golden vector without an actual cryptographic collision. The owner requirement remains explicit: detect the duplicate ID and reject before mutation. No forged digest is presented as a valid receipt. The complete current vector inventory and this pending item are recorded in the JSON's `pending_vectors` array and `decision-log.md`.

These vectors are contract fixtures, not evidence that a production producer currently implements this contract.

## 6. Owner acceptance gate and scope

This contract fixes the exact nested key/type/path/digest choices aligned by #992/#993/#994/#979/#980. The remaining executable-vector gap is one unconstructible cryptographic-collision case. #979's final narrow planning key is reflected after its withdrawal of the earlier broader key. The full-registry duplicate-row vector is a contract fixture, not evidence that production implements row isolation. No production module, registry, or service behavior is changed by this docs-only contract.
