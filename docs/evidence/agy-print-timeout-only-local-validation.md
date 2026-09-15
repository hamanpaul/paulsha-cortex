# AGY print timeout — local validation record

This record covers the local `#824` candidate in `feature/824-agy-print-timeout-only`.
It documents offline validation of the launcher-only timeout delivery. It does
not claim remote CI, archive, PR mergeability, issue closure, installation, or
loaded-runtime verification.

## Local gates

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Focused pytest | `python3 -m pytest tests/test_coordinator_agy_launcher.py tests/test_coordinator_launcher.py tests/test_planning_runtime.py tests/test_planning_job_argv_687.py tests/test_model_identities.py tests/test_trust_root_agy_builder_grant_805.py tests/test_agy_reviewer_json_schema_880.py -q` | 0 | `272 passed, 26 subtests passed` |
| Full pytest | `python3 -m pytest tests/ -q` | 0 | `5870 passed, 41 skipped, 183 subtests passed` |
| Pytest gate scope | `python3 -m pytest -q` | 0 | `5870 passed, 41 skipped, 183 subtests passed` |
| OpenSpec specs | `./scripts/openspec validate --specs` | 0 | `27 passed, 0 failed` |
| Diff check | `git diff --check` | 0 | no whitespace or conflict-marker errors |

## CLI help smoke

Both commands ran from a temporary directory outside the checkout with
`PYTHONPATH=$CANDIDATE_ROOT`.

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Top-level help | `python3 -m paulsha_cortex.cli --help` | 0 | rendered non-empty `cortex` usage text |
| `run work` help | `python3 -m paulsha_cortex.cli run work --help` | 0 | rendered non-empty `cortex run work` usage text |

## AGY parser negative controls

- AGY CLI version observed locally: `1.2.3`.
- All parser checks ran with no credential env, no prompt, stdin EOF, and an
  unknown sentinel flag after `--print-timeout`: `env -i HOME=/dev/null
  XDG_CONFIG_HOME=/dev/null PATH=/usr/bin:/bin "$AGY_BIN"
  --print-timeout <value> --cortex-timeout-parser-sentinel </dev/null`.

| Value | Exit | First parser result |
| --- | ---: | --- |
| `abc` | 2 | `time: invalid duration` |
| `2400` | 2 | `time: missing unit in duration` |
| `2400s` | 2 | unknown sentinel flag |
| `9223372036s` | 2 | unknown sentinel flag |
| `9223372037s` | 2 | `time: invalid duration` |

## Boundary reminders

- The only production file changed for this card is
  `paulsha_cortex/coordinator/launcher.py`.
- The downstream `#851` containment behavior stays intact: invalid timeout
  env still fails AGY capability probing closed without blocking a ready
  non-AGY primary runtime.
- PR-context policy/preflight is intentionally run on the committed candidate,
  after the local evidence above is already present in the tree.
