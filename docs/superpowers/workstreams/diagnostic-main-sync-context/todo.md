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
---

# DiagnosticReason main-sync structured context todo

## Tasks

- [ ] 在 `diagnostics.py` 增加 frozen `MainSyncContext` and strict from/to-dict shape validators；full object-id syntax、M/failure M equality、paths list 與 reason enums/optional fields均驗證。
- [ ] 讓 DiagnosticReason v3 唯一 reserved nested field `context.main_sync` 原生 JSON round-trip；保留 v1/v2 reader 與其他 string context 200 字語意，明確參數不能 stringify nested input。
- [ ] 新增 tests: multi-path >200-char exact round-trip; fetch後failure `main_head=M`; null M; invalid/truncated hash; mismatch; unknown/missing fields; type confusion; legacy v1/v2 load and v3 serialization including any old flat `context.main_sync` string.
- [ ] WorkflowRun JSON encode/decode fixture 讀回 nested main_sync exact equality，證明不是只在 transient dataclass 中保留。

## Sizing inputs

僅 `diagnostics.py` 一個 production module，domain breadth 0。State/consistency 1：需相容舊 durable reason 的 schema versioning/read-back，但本票沒有 concurrent state writer、跨物件 CAS 或 manager transition。Acceptance surfaces 2 與 orchestration 2 由 fix-standard deck 固定；完整 accepted triplet 使 spec stability 0。Formal helper result must be `(5, "yellow")`。

## Dependencies and boundary

此票被 probe child 01 所定的 payload use case 約束；child 03 recovery actions 和 child 04 Manager persistence 都 blocked by 此票。此票不寫 run row、不做 reset/dispatch、不執行 merge/ship。
