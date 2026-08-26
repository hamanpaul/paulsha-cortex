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
- #716：舊 standalone probe 未走完整 intake-to-terminal；現行 deployment-canary
  `_full_dispatch()` 更強。它屬發布後 live acceptance，不是 deterministic release gate。

## Execution

1. 以現行 fixture 新增三個 RED：shim shebang drift、toolchain wrapper shebang drift、
   polkit standalone comment-only drift。
2. 在 `install/core.py` 做最小 category-aware normalization，跑 focused 與 trust-root tests。
3. 更新三份 Todo、`.cortex/work-items.yaml`、Phase 2b runbook、changelog 與 governed
   merge summary/risk，移除過時 authority。
4. 跑完整 pytest、OpenSpec、build/twine/clean-wheel smoke、PR-context preflight、standard
   review 與 adversarial review；每條 finding 獨立修／駁／列管後重審。
5. archive OpenSpec、push PR，完成 exact-head approval、CI、thread audit 與 merge。
6. 將版本升至 `0.1.10`，對 final main 跑 exact-SHA RC，發布 immutable release；依
   shipped evidence 關閉 #681/#695，並把 #716 留作受保護 deployment-canary 驗收。
