# Cortex Phase 2 install and Docker release qualification plan

> Historical plan: its step 11 coupled live provider/full-dispatch evidence to package release.
> `2026-08-26-release-qualification-boundary.md` supersedes that release-gate decision;
> this file remains as the implementation record for the Phase 2 installer and attack matrix.

Authority: `openspec/changes/phase2-install-docker-qualification/`.

## Boundaries

- Main integration worktree: sibling worktree `paulsha-cortex-worktrees/phase2-install-qualification`
- Branch: `feature/phase2-install-docker-qualification`
- Production code owner: root agent; subagents may only edit files explicitly assigned to their task.
- Existing deployment, system services, credentials and production repo checkout are out of mutation scope.
- No GitHub Release or PyPI publication until exact-SHA RC evidence satisfies every required conjunct.

## Ordered execution

1. Freeze baseline and independently inspect `c35516e`, `98978b6`, current issue state and main code.
2. Cherry-pick only the two validated prerequisite commits; resolve conflicts as minimal isolated diffs.
3. RED: add public CLI/schema/transaction/credential/attestation/rollback contract tests.
4. GREEN: implement the installer in cohesive trust-root modules and wire `cortex install trust-root`.
5. RED: add Dockerfile/harness/evidence/workflow/release static and unit contract tests.
6. GREEN: implement artifact-only Ubuntu 24.04 systemd qualification and exact-SHA release verifier.
7. Run focused tests after each slice; then full suite, package build, twine, clean-wheel smoke and preflight.
8. Request independent review; verify every finding, fix or rebut with evidence, then re-review after fixes.
9. Run bounded adversarial review with the declared FAIL criteria, archive OpenSpec only after all source gates pass.
10. Commit conventionally and open the prerequisite/integration PR path authorized by the implementation plan.
11. Run manual RC qualification only where the protected environment/provider credentials exist. If unavailable or
    any conjunct fails, stop release and report the exact blocker; do not tag, publish or alter production.

## Agent task boundaries

- `baseline-review`: read-only inspection of commits/issues; no file writes.
- `installer-tests`: tests under `tests/test_trust_root_install_*.py` only.
- `docker-ci`: `.github/workflows/`, `qualification/`, and its dedicated tests only.
- `installer-core`: `paulsha_cortex/trust_root/install/` plus minimal CLI wiring after tests exist.
- Review agents are read-only and cannot approve their own changes.
