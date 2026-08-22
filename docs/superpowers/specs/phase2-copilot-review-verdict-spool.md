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
    - argv: [python3, -m, unittest, tests.test_coordinator_launcher, tests.test_review_verdict_channel_p2a]
      cwd: .
      timeout_seconds: 300
  full_suite:
    argv: [python3, -m, policy_check, --repo, .]
    cwd: .
    timeout_seconds: 600
    baseline: no-regression
---
