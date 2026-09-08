# AGY probe construction containment — local validation record

This is a pre-archive validation record for `agy-probe-construction-containment`.
It does not claim archive, merge, issue closure, installation, loaded-runtime
verification, or completion of the wider R14 work.

## Provenance boundary

- **Producer:** `wf-68ec7e3df5-subagent-build-212`.
- **Tested code subject:** `8a0132b28dcc89c9dc8c8be270983a6afc5b6dc6`.
- **Preservation source:** the externally retained checkpoint bundle
  `851-interrupt212-checkpoint.x6TLOB/generated-files.tar`, SHA256
  `880506a4863692d24ed1b875aa0b906d4cbc57ecaa0e6de5de13b52249dd2b70`.
  The bundle is outside this repository; the four files below are the durable
  repository-relative copies used by later reviewers.
- The four transcripts were copied byte-for-byte from that bundle. They are
  the producer's merged `stdout`/`stderr` streams and were not cleaned,
  truncated, or rerun by this preservation card. A shareable-content scan at
  preservation found no credential-like material or private absolute-path
  marker in the four transcripts.
- The producer timestamp is the first UTC line emitted into each transcript.
  There is no independent initial timestamp chunk in the retained bundle, so
  the timestamp is recorded as an observation, not as a signature or
  independent time attestation. The source hash proves the retained bytes,
  not the producer identity or execution time.
- Commands below use `$HOME` and `$CANDIDATE_ROOT` substitutions for private
  checkout prefixes. This changes only the documentation spelling of the
  command prefix; the preserved transcript bytes are unchanged.

## Producer observations

| Check | Command recorded from producer run | Observed UTC | Exit | Producer result | Durable transcript |
| --- | --- | --- | ---: | --- | --- |
| Focused regression | `env -u PSC_REPO_ROOT $HOME/prj_pri/paulshaclaw/.venv/bin/python -m pytest tests/test_model_identities.py tests/test_planning_runtime.py tests/test_coordinator_agy_launcher.py tests/test_planning_job_argv_687.py -q 2>&1` | `2026-09-08T07:49:58Z` | 0 | `109 passed, 16 subtests passed in 0.61s` | `docs/evidence/agy-probe-construction-containment-logs/focused-pytest-transcript.txt` — 205 bytes, SHA256 `04b06f44a863ffffa4ddd2e5b6efc15975ca179c15507a4f9fc7f314d6554fb1` |
| Full pytest | `env -u PSC_REPO_ROOT $HOME/prj_pri/paulshaclaw/.venv/bin/python -m pytest -q 2>&1` | `2026-09-08T07:49:59Z` | 0 | `5656 passed, 44 skipped, 173 subtests passed in 190.73s (0:03:10)` | `docs/evidence/agy-probe-construction-containment-logs/full-pytest-transcript.txt` — 6458 bytes, SHA256 `90084987eff8fd1e2de96ec5303ac1ee5fc608ee6f462cb8b34f8fcaa4aacd6e` |
| CLI help smoke | `env -u PSC_REPO_ROOT PYTHONPATH=$CANDIDATE_ROOT $HOME/prj_pri/paulshaclaw/.venv/bin/python -m paulsha_cortex.cli --help 2>&1` with cwd outside the checkout | `2026-09-08T07:49:58Z` | 0 | Non-empty top-level CLI help rendered | `docs/evidence/agy-probe-construction-containment-logs/cli-help-transcript.txt` — 2739 bytes, SHA256 `20a80b3f8219edc2e5b021c5096655e99e55ee29d7c307b9a272fd9d209032da` |
| CLI work help smoke | `env -u PSC_REPO_ROOT PYTHONPATH=$CANDIDATE_ROOT $HOME/prj_pri/paulshaclaw/.venv/bin/python -m paulsha_cortex.cli run work --help 2>&1` with cwd outside the checkout | `2026-09-08T07:49:59Z` | 0 | Non-empty `run work` help rendered | `docs/evidence/agy-probe-construction-containment-logs/cli-run-work-help-transcript.txt` — 3197 bytes, SHA256 `16e6a053b3d80659e3b8c4c0889ad0cc19e7acaf6f1f4971dca80fb4bb313548` |

## Current-card limits and downstream boundary

The current card preserves the producer evidence and runs the authoritative
checks in its own provisioned worktree. It does not relabel the producer's
tests as current-card execution, and it does not rewrite the original
workflow evidence or older reports. The active OpenSpec task ledger remains
pre-archive-only with downstream review, archive, remote CI, merge, closure,
and loaded-runtime actions explicitly pending. Those actions remain Manager or
operator responsibilities.
