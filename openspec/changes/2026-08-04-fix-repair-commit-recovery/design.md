---
status: accepted
work_item: fix-repair-commit-recovery
---

# fix-repair-commit-recovery Design

## Decisions

- 新增獨立窄化 `recover-repair-commit` work action（模板：`_recover_planning_action`），
  不放寬 `retry-build` 的 exact `expected_candidate` CAS 與既有 exited/0 unbound terminal
  窄化入口。
- 判準全部取自系統可驗證事實：worktree 路徑取自 failed builder job row；operator 的
  `expected_run_id`＋`expected_candidate` 雙 CAS 只做交叉比對（HEAD 精確相等、worktree
  乾淨、descendant lineage、authority 授權），任一不符 fail closed。
- adoption 以 manager 登錄的 adoption job row 承載觀測事實（identity 複製自 failed job、
  `subject_head`=adopted SHA、exited/0、`workflow_evidence` 指向
  `cortex-work-repair-adoption/v1` record），不新增任何 run／job row 欄位；failed job row
  原樣保留。
- registry 新增原子 `_manager_adopt_repair_candidate`（單一 persist 完成 candidate bind、
  final build step 標 passed、verify phase 前進），adoption 不授予品質判定，verify →
  foreign review → exact-head final 對 adopted candidate 完整重新把關。
- `resume`／`_dispatch_workflow_card` 的 stale failed job 判定補「exited 且 exit code 非 0」
  分支；exited/0 的既有三條路徑不動；失敗回報附掛唯讀 terminal 診斷。

詳細 D1–D5 與風險緩解見 `docs/superpowers/specs/fix-repair-commit-recovery-design.md`。
