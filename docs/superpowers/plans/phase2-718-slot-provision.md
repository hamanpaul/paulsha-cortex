---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - copilot-oauth-home
  - egress-allowlist
  - phase-3-signing
---

# #718 frozen Copilot toolchain and xhigh argv

The live `c4d5a6d` candidate still needs an operator wrapper. Generated
toolchain metadata treats Copilot as a single forwarding shell file, so the
job can fall through to `/usr/bin/copilot` 0.0.330 instead of the operator's
Copilot CLI 1.0.80. Its PATH can also resolve a stale
`/opt/cortex/toolchain/bin/node --jitless` wrapper; Copilot uses WebAssembly and
must run under the JIT profile with the real system Node. The requested builder
shape is model `gpt-5.4`, effort `xhigh`.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/trust_root/permgen.py`
- `paulsha_cortex/coordinator/launcher.py`
- `tests/test_trust_root_executor_toolchain_640.py`
- `tests/test_coordinator_launcher.py`

Repair exactly:

1. Model Copilot as a Node package that must be frozen as one complete package
   tree under the root-owned toolchain. Resolve the operator-selected entrypoint
   to its actual package root (the directory containing that package's
   `package.json`), copy that tree, and generate a root-owned toolchain wrapper
   that invokes the copied local entrypoint. It must never forward or fall back
   to `/usr/bin/copilot` or another PATH-selected Copilot.
2. The generated wrapper must invoke the deployment-validated absolute system
   Node binary directly, not `env node` and not any `toolchain/bin/node`
   shadow. Do not add `--jitless` or `NODE_OPTIONS`; the existing Copilot JIT
   hardening profile is authoritative. Add a probe that proves a minimal
   WebAssembly module works with that same Node execution path.
3. The deployment plan/probes must fail closed if the package root, copied
   entrypoint, absolute Node runtime, version output, or no-fallback invariant
   is missing. Preserve root ownership and read/execute-only job access.
4. Extend Copilot argv with a validated effort (`low|medium|high|xhigh`) and use
   `xhigh` by default. `SubprocessLauncher` must pass an explicit effort through
   to Copilot as it already does for `cg`; model `gpt-5.4` plus default effort
   must produce `--model gpt-5.4 --effort xhigh` exactly once.

Add focused generator/argv tests, run the toolchain and launcher test files,
commit all changes including this plan, and leave the worktree clean.
