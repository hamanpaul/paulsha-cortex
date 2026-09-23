---
status: accepted
work_item: authorized-merged-run-proof-admission
---

# 已授權合併 run 的 admission 與強證據 oracle 規格（#975）

## 需求 authority 與執行順序

本規格只對應 live issue [#975](https://github.com/hamanpaul/paulsha-cortex/issues/975)，父票為 [#962](https://github.com/hamanpaul/paulsha-cortex/issues/962)，祖先 issue 為 [#887](https://github.com/hamanpaul/paulsha-cortex/issues/887)。#975 是 #962 的第一個實作切片，只承接 admission、phase-free journal binding 和唯讀強證據驗證；不承接正式 completion 或 terminal state write。

先完成並合併前置 [#961](https://github.com/hamanpaul/paulsha-cortex/issues/961)，讓 WorkAuthority 可依精確 GitHub merge evidence 收斂 local active／GitHub archived 衝突。後續順序為 #975 → [#976](https://github.com/hamanpaul/paulsha-cortex/issues/976) → [#977](https://github.com/hamanpaul/paulsha-cortex/issues/977)。#976 只新增受限 registry transition；#977 才建立 CompletionRecord、shipped outcome 並完成 Manager orchestration。三切片合併後仍回到 #962 驗收完整 R1–R4、R6、R8(a)–(d)；#962 與 #887 均不得由 #975 單獨關閉。

## 問題與現況證據

#887 現場 run `workflow-ced42c7b999df8bc222d` 已由 PR #884 合併。兩條失效入口不同：

1. 新 review run 的 WorkAuthority 前進後，claim key 不符，現行 `_claim_action` 會做 authority-restart reset。
2. 舊 run 已被 reset 到 verify：reset 已同步 claim key、重設 verify/review gates、清除非 brainstorm refs、移除 verified_head，並標記 `retry_classification=authority_restart`。因此它不再命中 claim-key mismatch 分支，Manager 會將 pending verify step 送進一般 dispatch。

現行 Manager reconciliation 只接受 review 或 ship+done；verify-reset run 沒有安全 admission/proof route。現有 reconciliation 正例允許僅含 run_id/repo/work_id/head 的最小 authorization payload（`tests/test_workflow_production_wiring.py::test_post_merge_closure_skips_active_planning_path_reconciliation`）；新 admission 必須驗更完整的 binding，因此兩種判準須分開。journal 的 merged/done 只能作為候選線索；它本身不是 authorization、gate pass 或 completion 證明。

2026-09-23 對 checkout `main @ ea3f81eff45be532d7f99155399b02d4e37be1a0` 的唯讀核對：

- `work_actions.py:1723–1734` 的 `_claim_action` 收到 `state_path`；:1913–1959 的 mismatch 分支會呼叫 registry reset，且目前只看 claim-key mismatch。
- `work_actions.py:598` 的 `_run_state_path()` 回傳 coordinator `delivery-journal.json`；work-action 將該同一個 Path 傳進 `_claim_action`（:580–588）。
- `manager.py:11407–11452` 的 `_merged_delivery_reconciliation_pending` 目前只接受 review 或 ship+done，並讀 coordinator root 下的 journal；:11623 起呼叫該 helper，:11715 起檢查 current step 並可能進入 dispatch。
- `registry.py:2769–2844` 的 authority-restart reset 會同步 claim key、清空 verified_head 並將 retry classification 設為 authority_restart。
- `work_actions.py:1011–1130` 已有 authorization wrapper/hash/identity 檢查；新 oracle 應沿用現有驗證器，不另造較弱路徑。
- 目前 live #961、#962、#975、#976、#977、#887 均為 OPEN；#975 blocked by #961，#976 依賴 #975，#977 依賴 #975 與 #976。以上是規格基線，不代表這些行為已修正或 runtime 已驗證。

## Requirements

### R975.1 嚴格且 phase-free 的新 admission binding

新增 Manager 私有 helper，契約為 `manager._merged_delivery_journal_bound(run, *, journal_path)`，使用呼叫端傳入的精確 journal path，且不以 run phase 作前置條件。只檢查 admission 所需的 journal binding：

- journal 是 non-symlink regular JSON file，讀取或 decode/JSON 解析失敗時 fail-soft 回 False。
- schema 恰為 `cortex-delivery-journal/v1`；以同一 row 精確比對 run_id、repo、work_id。
- ship phase 是 merged 或 done，ship.head 等於 run.candidate_head，merge_commit 符合 40-hex SHA。
- merge_authorization locator/path、64-hex hash 與 payload 必要欄位完整；payload 的 run/repo/work/head、workflow_step_ids、PR number、change、todo paths 均與該 run 及 delivery binding 相符。workflow_step_ids 依現行 `_delivery_journal_row()` 的 `run_id:phase:card` 規則由 `run.steps` 推導；不得只與同一 journal row 中可一併偽造的欄位互比。
- helper 不驗證遠端、不寫 state，也不將 journal phase 映成 verified/passed/done。

既有 `_merged_delivery_reconciliation_pending(run, *, coordinator_root)` 保留原本的 review 或 ship+done phase allowlist、最小 journal 判準、raw JSON 行為與既有呼叫結果；不委派新的嚴格 helper，也不擴大其可接受的 phase。它對現存最小 payload fixture 仍須回 True；同一 fixture 對新 helper 必須回 False。新 review mismatch、舊 verify-reset 與 proof oracle 只能使用嚴格 helper 的結果作 admission，嚴禁將舊 reconciliation 的寬鬆 True 當作新 admission、proof 或 terminal authority。兩者均不得改用 `work_actions._load_runs` 對整份 journal 作更嚴格的 row 驗證。

### R975.2 新 review mismatch 的安全 admission

當 `_claim_action` 找回 ongoing、phase review 的 run，claim key 與目前 authority 不符，且 R975.1 對本次傳入的 `state_path` 完整命中時：

- 在呼叫 `_manager_reset_workflow_for_authority_restart` 前攔截，回傳 action=resume、reason=`merged-delivery-closure` 及同一 run。
- 不重寫原 `retry_classification`，不呼叫 retry classifier，不寫 registry、不派 verify job。
- 記錄一行 info log，含 run_id 與 candidate_head；不得輸出 authorization path、workspace path 或敏感 payload。
- 若 binding 不成立，維持既有 authority-restart fallback；journal I/O/格式錯誤不得使 claim 擲出未處理例外。

### R975.3 舊 verify-reset run 在一般 verify dispatch 前攔截

只把以下 run 納入舊 run 候選：status ongoing、current_phase verify、retry_classification=authority_restart、claim key 已符合由目前 WorkAuthority 計算的 expected key，且 R975.1 對同一 run/head 有完整 merged binding。

此候選必須在 Manager 取一般 pending workflow step 並 dispatch 前送入強證據 oracle。命中後回傳/轉入 `merged-delivery-closure` route；claim/admission 不寫 registry、不改 gates、不補 verified_head、不標 done、不新增 verify job。缺少完整 binding 時維持既有 verify／needs-human fallback，不能把任意 verify run 誤認成 recovery 候選。

### R975.4 重驗原始 Manager authorization 與 trusted evidence

Manager 私有 proof oracle 必須重新讀取 journal row 及其 authorization evidence，並重驗同一份 WorkRun/PR delivery binding：

- authorization 是 non-symlink、non-writable regular file；讀取的 wrapper 正好是 `{payload, hash}`，payload canonical JSON hash 等於記錄 hash。
- authorization schema/version、run_id、repo、work_id、workflow_step_ids、head、tree_hash、PR number、change、todo paths、review binding 都與該 WorkRun、journal 及 delivery binding 一致。
- authority digest 屬原始 merge authorization。因 merge/archive 會推進目前 WorkAuthority，原 digest 可不同於目前 digest，但必須保留原始 payload/hash、格式為 64-hex，不能改寫成目前 digest冒充原授權。#977 會以 #961 收斂後的目前 WorkAuthority 建立最終 CompletionRecord。
- 沿用現有 verifier 重驗 authorization 引用的 foreign-review、preflight/checks，以及 Copilot 或 maintainer-review evidence。任一證據缺失、損壞、hash/identity 不符、不可重驗或 review authority 不明確即拒絕 proof。

### R975.5 重查同一 PR 的強 merge facts

Oracle 必須以 journal 與 authorization 綁定的同一 repository/PR 重新讀取 remote facts，且全部成立：

- PR 狀態為 merged；PR head 精確等於 candidate_head；merge commit 精確等於 journal merge_commit。
- 由既有 closure/Git ancestry verifier 證明 candidate 是該 merge commit 的 parent，且 merge commit 是 default branch ancestor。
- remote provider 回應有效且資料指向相同 source/PR。provider degraded、錯 PR、缺 merge commit 或 ancestry 不成立均拒絕。
- PR closed、issue closed、journal 的 merged/done 字串或另一個 PR 的 merge facts都不能替代這條 proof chain。

Oracle 僅使用 read-only remote facts/ancestry 查詢；不得呼叫現行會在 `delivery.py` 寫入 CompletionRecord 的 `ShipOrchestrator.verify_remote_closure()`，也不得為通過 `evaluate_remote_closure()` 而偽造 `completion_record_valid=True`。required issue、OpenSpec、Todo 與正式 completion gate 留給 #977。

本切片只輸出 merge authorization/evidence/ancestry 的 proof-bound admission；required issue、OpenSpec archive/active absence、Todo 與完整 CompletionRecord closure 由 #977 的 finalizer 依 #962 R6 驗收。

### R975.6 強證據不足時 fail-closed，分辨 fallback

- journal 缺失、格式錯誤、phase 未 merged/done、run/head identity 不符或必要 locator 不完整：新 mismatch 沿用既有 authority-restart；舊 verify-reset run 沿既有 verify／needs-human 路徑。
- 完整 merged binding 已指出同一 run/head 已 merge，但 authorization、trusted evidence、remote provider 或 ancestry proof 不成立：回傳可診斷 stop，不得 dispatch 已知 merged candidate、宣稱完成或將該 run當一般 verify 重派。
- proof 成功也只授權內部 Manager closure 路由，不直接產生任何正式完成狀態。

### R975.7 Oracle 與 admission 不寫 terminal state

本票新增的 admission/proof 路徑是唯讀：不建立 CompletionRecord、不 append engineering outcome、不呼叫 registry mutation、不改 WorkflowRun/gate/step/verified_head、不持久化 proof、不新增 CLI/API。proof result 只能是 Manager 內部、當次流程使用的暫時結果，至少綁定 run_id、repo/work_id、candidate_head、authorization hash、PR/merge commit 與已驗證的 evidence identity；下游 #977 必須再次確認該結果仍對應同一 run/candidate。

### R975.8 測試與回歸

在 #961 合併後，將本切片案例加入 `tests/test_post_merge_authority_restart_guard.py`，使用 temporary files 與 stubbed providers，不依賴 GitHub network：

- 嚴格 phase-free helper 正反例；精確傳入 state_path；原 reconciliation review、ship+done phase contract、最小 payload 正例和 raw JSON fallback 不變。明測「舊 helper=True、新 helper=False」時新舊 admission 均不得放行。
- 新 mismatch 正例：closure reason、retry classification 不變、無 authority reset、無 registry mutation、無 verify dispatch、info log 含 run_id/candidate_head。journal 不完整時舊 reset 行為保留。
- 舊 verify-reset 正例：只接受 ongoing/verify/authority_restart/已同步 claim key；有效 proof 在一般 pending verify dispatch 前攔截。
- Authorization 正例與 missing/symlink/writable file、wrapper/hash、steps/PR/head/tree/change/todo/review binding 錯誤負例。
- Trusted evidence 缺失或驗證失敗、foreign review/Copilot/maintainer review 身分錯誤負例。
- Remote PR 未 merged、PR head/merge commit 不符、candidate 不是 merge parent、merge commit 非 default branch ancestor、provider degraded 或 source/PR 不同負例。
- proof 成功與失敗都不呼叫 CompletionRecord/outcome/registry writer；完整 binding 但 proof 失敗時不派已知 merged candidate。
- 保留既有 `test_work_actions_retry_invalidation.py`、`test_workflow_production_wiring.py`、`test_work_actions.py` 與 `test_work_claim.py` 相關原斷言；#962 後續增補終態/冪等整合測試，不由本票代替。

## #887 完整 AC 與子票分工

| #887 AC | 本票 #975 | #962 其餘 owner |
|---|---|---|
| R1 | 新 review mismatch admission、保留 retry classification、避免 reset | #977 接既有 review→ship closure 並整合驗收 |
| R2 | 辨識舊 verify-reset 候選並於一般 verify dispatch 前攔截；只驗 proof | #976 提供受限 registry transition；#977 寫入 completion 並收尾 |
| R3 | journal/auth/evidence/remote proof fail-closed；已知 merged 不重派 | #976 守 CAS/零 partial terminal write；#977 守零 CompletionRecord/outcome/done |
| R4 | 嚴格 phase-free admission helper、傳入 state_path；舊 reconciliation 判準與 phase contract 原樣保留且不得作新 admission authority | #962 保留全整合回歸 |
| R5 | 不負責 | 前置 #961 依 exact remote evidence 收斂 active/archived authority |
| R6 | 只負責原 Manager authorization、trusted evidence、PR merge/ancestry proof | #976 負責 exact registry CAS；#977 負責完整 closure、CompletionRecord、outcome-first 與 re-entry |
| R7 | 不負責 | 前置 #961 保留其他 conflict 原錯誤 |
| R8(a) | 新 mismatch admission/no-reset/no-verify 的案例 | #977 驗既有 closure 與 outcome 整合 |
| R8(b) | 舊 verify-reset 的 admission 和強 proof 正例，不宣稱終態完成 | #976 驗 transition 正例；#977 驗 CompletionRecord、done/ship 與恰一筆 outcome |
| R8(c) | authorization/evidence/remote proof 負例及零 dispatch/零本票 writes | #976 驗 CAS 負例；#977 驗零 outcome/done 和重試結果 |
| R8(d) | journal 缺失/損壞/未 merged/identity 不符的舊新 fallback | #977 驗整合層可診斷 stop 與不重派 |

#887 完整 R1–R8 和 #962 R1–R4/R6/R8(a)–(d) 仍是 aggregate acceptance；本表只固定 ownership，未交給 #975 的驗收不得刪除或視為完成。

## Boundary

- Production scope 僅 `paulsha_cortex/coordinator/work_actions.py` 與 `manager.py` 的 admission/helper/oracle routing；registry、completion writer、OutcomeStore 為唯讀邊界或既有下游契約，不在本票修改。
- #961 的 claim.py active/archived arbitration 不複製；本票只能在 #961 合併後，以收斂後的 WorkAuthority 執行整合驗收。
- 不處理 #847 merge 前 authority-restart、#885 archive-applied、#882 一般 verify operator 出口、merge-authorized crash window、無本 run Manager authorization 的外部 merge、monitor projection follow-up 或 retire-delivered 語意。
- 不放寬通用 verify/ship validator，不新增 persisted schema/欄位、CLI 或使用者可呼叫 proof endpoint。
- 實作 PR 依 repo policy 更新 changelog fragment、CHANGELOG [Unreleased]、`docs/unified-work-lifecycle.md` 並以 PR context 執行 policy/test gates；這些是後續程式碼交付，不是本次 tmp 文件寫入。
