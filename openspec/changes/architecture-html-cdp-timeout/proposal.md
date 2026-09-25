## Why

On 2026-09-25, two different product pull request heads failed in the same Architecture HTML review step with Archify reporting a 15000 ms Target.getTargets timeout before viewport or capture evidence was produced. Both same-head reruns later passed the Architecture HTML check, but the rerun result does not identify the responsible layer or prove that content passed on the first run.

## What Changes

- Give the pinned Archify engine a structured diagnostic for Target.getTargets timeout and one narrowly bounded retry.
- Update the Cortex Architecture HTML workflow to save allowlisted hosted-runner context and per-attempt logs in its always-uploaded artifact.
- Keep exact HTML, Archify visual, native navigation/interaction, and capture gates required and fail-closed.

## Capabilities

### New Capabilities

- architecture-html-ci-review: Diagnose and recover only a transient Archify Chrome CDP target-discovery timeout while preserving the existing Architecture HTML acceptance checks.

### Modified Capabilities

None.

## Impact

- Upstream dependency: tt-a1i/archify, whose pinned visual-check implementation owns Chrome launch, CDP timeout classification, and browser-process diagnostics. A reviewed immutable upstream commit is required first.
- Local integration: .github/workflows/architecture-html.yml; existing tests/test_architecture_docs.py and tests/test_architecture_phase_dispatch.py; Architecture HTML workflow artifacts.
- No Architecture HTML source/data, #966 registry CAS, or #987 main-probe product logic changes are in scope.
