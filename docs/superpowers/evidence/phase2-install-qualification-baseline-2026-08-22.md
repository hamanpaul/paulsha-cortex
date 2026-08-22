# Phase 2 install qualification baseline — 2026-08-22

## Repository boundary

- Baseline: `origin/main@1015f6db6686463d35796303db1d55cdbd5740ef` after
  `git pull --ff-only`.
- Integration worktree:
  `/home/paul_chen/prj_pri/paulsha-cortex-worktrees/phase2-install-qualification`.
- Integration branch: `feature/phase2-install-docker-qualification`.
- The primary checkout's pre-existing untracked plans, specs, workstreams and `error.log`
  were inventoried before the fast-forward and left in place. They are not present in or
  modified by this worktree.
- Existing production services, deployment trees and credentials are outside mutation scope.

## Prerequisite commit adjudication

`c35516e` is not a standalone patch: its parent `e550c82` supplies the plan/changelog
context. `98978b6` is the ninth commit in its branch and narrows code first introduced by
`299fb83`; the same chain also contains the required verification contract/evidence hash
split in `345f9fc`. Applying only either tip would be incomplete.

The integration branch therefore replays the two reviewed chains as individual commits,
in their original order, rather than merging either live-closeout branch as a release
source. On the exact `98978b6` tip, the affected focused suite passed:

```text
177 passed, 2 subtests passed in 2.91s
```

After replay on the integration branch, the expanded affected suite passed:

```text
185 passed, 4 subtests passed in 2.82s
```

## Issue adjudication at baseline

All seven issues were still OPEN in the live GitHub state at the baseline check.

| Issue | Baseline decision | Evidence boundary |
|---|---|---|
| `#623` | PARTIAL | Main contains per-job clone/repo/env fixes, but exact-main intake-to-terminal qualification is still absent. |
| `#665` | STILL BLOCKING | `srt` and Manager `openspec` remain enumerated W+X conflicts; real systemd probes are skipped/pending. |
| `#681` | RESOLVED BY MAIN | Copilot package-tree/absolute-system-Node wrapper and negative probes are present on main. |
| `#692` | STILL BLOCKING | Job `HOME` remains optional in the runtime spec and is only injected when present. |
| `#695` | STILL BLOCKING | Main has only manually recorded hash evidence, not generated-vs-installed functional/comment drift attestation. |
| `#716` | PARTIAL | Egress and sandbox source controls exist, but a normal writable Codex turn was quota-rejected and not proven. |
| `#763` | STILL BLOCKING | Generated Manager gitconfig lacks the GitHub credential helper; the deployed root edit is only a workaround. |

This change is expected to close the source gaps for `#695` and the gitconfig portion of
`#763`. `#623`/`#716` require exact-candidate qualification. `#665` and `#692` remain
independent release blockers unless separately resolved and requalified. No OPEN issue is
treated as closed merely because a unit test passes.

