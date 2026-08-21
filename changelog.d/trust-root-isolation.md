# #718 trust-root isolation

- Register builder/reviewer per-job Codex-home and runtime-cache roots as real permission-plan assets; project deployment controls and existing auth into concrete Manager-provisioned slots with own-account ACLs only.

- Repair Codex-capable jobs to use canonical per-job `CODEX_HOME` and cache slots, remove reviewer access to the builder-only event spool, and provision immutable control leaves without truncating refreshable `auth.json` state.

- Added one canonical five-row per-job writable-surface table and projected it into
  slot derivation, rendered writable properties, and probe coverage.
- Added fail-closed job identity and slot-shape validation, with producer paths for
  commit and gate worktree routed through the canonical helper.
- Added `--ignore-user-config` to every Codex exec argv and updated the #716 byte
  identity golden tests.
- Routed commit, review, gate, and event slot paths through the canonical lexical
  slot helper; redirected roots and symlinked parents now fail closed. The scaffold
  command also creates root-owned Codex config/plugins/skills/hooks control inputs.
- Bound every slot basename to the same digest-bearing instance key used by systemd
  `%i`; harvest now selects the slot only from Manager registry `job_id`.
- Bound the real headless-hook call-site and its CLI override to that authoritative
  event slot, while the monitor safely harvests the one-level slot layout.
- Made Codex control-file scaffold installation create-only and content-preserving;
  repeated installs no longer truncate deployed `config.toml` or `hooks.json`.
