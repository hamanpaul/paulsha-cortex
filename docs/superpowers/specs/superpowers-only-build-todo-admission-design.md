---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
---

# Superpowers-only Build Todo Admission 設計（#1054）

## Decisions

### D1 — 在 Manager 實際 plan→build 派工邊界 admission

在 `manager._dispatch_workflow_card()` 的第一張 Builder card 路徑加入一個窄的 admission decision。判定必須位於任何 provider selection/preflight side effect、job id reservation、worktree creation、`registry.create_job()` 或 `launcher.launch()` 之前。只有 Manager 回報可用的 admission outcome 才繼續既有 Builder 派工；zero、multiple、snapshot 無效或 snapshot 尚未更新均回傳帶 `DiagnosticReason` 的 stop，且不得建立第一個 Builder side effect。

檢查只負責尚未建立第一個 Builder job 的首次轉換。已有 Candidate／Builder job 的 resume/retry 與其 evidence 世代轉換留給現有 Manager 規則或 #1051 recovery 子票；不因這張票重新計算 claim key、source revisions 或 delivery binding。

### D2 — 重用 WorkAuthority loader 與既有 source scanner

Manager 以 `(run.repo, run.work_id)` 呼叫既有 strict `load_work_authority()` 從 Monitor 的 confirmed snapshot 取得唯一 WorkAuthority，消費其 `mapped_todo_paths`、`source_revisions` 與 snapshot identity。不可直接讀 `.cortex/work-items.yaml` 作通過依據；不可在 Manager 內重建 YAML parser 或建立第二套 path/symlink guard。link 的既存 scanner source 驗證、Monitor correlation 與 source revision 仍由既有責任層執行。

對 authority missing/ambiguous、snapshot refresh error 或 source path 不符合 canonical workstream Todo 條件，轉成可讀且可行動的 fail-closed outcome。有效 source 的 frontmatter `issue` 必須與該 work item 的 issue identity 相符，`work_item` 必須與 authority work_id 相符，並有具體 Tasks；這些欄位由既有 Monitor scanner/canonical parser 定義，測試使用同一 parser，而不是只比對字串 basename。

### D3 — 使用現有 typed diagnostic 合約

分別持久化 `missing-canonical-todo-source` 與 `ambiguous-canonical-todo-source`，使用現有 `DiagnosticReason` 的 `reason`、`detail`、`source`、`context` 和 `next_step_hint`。context 至少包括 run_id、work_id、mapped count 與 `authority_ref`（snapshot hash 加 source revision 摘要）；multiple 診斷須提供讓 operator 選擇移除哪個多餘 path 的具體引用。zero 的提示只走 owner 發布→link existing path→fresh Monitor snapshot→正式 start/intake；multiple 才給精確 `unlink --kind path --ref` 範例。

### D4 — 保留 ship backstop 與分離子票的責任

不修改 `work_actions._ship_action()` 既有要求與 `delivery-needs-human` 行為。此 gate 不建立 Todo，也不處理 #983 類已有 Candidate／PR 的恢復。#810 的 merge 後 checkbox closure、#972/#973 的 main conflict，以及 #911 無 OpenSpec ship lane仍用現有 owners 和各自 acceptance。

### D5 — 真實 Manager/WorkAuthority 邊界測試

使用既有 Manager dispatch seam、strict WorkAuthority loader、Monitor source parser 和 path guard 建立 fixture。以計數器 spy 只觀察 job reservation/creation、worktree creator 和 launcher spawn；對 missing、ambiguous、invalid/stale authority 均需證明計數為零。測試唯一 verified mapping 的正向派工，並證明 link override 寫入不等於 Monitor snapshot 已確認。

## Error and recovery behavior

| admission input | result | side effects | operator action |
|---|---|---|---|
| fresh authority maps 0 Todo paths | `missing-canonical-todo-source` typed stop | Builder job/worktree/agent dispatch = 0 | owner publish canonical issue-backed Todo; link existing path; wait for fresh Monitor snapshot; use formal `start`/`intake` |
| fresh authority maps >1 Todo paths | `ambiguous-canonical-todo-source` typed stop | Builder job/worktree/agent dispatch = 0 | remove only extra path mapping; wait for fresh Monitor snapshot; use formal Manager admission |
| authority missing, invalid, or refresh failed | fail-closed typed stop with snapshot diagnostic | Builder job/worktree/agent dispatch = 0 | repair Monitor/source state and obtain a successful fresh snapshot |
| exactly 1 valid path in fresh authority | admission passes | existing Manager Builder dispatch proceeds | none |

## Projected five-dimension sizing

Scope is only the pre-first-Builder Manager admission and diagnostic contract in #1054. It does not absorb #1051's existing-Candidate recovery.

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 0 | One production responsibility and one production module: Manager dispatch admission. WorkAuthority, Monitor scanner, typed diagnostics, CLI link behavior and ship backstop are consumed through existing contracts. |
| `state_consistency` | 0 | No new durable store, claim era, CAS, source revision writer or delivery transition. The gate reads confirmed WorkAuthority and writes the existing Manager-owned typed stop. |
| `acceptance_surfaces` | 2 | `fix-standard` contributes two core gate-spine entries; R-09/R-16/R-19 are applicable to a code PR, so the sizing signal is above two. |
| `spec_stability` | 0 | Spec, design and Todo are accepted and complete with no blocking markers (`stability-risk-v2`). |
| `orchestration` | 2 | The loaded `fix-standard` combo has nine cards with multiple persona bindings. |
| **Total** | **4 / Yellow** | Official `current_sizing_snapshot()` result is required before this packet is relied on for implementation intake. |

## Implementation boundary

Primary source module is `paulsha_cortex/coordinator/manager.py`; tests exercise the existing WorkAuthority and scanner/path-guard implementation without changing its ownership. If implementation requires edits to a second production module, a new persistent field/store, claim-key or source-revision updates, a new CLI command, or a change to ship semantics, re-evaluate the dimensions and acceptance scope before proceeding. A Red result requires an issue-backed split, not a lowered declaration.
