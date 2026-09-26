#845 refine requirement delivery accounting adds the versioned R01–R14 manifest, trusted per-stage evidence consumer, machine-readable gaps, and a Trust Root registered CAS index. Read-only `cortex delivery status/gaps` and sidecar-only `reconcile` consume formal CompletionRecord/WorkAuthority/remote closure and #841 loaded-runtime receipts; missing live producer validation remains an explicit fail-closed gap. README and operator contract are updated.

Adversarial review follow-up (fail-closed hardening): the test stage no longer infers "covered" from an overall verification run status (`reviewing`/`verified`); it now only accepts a CompletionRecord test explicitly bound (via `acceptance_ids`) to that exact acceptance criterion and reported `passed`, otherwise the criterion is a test gap. `authority_ref` is no longer accepted merely non-empty: `validate_manifest` now re-hashes the accepted plan/spec content the locator points to, checks it against `authority_ref.revision`, and locks the manifest's requirement id/title inventory against it, so shrinking or renaming requirements while keeping the same accepted revision fails closed instead of silently dropping delivery gaps. Fixed a pre-existing casing mismatch (`Registry`→`registry`) between `refine-requirements-v1.json`'s R04 title and the accepted plan surfaced by the new check.

對抗審查第三輪修正（merge 階段 fail-closed 加嚴）：`RemoteClosureFacts` 新增
`closing_issues` 欄位，`fetch_remote_closure` 現在會一併讀取這條 PR 真正的
`closingIssuesReferences`；`requirement_delivery._verify_remote_merge` 在既有
`evaluate_remote_closure` 之上另外驗證（1）mapped issue 確實由**這個** PR 關閉
（不是人工或另一個 PR 關閉的巧合 closed 狀態），（2）mapped todo.md 遠端內容
確實全數勾選完成；缺任一項即記 `merge` 階段 `failed` 並留下具體 gap，不再只憑
issue 目前 closed 就記 verified。共用 gate `evaluate_remote_closure` 本身語意
未變，其他呼叫者不受影響。另修正 `reconcile_delivery` 的 generation 比較：先前
只在同一 `mapping_id` 下比對 source_generation，換了 candidate／completion
record（因而 mapping_id 不同）但屬同一 requirement／criterion／work/run 範圍的
「從未入索引過的舊 generation」重送時，會把已落地且較新的 covered mapping 誤標
成 stale、被舊資料取代；現改為跨 mapping_id 以此邏輯範圍比較 generation，較舊
generation 不得覆蓋或使較新有效 mapping stale，偵測到即回報
`late-source-generation-ignored` 並不落盤。測試補上可獨立表達 PR
closingIssuesReferences 與 todo 完成度的負例，以及舊 generation 回滾的 regression
測試。
