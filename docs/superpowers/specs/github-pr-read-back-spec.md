---
status: accepted
work_item: github-pr-read-back
issue: 994
---

# GitHub PR authenticated read-back 規格（Child C，issue #994）

## Requirements

Child C owns only a new authenticated GET/read-back operation in paulsha_cortex/coordinator/github_delivery.py. It returns observed PR facts for #980 to join with an already durable successful-create witness. The response itself is not a creator assertion or publication receipt.

### Request and response

The method requires an explicit expected witness tuple that came from a successful structured POST and is already durably persisted by #980/#983: expected repository (owner/name), positive non-bool PR number, positive non-bool REST id and non-empty immutable node_id; expected head repository/branch/40-lowercase-hex candidate SHA; expected base repository/branch; expected lowercase 64-hex publication_id and exact metadata fields needed by caller.

It issues authenticated gh api GET for repos/{repo}/pulls/{number}. If labels are part of the expected request metadata, it also issues GET repos/{repo}/issues/{number}. The result is immutable ObservedPublicationPullRequest with returned number/id/node_id, repository, state, head/base facts, marker, and validated metadata digest. It does not include a creator_verified=true flag.

### Exact validation

- Returned PR number/id must be positive integers, not booleans, and exactly match the successful POST witness; node_id must be a non-empty string and exact match.
- Returned base.repo.full_name and head.repo.full_name must exactly equal expected repositories; null/deleted head repo is invalid.
- State must be exactly open; head ref and lower-case 40-hex SHA, base ref all match exact expected tuple.
- Normalize body by mapping CRLF/CR to LF and preserving every other code point; do not apply Unicode normalization to body. Render expected body by appending the marker line to normalized marker-free body: if non-empty and not ending LF, insert exactly one LF; if already ending LF, insert no separator; if empty, insert no separator. Marker line is exactly <!-- cortex-self-publication-intent:v1:<publication_id> -->. Require the normalized response body to equal that rendered body exactly. Also scan every line: exactly one line must match the case-sensitive full marker grammar, and reject every other line whose casefolded text contains cortex-self-publication-intent: (including leading/trailing whitespace, uppercase, malformed hash, extra suffix or duplicate).
- Metadata validation uses normalized title and body from immutable intent. Title is NFC, while body receives only line-ending normalization as defined here and in Child A. Title is Unicode NFC; body line endings map CRLF and CR to LF while every other code point is preserved (body is not Unicode-normalized); expected body uses the exact rendering rule above. Labels are unique Unicode NFC strings, sorted for comparison. Request digest follows Child A's H("cortex-pr-request-metadata/v1", {"title": title, "body": marker_free_body, "labels": sorted_labels}).
- Unknown/missing/wrong-type fields, malformed JSON, repo transfer/foreign repo, state drift, any number/id/node_id/head/base/SHA/title/body/labels mismatch, missing/duplicate/malformed/wrong marker fail closed with a bounded diagnostic.

### Creator-proof boundary

A read-back GET proves only what GitHub returned at read time. It cannot prove who created a PR. #982's create result must contain a successful POST response tuple (repository, number, id, node_id); #980 must persist that witness conditionally via #983 before GET and compare all fields. If the POST response is lost/timeout or the witness is absent after restart, a GET/marker match remains ambiguous. No receipt or adoption is allowed. Existing PR metadata sync may preserve a marker but may never retro-fit one to create provenance.

### Acceptance criteria

- [ ] Positive GET fixture returns exact observed facts after matching the already durable successful POST tuple.
- [ ] Number, id, node_id, requested/base/head repo, state, head branch/SHA, base branch, title/body/labels, and marker each have missing/malformed/mismatch negatives.
- [ ] Exactly one marker line is required; malformed or duplicate marker-like lines fail.
- [ ] Lost/timeout POST or absent durable witness remains ambiguous even when GET body has expected marker and every identity fact matches.
- [ ] Test command log contains GET only; method has no remote mutation behavior.
- [ ] Caller cannot use read-back result alone to create a typed receipt or declare Manager as creator.

## Non-goals

No POST/create/adopt implementation (#982), no durable PR intent or journal (#980/#983), no receipt value/schema or registry append (A/B), no receipt consumer (#964), and no AC10 canary (#965/#847).
