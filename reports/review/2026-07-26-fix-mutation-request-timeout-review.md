---
workflow_run_id: "workflow-3ccae4d22110c39e780b"
workflow_card_id: "code-review"
workflow_job_id: "wf-89388a5e2d-code-review-353"
candidate: "3c0e92c6bf8bbae98966468c7ff0cc293f6b2646"
---
# Review: fix-mutation-request-timeout

**Candidate:** `3c0e92c6bf8bbae98966468c7ff0cc293f6b2646`
**Verdict:** Changes requested (blocking regression found)

## What was checked

- Diff of `580ed7e` (RED tests) and `3c0e92c` (implementation) against `openspec/changes/2026-07-25-fix-mutation-request-timeout/proposal.md` and `tasks.md`.
- `paulsha_cortex/coordinator/cli.py`: new `_REQUEST_TIMEOUTS` table, tiered `_submit_mutation_request` timeout lookup, `EXIT_SUBMITTED_PENDING = 3` pending path with tracking guidance referencing `cortex request show`/`cortex request wait` (both real porcelain subcommands, confirmed present in `paulsha_cortex/porcelain/request.py`).
- `tests/test_fix_mutation_request_timeout.py`: new focused regression suite — passes (3 passed, 6 subtests).
- `policy_check --repo .`: 25 pass / 0 fail / 1 pre-existing advisory warn (R-22 dangling doc refs, unrelated).
- `git diff --check HEAD~2 HEAD`: clean, no whitespace errors.
- Full `pytest tests/` run: 31 failed / 1176 passed / 1 error.

## Blocking finding: regression in existing coordinator CLI tests

Task 1.4 asserts "focused regression 全綠、policy_check --repo . 0 fail、git diff --check 乾淨", but the full suite is not green because of this change:

- `tests/test_coordinator_cli_complete.py::CliCompleteTests::test_complete_subcommand_routes_through_control_plane`
- `tests/test_persona_phase2_coordinator_cli.py::CliTests::test_complete_submits_control_request_only`

Both assert `polled == [(..., 5.0, 0.1)]` for the `complete` request type — the pre-existing fixed default timeout. The candidate's `_REQUEST_TIMEOUTS` maps `complete` to `30.0`, so these tests now fail with `[('req-...', 30.0, 0.1)] != [('req-...', 5.0, 0.1)]`.

Verified this is caused by the candidate, not a pre-existing/environment issue: cloned the repo at parent commit `b443ad0` (pre-feature) into a scratch worktree and both tests pass cleanly there (`9 passed`), while they fail on candidate HEAD.

The remaining 29 failures/1 error in the full run (`test_stage9_project_monitor_service.py`, `test_monitor_work_api.py`, `test_doctor.py::test_monitor_protocol_probe_rejects_transport_only_listener`, `test_persona_phase3_scope_ci.py::...test_subprocess_with_malformed_yaml_still_exits_zero`) are confirmed pre-existing/environmental and unrelated to this change:
- The monitor/stage9/doctor failures are `PermissionError: [Errno 1] Operation not permitted` from `socket.socket(AF_UNIX, ...)` — a sandbox restriction on Unix domain sockets, reproduced identically on the unmodified parent commit in a separate writable clone.
- The `test_persona_phase3_scope_ci` failure is `OSError: [Errno 30] Read-only file system` writing to `paulsha_cortex/persona/personas.yaml` in-place — an artifact of this review checkout being mounted read-only, not a code defect (the same test passes in a writable clone of the same commit).

## Non-blocking notes

- The tracking guidance text (`cortex request show <req_id>` / `cortex request wait <req_id> --timeout ...`) correctly references real porcelain subcommands (`paulsha_cortex/porcelain/request.py`), so the pending-path UX is sound.
- README.md and CHANGELOG.md/changelog.d entries accurately describe the tiered timeouts and match the real `work-action` req_type (not the bare, nonexistent `work` type), which is good — but see the minor finding below about dead table entries.
- Minor: `_REQUEST_TIMEOUTS` contains `'work': 30.0` and `'run': 30.0` entries that are unreachable — `paulsha_cortex/control/contract.py`'s `REQUEST_TYPES` frozenset has no `work` or `run` type, and no `cli.py` branch ever submits those strings (the `work` subcommand submits `work-action`, already separately keyed). Not blocking, but should be cleaned up to avoid confusion.

## Recommendation

Request changes: update the two broken tests to expect the new tiered timeout (or intentionally decide `complete` should keep 5.0s if that was unintended), rerun the **full** test suite (not just the new focused file) to confirm zero regressions, then resubmit.
