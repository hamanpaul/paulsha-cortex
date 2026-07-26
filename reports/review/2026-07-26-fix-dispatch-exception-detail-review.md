---
workflow_run_id: "workflow-f84a496d45d6cf6bed56"
workflow_card_id: "code-review"
workflow_job_id: "wf-f7f4008f33-code-review-348"
candidate: "d7e3ab24e6fcfb57980ecf1d7658ed4c05300bea"
---
# Review: fix-dispatch-exception-detail

**Candidate:** `d7e3ab24e6fcfb57980ecf1d7658ed4c05300bea`
**Work item:** fix-dispatch-exception-detail (#100)

## Scope verified

Diff stat matches the proposal's declared "What Changes":

- `paulsha_cortex/coordinator/autonomy.py` — `DispatchReadyError` per-slice exception summary + length cap.
- `paulsha_cortex/coordinator/manager.py` — `run_tick` catches `DispatchReadyError` separately, keeps `exc.jobs` in `dispatched`, and appends per-slice `{slice_id, type, message, stage}` dicts to `errors`.
- `paulsha_cortex/coordinator/manager_daemon.py` — `_log_error` now prefixes every emitted line with `contract.utcnow()` (ISO-8601, ISO `isoformat()` with UTC offset). Confirmed this is the *only* stderr/log emission point in the module (all six call sites route through `_log_error`), so "every line" is honestly satisfied.
- `CHANGELOG.md` / `changelog.d/fix-dispatch-exception-detail.md` / `README.md` / `openspec/.../tasks.md` synced per task 1.4.
- New regression file `tests/test_fix_dispatch_exception_detail.py` (RED commit `bee6f4d`, GREEN commit `d7e3ab2`).

No out-of-scope files touched.

## Verification performed (read-only, in `candidate/`)

- `python -m pytest tests/test_fix_dispatch_exception_detail.py -v` → 3 passed.
- `python -m pytest tests/ -k "autonomy or manager"` → 169 passed.
- `python -m pytest tests/test_persona_phase4_fanout_autonomy.py tests/test_coordinator_dependency_ancestry.py tests/test_coordinator_dispatch_discipline_e2e.py` → 45 passed (existing `DispatchReadyError` consumers unaffected).
- `python -m pytest tests/` (full suite) → 1175 passed, 29 failed, 1 error. All 29 failures/1 error spot-checked and confirmed to be sandbox artifacts unrelated to this change: `PermissionError: [Errno 1] Operation not permitted` on `socket.socket(AF_UNIX, ...)` in `test_stage9_project_monitor_service.py`, and `OSError: [Errno 30] Read-only file system` writing to repo files (`persona/personas.yaml`) / pytest cache in `test_persona_phase3_scope_ci.py` and `test_monitor_work_api.py`. These are pre-existing environment limitations of the read-only reviewer sandbox, not regressions from this candidate.
- `python3 -m policy_check --repo .` → `fail: 0` (only a pre-existing R-22 advisory warn about 18 dangling doc references, unrelated to this change).
- `git diff --check bee6f4d d7e3ab2` → clean, no exit status errors.

## Design/spec conformance

- R1 (DispatchReadyError per-slice summary): **partially conformant** — slice id, exception type, and capped message are present as specified, but the implementation also unconditionally appends a `filename=<...>` field not present in the accepted spec/design (see Finding below).
- R2 (tick response `errors` transfer, `jobs` preserved): conformant. `run_tick` now catches `DispatchReadyError` separately, sets `dispatched = list(exc.jobs)`, and builds `errors` entries with `slice_id`/`type`/`message`/`stage` — verified both by reading `manager.py:1738-1750` and by the passing regression test `test_tick_handler_keeps_jobs_and_exposes_per_slice_error_fields`.
- R3 (manager.log ISO-8601 prefix): conformant. `contract.utcnow()` (`datetime.now(timezone.utc).isoformat()`) is prepended to the existing message with unchanged trailing content, preserving grep/parse compatibility as required; confirmed `_log_error` is the sole log-emission point in `manager_daemon.py`.
- R4 (stdlib-only, TDD, `policy_check` 0 fail, no `DispatchReadyError.__init__` signature change): conformant.

## Finding (important)

See structured findings: the `filename=` suffix appended to every per-slice error entry in `autonomy.py`'s `DispatchReadyError.__init__` is not part of the accepted design (`design.md` only specifies a `type: message` summary) and:

1. Produces confusing `filename=None` noise for the common case of non-filesystem exceptions (verified against the existing `ValueError` case in `test_persona_phase4_fanout_autonomy.py::test_dispatch_ready_bad_role_isolated_from_other_slices`).
2. Is appended *after* the 160-char message cap, so an exception with a long `.filename` can still blow past the design's stated flood-avoidance goal (reproduced: 552-char rendered entry for a single slice with a ~320-char filename).
3. The new regression test's assertion meant to cover this (`assert str(exc.filename) in rendered`) is coincidentally satisfied because the test's `FileNotFoundError` is constructed with a single string arg, leaving `.filename` as `None` — so the literal `filename=None` suffix trivially satisfies `"None" in rendered` without actually validating meaningful filename extraction.

Recommend removing the unconditional filename suffix (or only emitting it when non-`None`, counted toward the cap) to align with the accepted design and restore the flood-avoidance guarantee.

## Verdict

Core functional requirements (R2, R3, R4) are implemented correctly and fully tested; the change stays in scope and passes all relevant regression suites plus policy_check. R1 has a real, fixable deviation from the accepted design (unspecified, uncapped `filename=` field) that should be corrected before this is considered fully spec-conformant.
