---
status: accepted
work_item: diagnostic-main-sync-context
domain_breadth: 0
state_consistency: 1
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 5
sizing: yellow
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# DiagnosticReason main-sync structured context todo

## Tasks

- [ ] **source**：在 `paulsha_cortex/coordinator/diagnostics.py` 增加 frozen `MainSyncContext`、嚴格 validators、DiagnosticReason v3 typed projection 與 `diagnostic_reason(..., main_sync_context=...)` explicit typed entry；mapping/list 不得走一般 context 的 stringify 路徑。
- [ ] **source**：保留 v1/v2 reader 並正規化輸出 v3；一般 string context 維持 200 字及 16-key 規則。`context.main_sync.failure` 使用 #987 `MainSyncProbeFailure` 成員名；schema 只驗 40/64 hex full-id 語法，Git object format/commit 可解析性仍屬 probe/action。
- [ ] **tests**：覆蓋 candidate/main 的 full SHA 與 null M、完整多條 >200 字 conflict path、repair kind、skipped reason、fetch 後 `failure.main_head=M` 的 exact typed round-trip；negative cases 包含非法/縮寫 hash、M 不一致、錯型別、未知/缺失欄位、mapping/list 通用 context、同時設定 legacy string 和 typed object。
- [ ] **tests**：載入 v1/v2 payload，驗 legacy 欄位與舊 flat `context.main_sync` string 保留且 schema 讀回為 v3；v1/v2 中若該位置是 nested object 則 fail closed。
- [ ] **tests**：透過現行 `WorkflowRun.to_dict()`、JSON encode/decode、`WorkflowRun.from_dict()` 的 fixture 往返，逐欄比較 `context.main_sync`，確認 failure M、repair/skipped 欄位及每條 path 都無 stringify、截斷或遺漏；不修改 `workflow.py`。
- [ ] **documentation**：實作 PR 明列 schema v3 wire contract 與 #987 producer dependency；若一般使用者文件或 CLI surface 實際受影響，再另走有 issue authority 的範圍審查。此規劃 PR 以本 spec/design/todo、work-item binding 和 CHANGELOG entry 作文件交付。
- [ ] **CLI (R-16)**：檢查 `cortex --help`/CLI command surface，確認本票不新增 CLI option/help；如 implementation 需要 CLI 修改，停止並建立 issue-backed scope update。
- [ ] **changelog (R-09)**：本規劃 PR 提交 `changelog.d/main-sync-diagnostics-plan.md` 與 `[Unreleased]` entry；後續 implementation PR 仍須有自己的 fragment/entry。
- [ ] **test (R-19)**：新增測試由 repo `tests.yml`/pytest workflow 涵蓋；實作 PR 的 PR-context policy、focused/full tests 與 `git diff --check` 需按最後變更重跑。

## Sizing inputs

僅 `diagnostics.py` 一個 production module，domain breadth 0。State/consistency 1：需相容舊 durable reason 的 schema versioning/read-back，但本票沒有 concurrent state writer、跨物件 CAS 或 manager transition。Acceptance surfaces 2 與 orchestration 2 由 fix-standard deck 固定；完整 accepted triplet 使 spec stability 0。Issue 的五個驗收項目為 plan-review `invariant_count: 5`。以 current `fix-standard`、本三件套與 `current_sizing_snapshot()` 重算，預期 `(5, "yellow")`；若當前 helper 結果不同，依實算更新 sizing 欄位及理由，不採歷史 projection。

## Dependencies and boundary

此票被 child 01/#987 的 probe producer contract 約束；#987 尚為 OPEN，需先完成其 implementation/驗收才可進本票實作。child 03/#989 recovery actions blocked by #987/#988；child 04/#990 Manager durable writer blocked by #987/#988/#989。此票不寫 run row、不改 Manager/WorkflowRun/recovery action、不做 probe/reset/dispatch、不合併或 push Candidate、不執行 merge/ship。完成本規劃或其 PR 不代表 #988 product implementation 已開始或完成。
