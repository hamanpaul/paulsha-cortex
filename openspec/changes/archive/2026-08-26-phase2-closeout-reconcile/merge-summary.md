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
- Ignore only category-native comments; preserve shell shebang/semicolon and polkit hash/semicolon
  content, and reject unclosed JavaScript blocks.
- Pin canary builder identity, byte-compare its complete Manager-authored wrapper, require an exact
  bound-worktree Git HEAD proof, and verify Spark/xhigh/provider/cwd from the persisted Codex thread.
- Bind independent canary validation to external repo/work/issue plus unique log/artifact-set hashes.
- Add explicit qualified prior-receipt handoff and route plan/apply through one offline root-owned,
  tree-hash-sealed candidate CLI built from an exact hash-locked wheelhouse in a closed environment;
  pre-sweep every managed filesystem kind, reject marker-only provenance, preserve exact venv-link
  rollback authority, serialize all receipt operations host-globally, hold a full-lifecycle
  maintenance lease, admit lifecycle mutations only with its plan-bound token, allocate and prove
  absent a unique effective receipt, and persist its plan/service pre-state before stop. A dead
  helper leaves only its exact token authorized; a whole-shell crash requires explicit exact-plan
  recovery from an atomically published root-only reviewed plan plus durable root-private snapshot,
  with service restore only after safe rollback; recovery never recreates immutable ingress/venv.
  Venv construction checkpoints planned/building/ready inode/tree authority before final rename,
  and mount adoption carries its original inode authority across metadata-only upgrades.
- Preserve the complete qualification input as RC authority, rerun exact topology/hash/config
  verification in the release gate, and publish its deterministic install-input archive and passed
  qualification manifest beside the exact qualified wheel with REST digest checks for all assets.
- Persist release transaction ownership in the annotated tag so a later run can reconcile only an
  exact-SHA owned stale draft/tag after hard kill while retaining foreign or published releases.
- Keep the first worktree-isolation builder prompt autonomous and have qualification reconstruct and
  byte-compare its canonical prompt/terminal contract rather than trusting fixture-authored text.
- Bind the canary job environment to exact PATH/safe.directory with Git selector variables denied,
  bound every work ID to 128 characters, and publish a bundle digest only when it remains stable
  across Git verification.
- Replace stale work-item, Todo and Phase 2b upgrade authority with current installer／RC／canary paths.
- Keep deterministic release and credentialed deployment canary as distinct evidence profiles.

No source branch was merged, cherry-picked or copied wholesale. No credential, production service,
`/opt/cortex` state, old tag or release artifact was modified during reconciliation.
