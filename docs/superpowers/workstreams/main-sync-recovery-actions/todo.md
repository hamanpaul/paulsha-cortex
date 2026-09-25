---
status: accepted
work_item: main-sync-recovery-actions
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Main-sync recovery actions todo

## Tasks

- [ ] **source / dependency gate**：在進入 Cortex intake、實作或派工前，核對 #987 probe implementation／acceptance 與經對抗 review 修訂後的 #988 typed `DiagnosticReason` v3 implementation／acceptance 均已交付；按 live issue、accepted source、exact head 與 gates 核對 C/M producer、pre-valid-C failure representation 及 `context.main_sync` schema。規劃 PR 合併不能解除此 gate。
- [ ] **source**：僅修改 `paulsha_cortex/coordinator/work_actions.py`，建立 read-only shared main-sync recovery eligibility helper，供 `_claim_action` 和 `_retry_build_action` 共用；helper 與 registry reset admission 的 run/status/phase/job/build/ship 條件保持一致，任何不確定或 context error fail closed。
- [ ] **source**：從 #988 typed context 驗完整 C/M、repo object format 和 commit objects；要求 context C 等於現行 `WorkflowRun.candidate_head`。沒有合法完整 C 的 pre-valid-C failure、缺失/無法解析 context 或 typed probe failure 絕不提供 `retry-build`；只有有效 durable `main-sync-unavailable` reason 使用 `resume`。合格 operator `retry-build` 把 stop 時 M 固定作 `TARGET_MAIN_SHA` 並保留完整 conflict paths；action 不重探 `origin/main`。
- [ ] **source / tests**：只為 `main-sync-unavailable` 提供既有 `resume`；只有 `review-advance-failed` 同時包含 `merge authorization blocked:` 與 `not-mergeable` 才提供 `resume`。其餘 stop 的 actions/hints 不變。
- [ ] **tests**：對 run status、phase、needs_human、active job、build steps、passed Manager archive、Candidate/CAS、typed context、full SHA、commit object、C mismatch、missing/corrupt context 及 stop classification 建 positive/negative matrix；不合格時 action 不曝光、直接呼叫拒絕且 registry 無副作用。
- [ ] **tests**：用同一合成 WorkflowRun 實際呼叫 registry retry-build reset 確認受理；stop 後 main 前進 M1→M2 時，action 仍固定 M1，測試確認沒有重新 probe。reset 測試不接續派 Builder、不 merge、不 push。
- [ ] **documentation**：更新 recovery/status 操作說明，交代 M 是 stop 時固定值、resume 只處理 unavailable/精確 not-mergeable 條件，以及 automatic repair 與真 merge 留在後續 issue；若實際 CLI command/help surface 沒變，明列 R-16 核對結果且不增加 help 文字。
- [ ] **CLI (R-16)**：檢查 `cortex --help` 與 action usage；本票不新增 command/option，若實作需要新增 CLI surface，停止並先取得 issue-backed scope。
- [ ] **changelog (R-09)**：implementation PR 增加自己的 `changelog.d/<slug>.md` 與 `[Unreleased]` entry；本規劃 PR 有獨立 planning fragment/entry。
- [ ] **test (R-19)**：新增 tests 必須由 repo pytest workflow 執行；implementation PR 依最終 diff 跑 focused tests、必要完整 pytest、PR-context policy、OpenSpec、`git diff --check` 與遠端 CI。

## Sizing inputs

此票 production scope 僅一個 module `work_actions.py`，domain breadth 0。State consistency 2：persisted exact C/M 必須與 live Candidate 做 CAS，且 action availability 需和 registry reset 的原子受理前置條件一致。Acceptance surfaces 2：`fix-standard` 有 2 gate-spine，另計 R-09/R-16/R-19。完整 accepted spec/design/todo 無 blocker，spec stability 0；9 cards/9 persona bindings 得 orchestration 2。使用 `work_bridge.current_sizing_snapshot(workspace_root=".", combo_name="fix-standard", artifact_rows=[spec, design, plan])` 對本目錄三件套正式重算；若與 `6 / Yellow` 不同，以實際結果更新全部 sizing 欄位，不沿用 issue projection。

## Dependencies and boundary

- Hard dependency #987 與 #988：兩者的產品 implementation、acceptance 與 exact producer/schema 契約都交付後才能 intake 或實作。
- 此票是 #990 的 prerequisite；#990 之後以本票 recovery helper 顯示 status hint，負責 durable writer/read-back，不在此票改 Manager。
- 不自動 probe、reset、派 Builder 或修復；operator 顯式 action 才進既有 reset 路徑。#973／#943 保留 automatic repair、真 merge、merge candidate、新 Builder job、D re-verification/review/push 的完整驗收。
- 本 planning PR 只交付本 spec/design/todo、唯一 work-item binding、CHANGELOG entry 與 fragment；不 intake、不關閉 #989、不交付產品實作。
