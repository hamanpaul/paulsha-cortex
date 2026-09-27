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

對抗審查第八輪修正（deployment-canary receipt 只做結構檢查、checkout 外執行必壞，
共兩條）：

1. BLOCKER：`_validate_deployment_canary_qualification` 先前呼叫 `qualification/
   validate.py` 的 `validate()` 時固定傳 `require_release_profile=False`、
   `require_canary_profile=False`、`evidence_root=None`——只做 payload 結構檢查
   （provider 需求、必備 base tests 等），完全沒有比對真正的 evidence 檔案集合
   與內容；只含 `fresh-install`／`full-dispatch-closeout` 兩個測試名稱加一個假
   artifact 路徑（`evidence/summary.json`，不對應任何真實檔案）的 receipt 就會被
   採信為合法 deployment-canary qualification。現在一律以
   `require_canary_profile=True` 呼叫 `validate()`，並要求 receipt 額外帶
   `canary_target`（`repository`／`work_id`／`issue`／`evidence_directory`）：
   `repository` 綁定已驗證的 `target["repo"]`；`evidence_directory` 是相對
   delivery evidence root（工廠建立時由呼叫端提供，不是 receipt 內容能操控的
   路徑，經安全 locator 解析——禁止絕對路徑、`..`、symlink）的目錄，交給
   `validate()` 的 `evidence_root` 參數，讓它逐檔核對 artifact-inventory 宣告
   的檔案是否存在、內容 sha256 是否相符、evidence tree 是否缺漏或多出檔案，並
   比對 evidence 自報的 `dispatch-closeout.json` repository／work_id／issue 是否
   與外部帶入的 `canary_target` 一致——防止 candidate 自己在自報的 dispatch
   closeout 裡宣稱任意身分。因為 `evidence`（即 qualification.json payload）本身
   的 `ROOT_KEYS` 是封閉集合，`canary_target` 是 receipt 的獨立頂層欄位，不塞進
   `evidence` 裡。
2. MAJOR：`_validate_deployment_canary_qualification` 先前用一般
   `from qualification import validate` import，依賴 repo 根目錄的
   `qualification/` 剛好在 `sys.path` 上（測試/開發環境跑 `python -m pytest` 時
   cwd 會被加進 `sys.path`，恰好命中）；已安裝的 wheel 只打包 `paulsha_cortex*`，
   `cortex delivery gaps` 在 checkout 外執行時這個 import 一律 `ImportError`，
   導致就算 producer 交付完全合法的 receipt 也永遠被拒。新增
   `make_governed_live_receipt_validator(*, source_root, evidence_root)` 工廠
   取代原先單一全域 callable `governed_live_receipt_validator`：以
   `importlib.util.spec_from_file_location` 從 CLI `--source-root` 底下的
   `qualification/validate.py` 動態載入，路徑須嚴格在 `source_root` 之下且不得
   經過 symlink；缺檔、路徑逃逸或載入時任何例外一律回傳 `None`，deployment-canary
   kind 遇到 `None` 立即 fail closed，不影響 task-memory kind（它不需要
   `qualification/validate.py`）。`porcelain/delivery.py` 改以這個工廠、帶入
   `args.source_root` 與 `coordinator_root` 建立 `common["live_receipt_
   validator"]`。工廠回傳的 callable 仍只吃 receipt 本身，符合 `_verify_live`
   既有呼叫慣例——`source_root`／`evidence_root` 是呼叫端事先決定的信任邊界，
   不是從 receipt 推導。

兩條均先以 RED 測試（暫時把 `paulsha_cortex/coordinator/live_receipt_validators.
py`／`paulsha_cortex/porcelain/delivery.py` 還原成 `git show HEAD:` 版本，不用
`git stash`）確認可重現：13 個新／改測試因 `make_governed_live_receipt_validator`
在舊版不存在而 `AttributeError`；其中
`test_delivery_gaps_cli_with_governed_qualification_receipt_missing_source_
root_module_is_rejected` 直接證明 MAJOR 那條——`--source-root` 底下完全沒有
`qualification/` 時，舊版 CLI 仍回報 `closure_readiness == "ready"`。復原後
GREEN。

`tests/test_requirement_delivery.py` 新增 7 個測試（正例：完整合法 evidence
目錄＋外部 canary 身分才 ready；負例各一：缺 `canary_target`、evidence 少一個
canary-only 檔案、evidence 多一個未列入 inventory 的檔案、evidence 檔案內容被
竄改導致 digest 不符、`dispatch-closeout.json` 自報 repository 與外部
`canary_target` 不符、`evidence_directory` 用 `..` 試圖逃出 evidence_root、
`--source-root` 底下沒有 `qualification/validate.py`），並把既有 wheel-digest-
mismatch／qualification-not-passed 兩個舊測試改用完整合法 evidence 目錄＋
canary 身分，確保是這兩個具體綁定失敗、不是被新增的 `canary_target` 缺項短路。
`tests/test_requirement_delivery_cli.py` 新增 1 個端到端測試，證明 `cortex
delivery gaps` 這條 production CLI 入口在 `--source-root` 缺 `qualification/`
時同樣 fail closed，並把既有 ready 測試改用完整合法 evidence 目錄＋外部 canary
身分。`docs/superpowers/specs/requirement-delivery-accounting.md` 更新為工廠
呼叫方式與新的綁定規則表。

`tests/test_requirement_delivery.py`／`test_requirement_delivery_cli.py`／
`test_phase2_qualification.py`（139 個）與 `-k "delivery or qualification"`
（452 個）全數通過。

對抗審查第九輪修正（canary_domain 自報放行、target 欄位只部分綁定，共兩條）：

1. BLOCKER：`requirement_delivery._verify_live` 先前把 `independence.
   canary_domain` 當成 receipt 自報字串直接採信，只要求它跟 `review_domain`
   不同——同一個 reviewer 產生的 receipt，只要把 `canary_domain` 改成任意
   不同於 reviewer domain 的值就能通過 independence 檢查，這條檢查形同虛設。
   新增 `live_receipt_validators.derive_canary_domain(receipt)`：依
   `receipt["kind"]` 從已通過 schema 檢查的 evidence 導出 canary 執行環境
   身分——`cortex/deployment-canary-qualification/v1` 用 evidence 的
   `image.digest`（`qualification/driver.py` docstring 明示這是複製進候選
   掛載之前的參考映像，與候選 checkout／reviewer 執行環境無關）；
   `cortex/task-memory-live-canary/v1` 用 evidence 的 `executor.id`（本輪
   新增的必要欄位，原 schema 沒有任何可用的執行者身分）。`_verify_live`
   改吃新的 `domain_deriver` 參數（`inspect_delivery`／`reconcile_delivery`
   新增對應 `live_receipt_domain_deriver` 參數並轉交；`porcelain/delivery.py`
   接上 `derive_canary_domain`），independence 檢查只用導出值；receipt 若
   仍自報 `independence.canary_domain`，該值必須與導出值完全相同，否則拒絕。
   無法導出（kind 未登記、evidence 缺對應欄位）一律回傳 `None`，視為
   independence 檢查失敗，不代入預設值放行。
2. MAJOR：`_validate_deployment_canary_qualification` 先前只把
   `candidate_sha`／`artifact_sha256`（進而透過 `canary_target.repository`
   綁 `repo`）與 acceptance target 比對；target 其餘五個欄位——
   `source_revision`／`service`／`instance`／`profile_key`／
   `config_revision`——qualification evidence 的 schema 完全沒有對應內容
   可比對，因此完全沒被檢查。同一份真正、完整通過的 canary evidence 只要
   換掉外層 `receipt["target"]` 這五個欄位（例如 instance 或
   config_revision），就能重複核銷不同 profile/instance 的需求，違反 A04
   exact target。新增 `live_receipt_validators.deployment_canary_evidence_
   scope(target)`：只取這五個欄位算出 canonical SHA-256；
   `canary_target.evidence_directory` 現在必須精確等於這個 digest（不再是
   receipt 自由選擇的字串），因此真正的 evidence 檔案必須存在於
   `evidence_root/<digest>/...` 這個路徑之下——`evidence_root` 是呼叫端信任
   邊界，receipt 內容無法寫入。同一份 wheel canary evidence 換掉這五個欄位
   後，其唯一合法路徑會跟著改變，除非攻擊者真的取得 `evidence_root` 寫入
   權限並複製檔案到新路徑，否則無法通過新 target 的檢查。
   `cortex/task-memory-live-canary/v1` 的 evidence 完全沒有檔案樹可供內容
   定址，目前仍只靠 `evidence["target"] == 外層 target` 的自我一致檢查；這
   是已知的較弱綁定，非本輪對抗審查具體指出的項目，於文件明確記載，不佇裝
   已解決。

兩條均先以 RED 測試（`git show HEAD:` 還原 `live_receipt_validators.py`／
`requirement_delivery.py` 到本輪修法前版本，不用 `git stash`）確認可重現：
新測試因 `deployment_canary_evidence_scope`／`derive_canary_domain` 在舊版
不存在，`tests/test_requirement_delivery.py` 於收集階段就 `AttributeError`。
復原後 GREEN。新增 2 個 regression 測試：BLOCKER 用真正合法的 deployment-
canary evidence，僅把 receipt 自報的 `canary_domain` 改成一個「看起來合法、
確實跟 reviewer domain 不同」但跟導出值不符的字串，證明修法前會被放行、
修法後必須拒絕；MAJOR 用同一份針對 `instance="default"` 產生的真實
evidence，只把外層 `target.instance` 換成 `"other"`（`canary_target.
evidence_directory` 保持指向原 evidence 實體目錄），證明修法前會被放行、
修法後必須 fail closed。既有測試中沿用固定字面路徑 `"canary-evidence"` 與
自報 `canary_domain: "loaded-runtime"` 的 fixture 全部改為依 target 動態算出
的規範 digest，並移除不再需要的自報 canary_domain；`_task_memory_payload`
新增必要的 `executor.id` 欄位。未知 kind 的既有 regression 測試因獨立性
檢查提前 fail closed，改為斷言新的 `live-canary-independence-mismatch`
原因（原先是 `governed-live-receipt-validator-rejected`）。

`tests/test_requirement_delivery.py`／`test_requirement_delivery_cli.py`／
`test_phase2_qualification.py`（141 個）與 `-k "delivery or qualification"`
（454 個）全數通過。
