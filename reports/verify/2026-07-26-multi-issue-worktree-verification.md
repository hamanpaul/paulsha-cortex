---
workflow_run_id: "workflow-e7117d7992303798057d"
workflow_card_id: "verification"
workflow_job_id: "wf-7b67fee68a-verification-357"
candidate: "f88243085aa6e68f0af753245151a395b332536e"
---
# Verification: 2026-07-26-multi-issue-worktree

**Candidate:** `f88243085aa6e68f0af753245151a395b332536e`
**Verdict: REJECT — regressions found, needs fix before merge.**

## Scope reviewed

- `openspec/changes/2026-07-26-multi-issue-worktree/{proposal,design,tasks}.md`
- `paulsha_cortex/config/paths.py` (`worktree_root_for` / `worktree_root` delegate)
- `paulsha_cortex/coordinator/manager.py` build-phase dispatch (`_dispatch_workflow_card`)
- `tests/test_multi_issue_worktree.py` (new)
- `CHANGELOG.md`, `README.md`, `changelog.d/multi-issue-worktree.md`

## What works

- `worktree_root_for(repo)` / `worktree_root()` refactor in `config/paths.py` is a clean, correct delegate extraction; no behavior change for existing callers.
- Canonical primary-issue branch naming (`primary = min(issue_numbers)`, `feature/{primary}-{work_id}`) matches design D1 exactly; the `>1` raise is removed as intended.
- `work_bridge._pr_metadata` already iterated all `run.issue_refs` before this change, so multi-issue `Closes #N` behavior is correct (confirmed by the new `test_pr_metadata_closes_all_mapped_issues` test).
- The 4 new tests in `tests/test_multi_issue_worktree.py` all pass:
  - `test_build_branch_uses_canonical_primary_issue_for_multi_issue_run`
  - `test_single_issue_build_branch_unchanged`
  - `test_build_worktree_uses_run_workspace_root_not_manager_repo`
  - `test_pr_metadata_closes_all_mapped_issues`
- Docs (`CHANGELOG.md`, `README.md`, `changelog.d/multi-issue-worktree.md`, `tasks.md`) are consistent with the shipped behavior.

## Regression found

Design D2 says the build phase should *unconditionally* construct a run-scoped `ScriptWorktreeCreator` instead of reusing `dispatcher._worktree_creator`. The actual implementation in `paulsha_cortex/coordinator/manager.py:5312-5330` instead **conditionally** reuses the dispatcher's creator, only reconstructing when it detects a repo mismatch:

```python
creator = getattr(dispatcher, "_worktree_creator", None)
workspace_root = Path(run.workspace_root)
if creator is None:
    creator = seams.ScriptWorktreeCreator(repo=workspace_root, wt_root=worktree_root_for(workspace_root), base="main")
elif str(Path(getattr(creator, "repo_root", "")).resolve()) != str(workspace_root.resolve()):
    creator = seams.ScriptWorktreeCreator(repo=workspace_root, wt_root=worktree_root_for(workspace_root), base="main")
```

The `elif` uses `getattr(creator, "repo_root", "")`. Any injected worktree-creator object (test double, or a production creator) that doesn't expose a `repo_root` attribute equal to `run.workspace_root` is silently discarded and replaced by a brand-new real `seams.ScriptWorktreeCreator` hardcoded to `base="main"`. That real creator shells out to `git rev-parse --verify main^{commit}`, which fails whenever the target repo's default branch isn't literally named `main` (e.g. `master`) or hasn't been fully set up — raising `ValueError: git worktree base invalid: fatal: Needed a single revision`.

This breaks two pre-existing tests that inject a lightweight worktree-creator fake without a `repo_root` attribute (the common pattern used throughout this test suite, e.g. `class WorktreeCreator: def create(self, branch, base_sha=None): ...`):

- `tests/test_work_bridge.py::test_installed_defaults_start_to_ship_handoff_remains_monitor_ongoing`
- `tests/test_workflow_production_wiring.py::test_control_queue_manager_executes_heterogeneous_brainstorm_before_plan`

Both pass cleanly on the pre-fix commit `399a515` and fail on candidate `f882430` — verified by cloning the repo into a writable scratch directory (outside this read-only Candidate checkout) and running the exact same tests against both commits, which rules out this sandbox's own restrictions (see below) as the cause:

```
# at 399a515 (writable clone): 1 passed
# at f882430 (writable clone): 1 failed — ValueError: git worktree base invalid: fatal: Needed a single revision
#   raised from paulsha_cortex/coordinator/seams.py:80 via manager.py:5343 creator.create(builder_branch)
```

The new test suite (`tests/test_multi_issue_worktree.py`) does not catch this because its `_RecordingCreator` fake is constructed with `repo_root=workspace`, which happens to match `run.workspace_root`, so the buggy `elif` branch is never exercised for the "no-op / same-repo" case — leaving the discard-of-injected-creator defect unguarded.

### Suggested fix direction
Follow design D2 literally: always construct the run-scoped `ScriptWorktreeCreator` in the build phase and stop reading `dispatcher._worktree_creator` there at all (it's documented in D3 as intentionally out of scope for build phase). That removes both the `repo_root`-sniffing heuristic and the implicit dependency on test doubles exposing `repo_root`. Alternatively, if reuse-when-same-repo is intentionally desired, the comparison must not default-mismatch on missing `repo_root`, and `base` must not be hardcoded to `"main"` without a documented explicit contract for non-main-default repos.

## Non-issues (sandbox artifacts, excluded from verdict)

The full suite run in the read-only Candidate checkout also shows failures in `tests/test_stage9_project_monitor_service.py`, `tests/test_monitor_work_api.py`, and `tests/test_doctor.py::test_monitor_protocol_probe_rejects_transport_only_listener` — all `PermissionError: [Errno 1] Operation not permitted` on `socket.socket(socket.AF_UNIX, ...)`, unrelated to any file this change touches (monitor module is untouched). `tests/test_persona_phase3_scope_ci.py::test_subprocess_with_malformed_yaml_still_exits_zero` fails only because the Candidate checkout's `paulsha_cortex/persona/personas.yaml` is read-only in this sandbox (the test tries to temporarily overwrite it in place); it passes in a writable clone at the candidate commit. These are environment restrictions of this review sandbox, not defects introduced by the change.

## Commands run

```
git rev-parse HEAD
python -m pytest tests/test_multi_issue_worktree.py -q          # 4 passed
python -m pytest tests/ -q                                       # 31 failed, 1180 passed (see triage above)
# isolated repro in writable TMPDIR clone at 399a515 vs f882430
```
