---
status: accepted
work_item: phase2-closeout-reconcile
---

# Phase 2 closeout reconciliation implementation plan

## Boundary

- Target：`main` commit `7ced8df0a24c55c49ee894b3118ea18d2a97b552`。
- Provenance：PR #789 head `ed3a969e`、#790 head `71852d59`、#791 head `708135df`。
- 只在 `feature/phase2-closeout-reconcile` worktree 寫入；保留 root checkout 的
  dirty/untracked state。
- 不部署 `/opt/cortex`、不讀 credentials、不重開舊 PR、不修改 `v0.1.9`。

## Audit decisions

- #681：舊 publisher 有跨 filesystem rename 與 non-empty reinstall 缺陷；現行
  hash-bound native artifact、transactional installer、root-owned wrapper 與 job PATH 更強，
  因此拒絕移植。
- #695：舊 `permgen.build_attestation_inventory()` 會形成第二套 authority；現行
  `_generated_inventory()`／`installed_inventory()`／`verify_receipt()` 已完整取代。只修正
  audit 發現的 category comment normalization 缺口。
- #716：舊 standalone probe 未走完整 intake-to-terminal，也以 prompt 指定命令；PR #796
  只建立 lifecycle seam，尚未 pin／驗證 Codex command event。本 change 在現行
  deployment-canary 補 exact builder、Manager spec 與 `worktree-isolation` observation；
  成功 live run 仍屬發布後 acceptance，不是 deterministic release gate。

## Execution

1. 以現行 fixture 新增三個 RED：shim shebang drift、toolchain wrapper shebang drift、
   polkit standalone comment-only drift。
2. 在 `install/core.py` 做最小 category-aware normalization，跑 focused 與 trust-root tests。
3. 強化 `_full_dispatch()` 與 validator：exact Codex override、typed runtime、Manager-owned
   job spec、唯一 `worktree-isolation` command event 與 hash-only evidence。
4. 更新三份 Todo、`.cortex/work-items.yaml`、transactional runbook、changelog 與 governed
   merge summary/risk，移除過時 authority，將舊手工 runbook 標成不可執行。
5. 跑完整 pytest、OpenSpec、build/twine/clean-wheel smoke、PR-context preflight、standard
   review 與 adversarial review；每條 finding 獨立修／駁／列管後重審。
6. archive OpenSpec、push PR，完成 exact-head approval、CI、thread audit 與 merge。
7. 將版本升至 `0.1.10`，對 final main 跑 exact-SHA RC，發布 immutable release；依
   shipped evidence 關閉 #681/#695，並把 #716 留作受保護 deployment-canary 驗收。
