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

對抗審查第四輪修正（reconcile fail-closed 加嚴，共三條）：

1. BLOCKER：`_verify_remote_merge` 先前額外要求 CompletionRecord 的
   `target_ref_sha`（合併當下捕捉、不可變）等於「目前」remote default head，
   導致其他 PR 之後推進 default branch 只是正常事件，也會讓已完成、原本
   covered 的交付在下一次 `cortex delivery gaps／reconcile` 重跑時被誤判為
   `remote-default-head-mismatch` 而退化。已完成的交付現在只要求這條 PR 的
   merge commit 仍是「目前」default head 的祖先（merge ancestry）——這正是
   共用 gate `evaluate_remote_closure` 透過 `facts.merge_is_ancestor`（對目前
   default head 即時計算）已經驗證的條件，因此直接移除這條錯誤的額外相等
   檢查，不改 `evaluate_remote_closure` 本身語意，其他呼叫者（例如
   `ShipOrchestrator.verify_remote_closure`，其在合併當下寫入全新記錄，要求
   相等本身是對的）不受影響。
2. MAJOR：`reconcile_delivery` 跨 mapping 的 generation 守門先前只掃既有
   `covered` rows；若同一 requirement／criterion／repo／work／run 範圍內較新
   的 generation 已是 `blocked`／`stale`，遲到的舊 generation（不同
   CompletionRecord，因而 mapping_id 不同）會直接跳過守門、被當成新
   coverage 插入，把 index 翻回 `ready`。現改為以該 logical 範圍內**已見過
   的最高 generation（不論狀態）**為準；較舊 generation 一律回報
   `late-source-generation-ignored` 並不落盤，不再侵限於「既有 row 恰好是
   covered」的狀況。
3. MAJOR：`same-source-generation-content-drift` 先前只在 incoming row 與既有
   row 保持**相同 mapping_id** 時才比對；同一 generation 重讀若暫時失去已
   驗證欄位（例如 merge 證據因網路抖動短暫讀取失敗，導致 `merge_sha` 從實際
   值變成缺欄位），會改變 mapping_id 而完全繞過這條偵測，讓暫時性弱讀取
   靜默把既有 covered 換成 blocked／unknown。現在同一 logical 範圍、同一
   generation 但 mapping_id 不同時，會另外比較兩者的 stage evidence：只要
   任一原本 `verified` 的階段在新讀取中不再是 `verified`，即視為弱化，保留
   既有 covered、回報 `same-generation-weaker-evidence-ignored` 並不落盤，
   不讓暫時性讀取失敗覆蓋已驗證結果。

三條均先以 RED 測試（`git show HEAD:` 還原生產碼跑新測試）確認可重現後再修，
`tests/test_requirement_delivery.py` 新增三個對應 regression 測試，全數
GREEN；既有 `test_requirement_delivery*.py`／`test_github_delivery_client.py`／
`test_delivery_orchestrator.py` 與 `-k "delivery or remote_closure or retire"`
全數維持通過，確認未影響共用 gate 對其他呼叫者（ship/merge 時的新鮮記錄
校驗）的既有語意。
