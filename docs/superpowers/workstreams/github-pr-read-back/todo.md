---
status: accepted
work_item: github-pr-read-back
issue: 994
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Child C — authenticated PR read-back plan

## Authority and dependencies

- Proposed Child C under #978/#963/#847; issue #994. Accepted github-pr-read-back-spec.md and github-pr-read-back-design.md.
- Sole production module: paulsha_cortex/coordinator/github_delivery.py. Child A freezes envelope/marker schema; Child B owns registry append.
- After Child A/C exist, #982 depends on A+C for create marker/read-back tuple; #980 depends on A+B+C and #982/#983. Neither may block on #978 merge; #978 remains the aggregate that closes last. Live #978 aggregate also retains #862/#966/#968 gates for its registry portion.
- Read-back is not creator proof. #982 must durably report a successful POST response tuple; #980 persists it through #983 before GET; absent/lost response remains ambiguous.
- No repo/GitHub/Cortex changes were made during planning.

## Five-dimensional sizing

Projection using current feature-oneshot; recompute after issue/work registration:

| Dimension | Score | Evidence |
|---|---:|---|
| domain_breadth | 0 | one production module: github_delivery.py |
| state_consistency | 0 | GET-only read-back; no mutation |
| acceptance_surfaces | 2 | gate spine 4 + R-09/R-16/R-19 |
| spec_stability | 0 | accepted triad |
| orchestration | 2 | 11 cards, 11 persona bindings |
| **Total** | **4 / Yellow** | combo projection only |

## Tasks

- [ ] T0 — typed observation: define immutable observed PR facts with exact number/id/node_id, repository/state/head/base/marker and metadata.
- [ ] T1 — authenticated GET: read exact pull endpoint and, when needed, issue labels endpoint via gh api; keep all commands read-only.
- [ ] T2 — identity validation: require GET number/id/node_id to equal successful POST witness; validate exact repos, open state, head 40-hex SHA/branch, base branch.
- [ ] T3 — marker grammar: require exactly one standalone expected marker; reject duplicates, malformed/extra marker-like lines and mismatched publication ID.
- [ ] T4 — metadata normalization: NFC title/labels, LF body, exact marker append, unique/sorted labels and metadata digest.
- [ ] T5 — ambiguity tests: timeout/lost POST or absent durable witness plus perfect GET/marker remains ambiguous; method never returns creator proof or receipt.
- [ ] T6 — no-write tests/docs: assert runner saw GET only for every path; add changelog fragment, Unreleased entry, focused tests and PR-context policy gate.
- [ ] T7 — delivery accounting: record exact tests, review/CI/merge separately; do not claim #980 producer success or #847 AC10.

## Stop condition

If GitHub's successful POST response lacks exact number/id/node_id, or read-back cannot bind it to the GET response, return ambiguous and do not compensate by trusting the marker.
