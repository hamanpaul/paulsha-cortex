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

This repair card reran the five D4 parser rows against the local `agy` binary.
The earlier table only restated the expected outcomes; the rows below now bind
durable, byte-measured transcripts for the actual invocations.

- `command -v agy` resolved the local binary path recorded here as `$AGY_BIN`.
- The command spellings below use `$AGY_BIN` for that resolved binary path so
  this repository does not commit a personal absolute path; the transcript
  bodies keep the actual parser stdout/stderr and recorded exit codes.
- `agy --version` ran as `$AGY_BIN --version`, returned exit `0`, and printed
  `1.2.3`.
- Every parser check ran with no credential env, no prompt, and stdin EOF
  under `env -i HOME=/dev/null XDG_CONFIG_HOME=/dev/null PATH=/usr/bin:/bin`.
- Each transcript contains the complete merged stdout/stderr stream for that
  invocation plus a trailing `[exit code: N]` line.

| Value | Exact argv | Exit | First parser result | Durable transcript |
| --- | --- | ---: | --- | --- |
| `abc` | `["$AGY_BIN", "--print-timeout", "abc", "--cortex-timeout-parser-sentinel"]` | 2 | `time: invalid duration` | `docs/evidence/agy-print-timeout-only-logs/abc.txt` — 3000 bytes, SHA256 `6da04794988e903d672c9a1324495e39a7060ed64038845fc6874deaf3785a64` |
| `2400` | `["$AGY_BIN", "--print-timeout", "2400", "--cortex-timeout-parser-sentinel"]` | 2 | `time: missing unit in duration` | `docs/evidence/agy-print-timeout-only-logs/2400.txt` — 3011 bytes, SHA256 `ac635c0d8cdd537363ea364e9c64e6eab425a9933497dd7b0ac9512ac3563281` |
| `2400s` | `["$AGY_BIN", "--print-timeout", "2400s", "--cortex-timeout-parser-sentinel"]` | 2 | unknown sentinel flag | `docs/evidence/agy-print-timeout-only-logs/2400s.txt` — 2992 bytes, SHA256 `66f3e77331f2bac3efbf4b5e56813eee87cc4773a763eeb640486f1bc7113be7` |
| `9223372036s` | `["$AGY_BIN", "--print-timeout", "9223372036s", "--cortex-timeout-parser-sentinel"]` | 2 | unknown sentinel flag | `docs/evidence/agy-print-timeout-only-logs/9223372036s.txt` — 2998 bytes, SHA256 `722b51186d65b5be6c454b5f752c8afed43e815047ba8e969654e027f426384b` |
| `9223372037s` | `["$AGY_BIN", "--print-timeout", "9223372037s", "--cortex-timeout-parser-sentinel"]` | 2 | `time: invalid duration` | `docs/evidence/agy-print-timeout-only-logs/9223372037s.txt` — 3024 bytes, SHA256 `6a36fb4bf8e816f24c9133e3d2ca25fbb0b31ff473f371f791ad69f256533c63` |

## Boundary reminders

- The only production file changed for this card is
  `paulsha_cortex/coordinator/launcher.py`.
- The downstream `#851` containment behavior stays intact: invalid timeout
  env still fails AGY capability probing closed without blocking a ready
  non-AGY primary runtime.

## PR-context policy and preflight

These checks ran on the committed candidate with intended PR metadata:
title `fix(agy): 補齊 print timeout 交付`, base `main`, head
`feature/824-agy-print-timeout-only`, labels empty, body containing
`Closes #824` and a fully checked checklist.

| Check | Command shape | Exit | Observation |
| --- | --- | ---: | --- |
| Preflight | `python3 -m policy_check.preflight --repo . --offline --pr-title ... --pr-body-file ... --pr-labels '' --base main --head feature/824-agy-print-timeout-only` | 0 | engine PASS, policy PASS, OpenSpec PASS, tests PASS |
| Policy check | `python3 -m policy_check --repo . --pr-title ... --pr-body ... --pr-labels '' --pr-base-ref main --pr-head-ref feature/824-agy-print-timeout-only` | 0 | `24 pass, 0 fail, 2 warn` (`R-19` parser reliability advisory, `R-22` 73 pre-existing dangling references) |
