# Headless launcher session

- `SubprocessLauncher` now gives every headless spawn its own session/process group while preserving Claude stdin and the existing runner-specific overrides.
- Recording coverage now exercises all 14 legal executor/runner cells and keeps the unknown `cg` template hardening profile fail-closed; lifecycle docs distinguish POSIX session isolation from cgroup/restart, #824 timeout, and #851 probe work.
