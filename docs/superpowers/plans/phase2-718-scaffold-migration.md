---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - job-slot-identity
  - executor-toolchain
  - phase-3-signing
---

# #718 scaffold migration live repair

Candidate `6da98641a139439059d5695551580a07751aa2ac` failed three real fresh-install
probes in `CortexLayout.codex_authority_seed_commands()`.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/trust_root/permgen.py`
- `tests/test_trust_root_isolation_718.py`

Repair exactly these defects:

1. The generated command calls `mktemp -d <config>/codex-controls/<role>.tmp.*`
   before the deployment-owned `<config>/codex-controls` parent exists. Create
   that parent with the deployment owner/group and fixed safe mode before
   `mktemp`. Do not make it job-writable.
2. The post-copy sanitizer currently applies `-links +1` to directories. Normal
   non-empty directories therefore fail because directory link counts exceed
   one. Apply the hard-link rejection only to regular files. Preserve rejection
   of symlinks, special files, and copied xattrs.
3. The long `if [ ! -e controls ]; then <validation> && <copy>; fi` returns zero
   when a required input (`config.toml`, `hooks.json`, `plugins/`, or `skills/`)
   is missing. Make every missing/malformed control input produce an actionable
   stderr diagnostic and nonzero exit. Existing valid controls must remain an
   idempotent zero-exit no-op, and successful first install must remain atomic.

Add executable tests that run the generated shell commands in a temporary
layout and prove: valid nested plugin/skill directories succeed; each missing
required input fails; a hard-linked regular file fails; a second valid run is
idempotent. Run policy plus the two focused test files, commit all changes, and
report the SHA and exact test evidence.
