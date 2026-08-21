---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - executor-toolchain
  - copilot-oauth-home
  - phase-3-signing
---

# #718 measured Copilot and Agy egress hosts

The live controlled-egress canaries reached the current CLIs and observed hosts
that the candidate's `EXECUTOR_TOOLS.api_hosts` does not declare. The proxy
correctly denied them, so Copilot/Agy cannot complete without a durable typed
allowlist update.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/trust_root/permgen.py`
- `tests/test_trust_root_egress_716.py`

Update only the canonical `api_hosts` rows with these live-observed exact hosts:

- Copilot CLI 1.0.80 authenticated gpt-5.4/xhigh run:
  - `api.individual.githubcopilot.com`
  - `telemetry.individual.githubcopilot.com`
- Agy authenticated Gemini review path:
  - `oauth2.googleapis.com`
  - `daily-cloudcode-pa.googleapis.com`
  - `cloudcode-pa.googleapis.com`
  - `www.googleapis.com`
  - `lh3.googleusercontent.com`

Record concise evidence identifying the 2026-08-21/22 live proxy observation
and mark these exact rows measured. Preserve all existing hosts and the
deterministic deduped derivation through `egress_allowlist()`; do not add a
wildcard, suffix match, second allowlist, new principal, or broad Google/GitHub
domain. Add exact per-executor membership and no-wildcard tests, run
`tests/test_trust_root_egress_716.py`, commit all changes including this plan,
and leave the worktree clean.

## Scope correction after bounded stop

The previous attempt changed `ProxyServerDenyTests`, imported
`SimpleNamespace`, and removed `_Sink` / `_free_port` while chasing a local
socket-environment failure. Those edits are outside this allowlist task. Restore
that entire proxy deny fixture/helper region exactly to `HEAD` behavior and
remove the new `SimpleNamespace` import. Retain only the three new allowlist
tests near `EgressAllowlistTests` plus the exact `EXECUTOR_TOOLS.api_hosts`,
plan, and changelog updates. Do not redesign, mock, skip, or weaken any existing
proxy test. Run the allowlist test class; commit the scoped change and leave the
worktree clean. The Manager will independently run the full egress file.
