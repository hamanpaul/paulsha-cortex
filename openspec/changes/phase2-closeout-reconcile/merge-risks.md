# Governed merge risks and rollback

## Rejected source risks

- **#789 behavioral/atomicity conflict:** its temporary tree is not guaranteed to share the
  destination filesystem, and rename into an existing non-empty tree is not idempotent. Importing
  it would bypass the current plan/receipt transaction.
- **#790 authority conflict:** a `permgen` inventory would duplicate the install plan's canonical
  generated inventory and permit disagreement between install and verify.
- **#791 interface/evidence conflict:** a direct standalone probe is weaker than full intake-to-terminal
  dispatch and could pass without proving production lifecycle behavior.

## Selected-change risks

- **Over-normalization:** ignoring too much polkit text could hide functional drift. Mitigation:
  JavaScript comments are recognized only for the `polkit` category; code remaining after a leading
  block comment remains functional; regression coverage includes a changed inline rule.
- **Shebang false negative:** treating all `#` lines as comments hid executable interpreter drift.
  Mitigation: `#!` remains functional only for executable inventory categories; other categories
  keep their prior semantics.
- **Live-status ambiguity:** a package release could be mistaken for provider health. Mitigation:
  #716 remains open and explicitly requires a protected same-SHA canary; release never consumes
  canary inputs or evidence.
- **Legacy reference CLI:** `permgen.build_toolchain_plan()` remains as a compatibility/reference
  surface but is not the install authority. Any removal or deprecation is a separate change; this
  closeout does not strengthen it or route production install through it.

## Rollback

Before merge, discard only the isolated feature worktree. After merge, revert the closeout merge
commit; do not rewrite `v0.1.9` or any later tag. A production install, if separately authorized,
must use `cortex install trust-root rollback --receipt <exact receipt>` so only receipt-owned,
unchanged state is restored; this closeout itself performs no production installation.
