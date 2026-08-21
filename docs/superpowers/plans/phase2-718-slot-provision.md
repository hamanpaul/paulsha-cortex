---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - pre-start-surface-provisioning
  - copilot-runtime-toolchain
  - phase-3-signing
---

# #718 exact template log identity

Candidate `fa3b3f6468d179f63eaae68a7032c47d24d9302c` uses the unsuffixed slice id
for the builder/reviewer job-log directory, while the template unit mounts the
systemd-safe `template_plan.instance` produced by `job_segment(slice_id)`.
Long ids therefore fail at `226/NAMESPACE`.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/coordinator/job_workspace.py`
- `paulsha_cortex/coordinator/launcher.py`
- `tests/test_job_log_spool_708.py`
- `tests/test_coordinator_launcher.py` only if the launcher contract needs it

Repair exactly:

1. The template job's canonical log directory and spec `log_path` must use the
   same systemd-safe instance as unit `%i`; do not try to reverse the hash.
2. The Manager-only dispatch anchor remains the explicit unsuffixed
   `manager_log_path` / `LaunchHandle.control_log_path`. Exit sentinels and gate
   ledgers must consume that explicit field. Direct/legacy jobs retain their
   existing sibling path behavior.
3. Preserve the no-cross-mount-hard-link design and all current ownership/ACL
   checks. Do not change writable roots or provision unrelated surfaces here.

Add a long-id test that compares `template_plan.instance`, the job log parent,
unit `%i`, spec `instance`, and the separate explicit control anchor. Run the
focused job-log and launcher tests, commit all changes including this plan, and
leave the worktree clean.
