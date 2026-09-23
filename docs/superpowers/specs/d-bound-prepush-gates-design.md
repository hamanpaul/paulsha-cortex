---
status: accepted
work_item: d-bound-prepush-gates
---

# Push 前 D-bound gates 設計（#973 Child C1）

## Decisions

### D1 Candidate transition invalidates old evidence
在既有 Candidate C→D transition 後，依 exact-subject matching 使所有 C-bound delivery evidence 無效，並用測試驗證 #847 不會 bypass。

### D2 Local D evidence is a prerequisite to any remote action
按現有 pipeline 取得 build harvest、verify/tests、cross-domain review、required maintainer review，再跑 local exact-head preflight。Evidence 必須成功且 subject D；任何不明狀態均停在 local 階段。

### D3 Emit a bounded handoff
Local pass 只產生可辨識的 ready-to-push(D) handoff 給 C2。C1 不作 push 或 PR CI 查詢；PR CI 在 D push 並 create/update PR 後才可存在。避免 C1 引入新的 durable delivery transaction。

### D4 Keep delivery authority unchanged
使用現有 evidence readers 和 preflight，不新增 schema、remote journal、push/PR/merge/closure authority。若實作發現 ready handoff 需要新增跨 store durable state，停止並重做 sizing/scope review。

## Sequence

1. Read accepted Candidate D and invalidate all C-bound gate evidence.
2. Obtain successful exact-D build, verify/test, review, required maintainer-review evidence.
3. Run local exact-D preflight.
4. If any result is missing, failed, stale, ambiguous or wrong-head, stop with no remote effect.
5. If all pass, expose ready-to-push exact-D handoff for the existing C2 pipeline.

## Sizing boundary

Two production modules produce domain_breadth=1. The child reads/writes only the existing local Candidate gate evidence and ready-to-push decision, without remote push/PR state or a new durable cross-object transition, so state_consistency=1. Accepted fix-standard mechanics give acceptance=2, stability=0, orchestration=2; total 6 / Yellow.
