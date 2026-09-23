---
status: accepted
work_item: pr-create-adopt-provenance
issue: 982
parent: 963
---
# GitHub PR Create/Adopt Provenance API Design (#982)

## Decisions

### D1 — Add safe outcomes without breaking the current caller

Add `create_or_get_pull_request_with_outcome()` to `github_delivery.py` with `created`, `adopted`, and `ambiguous` results. Preserve the existing integer-returning `create_or_get_pull_request()` for the current Manager caller until #980 migrates that call site. The compatibility wrapper is not provenance and #982 does not edit `work_bridge.py`.

### D2 — Let #992/#994 own canonical identity and read-back

Use #992's frozen `manager_pull_request` union and #994's exact marker/request rendering. The client includes Manager's exact marker-bearing body in the initial POST and does not calculate a second request digest or publication ID. Marker-free inputs containing the casefold sentinel are rejected by the shared contract. The exact separator LF and CRLF/CR conversion rules are followed byte-for-byte.

### D3 — Distinguish successful POST from adoption

Return `created` only for a completed successful POST with exact repository, positive PR number, positive REST ID, and nonempty node ID. Preserve the tuple unaltered so #980 can persist it through #983 before #994 GET. Return `adopted` for any PR found before this operation has a confirmed create response; marker/body/head equality never changes that outcome.

### D4 — Never retrofit marker or infer lost response

The structured API does not invoke metadata synchronization on either its `created` or `adopted` path before returning the POST outcome. A separately invoked `ensure_pr_metadata()` after #980's valid-receipt/provenance guard preserves an exact marker and cannot add, replace, or remove it. Timeout/lost response or incomplete result stays `ambiguous`, even if a diagnostic lookup sees the expected marker and exact object. Never auto-create a replacement PR. #994 is read-only and reports observed facts; it cannot supply missing POST attribution.

### D5 — Respect actual prerequisites and module order

#982 is blocked by #992 and #994, not #978. Complete and merge #994 before serializing #982 because both change `github_delivery.py`. #980 then consumes A/B/C, #982, and #983; #978 remains open as the aggregate. #964/#965/#847 retain consumer, status/canary, and overall acceptance scope.

### D6 — Test in fixtures only

Use fake GitHub/`gh` responses to cover each POST outcome and metadata behavior; never create or change a live PR. Pin the old int API with a transition test. Confirm #982 writes no journal, sidecar, receipt, or registry data and does not run GET read-back.

## Verification boundary

Passing #982 tests proves only client create/adopt classification and marker-preserving behavior. It does not prove #980's durable intent/witness, #994 read-back, #993 append, consumer wiring, installed runtime, or #965/#847 live canary.
