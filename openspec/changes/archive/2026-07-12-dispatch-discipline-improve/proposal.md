## Why

cortex目前把agent process成功退出直接視為task完成，即使verification/review失敗，downstream仍可能被釋放；periodic manager同時會自動套用global broker reaper，存在跨project誤殺風險。這些缺口已在Claude與multi-agent co-work中實際造成false-green、共享盲點與人工恢復負擔，應先以現有coordinator骨架補上最小可信完成鏈。

## What Changes

- **BREAKING**：Job terminal `done`改為`exited`，只代表execution成功結束；舊/未知`jobs.json`採clean-start拒載，不提供自動migration。
- 在既有atomic coordinator state新增Slice lifecycle、immutable verification/review evidence與versioned CompletionRecord。
- 以typed-argv deterministic verification驗Candidate、必要產物、task tests、scope與no-regression full suite。
- normative/code task必須由不同independence domain的reviewer Job審查exact Candidate HEAD；informational/trivial可由明確`not-required` policy直接進verified。
- `depends_on`只在verified Candidate已成為configured remote target branch ancestor、CompletionRecord與Slice state一致時滿足；downstream worktree建立前再次驗actual base。
- periodic manager不再自動執行global broker cleanup；operator command預設dry-run，apply須以cwd root縮限並在signal前重新驗process identity。
- bounded fix-loop、resume、batch integration、automatic rollback、remote override、cost routing與journal platform維持deferred。

## Capabilities

### New Capabilities

- `trusted-dispatch-completion`: 將Job execution、Slice delivery、deterministic verification、ForeignReview、CompletionRecord與artifact-aware dependency release串成fail-closed完成鏈。
- `scoped-broker-cleanup`: 將broker cleanup改成operator-driven、cwd-scoped、signal前重新驗證的best-effort安全操作。

### Modified Capabilities

無；本repo尚無既有OpenSpec capability。

## Impact

- Affected code：`paulsha_cortex/coordinator/{registry,dispatcher,autonomy,manager,manager_daemon,launcher,completion,broker_reaper,seams}.py`、broker reaper shell script與coordinator CLI。
- Affected state/API：`jobs.json` schema、Job status vocabulary、spec frontmatter、handoff CompletionRecord、manager status與local operator actions。
- Affected tests/docs：coordinator unit/integration tests、disposable git remote canary、README與`CHANGELOG.md [Unreleased]`。
- Runtime boundary：維持對`paulsha-hippo`零runtime依賴；只支援shareable tier、preserving-commit merge與同target-branch dependency chain。
