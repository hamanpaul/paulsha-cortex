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
    - paulsha_cortex/coordinator/launcher.py
    - tests/test_coordinator_launcher.py
  checks:
    - persona-scope
    - command name policy argv [python3,-m,policy_check,--repo,.] timeout 180
  tests:
    - PYTHONPATH=tests python3 -m unittest tests.test_coordinator_launcher tests.test_review_verdict_channel_p2a
  full_suite:
    policy: python3 -m policy_check --repo .
    timeout_seconds: 600
