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

對抗審查第五輪修正（單一真相與 merge evidence 身分，共兩條）：

1. BLOCKER：`inspect_delivery` 對同一 requirement／criterion 判定 covered
   時，先前只要在 `mapping_results` 內看到任一 `covered` row 就通過；若同一
   次 source snapshot 內、同一 repo／work／run 邏輯範圍同時含較舊 covered
   與較新 blocked 的兩個 source_generation row（例如 producer 尚未清理掉舊
   紀錄），首次 `cortex delivery gaps`／`reconcile` 仍會回報 `ready`。新增
   `_current_generation_matches`：判定與 gap 回報前，先依邏輯範圍（
   requirement／criterion／repo／work／run）只保留最高 generation 的 row，
   與 `reconcile_delivery` 既有跨 mapping generation 守門共用同一套「以最高
   generation 為單一真相」規則，不再讓已被取代的較舊 covered row 蓋過較新
   的 blocked 事實；gap 回報也改用同一組有效 row，避免落選的較舊 covered
   row 讓真正阻擋原因消失不報。
2. MAJOR：merge 改看 ancestry 後，`_verify_remote_merge` 的 evidence digest
   與 `target_sha` 仍嵌入「目前」remote default head；其他 PR 之後正常推進
   default branch，會讓同一 generation、同一 mapping_id 的重讀在
   `reconcile_delivery` 的內容比對中被判定成
   `same-source-generation-content-drift`，使本已 covered 的交付卡在
   pending。改為：digest 只綁不可變事實（merge commit、PR head、PR
   number、mapped issues、todo 完成狀態），不再納入 default head；新增
   `_stable_evidence`／`_VOLATILE_EVIDENCE_FIELDS`，在 `_read_completion_
   review_for_history` 持久化與同 generation 內容比對前，把 merge 階段的
   `target_sha`（僅供報告參考的目前觀察值）從身分中剔除。`inspect_delivery`
   即時回傳值仍保留完整 `target_sha` 供人工/報告查看。

兩條同樣先以 RED 測試確認可重現後再修：`test_a01_snapshot_with_mixed_
generation_rows_uses_highest_generation_for_coverage`、
`test_a07_default_head_further_advancing_between_reconciles_does_not_drift`。
`tests/test_requirement_delivery.py`／`test_requirement_delivery_cli.py`／
`test_github_delivery_client.py`／`test_delivery_orchestrator.py`（141 個）
與 `-k "delivery or remote_closure or retire"`（280 個）全數維持通過。
- #845 對抗審查修復（第六輪）：同一 repo／work／run 在最高 generation 內 covered 與非 covered 並存時，該範圍不算 covered（只有範圍內每筆 row 皆 covered 才成立），缺額優先指向非 covered 的 row。

對抗審查第六輪修正（續，共三條）：

1. BLOCKER：`_verify_remote_merge` 判定 mapped todo 是否完成時，先前只看
   `fetch_remote_closure` 讀「目前」remote default head 的 todo.md 內容；若
   這條 PR 合併當下 mapped todo 尚未全勾、之後另一個無關 commit 才把它補
   勾，重跑 `cortex delivery gaps／reconcile` 會把同一筆原本 `merge` 階段
   `failed`（`remote-todo-incomplete`）的交付，靜默翻成 `verified`／
   `covered`——即使這筆 PR／merge 本身從未讓 todo 完成。`github_delivery.
   fetch_remote_closure` 新增 opt-in 參數 `todo_at_merge_commit`（預設
   `False`，其餘既有呼叫者 ship／retire-delivered／work bridge 不受影響、
   語意不變，仍讀「目前」default head）；為 `True` 時改讀 merge commit 當下
   的 tree 內容判定 `todo_complete`／`todo_revisions`。
   `requirement_delivery._verify_remote_merge` 呼叫時帶上
   `todo_at_merge_commit=True`，把 merge 階段的 todo 完成度綁回這筆 PR／
   merge 本身，不再受之後正常推進的無關 commit 影響。
2. MAJOR：`reconcile_delivery` 對同一 mapping_id（同 generation）重讀時，先
   前只要內容與既有持久化的 row 有任何差異，即無差別回報
   `same-source-generation-content-drift` 並拒絕更新——這條保護原意是擋
   `_evidence_weakened` 偵測到的暫時性弱讀取（見第四輪修正），卻連「先因缺
   live receipt 記為 `missing`／`blocked`、之後補上合法 live receipt 重跑」
   這種證據**變強**也一起擋掉，導致這筆交付的 gap 永遠清不掉。改為只在
   `_evidence_weakened(previous.evidence, incoming.evidence)` 為真（真的有
   原本 `verified` 的階段退回非 `verified`）時才回報 drift 並拒絕；其餘階段
   不弱化的內容變強／持平差異，一律接受 incoming 的更新結果。
3. MAJOR：`_authority_matches` 驗證 mapped todo 授權時，先前以 `tuple` 逐序
   比對 `row.get("todo_paths")` 與 `authority.mapped_todo_paths`；同一組已
   授權路徑只因兩邊記錄順序不同，就會被誤判 `mapping Todo paths are not
   authorized` 而 fail closed，即使集合完全相同。改以集合比對——缺漏或多
   餘路徑仍會讓集合不相等而正確 fail closed，只是不再要求順序一致。目前
   這行比對之前必經共用 gate `delivery._validate_work_authority`（無條件要
   求 `len(mapped_todo_paths) == 1`），因此真實呼叫路徑上尚不會出現多 todo
   path 的 `WorkAuthority`；regression 測試改為直接呼叫
   `_authority_matches` 並暫時中和該共用 gate，證明一旦上游放寬單一
   delivery target 限制，這行比對本身已是順序無關且 fail-closed 的正確
   實作，不必等放寬後才發現另一個 bug。

三條均先以 RED 測試（`git show HEAD:` 還原生產碼跑新測試）確認可重現後再
修，`tests/test_requirement_delivery.py`／`tests/test_github_delivery_client.
py` 新增對應 regression 測試，全數 GREEN；既有 `test_requirement_delivery*.
py`／`test_github_delivery_client.py`／`test_delivery_orchestrator.py`
（148 個）與 `-k "delivery or remote_closure or retire"`（287 個）全數維持
通過，確認未影響共用 gate（`evaluate_remote_closure`／
`delivery._validate_work_authority`）對其他呼叫者的既有語意。

對抗審查第七輪修正（BLOCKER：`live_receipt_validator` 從未接上 production
caller）：`porcelain/delivery.py` 的 `cortex delivery gaps／reconcile` 是
`inspect_delivery`／`reconcile_delivery` 唯一的 production caller，卻從未傳入
`live_receipt_validator`；`refine-requirements-v1.json` 每條需求都把 `live`
列為 required stage，因此即使 producer 交付合法 live receipt，也固定產生
`governed-live-receipt-validator-unavailable`，已交付的需求永遠無法 `ready`。

新增 `paulsha_cortex/coordinator/live_receipt_validators.py`：以
`receipt["kind"]` 為 key 的封閉登記表，只認得本 repo 既有、可機械驗證的兩種
live receipt kind，未登記 kind 一律 `False`（fail closed）：

1. `cortex/deployment-canary-qualification/v1`：deployment-canary
   `qualification.json`（`schema_version: 2`、`profile: deployment-canary`、
   `status: passed`）；直接呼叫 `qualification/validate.py` 既有 `validate()`
   做結構與 fail-closed 判定，不另寫一套，只額外綁定
   `evidence.candidate_sha`／`evidence.wheel.sha256` 精確等於該需求 claim 的
   `target.candidate_sha`／`target.artifact_sha256`。`qualification/` 不隨
   wheel 一起發佈，lazy import 失敗一律 fail closed。
2. `cortex/task-memory-live-canary/v1`：#857 task-memory canary evidence——
   `passed: true`；`content_retrieval` 與 `paths.{context-delivered,
   snapshot-ready,note-fetch}` 三個 delivery path 各自 `attempts ≥ 5` 且
   `successes/attempts ≥ 0.95`（並重新驗算 `success_rate` 與
   `attempts`/`successes` 一致）；`negative_controls` 覆蓋
   `permission-denied` 與 `cross-scope-rejection` 兩個固定案例且
   `status: passed`；`cross_project` 至少兩個不同 repo 且 `status: passed`；
   `evidence.target` 必須與 receipt 外層已驗證的 target（同時期 #841
   loaded-runtime receipt 的 artifact digest／source revision）逐欄位相等，
   無法綁定即拒絕。

`governed_live_receipt_validator` 只接收已由 `_verify_live` 驗過
schema／hash／target／期限／authority／independence 的 receipt 物件（不拿
`evidence_root`、不讀檔、不觸網），任何解析例外一律視為未通過；已接進
`porcelain/delivery.py` 的 `common["live_receipt_validator"]`，是
`inspect_delivery`／`reconcile_delivery` 目前唯一的 production caller。

先以 RED 測試確認可重現：暫時把 `paulsha_cortex/porcelain/delivery.py` 還原
成 `git show HEAD:` 版本、移除新模組，`tests/test_requirement_delivery.py`／
`tests/test_requirement_delivery_cli.py` 因 `ImportError` 收集失敗（RED）；
復原後 GREEN。新增 9 個 regression 測試（`test_requirement_delivery.py` 7
個直接驗證封閉登記表：兩種 kind 的 ready、未知 kind、qualification
候選 target 綁定不符、qualification 未通過、task-memory 低於門檻、
task-memory target 綁定不符各一，加一個 receipt 檔案 hash 被竄改仍 fail
closed 的 regression；`test_requirement_delivery_cli.py` 2 個端到端走
`cortex delivery gaps` CLI 入口，monkeypatch 掉唯一會觸網／觸 loaded-runtime
service 投影的邊界，其餘走 production code path，證明合法 fixture receipt
真的能讓需求 `ready`，未知 kind 會產生具體 `failed` gap）。`README.md` 與
`docs/superpowers/specs/requirement-delivery-accounting.md` 補上支援 kind
與綁定規則表。`tests/test_requirement_delivery.py`／
`test_requirement_delivery_cli.py`／`test_github_delivery_client.py`／
`test_delivery_orchestrator.py`／`test_phase2_qualification.py`
（218 個）與 `-k "delivery or qualification"`（444 個）全數通過。
