# Governed merge summary

## Inputs

| Role | Ref | Use |
| --- | --- | --- |
| target | `7ced8df0a24c55c49ee894b3118ea18d2a97b552` | released `v0.1.9` main tree |
| provenance | PR #789 head `ed3a969ecc9ecfbaf324667b703b9f44bed0fea1` | Copilot pinning intent |
| provenance | PR #790 head `71852d59166e0b5573da93bc103885659618a873` | generated-asset attestation intent |
| provenance | PR #791 head `708135dfe6d07a4e28a51a06038134306c21f02a` | agent-loop qualification intent |

The dirty root checkout was excluded. All writes occur on
`feature/phase2-closeout-reconcile` in its isolated worktree.

## Reconciliation

| Work item | Legacy proposal | Current canonical replacement | Decision |
| --- | --- | --- | --- |
| #681 | HOME/PATH-derived package-tree publisher and wrapper | hash-bound native artifact → install plan → locked staging/apply → root-owned direct wrapper → job PATH; exact-main RC attestation | reject legacy transplant; close as superseded by stronger shipped path |
| #695 | second inventory builder in `permgen` | `_generated_inventory()` → `installed_inventory()` → `attest_generated_inventory()` → `verify_receipt()` | reject parallel authority; fix only verified category-normalization defect |
| #716 | standalone launcher/template probe | protected `_full_dispatch()` plus exact run override, typed runtime + Manager job-spec binding, and `worktree-isolation` Codex command observation | reject weaker probe; ship the stronger contract but retain #716 until a live canary succeeds |

## Selected delta

- Preserve `#!` as functional content for `shim` and `toolchain_wrappers` inventory rows.
- Ignore only standalone JavaScript `//` and `/* ... */` comments for `polkit`; preserve
  inline rule content; reject unclosed blocks and semicolon-prefixed rules.
- Pin canary builder identity, bind its Manager-authored launch spec and validate exact-card Codex
  command events without uploading raw commands or outputs.
- Replace stale work-item, Todo and Phase 2b upgrade authority with current installer／RC／canary paths.
- Keep deterministic release and credentialed deployment canary as distinct evidence profiles.

No source branch was merged, cherry-picked or copied wholesale. No credential, production service,
`/opt/cortex` state, old tag or release artifact was modified during reconciliation.
