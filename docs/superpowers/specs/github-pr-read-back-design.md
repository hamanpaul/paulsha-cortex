---
status: accepted
work_item: github-pr-read-back
issue: 994
---

# GitHub PR read-back 設計（Child C，issue #994）

## Decisions

### D1 — Separate observation from creation proof

The authenticated GET returns exact observed facts only. GitHub's body marker is public correlation data. A successful POST response captured and durably written before GET supplies the creator-operation witness. The two records must agree on repo, number, REST id and node_id. A timeout or crash before persisting that response is ambiguous for automated provenance, even if a later GET matches every field.

### D2 — Use existing authenticated API seam

Implement on GitHubDeliveryClient and use its gh api runner. Pull facts come from exact /pulls/{number} endpoint; labels, if validated, come from /issues/{number}. The new method never calls existing create/metadata-repair code and cannot issue mutation verbs.

### D3 — Freeze marker and metadata digest grammar

The marker is one exact standalone line. Its publication_id is generated from the immutable intent core, whose digest excludes marker and read-back result. Metadata uses NFC title, LF-normalized marker-free body, sorted unique NFC labels. Rendering preserves all body characters and trailing LFs, inserts one LF only when a non-empty body lacks it, then appends the marker line. Response body must equal the rendered body after the same line-ending-only normalization; response/title comparison separately uses NFC for title only. Scan all lines case-insensitively for marker-like sentinel text; only the exact one marker line is accepted. C validates observation but does not generate an intent or decide which PR is adoptable.

### D4 — Preserve aggregate and sibling boundaries

Child C provides the GET contract needed by #982/#980, but does not solve the registry append (#978 Child B) or producer crash window (#980/#983). #847 AC10 remains a loaded-runtime canary after the complete integration.

## Validation matrix

| Scenario | Expected |
|---|---|
| POST witness and GET exact same number/id/node_id | observed facts returned |
| GET number/id/node_id differ from POST response | fail closed |
| exact repository, open state, exact head/base/SHA and marker | accepted observation |
| marker missing/duplicated/malformed/wrong ID | fail closed |
| same PR body marker but no durable POST witness | ambiguous; no observation accepted for producer |
| POST timeout then GET all matching | ambiguous; no receipt |
| closed PR, foreign repo, null head repository, wrong candidate | fail closed |
| read-back invocation | GET-only command arguments, no writes |

## Five-dimensional sizing

Using current feature-oneshot projection: domain_breadth=0 (one production module, github_delivery.py); state_consistency=0 (read-only observation); acceptance_surfaces=2 (gate spine 4 + applicable R-09/R-16/R-19); spec_stability=0 (accepted triad); orchestration=2 (11 cards and bindings); total 4/Yellow. Recompute after actual issue/work registration.
