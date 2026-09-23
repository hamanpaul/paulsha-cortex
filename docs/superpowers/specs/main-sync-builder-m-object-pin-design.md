---
status: accepted
work_item: main-sync-builder-m-object-pin
---

# Exact-M source pin 設計（#973 B1a）

## Decisions

### D1 Use Manager source clone as temporary object source
#972 fetch stores M in Manager ship/source clone, not necessarily Builder clone. Before reset, Manager confirms M exists and pins it on a per-attempt advertised `refs/heads/cortex-main-sync-pin/<attempt-token>` ref so B1b can explicit-fetch the object locally. No Builder network/main fetch is involved.

### D2 Derive token before job allocation
Compute the next build-attempt ordinal from persisted run attempts before reset. Derive token from run/C/M/repair kind/canonical paths/target ordinal. Job ID and B0 reservation ID are unavailable at this point and must not participate. Manual and automatic retry use this same token; the pending reset action carries it, and B1b later binds the allocated job ID in its receipt.

### D3 Make the ref exact and idempotent
Use create-only Git ref update; if present, accept only exact M. Do not move feature/main refs. Keep the pin until B1b imports and verifies it; remove only this exact pin after acknowledgement. A refused reset may leave an unreferenced pin but must never authorize dispatch.

### D4 Fail before reset if Manager lacks M
A SHA from #972 is not proof Manager object database still contains M. Resolve M as commit first. If absent, preserve typed context and stop; do not fetch N or silently substitute another object.

## Sequence

1. Validate run/C/M, compute target build-attempt ordinal and deterministic attempt token.
2. Verify exact M exists in Manager source clone.
3. Create/reuse the attempt-scoped source pin to exact M.
4. Build task/ref identity with B1c and persist the attempt token in reset action.
5. After reset, B1b explicitly fetches pin into Builder clone, validates private ref M and binds job id in receipt.
6. After B1b acknowledgment, remove only the exact Manager source pin under value guard.

## Sizing boundary

Manager-only, one namespaced Git source ref. This slice does not import to Builder or build the full task; state=1. Complete accepted artifacts and fix-standard produce **5 / Yellow**.
