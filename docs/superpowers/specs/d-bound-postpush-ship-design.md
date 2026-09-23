---
status: accepted
work_item: d-bound-postpush-ship
---

# Push 後 exact-D delivery 設計（#973 Child C2）

## Decisions

### D1 Consume C1's exact-D decision
C2 requires C1's ready-to-push handoff and rechecks Candidate/source ref at D before any remote mutation. A changed head or stale handoff returns to existing recovery; C2 never chooses a replacement SHA.

### D2 Push exact D and reconcile its receipt
Use the existing delivery journal to persist the push intent/result and verify the remote branch is exactly D after a lost receipt. Any other SHA or uncertain state blocks PR mutation/closeout.

### D3 Existing mapped PR update has its own witness
For an existing authorized mapping, before push write a durable update-intent row in the same delivery journal: run/claim/repo, immutable PR identity, base/head repository and branch, expected prior PR head C, and target D. Then push D and perform authenticated read-back of that exact mapped PR. Verify immutable identity and exact head D before writing the update-result receipt. A lost result may be reconstructed only from the durable intent plus remote branch D and authenticated same-PR read-back D. Missing intent, changed identity, stale C/E head or API ambiguity is needs_human; no search-based rebind. This is C2-owned update evidence; #980/#982 do not provide it.

### D4 New PR creation uses #980/#982 only
Without an authorized durable mapping, use #982 safe create/adopt and #980 same-run successful POST intent/result witness for a new PR. Their receipt proves only the create path. Repository/base/head equality or a matching external PR without that witness is ambiguous; preserve it and do not update or merge.

### D5 Read PR CI only after push and PR identity confirmation
After push and authenticated same-PR exact-D read-back, require PR CI for exact D. Pending/fail/wrong-head states keep existing merge/closeout authority blocked. Do not inspect PR CI before D is pushed and the PR points to D.

### D6 Keep existing lifecycle authority
Green exact-D PR CI is a prerequisite, not merge permission. Reuse current validator and merge/closure path. Extend the existing work_bridge delivery journal narrowly if it cannot hold the typed update intent/result; do not create a new authority or rely on memory-only state.

## Crash matrix

| Crash point | Durable/read-back evidence | Required action |
|---|---|---|
| Push succeeds before local receipt | journal intent plus remote branch exact D | reconcile only D; no other SHA |
| Existing mapped PR update result lost | C2 durable C→D update intent, remote D, authenticated GET of the same mapped PR at D | reconstruct result for same PR |
| Existing PR identity/head differs | intent plus authenticated GET mismatch/ambiguity | needs_human; no rebinding or merge |
| New PR create succeeds before local receipt | #980 same-run successful POST witness and #982 safe read-back | attach same PR; never duplicate |
| Similar external PR exists without witness | repository/base/head only | ambiguous/needs_human; do not adopt/update |
| CI pending/fails or PR head moves | exact current PR head and CI rollup | keep merge/closure blocked; D CI stale after head movement |

## Sizing boundary

Only `work_bridge.py` changes. The existing journal gains/uses the typed C→D update intent/result, and remote branch/PR/CI receipts reconcile across restart, so state=2. With domain=0, acceptance=2, stability=0 and orchestration=2, accepted fix-standard score is **6 / Yellow**.
