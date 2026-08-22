---
dispatch: auto
slice_id: phase2-copilot-review-verdict-spool
plan: docs/superpowers/plans/fix-copilot-review-verdict-spool.md
target_branch: feature/phase2-verification-hash-fix
executor: codex
model_id: gpt-5.6-luna
repo: hamanpaul/paulsha-cortex
verification:
  docs_class: code
  required_artifacts:
    - path: paulsha_cortex/coordinator/launcher.py
      must_change: true
    - path: tests/test_coordinator_launcher.py
      must_change: true
  checks:
    - kind: persona-scope
    - kind: command
      name: policy
      argv: [python3, -m, policy_check, --repo, .]
      cwd: .
      timeout_seconds: 180
  tests:
    - argv: [python3, -m, unittest, tests.test_coordinator_launcher.ArgvTests.test_copilot_verdict_spool_grants_exact_file_and_read_only_checks, tests.test_coordinator_launcher.ArgvTests.test_copilot_verdict_spool_rejects_broad_permission_modes, tests.test_coordinator_launcher.ArgvTests.test_copilot_argv, tests.test_coordinator_launcher.ArgvTests.test_copilot_builder_commit_required_scopes_tool_and_git_write_dirs, tests.test_coordinator_launcher.ArgvTests.test_copilot_builder_commit_required_false_preserves_existing_argv]
      cwd: .
      timeout_seconds: 300
  full_suite:
    argv: [python3, -m, policy_check, --repo, .]
    cwd: .
    timeout_seconds: 600
    baseline: no-regression
---
