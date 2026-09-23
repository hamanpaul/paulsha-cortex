---
status: accepted
work_item: completion-record-conditional-writer
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
issue: 996
---

# CompletionRecord 精確條件建立工作計畫

唯一 owner：issue #996 `completion-record-conditional-writer`（GitHub issue #996 已建立）。本票是 #977 的前置 writer primitive，不做 Manager orchestration、proof、remote closure、OutcomeStore 或 registry transition。#962 R1–R4/R6/R8(a–d) 與 #887 全部 AC 維持 aggregate gates。

Planning views：[spec](../specs/completion-record-conditional-writer-spec.md)、[design](../specs/completion-record-conditional-writer-design.md)。

## Boundary

唯一 production diff 為 `paulsha_cortex/coordinator/completion.py`。新 API 對單一路徑提供 absent create 或 complete-payload exact reuse；existing mismatch/error 一律 fail closed、不 quarantine、不改動。既有 `write_completion_record` contract/callers 保持不變。若需要修改別的 production module，停下並重算/另拆，不能沿用本 sizing。

focused tests 放在既有 completion record test module 或新 `tests/test_completion_record_conditional_writer.py`。PR 依 repo policy 補 changelog fragment、CHANGELOG [Unreleased]、API/lifecycle docs、CLI help compatibility、含 PR context 的 policy check 與 repo-required tests。

## Tasks

- [ ] **T1 tests / RED exact contract (I2, I4–I6)**：測試輸入 normalization、相同完整 payload exact reuse、completed_at/optional/volatile WorkAuthority/source revision 不同即 conflict；任何錯誤 payload 不能接受 existing row。
- [ ] **T2 source / typed conflict and secure reference reads (I1–I5)**：新增明確 conditional-create API、typed conflict taxonomy；target record 與 verification/review refs 都用 anchored dirfd + no-follow descriptor、regular-file check、pre/post entry identity compare，並驗證 references；不重用 `is_symlink()`→`read_text()` helper，不呼叫 legacy quarantine helper。
- [ ] **T3 source / create-if-absent CAS (I1, I3, I6)**：同目錄 temp write、flush/fsync、no-replace atomic publish、目錄 fsync；`EEXIST` 走 exact read/compare。不得 `replace` existing target。
- [ ] **T4 tests / subprocess races and crash faults (I3–I6)**：barrier subprocess 測同 payload 與不同 payload 競爭；驗證恰一筆不可覆寫、exact callers 同 hash、衝突者回報 conflict且 existing bytes 不變；注入 temp/fsync/publish/crash faults。
- [ ] **T5 tests / malformed, evidence-ref swap and immutable conflicts (I4, I5, I7)**：覆蓋 malformed/unreadable、target symlink、directory/nonregular、reference failure；以 deterministic hooks 在 evidence ref open 前與 open 後注入 symlink entry swap，確認都拒絕、不得 publish record。確認沒有 quarantine/move/unlink/replace，existing bytes 完全相同。保留 legacy writer regression。
- [ ] **T6 documentation / parent contract (I7, I8)**：記錄新 API semantics、exact timestamp handling、legacy writer compatibility、外部 authority freshness 留給 Manager/#975、registry CAS 留給 #976；保留 #962/#887 aggregate acceptance。
- [ ] **T7 documentation / policy and gates (I8)**：更新 completion API docs、docs/unified-work-lifecycle.md、changelog fragment、CHANGELOG；驗 CLI help 相同，跑 focused tests、repo required tests、帶 PR context 的 policy check。
- [ ] **T8 closeout accounting**：分開報告 implementation/tests/PR/merge/runtime；此 child 完成不等於 #977/#962/#887 完成。

## 五維 sizing

以 repo `work_bridge.current_sizing_snapshot` 對 plan + accepted spec/design、`fix-standard` combo 與 repo 完整 `ACCEPTANCE_SURFACE_RULES` 實算：

| 維度 | 分數 | 推導 |
|---|---:|---|
| domain_breadth | 0 | 唯一 production module 是 `coordinator/completion.py`。 |
| state_consistency | 2 | Record path 與 verification/review evidence refs 為多個競爭 filesystem entries；descriptor no-follow identity checks 須阻止 ref-entry swap 後仍寫入 record。 |
| acceptance_surfaces | 2 | fix-standard 2 gate-spine 加 R-09/R-16/R-19，訊號值 5。 |
| spec_stability | 0 | triad accepted 且 planning completeness 無 blocker。 |
| orchestration | 2 | combo 9 cards、9 persona bindings。 |
| **合計** | **6 / Yellow** | **0+2+2+0+2=6**。 |

若需改第二個 production module，domain_breadth 至少升為 1；本計畫 state_consistency 為 2，其餘維度不變時總分即至少 7 / Red。依 helper 真值回報，不降維度或 acceptance；任何 contract/scope/plan 更新後重跑 current sizing/completeness。

## Completion accounting

child 通過只證明 `completion.py` conditional writer contract，不證明 WorkAuthority 當前性、#975 proof、remote closure、OutcomeStore append、#976 terminal CAS、#977 integration、#962 aggregate、#887 parent 或 installed runtime。
