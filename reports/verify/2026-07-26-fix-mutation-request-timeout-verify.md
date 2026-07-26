---
workflow_run_id: "workflow-3ccae4d22110c39e780b"
workflow_card_id: "verification"
workflow_job_id: "wf-89388a5e2d-verification-352"
candidate: "3c0e92c6bf8bbae98966468c7ff0cc293f6b2646"
---
# Verification: fix-mutation-request-timeout

**Candidate:** `3c0e92c6bf8bbae98966468c7ff0cc293f6b2646`

## Scope reviewed

- `paulsha_cortex/coordinator/cli.py`: new `_REQUEST_TIMEOUTS` table (`fanout`/`tick` 60s, `complete`/`work`/`work-action`/`run` 30s, else `DEFAULT_REQUEST_TIMEOUT_SECONDS` 5s) applied in `_submit_mutation_request`; on timeout, the code now prints the `req_id` plus tracking guidance (`cortex request show <req_id>` / `cortex request wait <req_id> --timeout <n>`) and returns the new `EXIT_SUBMITTED_PENDING` (3) instead of `1`, leaving the success/error-result paths unchanged. This matches proposal.md's goal and design.md's three decisions verbatim (tiered table, pending+req_id+guidance, exit-code differentiation, no `--json` envelope change).
- Confirmed `cortex request show`/`cortex request wait` are real, wired porcelain subcommands reachable via the umbrella `cortex` entrypoint (`paulsha_cortex/cli.py` → `porcelain.request`), so the guidance text is not a dead reference.
- Docs sync: `CHANGELOG.md [Unreleased] > Fixed`, `changelog.d/fix-mutation-request-timeout.md`, and `README.md`'s mutation-timeout callout all consistently describe the new tiered timeouts and Issue #152.
- `openspec/changes/2026-07-25-fix-mutation-request-timeout/tasks.md`: all 4 checkboxes (RED test, implementation, changelog/README sync, regression/policy/diff-check) are checked; checkbox 1.1–1.3 are substantiated by the diff.

## Commands executed (read-only, in `candidate/`)

```
git rev-parse HEAD
git show 3c0e92c -- paulsha_cortex/coordinator/cli.py
python3 -m pytest tests/test_fix_mutation_request_timeout.py -v
python3 -m policy_check --repo .
python3 -m pytest -q
git diff --check HEAD~2..HEAD
python3 -m pytest tests/test_coordinator_cli_complete.py tests/test_persona_phase2_coordinator_cli.py -v
```

## Results

- Focused regression (`tests/test_fix_mutation_request_timeout.py`): **3/3 passed** (6 subtests) — covers tiered timeout selection, unchanged success path, and pending exit-code + tracking-guidance path.
- `policy_check --repo .`: exit 0, `- fail: 0`.
- `git diff --check HEAD~2..HEAD`: clean, no whitespace errors.
- Full suite (`pytest -q`): **31 failed, 1176 passed, 1 error**. 29 of these failures are the same pre-existing `AF_UNIX` socket sandbox-permission failures previously documented in `reports/verify/2026-07-26-fix-dispatch-exception-detail-verify.md` (test_stage9_project_monitor_service.py, test_monitor_work_api.py, test_doctor.py, test_persona_phase3_scope_ci.py) and are unrelated to this diff.
- **Regression introduced by this change (2 tests, confirmed by isolated re-run):**
  - `tests/test_coordinator_cli_complete.py::CliCompleteTests::test_complete_subcommand_routes_through_control_plane`
  - `tests/test_persona_phase2_coordinator_cli.py::CliTests::test_complete_submits_control_request_only`
  Both assert `poll_done_fn` is called with the literal old default timeout `5.0` for `req_type="complete"`. The candidate's `_REQUEST_TIMEOUTS` table now maps `"complete"` to `30.0`, so these two pre-existing assertions fail with a clear diff (`30.0` vs expected `5.0`). These are not sandbox artifacts — they are direct, deterministic consequences of the timeout-table change and were not updated alongside it.

## Conclusion

The implementation is functionally correct and matches proposal.md/design.md intent, and the newly-added focused regression suite, policy_check, and diff-check all pass. However, the change has an unaddressed side effect: it breaks two pre-existing tests that still encode the old hardcoded 5.0s timeout for the `complete` request type. tasks.md's 1.4 claim of "focused regression 全綠" is accurate only for the new test file and does not reflect this full-suite regression. This should be fixed (update the two assertions to the new tiered timeout, or otherwise reconcile) before the work item is considered complete.
