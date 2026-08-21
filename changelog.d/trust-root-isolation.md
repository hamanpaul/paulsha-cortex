# #718 trust-root isolation

- Added one canonical five-row per-job writable-surface table and projected it into
  slot derivation, rendered writable properties, and probe coverage.
- Added fail-closed job identity and slot-shape validation, with producer paths for
  commit and gate worktree routed through the canonical helper.
- Added `--ignore-user-config` to every Codex exec argv and updated the #716 byte
  identity golden tests.
