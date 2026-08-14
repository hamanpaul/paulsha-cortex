# workstream-518-507-527

- **`#518`／`#507`／`#527` workstream 佈線**——新增三份 todo 來源並在 `.cortex/work-items.yaml`
  註冊對應 work item，讓 cortex 可自行受理這三張 issue：
  - `fix-instance-config-isolation`（`#518`）：`PSC_PROJECT_CONFIG_ROOT` 未隨 instance 隔離，
    `hippo` 繼承 `cortex` 的 `workspaces` 而掃描相同的 13 個 repo，per-process 節流閘門互不知情
    致合計速率翻倍——`#506` secondary rate limit 的結構性成因之一。
  - `fix-planning-rollback-destroys-operator-work`（`#507`）：`_restore_operator_tree()` 抹除
    worktree 內除 `.git` 外全部內容再還原 T0 baseline，實測兩度造成不可復原的資料遺失
    （operator 未追蹤檔、以及 cortex 自己前一代的合格 planning artifact）。
  - `fix-build-needs-human-diagnostics`（`#527`）：build 階段無聲掛上 `needs_human`，
    `evidence_refs` 空、`next_actions` 空、`cortex status` 不呈現；與 `#511`／`#514`／`#515`
    同屬「狀態轉換未強制附帶可稽核理由」，為第四次命中。
- 同時補上 `#514`（`fix-brainstorm-revalidation-diagnostics`）的 work item 註冊——PR `#522`
  只佈了 todo.md，registry 條目先前僅存在於 operator 本機未提交狀態，正是 `#507` 抹除風險的
  暴露面之一。
- 純佈線變更：不改動任何執行路徑程式碼。
