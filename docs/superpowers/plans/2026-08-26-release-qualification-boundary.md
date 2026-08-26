# Release qualification boundary correction plan

Authority: `openspec/changes/release-qualification-boundary/`.

## Boundary

- Worktree: `$HOME/prj_pri/paulsha-cortex-worktrees/release-qualification-boundary`
- Branch: `feature/release-qualification-boundary`
- Base: `origin/main` at `b46417b96d0b8d903c622cdf6b207335588795ce`
- Scope: qualification workflows, harness/evidence validation, focused tests, OpenSpec and release docs only.
- Explicitly excluded: uploading operator credentials, changing a probe repository, production install, PyPI publish.

## Ordered execution

1. Record the contradictory SHA/external-write contract in OpenSpec and validate it strictly.
2. Add RED workflow tests for a secret-free, external-write-free release profile and an isolated live canary.
3. Add RED validator/schema tests proving profile separation and cross-profile rejection.
4. Implement schema v2 and profile-aware validator/redaction behavior.
5. Implement runner/driver branching while preserving all shared installer and attack-matrix gates.
6. Make `rc-qualification.yml` deterministic; add `deployment-canary.yml`; bind release to release-profile evidence.
7. Run focused tests, full pytest, build/twine/clean-wheel smoke, OpenSpec strict checks and PR-context preflight.
8. Independently review each finding as fix/rebut/accepted-boundary, archive the OpenSpec change and re-run gates.
9. Commit with a conventional message, push, open a zh-TW PR, obtain independent final-head approval and merge.
10. Run exact-main deterministic qualification, dispatch `v0.1.9` release and verify tag target, non-draft release and wheel asset.

## Release proof required

- The deterministic qualification run succeeds on the exact merged main SHA without environment secrets.
- Its schema-v2 evidence says `profile=release` and passes exact wheel/bundle validation.
- Main Tests and final-head approval/checks are green for the merge commit's unique PR.
- `v0.1.9` is an annotated tag at that exact SHA and the GitHub Release is published with exactly the rebuilt wheel.
