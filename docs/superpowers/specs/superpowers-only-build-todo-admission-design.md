---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
---

# Superpowers-only Build Todo Admission 設計（#1054）

## Decisions

### D1 — 把 gate 放在 first Builder 實際派工點

`manager._dispatch_workflow_card()` 的 first Builder 路徑，在 provider selection/preflight、job id reservation、Builder worktree creation、`registry.create_job()` 和 `launcher.launch()` 前做 admission。plan final card 先轉入 build 再走到 dispatch seam 是既有時序；只在 plan-final transition 設 gate 會漏掉 direct resume，故相同 admission helper 也要由 `resume_workflow_run()` 對尚無 Builder job 的 exact run 呼叫。

此 gate 只對每 run 第一個 Builder job 生效。已有 Builder job 的 run 不在此票的重置範圍；已有 Candidate/PR 的正式 recovery 屬 #1055。

### D2 — 將 Todo qualification 和 fresh Monitor authority 當前置契約

Manager 不把現有 scanner/link code 誤當語意驗證。#1063 負責 qualified canonical Todo result 與已存在 safe scanner source 的 link admission；#1064 負責 latest Monitor attempt/generation/input watermark；#1065 負責 strict WorkAuthority reader，且先驗 freshness 再選 row。#1054 的 code only consumes these typed facts and fails closed on missing/unknown fields.

Trusted Monitor result 必須證明 latest attempt successful、snapshot 年齡在明確上限、current `.cortex/work-items.yaml` correlation input revision 與成功 generation 一致、Todo source revision 出自同一 generation。只讀到 old last-good WorkAuthority row 或重算其 payload hash不算 fresh。

### D3 — First Builder gate 內做 exact run/claim reconciliation

從 loader 取得與 `(run.repo, run.work_id)` 精確匹配的 WorkAuthority。計算 `authority_digest_without_planning_outputs(authority)`，其結果必須與持久化 `run.source_revision` 相等；它只排除本 workflow 產生的 `superpowers_spec:` / `superpowers_plan:` revisions，Todo mapping、Todo source revision、issue/PR/openspec authority 仍必須比對。

再用 `claim_key_for_authority_digest(repo=run.repo, work_id=run.work_id, authority_digest=run.source_revision)` 重算並比對 `run.claim_key`，同時確認 registry 中 exact run id 仍是該 WorkAuthority 的 pre-Builder ongoing run。Mismatch 產生 `stale-pre-builder-claim`。不得把 current full digest 的差異默認成無害 plan drift；也不得把 old run 的 claim key／source revision 原位改新。

### D4 — Typed outcome 與可執行修復指引

零 Todo、multiple Todo、authority 無效/不可信、stale pre-Builder claim 各自有穩定 reason、detail、source、context、run/work identity 和 authority ref。zero 說明 owner publish → link existing qualified path → 等 trusted successful generation；multiple 列出 extra mapped paths 和 exact unlink action。Authority revision 改變後，next action 明示不要 resume 舊 run。

若仍需繼續工作，只有符合「無任何 Builder job、無 Candidate、無 PR、無 active job」的未交付舊 run 才能走既有 exact-CAS `cortex work abandon ... --expected-run-id`，然後 formal `start`/`intake` 新 generation。此為 operator 取消/放棄舊 run，非舊 run 成功或 recovery；old claim/evidence 不重寫。Candidate/PR 已存在時指向 #1055，不建議 abandon。Todo owner source 不得被 abandon 的 planning-artifact GC 移除，應以 fixture 驗證。

### D5 — 以真實 dispatch seam 量測 side effect

用 Manager 與真 WorkAuthority/Monitor/source qualification fixture，包裝計數器觀察 Builder job reservation/creation、worktree create 和 launcher dispatch。缺少/不唯一/不合格/stale/untrusted/claim drift 與 direct resume 皆需在三者之前停止；只有一筆 current-generation qualified Todo 且 exact run/claim match 時才抵達正常 Builder 派工。Tests 不連外、不操作正式 state。

### D6 — 將依賴、責任邊界與交付 gates 留在 issue-backed plan

#1054 被 #1063（Todo qualification/path）、#1065（strict WorkAuthority freshness consumer）阻擋；#1065 等 #1064（Monitor generation producer）。#1055 仍 hard-blocked by #1054，且只處理已有 Candidate/PR。保留 #810 closure、#911 OpenSpec=0 lane、#972/#973 conflict scope 和 ship 唯一 Todo backstop。

Implementation 必須更新 lifecycle docs 和 changelog fragment/Unreleased；執行 focused tests、完整 tests、OpenSpec、PR-context policy 和 diff check。planning PR 不 intake/merge、不改產品 code、正式 run、Monitor snapshot、registry、journal 或 PR。

## Admission matrix

| First-Builder input | Outcome | Builder job/worktree/agent |
|---|---|---|
| 0 qualified Todo | `missing-canonical-todo-source` | 0 / 0 / 0 |
| 2+ qualified Todo | `ambiguous-canonical-todo-source`, list paths | 0 / 0 / 0 |
| Last refresh failed, old successful Todo row remains | trusted authority unavailable | 0 / 0 / 0 |
| link override newer than last successful generation, or snapshot stale/unknown | trusted authority unavailable | 0 / 0 / 0 |
| invalid issue/work_item/Tasks/path qualification | source not counted; typed stop | 0 / 0 / 0 |
| Todo/source digest differs from exact pre-Builder run claim | `stale-pre-builder-claim`; no direct resume | 0 / 0 / 0 |
| only workflow spec/plan revisions differ; derived claim still matches | admission continues if one qualified Todo is otherwise current | normal dispatch |
| exactly 1 qualified Todo, same trusted generation, exact claim match | admission passes | existing Builder flow |

## Projected five-dimension sizing

This child is only Manager's first-Builder gate, stale direct-resume stop and typed diagnostic integration. Source semantic validation/path existence are #1063; generation producer is #1064; trusted WorkAuthority reader is #1065. No new durable field/store or claim mutation is in this implementation scope.

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 0 | One production responsibility in `manager.py`; consume existing claim digest helpers and issue-backed WorkAuthority APIs. |
| `state_consistency` | 0 | Read-only comparison to persisted `source_revision`/`claim_key`; no claim rewrite or new state. Existing operator abandon/start APIs are not modified. |
| `acceptance_surfaces` | 2 | `fix-standard` gate spine plus repo-wide R-09/R-16/R-19 process surfaces. |
| `spec_stability` | 0 | Complete accepted spec/design/Todo triad without blocking marker. |
| `orchestration` | 2 | `fix-standard` has nine cards with multiple persona bindings. |
| **Total** | **4 / Yellow** | Must be recomputed with the official current snapshot before implementation intake. All three source prerequisites must land first. |

## Non-goals

- No semantic Todo parser, path-existence/link-write guard, Monitor refresh generation or WorkAuthority freshness implementation.
- No changing claim digest rules or rewriting/superseding an old claim automatically.
- No abandon/recovery implementation. The plan only directs an operator to the existing exact-CAS pre-delivery abandon action under R4/R5 predicates.
- No #1055 existing Candidate/PR recovery; no #810 checkbox closure; no #972/#973 PR conflict; no OpenSpec requirement added to #911's supported lane.
- No change to `work_actions._ship_action()` unique-Todo backstop, CLI commands, or runtime service.

## Implementation boundary

Primary production module is `paulsha_cortex/coordinator/manager.py`. If code work needs changes to source qualification, provider refresh state, WorkAuthority loader, claim keys, persisted run state or delivery semantics, stop and resolve the named prerequisite/issue before implementation; do not silently expand this Yellow slice.
