---
status: accepted
work_item: authorized-merged-run-proof-admission
---

# 已授權合併 run 的 admission 與 proof oracle 設計（#975）

## Decisions

### D1 將 claim admission 與 Manager proof 分成兩個唯讀關卡

兩個事故入口分開辨識，不把「claim key mismatch」當作所有 recovery 的唯一入口：

1. `work_actions._claim_action` 的 review/mismatch 分支，在 authority-restart reset 之前檢查完整 merged-journal binding。命中後只回傳 `resume/merged-delivery-closure` 和同一 run；不 reset、不寫 registry。
2. Manager resume/tick 對已同步 claim key 的舊 verify-reset run，在讀取 pending verify step 或呼叫 dispatcher 前檢查精確候選 predicate，再呼叫 proof oracle。這涵蓋已被 reset、因此不會再命中 mismatch 的歷史 run。

兩條新 admission 路共用嚴格 journal binding 與 proof oracle。既有 reconciliation 維持自己的最小判準；#975 不把這些 gate 併成直接終結 run 的新 shortcut。

### D2 新 admission 用嚴格 phase-free helper，舊 reconciliation 保持原判準

新增 `_merged_delivery_journal_bound(run, *, journal_path)` 作為新 review mismatch 與舊 verify-reset 的嚴格 admission parser。呼叫端傳入收到的 `state_path`；不得在 helper 或 caller 從 parent 重建路徑。helper 只讀 raw JSON，以單一 run row 驗證 schema、identity、merged/done phase、head、merge SHA、authorization locator/hash/payload 及 delivery binding。workflow_step_ids 從 `run.steps` 依 `_delivery_journal_row()` 規則推導後比對，不以 journal 自述值自證。

既有 `_merged_delivery_reconciliation_pending(run, *, coordinator_root)` 保留 review 或 ship+done phase gate、coordinator-root journal path、最小 payload 判準、malformed/missing-file fail-soft 與 raw JSON 語意，不委派嚴格 helper。現存最小 payload 正例仍回 True，但嚴格 helper 對同一資料必須回 False。新 claim/Manager admission 與 proof oracle 只看嚴格 helper，不得拿舊 reconciliation 的 True 當已合併 run 的新 admission authority；亦不引進整份 journal strict-loader。

### D3 proof oracle 重讀並重驗原 authorization

Oracle 使用現有的 authorization identity/hash 與 trusted evidence verifier，路徑在 Manager 控制下傳入；不接受呼叫者只傳「已驗證」布林值，也不將 journal 中已序列化 payload 當成檔案證據本身。

- 讀回 immutable authorization wrapper，要求 wrapper/payload/hash 精確相等且 canonical hash 重算一致。
- 精確比對 run/repo/work/steps/head/tree/PR/change/todo/review identity。
- 原 authorization 的 authority digest 代表 merge 當時的授權。archive 後目前 WorkAuthority 可能已因 #961 正常前進；因此允許原始 digest 與目前 digest 不同，但要求它是格式正確、hash 保護的原始值且不得被覆寫。current authority 和原 authorization 各自保留用途。
- 以既有路徑重驗 foreign-review、preflight/checks、Copilot 或 maintainer-review proof；任一證據不完整即拒絕。

### D4 僅接受同一 PR、同一 candidate 的 GitHub merge ancestry

Oracle 將 PR number、candidate head 和 merge commit 綁回同一 authorization/journal/WorkRun，再使用既有 GitHub terminal/closure evidence 重查：

- 同一 PR merged，PR head 精確等於 candidate。
- 遠端 merge commit 等於 journal 記錄值。
- candidate 是該 merge commit 的 parent，且 merge commit 是 default branch ancestor。
- provider/source 無法證明同一 PR 或狀態 degraded 時拒絕。

不把 PR/issue closed 當 merged proof，不使用另一 PR 的 merge facts，也不將 journal phase 當權威。required issue、OpenSpec、Todo 和 CompletionRecord 完整條件由 #977 使用 proof 結果後另行檢查。

現行 `ShipOrchestrator.verify_remote_closure()` 會寫 CompletionRecord，不能由本票的唯讀 oracle 直接呼叫。以 read-only remote facts/ancestry 查詢取得 PR/head/merge parent/default-branch ancestor 證據；不可把 `completion_record_valid` 偽設為 True 來套用完整 terminal gate。#977 才負責完整 closure validator 與寫入。

### D5 結果區分「不符合候選」和「候選已知 merged 但 proof 不足」

Helper false 表示沒有可用的 admission binding：review mismatch 沿舊 authority-restart；舊 reset run留在既有 verify/needs-human 行為。Helper true 但強證據重驗失敗表示已知 candidate 不能安全再 dispatch；Manager 回診斷 stop 並結束本次 resume/tick，不走一般 pending verify path。

Proof 成功只回傳帶 run/head/authorization hash 綁定的私有暫時結果，交給同次 Manager closure 流程。Proof 失敗不輸出該結果；不得 fallback 到「只依 journal」或「重新派 verify」。

### D6 本票不寫 durable state

新 helper、admission branch、proof oracle 都只作讀取及控制流程選擇。禁止在本票呼叫 registry mutation、CompletionRecord writer、OutcomeStore append、gate/step writer 或新增 proof persistence。#976 僅負責 registry CAS primitive；#977 才能把有效 proof 接到 CompletionRecord/outcome/done sequence。這個邊界讓 proof 成功也不會被誤解為全流程已完成。

### D7 以失敗注入與 side-effect spies 驗證安全邊界

用 temporary journal/auth/evidence files 與 stubbed GitHub provider 建立可重現 fixtures。測試將 claim、嚴格 helper、舊 reconciliation 和 Manager pre-dispatch route 分開，包含舊 helper=True 而嚴格 helper=False 的最小 payload 正例；以 mutation spies 證明新 admission 不呼叫 registry/write APIs。各 proof family 至少有一個正例及身份/資料/來源不符負例。避免在本票端到端地建立 CompletionRecord，該流程由 #977 驗收。

### D8 父票分工與五維 sizing

#975 在 #962 三切片中只擁有 R1/R2 admission、R3/R4 admission-binding、R6 的原始 authorization/trusted/remote merge proof，以及 R8(a)–(d) 的 admission/proof cases。#976 擁有受限 registry transition；#977 擁有完整 closure、CompletionRecord、outcome-first、terminal write、crash/re-entry 和 aggregate integration。#961 保有 R5/R7/R8(e)–(f)。

Sizing 按 repo 當前純 helper 計算，不以驗收案例數估計 mechanical dimensions：

| 維度 | 分數 | 推導 |
|---|---:|---|
| domain_breadth | 1 | 本票 production touchpoints 為 work_actions.py、manager.py 兩個模組，仍在同一 coordinator 領域。 |
| state_consistency | 1 | 只讀跨 journal、immutable authorization/trusted evidence、WorkRun/WorkAuthority 與遠端 GitHub facts 的精確關聯；本票沒有 durable write、CAS 或跨-store restart window。 |
| acceptance_surfaces | 2 | fix-standard core gate_spine=2；適用 R-09/R-16/R-19 共 3 條，signal=5，helper 映射為 2。 |
| spec_stability | 0 | spec/design/plan 三個種類皆 accepted，無缺項、拒收 artifact 或 blocking marker。 |
| orchestration | 2 | fix-standard 有 9 cards，9 個 card 均有 persona_binding。 |
| **總分／band** | **6 / Yellow** | **1+1+2+0+2=6；Green 0–3，Yellow 4–6，Red 7–10。** |

輸入已按現行 repo 的 `planning.compute_sizing_score`、`ACCEPTANCE_SURFACE_RULES` 和 `fix-standard` combo 核對。若實作新增 durable proof/state、registry transition、CompletionRecord/outcome writer，或擴到第三個 production module，必須重新宣告 dimensions、重算 sizing；本票不能吸收 #976/#977 工作來維持 Yellow。#962 全 scope 的 7/Red aggregate 不因三張 Yellow child 而改變。

## Rejected alternatives

- 只在 mismatch 分支加 guard：無法攔截 claim key 已同步的 verify-reset 舊 run。
- 把 review/verify phase 放進 journal helper：會讓新舊入口無法共用同一 binding，並把 admission parser 與 routing state 混在一起。
- 依 PR closed 或 journal merged/done 放行：兩者皆不能證明原 Manager authorization 與 exact merge ancestry。
- 要求原 authorization digest 等於 merge/archive 後的 current WorkAuthority digest：會錯誤拒絕合法 authority refresh。保留原 hash-bound digest，current authority 留給 #977 CompletionRecord。
- proof 不足時派 verify：對已知 merged candidate 重派會重現本 issue 所修事故。
- 在 claim 或 proof oracle 直接建立 CompletionRecord/outcome/done：越過 #976/#977 的 writer ownership，擴大 durable state consistency 與 recovery 範圍。
- 對整份 delivery journal 使用既有 strict loader：可能因不相關 row 的歷史格式改變既有 admission 結果；本票只驗當前 run 的 raw JSON row。

## Operational boundary

本設計不修改 repo、GitHub issue 或 runtime；規格文件只在指定 intake 目錄。實作必須等 #961 合併，使用新 WorkAuthority load semantics，再由 #962 做跨切片 R8(a)–(d) 的完整驗收。完成 #975 只代表 admission/proof slice 通過自身 tests/review/merge，不代表任何 run 已完成或 #962/#887 可關閉。

## #977 consumer proof 組裝

私有唯讀 oracle 回傳 immutable `run_binding`、`work_authority_binding`、`merge_authorization`、`step_bindings`、`gate_bindings` 與本地 `completion_record_inputs`，各欄依同 run/claim/head 的 Registry、原始 Manager 授權、job 與 trusted evidence 重新讀取並驗 hash。reset 後 pending/cleared 的 step/gate 不是歷史 passed 證據；job-backed step 缺少唯一成功 job／可信 evidence、Manager-only step 缺少原生 durable provenance 時只回 typed stop。不得只回 passed 布林值或讓 #977 補造 proof。

`target_ref_sha` 不由本 oracle 回傳：#995 唯讀 inspector 每次 fresh GET default head，#977 才把它寫入記憶體 CompletionRecord draft。`completed_at` 由 #977 對既存 record 固定沿用或在新建時產生。每個 durable boundary 前由 #977 重新載入 current WorkAuthority/source revisions 與本 oracle；此切片不寫 CompletionRecord、OutcomeStore、Registry、journal 或 workspace。step ID 依原授權 `run_id:phase:card` 規則核對；job-backed step 以 run+phase+card+claim era+head 關聯唯一成功 job，不發明 registry.workflow_step_id 欄。若現有 durable evidence 不足以建立合法 terminal steps/gates，#975 proof 必須拒絕而不能把缺欄責任轉嫁 #976/#977。
