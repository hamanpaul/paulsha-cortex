---
status: accepted
work_item: retry-build-pinned-main-merge-task
---

# 固定 C/M task/transport aggregate 設計（#973 Child B1）

## Decisions

### D1 Preserve the tuple; do not fetch main from Builder
#972 supplies exact C/M/classification. The Manager source clone owns M after probe; the independent Builder clone may not. B1a creates an attempt-scoped temporary source ref from a stable pre-reset token, B1c names the matching expected Builder private ref and builds the task before reset, and B1b explicitly fetches the object after reset into the independent Builder clone private namespace before launch. This carries the exact object without credentials/network fetch from Builder.

### D2 Keep base C and pinned M separate
Before reset, B1c validates Manager-side tuple/pin and names the deterministic expected Builder private ref; it does not require the per-job ref to exist. After B0 reset and before Builder launch, B1b explicitly imports exact M into that private ref and verifies the object; failure blocks launch and B2 persists a #990 stop from B0 snapshot (the consumed reservation is not refunded). If M is absent from Manager source, stop before reset. If origin/main advances to N, the task still targets M.

### D3 One helper for manual and automatic task construction
B1c is a pure pre-reset action/task helper shared by operator retry-build and B2. It binds exact tuple, expected private-ref name and deterministic B0 reservation key for automatic work. It does not check a clone ref that cannot exist yet. B1b owns the post-reset, pre-launch private-ref verification; manual path keeps #989 authority and does not consume automatic budget.

### D4 Keep proof and adoption with Manager
The prompt/task defines the permitted merge; B3 validates Git commit parents, classification, and tree. B4 supplies quarantine import plus expected-ref CAS. No task contract is proof.

### D5 Aggregate sizing stays Red
This aggregate touches three production modules and cross-clone repository state. Full sizing is 7 / Red. B1a/B1b/B1c are the Yellow implementation tickets; aggregate requirements remain intact.

## Flow

1. A and B1a establish exact post-archive producer provenance and exact-M source pin.
2. B1c constructs exact C/M task and deterministic target ref name before B0 resets the final card.
3. After reset, B1b provisions the Builder clone, explicitly imports/verifies M while C remains HEAD/base, then binds actual job id in the receipt and allows launch.
4. Builder authors D; B3/B4 independently prove and adopt it.

## Sizing

`manager.py` + `seams.py` + `work_actions.py` give domain_breadth=1; transfer/task/dispatch consistency gives state_consistency=2. With accepted triad and fix-standard mechanics: 1+2+2+0+2 = 7 / Red.
