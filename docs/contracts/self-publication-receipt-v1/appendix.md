# Proposed appendix: self-publication receipt v1 wire contract

**Status:** draft proposal for owner alignment under issue [#1029](https://github.com/hamanpaul/paulsha-cortex/issues/1029). This document is not an accepted or implemented contract. The accompanying vectors are synthetic and are computed independently in `compute_vectors.py`.

This proposal stays within the #1029 schema boundary. It does not change producer behavior, publication eligibility, registry write ordering, or any issue owner's production scope. The parent envelope and digest rules below are inherited from [#992](https://github.com/hamanpaul/paulsha-cortex/issues/992), not redefined.

## 1. Shared encoding and primitive rules

### 1.1 Canonical JSON and structured hashes

`J(v)` is UTF-8 bytes of exactly:

```python
json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

`H(domain, v)` is lowercase hex SHA-256 of `ASCII(domain + "\n") || J(v)`. Domain strings are ASCII and the separator is exactly one LF byte. Do not append a newline to `J(v)`.

Reject duplicate object keys before conversion to a normal mapping, `NaN`, `Infinity`, `-Infinity`, non-JSON values, unpaired Unicode surrogates, uppercase/non-64-hex digest strings, and booleans where an integer is required. All hashes in wire values are lowercase 64-hex strings. Git commit identities are lowercase 40-hex strings.

### 1.2 Raw-byte hashes versus structured hashes

The following are ordinary `SHA256(bytes)` values, with no domain prefix:

- `sha256` for artifact bytes, peer evidence bytes, accepted-input snapshot bytes, and immutable intent sidecar bytes;
- `pre_publication_authority_sha256`, whose origin is the exact WorkAuthority digest described in #992/#979/#980.

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

`receipt_id = H("cortex-self-publication-receipt/v1", envelope_without_receipt_id)`. `transaction` has exactly `kind`, `intent_ref`, `intent_sha256`; `kind` must equal the producer-specific intent schema literal below. The pure receipt value parser checks shape, primitive types/ranges, digest syntax, `receipt_id`, and cross-bindings whose operands are contained in the receipt itself (including PR witness/read-back tuple equality and the marker derived from `publication_id`). It does not open refs or claim to recompute event/publication IDs whose basis is in sidecars.

### 1.5 Value parsing, sidecar resolution, and reload

- **Pure value parsing:** `WorkflowRun.from_dict` and each receipt `from_dict` validate only serialized receipt values: exact keys/types, digest syntax, `receipt_id`, producer-kind/transaction-kind pairing, local producer-union joins, and any formula whose complete basis is included in the receipt. No filesystem or GitHub access occurs. Separately, after the intent sidecar has been resolved and parsed into an intent value, a pure intent-value validator can recompute `request_metadata_sha256` and the PR `publication_id` from that intent core. The receipt does not contain the intent core, so this PR validation cannot be done from the receipt alone or during `WorkflowRun.from_dict`.
- **Sidecar resolution:** a separate producer/append validation boundary resolves immutable snapshot/evidence/intent refs, hashes their exact stored bytes, parses their exact schemas, passes the parsed values to pure sidecar-value validators, rederives event and publication bases, checks receipt-to-sidecar joins, and verifies the producer-specific operation/evidence rules below. This stage reports missing, changed, or mismatched sidecars. Trusted WorkAuthority origin and current run/claim proof remain #979/#980 producer responsibilities.
- **Typed-valid reload:** a receipt that passes the pure value parser reloads as a typed-valid receipt value even when sidecars are not opened during registry load. “Typed-valid” is not a provenance verdict. Reload neither fetches sidecars nor upgrades the receipt to producer-verified; only the sidecar-resolver/producer boundary can establish that proof. Opaque invalid rows/containers stay opaque and append-conflicting.

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

Normalize the request before computing any ID:

- NFC-normalize `title`;
- in `body`, replace CRLF and lone CR with LF and preserve every other code point; reject any existing marker line or marker-like line containing `cortex-self-publication-intent:` under case-fold comparison;
- NFC-normalize labels, reject empty labels, deduplicate equal normalized labels, then sort the remaining labels by Unicode code-point order.

`request_metadata` has exactly `title`, `body`, `labels`. `request_metadata_sha256 = H("cortex-manager-pr-request-metadata/v1", request_metadata)`. For this proposal the immutable intent retains the normalized request values as well as that digest, allowing the digest and later read-back to be recomputed from durable facts.

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

For authenticated GET read-back, scan the unmodified response `body` before normalization or trimming, splitting only on CRLF, CR, or LF line boundaries. A line is marker-like when its case-folded text contains `cortex-self-publication-intent:`. Exactly one marker-like line must exist, and it must byte-for-codepoint equal the expected marker line. Reject duplicate exact lines and every malformed marker-like line, including wrong version/ID, altered case, surrounding whitespace, or other text on the line. After the marker scan, replace CRLF and lone CR with LF in the entire GET body, then require it to equal the exact rendered marker-bearing body derived above. Thus line-ending style may differ, while all other body content and the marker must match. The GET body is validation input and is not duplicated in `published_object`; the receipt continues to store the metadata digest.

#### Receipt union and read-back

`acceptance_evidence` has exactly:

```json
{
  "intent_ref": "<relative intent ref>",
  "intent_sha256": "<64-lower-hex raw-byte hash>",
  "post_witness": {"repository": "<owner/name>", "number": 7, "id": 7007, "node_id": "<nonempty immutable node ID>"}
}
```

`post_witness` has exactly `repository`, `number`, `id`, `node_id`; `number` and `id` are positive JSON integers (booleans rejected); `node_id` is a nonempty string. This value exists only after a successful structured POST response was durably recorded. Timeout/lost response is ambiguous even when a later GET finds a matching marker.

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

`head` and `base` keys match the corresponding intent objects. State is literal `open`. Repository, number, REST ID, and node ID must each equal the durable POST witness and the GET response; all base/head/marker fields must equal the intent. Normalize GET title and labels under the same NFC/deduplicate/sort rules and compare them with intent `request_metadata`; scan raw GET marker lines, normalize its line endings, then compare the complete body with the exact rendered marker-bearing body as described above. The observed metadata digest must equal `request_metadata_sha256`. The receipt records the digest in `published_object`; it does not duplicate PR body/title/labels there.

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
{"kind":"cortex-publication-receipt-invalid-row/v1","raw":<original JSON value>,"diagnostic":{"code":"<closed diagnostic code>","index":0}}
```

Allowed row diagnostic codes are `unknown-schema`, `unknown-producer-kind`, `malformed-known-v1`, `invalid-digest`, and `cross-binding`. Keep `raw` by defensive deep copy. Preserve its full nested value without conversion to a valid receipt.
- A row containing duplicate JSON object keys cannot be recovered from an already-built Python dict. At the raw registry decoder boundary, retain that row's exact UTF-8 source slice as:

```json
{"kind":"cortex-publication-receipt-invalid-json-row/v1","raw_json":"<exact row source text>","diagnostic":{"code":"duplicate-json-key","index":0}}
```

The source slice includes the row's original whitespace and escapes. `raw_json` is a string, not reparsed data. The duplicate-key vector names the actual registry location `/workflows/0/publication_receipts/0` but is explicitly an isolated raw receipt-row decoder fixture, not a complete registry document or loadable `WorkflowRun`; it claims only row classification, with `history_loads=false`. The raw-aware registry decoder must identify the row before constructing the row mapping; a post-`json.loads` value parser cannot meet this rule. The proposed owner is #993's registry load boundary, with #992 owning only the round-trip typed value.
- A non-list container is represented by exactly:

```json
{"kind":"cortex-publication-receipts-invalid-container/v1","raw":<original JSON value>,"diagnostic":{"code":"container-not-list"}}
```

Keep this object in the `publication_receipts` field across `to_dict`/`from_dict` and reload. Never coerce it to `[]`. Unknown wrapper kind/version is itself an opaque invalid value.
- `from_dict` recognizes the exact v1 invalid-row, invalid-JSON-row, and invalid-container wrapper shapes emitted by `to_dict`; a subsequent `to_dict` must reproduce the same wrapper, raw nested value/source text, diagnostic code, and index byte-for-value. It must not wrap a known wrapper again, discard its diagnostic, or reinterpret it as a receipt. An unrecognized wrapper kind/version is retained as opaque invalid data and is never valid provenance.
- Serialization preserves invalid row/container diagnostics and defensive copies. It may materialize a missing legacy field as `[]` when the run is next written; a missing field and an explicit `[]` both round-trip as empty history. Unknown or malformed history remains readable but is never valid provenance. Any invalid row, wrapper, or container that could make replay/conflict detection ambiguous causes append to fail closed without writing.
- #993 append is fail-closed whenever the container is invalid or any history ambiguity could permit duplicate/conflicting publication. The append API receives a nonempty ordered batch from one event and producer kind; it validates the whole batch before mutation and persists either all-new rows once or an exact full-batch replay with identical patch. Partial replay/conflict writes nothing; same-event distinct-output publication IDs are allowed per #993's live amendment.

### 4.2 Strict JSON boundary

The raw decoder rejects duplicate keys for envelope and strict nested values. For duplicates inside `workflows[i].publication_receipts[j]`, #993's load boundary must isolate and preserve that row as described above. Duplicate keys elsewhere in the registry remain a registry-level malformed-input condition unless #993 adopts a broader per-run recovery format. `WorkflowRun.from_dict` alone cannot detect duplicate keys or resolve sidecars.

### 4.3 Proposed object-conflict keys (owner confirmation pending)

The proposal uses these exact keys when append checks whether a different event already claimed the same published object:

- Planning workspace object key: `(run_id, claim_key, producer_kind, workspace_ref)`, where `producer_kind` is the exact receipt union tag (`brainstorm_artifact` or `plan_materialization`) and `workspace_ref` is the exact workspace-relative `published_object.ref`. This deliberately omits the output kind so two producer rows cannot claim one path under different planning labels.
- PR resource key: `(repository, number)`, using the authenticated GET observation's canonical repository and positive PR number. This is the external GitHub resource identity; it is not the POST REST `id` or `node_id`.

An exact replay of the same publication remains governed by the receipt identity/full-batch replay rules. A distinct valid publication ID for the same proposed object key conflicts before persistence. `same-planning-ref-different-event-conflicts` and `same-pr-object-different-publication-conflicts` carry literal receipts and expected zero-write conflicts. These key choices are proposals for #993/#994 owner concurrence; neither tuple is accepted by this appendix yet.

## 5. Canonical vectors and negative vectors

`golden-vectors.json` contains complete synthetic instances of all three variants, each exact snapshot/evidence/intent fixture needed by the proposal, canonical `J` bytes for every hash input, and independently computed expected values for:

- raw snapshot/evidence/artifact/intent SHA256;
- structured acceptance facts, event, publication, metadata, and receipt IDs;
- PR marker and rendered body;
- generic `J/H` vectors, including literal empty-object input `{}` with canonical bytes `{}` and `H("test/v1", {}) = 24b225963d2bc670202bd0dde4768073c86ccdd932e51789a2a2ccc7d91b283f`.

The brainstorm positive is reachable under current `planning.py` deterministic defaults and has two outputs for its single event: one spec receipt and one design receipt, each with a distinct operation-derived publication ID. It is not a fabricated accepted question pack. Producer eligibility vectors include both a new brainstorm output and an overwrite of an existing authority-owned artifact; they reject no-write same-byte operations, duplicate/missing/mismatched evidence rows, wrong evidence scope, and wrong path domain. The plan vectors accept only the new-target operation and reject an existing same-byte target. The append vectors include the full brainstorm all-new batch, exact full-batch replay, partial replay, duplicate pair in a batch, mixed event, same pair with a different internally valid PR witness/read-back payload, publication-ID reuse across events, and the same proposed PR resource key claimed under a different publication ID. A separate plan vector uses the same workspace ref under a different event to exercise the proposed planning object key. Each append scenario carries literal history, incoming batch, coupled patch equality token, and expected persist count/result. The patch token is test-harness equality data, not a proposed #1029 wire member.

Negative value vectors include full receipt objects for unknown schema/kind, missing and extra keys, wrong union type, uppercase/short digests, cross-variant transaction kind, event/publication formula mismatch, published-object/intent mismatch, and bool-as-integer. Raw boundary vectors reject duplicate keys, NaN, both infinities, and unpaired surrogates. Round-trip vectors give exact invalid-row, invalid-container, missing-field, and unknown-wrapper behavior; the duplicate-key row vector separately tests isolated raw row-decoder classification and explicitly does not claim a successful full registry load. Append must not write when loaded history is opaque or invalid. PR read-back vectors use literal raw GET response bodies: they independently mismatch each POST tuple field (`repository`, `number`, REST `id`, `node_id`) against the observation; accept CRLF/CR bodies after line-ending normalization; reject a wrong publication marker, duplicate exact marker lines, malformed marker-like lines (leading/trailing whitespace, altered case, and wrong version), and a body that differs from the exact rendered marker-bearing body after permitted line-ending normalization. The normalization vector records NFC title/labels, post-NFC label deduplication and sorting, and line-ending normalization.

One `#993` negative case is deliberately pending: a pair of distinct valid receipt preimages with one SHA-256 `receipt_id` cannot be produced as a golden vector without an actual cryptographic collision. The owner requirement remains explicit: detect the duplicate ID and reject before mutation. No forged digest is presented as a valid receipt. The complete current vector inventory and this pending item are recorded in the JSON's `pending_vectors` array and `decision-log.md`.

These vectors are contract fixtures, not evidence that a production producer currently exists or that any owner accepted this proposal.

## 6. Owner acceptance gate and scope

This proposal supplies exact nested key/type/path/digest choices for owner review; it is not accepted. Written alignment is still required from #992, #993, #994, #979, and #980 owners. The raw duplicate-key fixture is decoder-only and does not establish complete registry-load recovery; proposed planning/PR object-conflict keys and one unconstructible cryptographic-collision vector also remain explicitly pending in `decision-log.md` and `golden-vectors.json`. No production module, registry, or service behavior is changed by this draft.
