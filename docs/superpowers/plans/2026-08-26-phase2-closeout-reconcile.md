---
status: accepted
work_item: phase2-closeout-reconcile
---

# Phase 2 closeout reconciliation implementation plan

## Boundary

- Target：`main` commit `7ced8df0a24c55c49ee894b3118ea18d2a97b552`。
- Sources：PR #789 head `ed3a969e`、#790 head `71852d59`、#791 head
  `708135df`。
- 只在 `feature/phase2-closeout-reconcile` worktree 寫入；保留 root checkout 的
  dirty/untracked state。
- 不部署 `/opt/cortex`、不讀 credentials、不重開舊 PR、不修改 `v0.1.9`。

## Execution

1. 從三個 source RED commits 只取具名 regression files，先在 target 上跑出預期 RED。
2. 以 source final diff 為證據，逐 function 移植 #681；先跑其 focused tests，再與現行
   installer/toolchain regressions 交叉驗證。
3. 逐 function 移植 #695，保留現行 `permgen` registry/RC inventory；驗證 functional
   drift fail、comment-only warn、credential redaction。
4. 移植 #716 probe/CLI/launcher seam；保留現行 #720/#725/#726 sandbox 與 egress 決策，
   驗證 scripted bypass、fallback、SKIP、quota/model mismatch 皆 fail closed。
5. 把三項 final tests、canonical specs、workstream Todo 與 work-item links 收斂成一致 authority；
   產出 governed merge summary/risk/rollback。
6. 跑完整 pytest、OpenSpec、build/twine/clean-wheel smoke、PR-context preflight、standard review
   與 adversarial review；修後重跑。
7. archive OpenSpec、bump patch、push PR，完成 exact-head approval/CI/merge。
8. 對 final main 跑 exact-SHA RC 並發布新 patch；獨立驗證 annotated tag、release metadata
   與 wheel SHA-256，再依 evidence 收斂 issues/Todos/PR comments。
