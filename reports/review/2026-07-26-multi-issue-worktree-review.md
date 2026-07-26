---
workflow_run_id: "workflow-e7117d7992303798057d"
workflow_card_id: "code-review"
workflow_job_id: "wf-7b67fee68a-code-review-358"
candidate: "f88243085aa6e68f0af753245151a395b332536e"
---
# Review: multi-issue-worktree

**Candidate:** `f88243085aa6e68f0af753245151a395b332536e`
**Verdict:** Changes requested (blocking regression found)

## What was checked

- Diff of `ee18cbd` (RED tests) and `f882430` (implementation) against `openspec/changes/2026-07-26-multi-issue-worktree/{proposal,design,tasks}.md`.
- `paulsha_cortex/config/paths.py`: `worktree_root_for(repo)` extraction with `worktree_root()` delegating to it — correct, matches D2, no behavior change for the no-arg case.
- `paulsha_cortex/coordinator/manager.py` build phase: removed the `len(issue_numbers) > 1` raise, canonical `primary = min(issue_numbers)` branch naming, and an attempt at a run-scoped `ScriptWorktreeCreator`.
- `tests/test_multi_issue_worktree.py`: new focused suite — passes (4 passed).
- `work_bridge._pr_metadata`: unchanged, already iterated all `run.issue_refs`; the new test correctly locks in `Closes #34` / `Closes #39` ordering.
- CHANGELOG.md / changelog.d / README.md / tasks.md updates: consistent with the shipped code and task checkboxes.
- `git diff --check 399a515 f882430`: clean, no whitespace errors.
- Full `pytest tests/` run, cross-checked against parent commit `399a515` in a writable scratch copy (`git archive` extraction, since both the sandbox checkout and `.git` are read-only) to separate real regressions from sandbox artifacts.

## Blocking finding: build-phase silently discards injected WorktreeCreator seams

The new build-phase logic (`manager.py:5310-5328`) is:

```python
creator = getattr(dispatcher, "_worktree_creator", None)
workspace_root = Path(run.workspace_root)
if creator is None:
    creator = seams.ScriptWorktreeCreator(repo=workspace_root, wt_root=worktree_root_for(workspace_root), base="main")
elif str(Path(getattr(creator, "repo_root", "")).resolve()) != str(workspace_root.resolve()):
    creator = seams.ScriptWorktreeCreator(repo=workspace_root, wt_root=worktree_root_for(workspace_root), base="main")
...
worktree = str(creator.create(builder_branch))
```

The `elif` branch is meant to detect "dispatcher's creator already points at the run's repo, reuse it" vs. "different repo, build a scoped one" (design D2). But it keys this off a `.repo_root` attribute that **does not exist** on the real seam:

- `seams.ScriptWorktreeCreator.__init__` only assigns `self._repo` (private) — it never sets `self.repo_root`.
- The `WorktreeCreator` Protocol (`seams.py:18`) only requires `create(branch, *, base_sha=None)`, not `repo_root`.

So `getattr(creator, "repo_root", "")` returns `""` for the real production creator *and* for essentially every existing test fake in the repo (they only implement `create`). `Path("").resolve()` resolves to the current process's cwd, which almost never equals `run.workspace_root`, so the `elif` branch is taken and the caller-supplied creator is **silently discarded and replaced** with a live `ScriptWorktreeCreator(base="main")` that shells out to real git against `run.workspace_root`.

This breaks any existing dispatch path that relies on an injected `WorktreeCreator` test double returning a canned path without touching git — and it breaks real single-issue builds whenever the target repo's default branch isn't literally named `main` (e.g. freshly `git init`'d repos in tests, or any repo using `master`/another default).

Confirmed as a genuine candidate-caused regression (not a sandbox/environment artifact) by running both commits from a writable scratch copy:

- `tests/test_workflow_production_wiring.py::test_control_queue_manager_executes_heterogeneous_brainstorm_before_plan` — passes on parent `399a515`, fails on candidate `f882430` with `ValueError: git worktree base invalid: fatal: Needed a single revision`.
- `tests/test_work_bridge.py::test_installed_defaults_start_to_ship_handoff_remains_monitor_ongoing` — same pass→fail transition, same error.

Both failures happen because the tests inject a plain `WorktreeCreator`-shaped fake (no `repo_root` attribute) into a real `Dispatcher`, and the new code swaps it for a real `ScriptWorktreeCreator` that then fails to resolve `main^{commit}` in the test's freshly-initialized repo.

The design doc (D2) actually specifies always constructing the run-scoped creator for the build phase, not conditionally reusing the dispatcher's — the conditional-reuse code path adds complexity that doesn't work as intended and is the direct cause of this regression. Recommend simplifying to unconditionally construct `seams.ScriptWorktreeCreator(repo=run.workspace_root, wt_root=worktree_root_for(run.workspace_root), base="main")` for the build phase, matching the design, rather than trying (and failing) to detect "same repo, reuse existing creator."

## Full-suite run reconciliation (for completeness)

Full `pytest tests/` in the read-only sandbox checkout: 31 failed / 1180 passed / 1 error. Reconciled against the same run on parent commit `399a515` (writable scratch copy): 28 failed / 1179 passed (0 errors). The 28 pre-existing failures are unchanged and confirmed environmental:

- `test_doctor.py`, `test_monitor_work_api.py` (3), `test_stage9_project_monitor_service.py` (24): all `PermissionError: [Errno 1] Operation not permitted` from `socket.socket(AF_UNIX, ...)` — this sandbox forbids creating Unix domain sockets; reproduced identically on the parent commit.
- `test_persona_phase3_scope_ci.py::CatalogErrorShadowTests::test_subprocess_with_malformed_yaml_still_exits_zero`: fails only in the read-only sandbox checkout (`OSError: Read-only file system` writing back `personas.yaml` in a `finally` cleanup); passes on the candidate commit when run from a writable scratch copy, so this is a checkout-mount artifact, not a candidate defect.

The two genuinely new failures on top of that pre-existing baseline are exactly the `test_workflow_production_wiring.py` and `test_work_bridge.py` cases documented above.

## Non-blocking notes

- `config/paths.py` `worktree_root_for`/`worktree_root` refactor is clean and correctly preserves `PSC_WORKTREE_ROOT` override semantics.
- `manager.py`'s canonical-primary-issue branch naming (`primary = min(issue_numbers)`) and the removal of the `>1` raise correctly implement D1 and are covered by `test_build_branch_uses_canonical_primary_issue_for_multi_issue_run` / `test_single_issue_build_branch_unchanged`.
- Docs (CHANGELOG.md, changelog.d/multi-issue-worktree.md, README.md, tasks.md) accurately describe the intended behavior and are internally consistent with each other.
