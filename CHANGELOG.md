# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.17。

## [Unreleased]

### Fixed
- **Issue #536（後半）：planning artifacts 發佈與 run 狀態更新不是同一事務，中間態對
  所有恢復迴圈永久隱形**——define 的 brainstorm 有兩次分離的 durable 寫入：先發佈
  spec/design/plan 到 operator worktree，再把 run 推進到 `plan` 並寫入 gate_refs／
  planning_authority。兩者之間崩潰就留下「artifacts 已落地、run 狀態停在原地」的中間
  態。journal（`planning-transactions/<run_id>.json`）本來就記了 before/after hash，缺
  的是**誰去看它**：`reconcile()` 只能由持有該 run 的呼叫端逐 run 觸發，run 一旦離開
  `ongoing`（superseded／done）就再也沒有任何迴圈會碰它——實測 coordinator root 上就
  躺著兩份孤兒 journal，其中一份正是 #536 現場的 `workflow-7a430d31eff66ef13630`（run
  已 abandon 成 superseded，兩份 spec/design 殘留檔永久留在 operator worktree，成為
  下一世代 define 撞 #416／#535 authority fail-closed 的地雷）。修法：新增
  `prepare_commit()` 把事務邊界寫成 durable 事實（journal schema v3 的 `phase`，
  `prepared` 之後不得再發佈）；新增 `reconcile_planning_transactions()`——掃整個 journal
  目錄、與 run 狀態無關的**唯一**恢復路徑，由 tick 驅動，判準只有一條「registry 的 run
  row 上有沒有這次的 brainstorm gate ref」：有則前滾（逐位元組驗證後退役 journal）、
  沒有則回退（選回退的理由是沒有 gate ref 就代表這批產出從未被綁進任何 run，前滾在語意
  上不成立；現場殘留的 `expected_gate_ref` 更是 `null`，根本沒有可前滾的目標）。既有
  v2 journal 相容，因此實際部署的殘留與未來崩潰走同一條路自癒。護欄：未滿 5 分鐘的
  journal 視為可能仍在飛而不碰；已被 git 追蹤的殘留檔跳過刪除並回報 `adopted`（#507
  教訓）；找不到 run row 時 fail closed 只回報不刪檔；sweep 整批失效比照 #246 降級不
  癱瘓 tick。收斂結果全部落結構化 log 並進 tick summary，drift 且 run 仍 ongoing 時補
  `needs_human` facet。不動 #538 已修的 resume 迴圈 phase filter。詳見
  `changelog.d/publish-state-transaction.md`。新增
  `tests/test_planning_publication_transaction_536.py`（14 個回歸測試）。

- **Issue #540：tdd-red terminal 採信三段連鎖——builder 的正確 RED commit 無法被採信**
  ——run `workflow-084f75e2178cf7547476` 的 builder 交付了合格 RED commit，terminal
  採信卻連撞三個獨立缺陷。**(1) gate 宣告缺漏事前無診斷**：manager env 漏
  `PSC_GATE_CMD_PYTEST` 時 ledger 是 `gates: []`，帶 `test_policy` 的 build 卡必然以
  `gate-ledger-missing-expected-gate` fail closed（正確的反自證行為），但錯誤只進
  `manager.log`；新增 `cortex doctor` 的 `gate-declarations` probe，以 packaged deck
  每張卡的 `test_policy` 經 `terminal_contract.expected_gate_names_for_test_policy`
  （與 harvest 端同一判準，實作自 manager 移入、manager 保留薄轉呼叫）導出應驗 gate
  集合並比對宣告，未涵蓋或宣告不合法皆為 required fail。**(2) ledger 凍結後無官方
  重驗路徑**：`resume` 只重讀舊 ledger 再拒一次、`retry-build` 只受理最後一張 builder
  卡（tdd-red 是中段卡）、`recover-pre-candidate` 需 null candidate，operator 只能手動
  跑 gate_ledger CLI；新增 `regenerate-gates` work action，以 exact WorkflowRun CAS
  對既有 builder job log 依當前宣告重跑 gate、原子覆寫 ledger，**不改判**——不重派
  模型、不動 commit、不改任何 run 狀態，run 仍停在 needs_human 由既有 `resume` →
  harvest 重新評估。**(3)【主修】dispatch prompt 從未告訴模型 canonical gate 名稱**：
  採信要求 envelope 自報的 gate 名 ⊆ ledger 的 gate 名（由 `PSC_GATE_CMD_<NAME>` 導
  出），prompt 卻只寫 `"gate name"`，模型自由發揮寫了 `'focused pytest RED
  expectation'` → `gate-evidence-unknown-gate` 必敗（與 `#486` 同構）；比照 `#521`
  改為機械生成：`gate_ledger.declared_gate_names()`／`ledger_gate_names()`／
  `gate_evidence_name_hint()` 由 `load_gate_specs()`（ledger gate 名的唯一產生處）導
  出，`_workflow_job_prompt` 新增 `env` 參數（與 launcher 交給 ledger writer 的 env
  同源）把 `allowed_names` 與說明注入 `terminal_schema`，prompt 端不再持有第二份真實
  來源。另補 `#307` 反轉語意的 prompt 面：`red-required` 卡附
  `red_required_policy`（由反轉判準常數產生），明示「ledger 顯示 pytest failed 時仍回
  `status=passed` 並誠實自報 `pytest: failed`」，消除泛用 `status_policy` 與實際採信
  規則相反的字面陷阱。詳見 `changelog.d/gate-acceptance-chain.md`。新增
  `tests/test_gate_acceptance_chain_540.py`（15 個回歸測試）與 2 個 doctor 整合測試。

- **Issue #507：planning 失敗時整棵 operator worktree 抹除還原，靜默銷毀並行的 operator
  工作（實測資料遺失）**——`planning_runtime._invoke_json` 的 finally 區塊只要偵測到
  T0→T1 之間 operator worktree 有任何差異，就呼叫 `_restore_operator_tree()`：刪光
  worktree 內除 `.git` 以外的**全部內容**再從 T0 baseline 整棵還原。偵測條件
  （`_tree_snapshot` 前後比對）分不出「launcher 越界寫入」與「operator／其他 agent／
  編輯器的正常並行編輯」，而 launcher 早已以 `cwd=sandbox`（拋棄式複本）執行——安全網的
  補救動作被設成整棵樹抹除，誤傷機率遠高於它要防的越界；baseline 又由非原子 `copytree`
  取樣，歸因本身就不可靠。Phase 1 dogfooding 兩次實測命中：(1) run
  `workflow-0529388d8e290c8fb938` 抹除 operator 在視窗內新建的
  `docs/superpowers/workstreams/<slug>/todo.md`，連帶讓 `.cortex/work-items.yaml` 留下
  懸空連結、`active_todo` 為假、lifecycle 停在 `topic` 不可 claim；(2) 更嚴重的形態是
  **被抹除的是 cortex 自己的成功產出**——前一代 planning 產出的三份合格 artifact 屬未追蹤
  檔、不在後續那次的 baseline 內，遭下一次失敗的 rollback 刪除，run 的
  `planning_authority` 隨即指向不存在的檔案（`workflow planning input missing`），work
  item 卡死且 git 救不回。R0 修法四項：整棵還原的程式路徑**移除**
  （`_restore_operator_tree()` 刪除，`_make_tree_traversable()` 收斂為只能指向拋棄式
  sandbox）；drift 分析改走全新的**唯讀且對讀取失敗容錯**的 `_tree_manifest()`／
  `_diff_tree_manifests()`，預設不改寫 operator worktree 一個位元組；受影響檔案的 T0／T1
  兩版**完整備份**進 `<coordinator_root>/evidence/planning-worktree-drift/<run_id>-<digest>/`
  並落一份 `cortex-planning-worktree-drift/v1` 結構化 diff 報告（失敗訊息帶計數與 evidence
  路徑，另落完整 `logger.error`）；還原改為需明示 opt-in 的逐路徑 `rollback_scope`，經三道
  fail-closed 閘門把守——不在本次 diff 內、命中受保護的權威文件
  （`docs/superpowers/{workstreams,specs,plans}/**`、`openspec/changes/**`、`.cortex/**`）、
  備份未成功者一律拒絕還原（**備份不成功就不准抹除**）。`manager.apply_workflow_action`
  把 `evidence_root` 與 `run_id` 交給 runtime factory，operator 得以用同一組 run_id 同時撈
  `planning-recovery`／`planning-artifacts`／`planning-worktree-drift` 三份 evidence。
  結構解（planning 產出完全不進 operator 樹）、baseline 取樣的非原子 race、planning 期間的
  advisory lock、以及 worktree drift 仍被分類為 `content` 而禁用 `recover-planning`，
  均留待後續。

- **Issue #478／#535（前代殘留回收族）：worktree registry 原子回收與 planning evidence
  世代隔離**——`#478`：`recover-pre-candidate` 只刪 build worktree 目錄、未清 git worktree
  registry，下一 tick 的 `git worktree add` 立即以 `cannot force update the branch ...
  used by worktree at ...` 失敗，slice 被打回 `needs_human`。根因是兩份各自手寫的回收
  片段——`manager.apply_slice_action` 在 `dispatcher._git_runner` 為 `None`（生產合法狀態）
  時整段跳過 git 清理，且呼叫 seam 時多塞前導 `git`；`work_actions._recover_pre_candidate_action`
  用裸 `subprocess.run` 無 `-C <repo>` 又 `check=False` 吞錯；兩者皆只在「目錄還在」時才
  清理，「目錄已消失但 registry 殘留」的既存壞狀態永遠自癒不了。新增
  `coordinator/worktree_reclaim.py` 收斂為單一回收函式：後置條件（目錄不存在 ＋ registry
  無該筆）驗證不過即 fail closed 不回 `ok`；registry 探測先於目錄探測以支援自癒
  （`worktree remove --force` 失敗再以 `worktree prune` 兜底）；dirty 內容先封存到
  `evidence/worktree-reclaim/` 再刪，封存失敗即拒絕刪除（對應未追蹤 `.project-policy.yml`
  被靜默刪除的回報）；並設安全閘拒收「非 linked worktree」的路徑，避免陳舊 job 記錄
  （實測 `job.worktree` 會等於 run 的 `workspace_root`）讓回收遞迴刪掉主 checkout。
  `abandon`（supersede）路徑改走同一支回收函式，補上 `#527` 根因之一的「supersede 不回收
  build worktree」。`#535`：brainstorm evidence 的 content-addressed 檔名原本只由
  `(scope, question_pack_id)` 決定，前代 abandon 後殘留檔佔住同一落點，下一世代 byte 不同
  即撞 no-clobber fail-closed。改為命名空間帶 run identity
  （`brainstorm-<run_id>-<hash>.json`，run_id 亦進 hash 輸入）——取捨為不搬動前代 evidence，
  因為搬檔會讓前代 run 逐字記錄的 `gate_refs`／`evidence_refs` 絕對路徑整批懸空，違反審計
  不可變原則；世代內的衝突偵測維持 fail closed。no-clobber 錯誤訊息另附
  `existing owner=/mtime=/publishing run=`，operator 不必再挖 mtime 對時間軸。

- **Issue #499 #500 #487 #485：三套 outcome 分類器收編成單一 taxonomy 模組**——
  「executor 失敗該歸哪一類」在 planning／build／review 三個 lane 各自實作、各自漂移，
  同型缺陷已第六次命中。根因不是關鍵字表寫錯，是餵進表裡的東西一開始就不該進來
  （nested tool result、init metadata、CLI banner），或該當證據的結構化終局記錄被忽略。
  新增 `coordinator/outcome_taxonomy.py`：四大類 outcome family（transient-service／
  content／environment／auth）＋共用 markers 表＋證據分層，三個 lane 共同消費；#533 的
  planning 先行實作一併收編。四張 issue 各自的誤判：#499 Claude review 429 被投影成
  `foreign-review-absent`／`provider_outcome` null，改以結構化權威落 `rate_limited` 並
  保留權威重置時刻（新增可選欄位 `provider_outcome.reset_at`）；#500 nested tool result
  的 `timeout` 字樣被判 network transient，改由證據分層排除、終局 `aborted_streaming` 落
  `unknown`；#487 init skill 清單的 `doc-coauthoring` 命中無界 `oauth` 被判 auth，收緊為
  `\boauth\b` 並排除 init metadata；#485 Codex stdin banner 讓每次 foreign review 都成
  `invalid-process-output`，改為 parse 前只剝離精確、位於串流開頭的已知 banner。
  各 lane 對每類 outcome 的後續處置（retry／needs_human／終止）維持現狀。
- **R0.5 D1（部分）：auto-claim label 判定改走 monitor 鏡像**——monitor 把持有
  `cortex:auto-on-going` 的 open issue 編號寫進 provider observations（issues 回應本來就含
  labels，零額外 API）；canonical claim 路徑據此導出 `auto_label`（原硬編 False）；
  auto-claim scan 廢除每 tick O(mapped issues) 的 live label sweep（實測 57 次/tick），
  鏡像 False 零 API、鏡像 True 僅一次 targeted 複驗（以 live 為準、確認即 early-break）。
  observations 缺失一律保守 False。
- **Issue #536（最小修）：define 階段的 ongoing run 不再對 tick resume 迴圈隱形**——
  phase filter 納入 `define`；stalled define run 每 tick 由 `resume_workflow_run` 接手
  （其本已支援 define：reconcile planning transaction → dispatch planner 卡）。
  needs_human 縱深防禦守衛不變。
- **#518 workstream todo scope 修正**——初版誤把 installer 已具備的 config-root 派生當成
  待實作項；收斂為 legacy env 遷移＋exact-project workspace 語意＋doctor 共用 root 告警。
  舊世代 run 已結算，此為換代邊界的 todo 修正。
- **planner launcher 暫時性服務失敗被判 `content` 死路**——agy 暫時性 503 會印錯誤文字但
  exit 0，launcher parse 不到 JSON 即以 `content` 分類收場，而 `content` 禁用
  recover-planning：自癒型服務錯誤變永久死路。修法：`_extract_json` no-JSON 失敗帶 stdout
  截斷片段（503 當場可見、不再隨 temp_dir 丟棄）；新增
  `_is_planning_transient_service_failure` 判準（比照 #416 殘留例外），服務層暫時性樣態
  改判 `environment` 使 recover-planning 可用；內容不從維持 `content`。
- **Issue #523：degraded 保留分支的 ownership collision 讓 work model refresh 永久失敗**
  ——`monitor/lifecycle.py` 的保留分支只比對 work_id、不比對 sources，source 歸屬由
  fallback work item 轉移到新宣告的 work item 時，舊 fallback 連同舊 sources 被整筆放回，
  兩者同時宣稱擁有同一個 source → `validate_ownership()` raise。而該例外發生在
  `WorkSnapshot.__post_init__`、早於 `replace_durably()`，那一輪算出的 provider 新狀態
  （含「backoff 已結束」）一併被丟棄，`previous` 永遠停在崩潰前那版、`degraded` 永遠為真，
  下一輪重演——provider 無法離開 degraded，因為記錄它恢復的那次寫入正是拋例外的那次寫入。
  修法：保留時剝除已被本輪認領的 source（全數被認領即整筆丟棄，原本無 source 者維持既有
  語意）；projection 驗證失敗降級為保留上一版 projection ＋ 讓 provider 觀測落地，並把
  失敗原因寫入 provider diagnostics。**成因更正**：先前記為「時序競態」有誤，真正的觸發
  條件是 `correlation.degraded`，亦即限流本身——`#506` 與本缺陷互鎖。
- **Issue #530：claim 的 GitHub provider 檢查是 repo 範圍且無條件，把一次 GitHub 可用性
  事故放大成整個 fleet 的派工停擺**——`_authority_from_canonical_row` 在驗完「必須有
  todo-kind 來源」之後，完全不看那些 source 由誰供應，直接要求 `github:<repo>` 為 `ok`。
  實測 2026-08-14 帳號遭 abuse-detection 封鎖 REST 期間，confirmed sources 只有一筆
  `kind=todo`／`provider=repo:...` 的 work item（`fix-instance-config-isolation`）被
  `provider-authority-rate-limited-canonical` 擋死——它要讀的 todo 就在磁碟上、內容從未變動。
  連「修 GitHub 壓力問題」本身都因此做不了，形成「限流 → 無法派工 → 修不了限流」的死結。
  修法為**收斂適用範圍而非放寬強度**：新增 `WorkAuthority.requires_github_authority`
  （預設 True），由 confirmed sources 的 `provider` 前綴（`github:`／`github-terminal:`）
  與 `kind` 判定；為 False 且 last-known-good 齊全時才豁免。`provider` 欄位缺席一律保守視為
  需要 GitHub 權威。同時修正第二層放大：`_authority_is_fresh` 不再以 GitHub 的 last-success
  時鐘判定不依賴 GitHub 之 authority 的過期。第三層（`reduce_lifecycle` 的
  `provider_degraded_freeze` 與 `hard_gates.auto_claim`）留待後續。
- **Issue #506：auto-claim scan 是 fleet 對 GitHub 最大的持續壓力來源，且不受
  `#512` 的節流閘門管轄**——`run_auto_claim_scan()` 對每個 `confirmed_todo` authority
  的每個 mapped issue 各發一次即時 `gh api` 讀 label。實測 cortex instance 有 57 個
  這樣的 issue，配上 `PSC_MANAGER_INTERVAL_SECONDS=30` 就是 **114 次／分鐘的連發**；
  而 PR `#512` 的 `GitHubPressureGate` 只注入到 `monitor/providers.py`，`coordinator/`
  這一側完全不受節流也不受退避管——monitor 進入退避時 manager 照打。實測把整個帳號
  推進 secondary 懲罰窗（`gh api rate_limit` 顯示 `core remaining 4991/5000`，同一條
  `--paginate` 請求 0.4 秒即 403），`provider-authority-rate-limited-canonical` 因而
  擋下所有 claim，形成「限流 → 無法派工 → 修不了限流」的死結。修法兩項：(1) 逐次讀取
  之間插入 `PSC_MANAGER_GITHUB_INTERVAL_MS`（預設 1000ms）的間隔，且**跨 authority
  累計**——secondary limit 綁 token 不綁 repo，per-authority 重置節流等於沒有節流；
  (2) 命中 rate-limit 型失敗即**中止整輪掃描**，其餘 authority 標成
  `github-rate-limited-scan-aborted`。舊行為是每個 authority 各自撞一次才 break 自己
  那圈，於是限流期間每個 tick 仍送出 O(authorities) 次必定失敗的請求，每一次都在延長
  懲罰窗——「越限流越打、越打越限流」的正回饋。非限流的讀取失敗維持舊語意（只擋該
  authority），以測試釘住。
- **Issue #524：planning 成功的 in-flight run 被自行 supersede，其產出又使後續
  世代 fail-closed**——`claim_key`／`run.source_revision` 都由
  `work_authority_digest()` 導出，而該 digest 折入 `source_revisions`；run 自己的
  `brainstorming`／`writing-plans` 卡把 spec/design/plan 寫進 governed roots 後，
  monitor 會把它們當成新的 confirmed source 併入同一個 work item，digest 因此改變、
  run 的持久化識別與「目前 authority 算出來的識別」再也對不上。`_claim_action()`
  的 active-run 偵測第二段 fallback 只在 auto-scan／explicit resume 時才跑，
  `start`／`intake` 整段跳過，於是把仍在 flight 的 run 當成陳舊世代，
  `_manager_create_workflow_run()` 再無條件把同 `(repo, work_id)` 的 ongoing run
  全部作廢（現場 `workflow-009fe9ab303df196209d` 四張卡全 passed、phase 已達 build
  仍被 90 秒後自行 supersede）。修法在 `_claim_action()` 補上不分呼叫端的 in-flight
  保護傘，判準為「`run.status` 與 `workflow_status()` 皆為 ongoing」且「剝除
  `superpowers_spec:`／`superpowers_plan:` 這兩類 planning **產出** source 後重算的
  authority digest 與 run 的 `source_revision` 逐字相符」——即整段漂移都是 run 自己
  造成的才保護，真正的 authority 變更維持既有換代語意。另修 `_artifact_rows()` 依
  檔名尾綴把 `*-design.md` 還原為 kind `design`（monitor 一律標成
  `superpowers_spec`，與 planning 產線的 `design` 不一致），讓下一代能承接前代的
  artifact authority，解開 `planning artifact lacks current planning authority`
  的死結。詳見 `changelog.d/supersede-active-run.md`。
- **Issue #519：semantic-reclaim 世代熔斷補上帶審計的重置路徑**——`#218 AC2` 的
  世代熔斷對 `(repo, work_id)` 的全部 superseded 歷史無條件累加，且沒有任何重置
  路徑，導致根因（`#507`／`#511`／`#516` 等 cortex 自身缺陷）修好之後 work item
  仍永久鎖死。新增 `cortex work reset-reclaim-budget`（必帶 `--actor`／單行
  `--reason`，走既有單一 writer work-action 路徑），以 **append-only 水位**
  （狀態檔新增加法相容的 `reclaim_resets` 根欄位）記錄「本次赦免哪幾個 superseded
  run_id」，熔斷計數改為扣掉已赦免者——既有 run 紀錄一列不刪不改，run 歷史維持
  為稽核來源；重置後新產生的世代照常累加，熔斷會再次上膛。每次重置落一筆
  immutable `cortex-work-reclaim-reset/v1` evidence（canonical json hash 命名、
  原子唯讀寫入、內容衝突 raise）。熔斷本身的 `needs_human` 結果同步回報
  `legal_next_steps`／`next_step_hint`，直接指出這條解鎖路徑。未採建議 1（引擎
  版本維度），理由見 PR。詳見 `changelog.d/reclaim-budget-reset.md`。
- **Issue #520：必要標題要求改由驗收判準機械產生，消除雙讀法**——integrator
  prompt 舊句「required headings: Requirements for spec, Decisions for design,
  Tasks for plan」原意是逐 kind 對應，字面卻同樣可讀成「必要標題是
  `Requirements for spec`」。planner 採了後者、產出 `## Requirements for spec`，
  而 `_has_required_heading()` 是 casefold 後完全相等比對（標題正規化只剝編號
  前綴、不剝 ` for spec` 尾綴），因此必然 `required-section-missing`，Phase 1
  派工死鎖。修法採 `#520` 建議 4：`planning.py` 的 `_ACCEPTED_HEADINGS` 成為唯一
  真檔，`_REQUIRED_HEADINGS` 由它 casefold 派生（判準值一字未改），新增純函式
  `required_heading_hint()` 機械產生 prompt 文字，`planning_runtime.py` 直接呼叫
  ——prompt 端不再持有第二份真實來源（判準與 prompt 不同步已造成 `#516`／`#520`
  兩次確定性失敗）。產生的文字逐 kind 給精確標題、明確禁止附加 kind 名稱，並揭露
  完整可接受集合。validator 邏輯與判準內容不動。
  詳見 `changelog.d/planning-heading-prompt.md`。
- **Issue #516：integrator prompt 補上兩個 echo-back 欄位的值來源**——
  `_validate_primary_integration()` 要求 integrator 輸出的 `question_pack_id`
  與 `secondary_evidence_hash` 與輸入完全相符，兩個值也都已在模型輸入裡
  （`question_pack.pack_id`、`secondary_evidence.evidence_hash`），模型只需原樣
  複製；但 prompt 只把它們當欄位名列在輸出鍵清單，且輸入欄位名（`evidence_hash`）
  與輸出欄位名（`secondary_evidence_hash`）不同，後者字面上像是要模型自己算 hash。
  planning 因此反覆以 `primary integration evidence hash mismatch` 落 needs_human，
  Phase 1 派工死鎖。prompt 現在明寫兩者「copied verbatim from」的來源欄位並禁止
  自行計算 hash（`do not compute, derive, or invent a hash`），`#406` 註解旁補記
  本次補齊的欄位。只改 prompt 文字與對應測試，validator 邏輯與資料流不動。
  詳見 `changelog.d/integrator-prompt-semantics.md`。
- **Issue #511（診斷面，前兩項）：planning artifact 被拒時的原因與內容可觀測**——
  `manager._publish_planning_artifacts()` 過去只取 `assess_planning_artifact()`
  的布林值，`reasons`／`blocking_markers` 全被丟棄，被拒內容又只活在 planning
  launcher 的 `TemporaryDirectory` 而無副本留存，operator 只看得到一句「不被接受」，
  只能盲目重試。現在 (1) 拒收訊息帶上
  `(reasons=...; markers=Lnn:...; evidence=...)`，保證單行且長度受限
  （`PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH = 400`），並另落一筆
  `planning-artifact-rejected` log；(2) 被拒 artifact 的 `kind`／`path`／完整
  `content`／`reasons`／`markers` 落 `cortex-planning-artifact-rejection/v1`
  evidence 至 `<coordinator_root>/evidence/planning-artifacts/`（內容上限 64K
  字元，超過標記 `truncated`），evidence 記錄本身 fail-open。落點刻意避開被
  cortex daemon 監控的 `artifact_root`（#507 會抹樹還原）。
  詳見 `changelog.d/artifact-rejection-diagnostics.md`。
- **Issue #506（部分實作）：Monitor GitHub 掃描 burst 減壓**——新增
  `paulsha_cortex/monitor/github_pressure.py` 的 `GitHubPressureGate`：
  (1) 每次 `gh` 請求前插入可設定的間隔＋jitter，把一輪數百次請求攤平而非齊發
  （`github_request_interval_ms` 預設 200、設 0 停用；`github_throttle_budget_seconds`
  作為上限保護並夾在 refresh interval 一半以下）；(2) rate-limit 型失敗時以
  不計配額的 `gh api rate_limit` 分診 primary／secondary，給出可分辨的 diagnostic；
  (3) 命中後採指數退避（尊重 `Retry-After`／`x-ratelimit-reset`），退避期間
  provider 的 `scan()` 直接跳過、不發任何請求。`GitHubTerminalProvider` 一併接上
  同一套分診與退避，且不再重試 rate-limit 失敗。所有 rate-limit diagnostic 維持
  被 `is_rate_limit_signal` 認得，`coordinator/claim.py` 的
  `provider-authority-rate-limited-canonical` 行為不變。
  詳見 `changelog.d/rate-limit-pressure.md`。

## [0.1.8] - 2026-08-12

### Fixed
- **Issue #465：workflow-lane handoff manifest 未寫 repo 歸屬，recent_done 永遠 repo=null**：complete_tick 終局 manifest dict 補 `workflow_repo` 欄（值取自 job record 派工時的 `workflow_repo`），讀取端 `_repo_from_manifest`（#230／PR #349 契約）現成接住；slice-lane 寫 `null`、舊 manifest 缺鍵維持 `repo=null` 不推斷；下游 paulshaclaw cockpit 不需改動。詳見 `changelog.d/workflow-lane-manifest-repo.md`。
- **Issue #466：profile 巷道對 patchmud main（PR #15 後）的 drift 修正**
  （`paulsha_cortex/coordinator/model_profile.py`）：
  - **A-1 report 聚合鍵改從 report 本身取**：patchmud PR #15 起 `run.yaml` 記
    `normalize_model_spec()` 展開後的完整 model spec（非 CLI 別名，且
    anthropic↔claude CLI fallback 隨憑證狀態浮動），舊實作以別名查
    `clear_rate` 榜必落 `identity-not-in-report`、巷道永遠產不出實測封套。
    新增 `_report_group_key()`：profile 的 runs_root 為單一身分專用，report 內
    必恰一組 `(model, loadout)`，多組即 `report-group-ambiguous` fail-closed。
  - **A-2 adapter 別名表更新**：patchmud 已落地 codex／agy OAuth headless
    adapter（paulsha-patchmud#14），「僅 anthropic adapter」的誠實約束註解過時；
    補 `("agy", "gemini-3.1-pro-high") → "agy:gemini-3.1-pro"` 對應（完整 spec、
    不用短別名），明寫 CLI adapter effort 硬編 `high` 的對應限制；codex 身分
    待 #456 R4 登錄後補格。
  - **A-3 deck 指紋改聚合 encounter provenance pin**：原 rglob 全檔 hash 會把
    `patchmud validate-deck` 對 `reference_timings` 的例行覆寫誤判成 deck 變更、
    誤觸全量重評；改為聚合各 encounter `provenance.yaml` 的 `content_sha256`
    （與 patchmud `encounter_content_sha256` pin 同語意，#452 D 票面原意），
    provenance 缺漏 fail-closed。
  - **A-4 run 封存耐久化**：runs_root 從 `mkdtemp` 改落 patchmud repo
    `runs/profile-<executor>-<model_id>-<stamp>/`（比照 #455 實測慣例，不進
    版控），registry 的 `profile_provenance.observation.runs_root` 記出處——
    落進 registry 的封套值可回溯到 events／ledger／replay 證據。
- **spec 勘誤追記**（`envelope-mapping-spec.md`、`benchmark-cost-baseline.md`）：
  paulsha-patchmud#21 證實「haiku 4/8」與「同母題變體 clear 分歧」兩個定案錨點
  實為 unified diff 協定噪音（非能力／變體訊號）；定案方向不變，但 R3 人工閘
  追記「pilot-v1 來源的降級提案 MUST 先以 `end_reason`／`protocol_failed`
  排除協定噪音」（paulsha-patchmud#24 落地後可直接讀 report `runs[]`）。
- **Issue #464：`test_server_socket_has_0600_permission` 於 Python 3.13 CI 偶發 `0o755≠0o600`——研判更正：非 #439 umask footgun 復發，而是測試 setUp readiness gate 的 bind→chmod 競態窗**：票上原研判「socket 建立當下 umask(0o177) 沒生效」不成立——umask dance 已由 PR #444（merge `d78d1d9`）移除，且失敗 run 31520253693 的 headSha `cf791a2` 已包含該修復（ancestor 關係經 git 驗證），`test_serve_forever_does_not_touch_process_umask` 亦保證 `serve_forever()` 不呼叫 `os.umask`。真因：`Stage9ServerTests.setUp` 以「socket path 存在」輪詢為就緒條件，但 path 在 `listener.bind()`（`server.py:166`）當下即存在、`os.chmod(0o600)`（`:168`）在其後才收斂——CI runner 忙碌時 server thread 於兩行之間被排程延遲，main thread 見 path 即放行，測試 stat 到 bind 預設 mode `0o777 & ~umask(0o022) = 0o755`（即觀測值 493）。修法：setUp 改用既有 `wait_until_ready(timeout=2.0)`（`_ready_event` 於 `server.py:183` set，嚴格 happens-after chmod 與 listen，threading.Event 提供確定性 happens-before）取代 exists-poll；`test_server_socket_has_0600_permission` 斷言本體不動（拒絕「測試側先 chmod 再斷言」——會讓驗證 server 自行收斂 0600 的斷言空洞化）。新增回歸測試 `test_wait_until_ready_blocks_until_socket_mode_tightened`：slow-chmod interposition（仿 #439 `_slow_bind` 手法）把 server thread 釘在 bind→chmod 窗口內，斷言窗口內 path 已存在但 `wait_until_ready(0.2)` 為 False、放行後為 True 且 mode 為 `0o600`。權限窗口安全評估：窗口內 `listen()` 未執行（connect 必 ECONNREFUSED）、production 父目錄先被 `_prepare_run_dir` 收斂 `0o700`（`service.py:98-101`）、窗口位於 `_SOCKET_PATH_LOCK` 臨界區——非安全邊界，`server.py` 免改。RED/GREEN：以 bind/chmod 間暫插 `time.sleep(0.05)` mutation 在舊 setUp 下確定性重現 `0o755≠0o600`、新 setUp 下同 mutation 轉 GREEN（mutation 已還原）；`Stage9ServerTests` 重複 50 次無 flaky；CI 為 serial pytest（無 xdist/randomly），修法為 happens-before 關窗而非 timing 調參。
- **Issue #469：slice-lane job 不帶 `workflow_repo`，`recent_done`/`slices` 的 repo 歸屬仍為 `null`（#465 follow-up）**：#465 補了終局 manifest 寫入端（`"workflow_repo": job.get("workflow_repo")`）、#349 補了讀取端（`_repo_from_manifest` 三鍵投影與 `slice_status_entry` 的 builder/reviewer job fallback），但 slice-lane 派工路徑（`autonomy.dispatch_ready` → `_record_launching_job` → `registry.create_job`）從不寫 `workflow_repo`，兩端都只等資料。修法為顯式宣告制：spec frontmatter 新增 optional `repo: owner/repo` 欄（shape 驗證 fail-closed），派工時寫進 builder job record 既有 `workflow_repo` 欄，`_launch_foreign_review` 的 reviewer job 繼承 builder 的值——終局 manifest、`recent_done`、`slices`/`attention` 的 repo 歸屬全鏈打通，覆蓋所有終局狀態。未宣告的 spec 維持 `repo=null`：不從 repo_root 路徑推導、不從 git remote 推導（#230/#349「missing 回 null 不推斷」契約；與 workflow lane `run.repo` 來自 work item 顯式宣告同構）。否決 issue 案 2（自 completion record 投影 `work_authority.repo`）：slice-lane completion record 從不含 `work_authority`（僅 workflow-lane delivery 會寫），且 completion record 僅存在於 passed/candidate-merged 終局，failed/needs_human/verified 依舊拿不到歸屬。deck 契約表同步：`EMITTED_FRONTMATTER_FIELDS` 加 `repo`、deck emit 出 `repo: null` 佔位（三個 exact-keyset 對齊測試強制 meta/emit 同步；自動帶入 claim/work item 的 repo 為 follow-up）。新增回歸測試：frontmatter `repo` 宣告解析與非法 shape fail-closed、`dispatch_ready` 寫入 job `workflow_repo`、slice-lane 終局 manifest 帶宣告 repo／無宣告維持 null、`recent_done` 投影、reviewer job 繼承。

## [0.1.7] - 2026-08-12

### Added
- **Issue #452：以 patchmud 一次性評測產生模型能力封套，claim 時解析 planner／builder／reviewer；無 patchmud 走 bypass 預設**：(A) 新增選配評測巷道 `cortex model profile`（`paulsha_cortex/{porcelain,coordinator}/model_profile.py`）——偵測不到 patchmud 即明確 skip＋exit 0；對 registry 內 `source==default` 身分跑 deck（8 關全跑、429 指數退避）、收 report 餵 `map_report_to_envelope`、產 unified diff 預覽、經明確 `--apply` 才寫 packaged registry 檔（#454 R3 人工複核閘；below-green-floor／incomplete-deck-sample 絕不落檔）；patchmud 僅 anthropic adapter，roster 只有 `claude/sonnet` 可驅動，其餘身分逐格誠實回報 `adapter-unavailable`。(B) registry schema v2→v3：`ModelIdentity` 加封套四欄位＋`profile_provenance`（全選填、fail-closed 驗證、registry 永不寫入預設值），`DEFAULT_ENVELOPE` 單一真值搬移至 `model_identities.py` 並新增查表投影 `project_envelope()`；v1/v2 檔案照載、shadow 語意不變；packaged registry 升 v3 登錄 #456 R3 的 5 身分候選 roster（agy 列首位，planner 熱路徑選擇不變）；**部署遷移註記**：host overlay 已宣告被收編四鍵之一（`copilot/gpt-5.4`／`claude/sonnet`／`codex/gpt-5.3-codex-spark`／`cg/glm-5.2`）且不與 packaged 逐欄相等時，升級後 registry 載入 fail-closed——升級前請自 overlay 移除該鍵或改成逐欄相等。(C) `MODEL_CHAIN_RESOLUTION_SOURCES` 擴充 `patchmud-profile`／`default-envelope`，解析優先序 override > measured 側寫 > registry/預設，`resolved_model_chain` 記實際 source；提供 `capability_lookup` provider（`build_capability_lookup()`：#209 R1 六項全評估不短路、排除原因可觀測；全 default 回 `None` 維持 bypass 字節）——claim 熱路徑接線待 #211 readiness pipeline 落地；yellow plan review `envelope_lookup` seam 已實際接上（兩鍵任一 default → `None`，v1 證據字節與 v0.1.6 逐位元相同）；primary_domain 偏好維持排序語意（preferred 排前不剔除其餘候選，保住 #262 re-route fallback），measured band 部分剔除理由落 manager log。(D) 評測指紋六元組存 provenance，指紋未變 `already-profiled` skip、deck pin 變更重評、`--force` 強制；熱路徑永不同步觸發評測；tick 補評測 hook 經評估不落地（取捨記於 PR），以 `cortex inspect models` 顯示每身分封套值＋來源＋provenance。另修 doctor `review-sandbox` probe：candidate-only 宣告降級 warn（#456 R6 登錄不隱含可用）。詳見 `changelog.d/patchmud-envelope-profile.md`。
- **Issue #454（#452 子項）：patchmud ranked 榜 → 封套四欄位的映射純函式與門檻定案**：新增 `paulsha_cortex/coordinator/envelope_mapping.py`（`map_report_to_envelope()`：吃 patchmud report.yaml schema v1 的 dict＋身分／deck 識別資訊，吐封套四欄位＋逐欄 provenance；純函式無 I/O、不 mutate 輸入、重跑 bit-identical、禁止 import patchmud）、`tests/test_envelope_mapping.py`（34 案）與 `docs/superpowers/specs/envelope-mapping-spec.md`。票面四待決全數定案：(1) v1 只落 `accepts_bands`，其餘三欄（`invariant_ceiling`／`consistency_scope`／`acceptance_modes`）誠實維持 #453 預設、逐欄留 `not-measurable:*` 理由碼；(2) 門檻定 `clear-rate-ladder-v1`——固定門檻（否決 report 內相對排名）、整數交叉相乘，`clear_rate ≥ 3/4 → [green, yellow]`、`≥ 1/4 → [green]`、低於地板 → 空集且 `registry_writable: false` 不得落 registry；樣本判準 `runs ≥ encounter_count`（#455 全跑定案）；未標註 band 的 deck 走階梯、將來 card 標註後 per-band clear-rate 整體取代（前置：上游 report 增列 per-run encounter id）；planner red 依 #223 收斂路徑結構性釘入不受門檻管轄；(3) 人工複核閘要——函式只產 diff 預覽 payload，registry 寫入經 #452 CLI 人工確認；(4) 映射歸屬 cortex 側，patchmud 維持零 cortex 依賴。另承接 #453 R5 留白定混合 provenance 的 seam 投影規則（seam 所需欄位任一 default → 維持 bypass 字節；v1 決策下 plan-review seam 與 v0.1.6 逐位元不變）。詳見 `changelog.d/envelope-mapping.md`。
- **Issue #453（#452 子項）：定案無 benchmark 時的保守預設封套值**：新增 `docs/superpowers/specs/default-envelope-values-spec.md`——四欄位各給唯一定案值與可追溯推導：`accepts_bands` 依 persona 定為 builder/reviewer `[green, yellow]`、planner 全值域含 red（#223 攔截鏈：red 在 plan 相位被 `needs_decomposition` 路由回派 planner，build/review 不可達）；`invariant_ceiling` 走 bypass 例外 sentinel `null`（值域無界無上確界、歷史分布經查證不存在——#210 R3＋runtime evidence root 零命中；#209 R2 缺省語意本就為此欄預留 bypass）；`consistency_scope`／`acceptance_modes` 取 #209 R2 全值域（封閉有限值域、上確界可寫出，且可觀測性靠值與 provenance 可辨而非過濾）。存放機制定案 `DEFAULT_ENVELOPE` per-persona 常數於查表投影套用、registry 檔案永不寫入預設值（per-persona 預設無法攤平成 (executor, model_id) 每列單值）。另定證據層規則（`source=="default"` 在既有 capability_probe／envelope_lookup seam 維持 bypass 字節）與 #452 實作必須照做的 bit-identical 回歸測試規格（T1 golden 雙配置決策軌跡／T2 預設值恆不排除 property test／T3 loader 相容）。詳見 `changelog.d/default-envelope-values.md`。
- **Issue #455：評測成本實測與 pricing snapshot 處置**：新增 `docs/superpowers/specs/benchmark-cost-baseline.md`——以單一身分實跑 patchmud `decks/pilot-v1` 全 8 關，落地 per-encounter 與全 deck 的 tokens／wall-time／USD 實測成本表；據此外推 N=11 格（#456 定案，近期可實測 builder 3 格）全量 profile 的預算上界；並定案三項：pricing snapshot 是否納入 #452 評測指紋、補評測的排程位置（tick idle vs 一律手動）、重評觸發條件是否需修正。詳見 `changelog.d/benchmark-cost-baseline.md`。
- **Issue #442（第二部分）：ship-phase 卡試啟用 `provider:executor` auth 閘門**：`openspec-archive`／`policy-commit` 兩張 ship-phase 卡的 `runtime_capabilities` 加宣告 `provider:executor` 動態 sentinel（#369 建立、原本無卡消費的既就緒機制），dispatch 前 preflight 逐 identity candidate 解析成其實際 executor 並經 `coordinator.executor_auth` 做登入態探測——限流／登出擋在 model session spawn 之前，並依既有 candidate 順序 re-route。啟用前已於部署環境驗證 claude／codex／copilot CLI 皆在場且探測可用；`manager.py` hold 註解同步更新（其餘卡維持 hold，待 ship-phase 觀測無誤再擴大）。詳見 `changelog.d/enable-provider-executor-gate.md`。
- **Issue #456：定案候選 (executor, model_id, persona) 身分矩陣**：新增 `docs/superpowers/specs/model-persona-roster-matrix.md`，以 launcher 硬約束先於 benchmark 排除 4 格（copilot×planner、copilot×reviewer、agy×builder、cg×builder，逐格附程式碼依據），定案 5 身分登錄 roster（`agy/gemini-3.1-pro-high`、`copilot/gpt-5.4`、`claude/sonnet`、`codex/gpt-5.3-codex-spark`、`cg/glm-5.2`）、`independence_domain` 依模型血統填法與 builder/reviewer 分離相容性、「registry 登錄 ≠ 本機可用」三 seam 分離機制（與 #442 解耦），並定案「待 benchmark」格數 **N = 11** 供 #455 消費。roster 已實測通過 `model_identities.py` 既有 fail-closed 驗證。docs-only。詳見 `changelog.d/model-persona-roster-matrix.md`。

## [0.1.6] - 2026-08-11

### Added
- **Issue #442（第一部分）：新增 `cg`（copilot API／glm-5.2 via llm-share）launcher 支援**：`launcher.build_cg_argv` 依 operator 提供並 smoke 驗證的介面契約（prompt 經 stdin、`--headless --stdin`、model 預設 `glm-5.2`、effort 合法值 low/medium/high/xhigh）組出 argv，登記進 `_ARGV_BUILDERS`；cg 為 zero-tool executor，`build_cg_argv`／`SubprocessLauncher.__init__` 對 commit_required／unsafe／builder 語境一律 raise，只服務 read-only 的 planner／reviewer。`SubprocessLauncher.launch()` 新增 stdin plumbing（`printf %s <prompt> | <inner argv> 2>/dev/null`），其餘 executor 零影響。詳見 `changelog.d/cg-launcher-support.md`。
- **交付後孤兒 run 的明確退休路徑 `cortex work retire-delivered`（Gap 1）**：交付發生在 cortex 管線之外（fallback 巷道 subagent 直接做完並 merge）時，對應的 `WorkflowRun` 會卡在 `ongoing`／`verify`／`needs_human`、且其 build 階段建的 PR（`pr_refs`）早已 terminal，既有 `abandon` 的 pre-delivery 閘門使之無法退休、亦無法 ship，形成死角。新增獨立、意圖明確的退休 work-action `retire-delivered`：先透過既有 provider seam（新增 `GitHubDeliveryClient.fetch_pr_lifecycle_status`，走既有 `_api`、不自 subprocess `gh`）驗證每個 `pr_ref` 的 PR 都為 terminal（merged／closed），再落 audit evidence（`work-retire-delivered/`，schema `cortex-work-retire-delivered/v1`）並將 run 標為 `superseded`；registry 層維持純粹、不打 GitHub，退休 admission 只要求 `ongoing`＋無 active job＋`pr_refs` 非空。沿用 exact WorkflowRun CAS（`--expected-run-id`）＋bounded actor/reason，並具 idempotent 重入（已 superseded 時從 durable evidence 重讀 terminal 證明、不再打 GitHub）。**刻意不弱化既有 `abandon` 的 pre-delivery 嚴格性**。詳見 `changelog.d/orphan-run-retirement.md`。

### Fixed
- **Issue #445：`test_server_discards_finished_connection_threads` thread-timing flaky（連線執行緒已 `stopped` 但尚未從 `_connection_threads` list 移除）**：真正成因是 `MonitorServer.serve_forever()` accept loop 內 `t.start()` 與「登記進 `_connection_threads`」順序反了的 TOCTOU race——極快完成的連線處理執行緒可能在被登記進 list *之前* 就已跑完並自行嘗試 self-remove（此時它不在 list 裡，形同無效），accept loop 隨後才把這條已死執行緒 append 進去，且無下一條連線觸發清理，殘留永久留在 list 裡，任何長度的 poll 都等不到。修法：改為先登記再 `start()`，關閉整個 race window；測試側 poll 上限同步由 1.0s 提高到 2.0s 作防禦餘裕。詳見 `changelog.d/connection-thread-flaky.md`。
- **退休類 work-action 在 provider rate-limit 下不再硬失敗（Gap 2，#370 只保護了 resume 的延伸）**：`claim.load_work_authority` 新增 opt-in 參數 `allow_rate_limited_last_known_good`（預設 `False`，逐層透傳至 `_authority_from_canonical_row`）——僅在「canonical GitHub provider 因 rate-limit degraded **且** snapshot 仍留有 last-known-good `revision`/`last_success_at`」的窄條件下改用 last-known-good 續行，不 raise。`execute_work_action` 只對退休語境（`_RETIREMENT_ACTIONS = {abandon, retire-delivered}`）帶入此旗標，claim/start 等需要即時 authority 的語境維持嚴格 fail-closed 預設；非 rate-limit 的 degraded／缺 revision 情境即使在退休語境下仍嚴格拒絕。因退休不依賴 issue 即時開關狀態，正好在系統被限流、最需要清理 stuck run 時仍能退休。詳見 `changelog.d/orphan-run-retirement.md`。

## [0.1.5] - 2026-08-11

### Added
- **Issue #395：cortex 無法接手 mid-flight worktree（continuation/adoption 型工作）設計文件**：`feature-oneshot`（含 #324 的 `small-fix`）一系 deck 一律從 origin target branch 開全新 worktree、依凍結 plan 從頭實作，無法承接四類續作型工作（進行到一半的 merge、lane worktree 大量 uncommitted WIP、已完成未併入的 lane 分支序列、既有分支的驗證與 fixup）。新增 `openspec/changes/2026-08-11-continuation-adoption-dispatch/`（proposal／design／tasks／`specs/trusted-dispatch-completion/spec.md` delta）與 `docs/superpowers/specs/continuation-adoption-dispatch-{spec,design}.md`，定案 continuation slice 的最小 schema 擴充（單一巢狀 `continuation` frontmatter 欄位：`mode`／`existing_worktree`／`existing_branch`／`adopt_dirty`，不擴散 `EMITTED_FRONTMATTER_FIELDS` 既有雙向等式的維護面）、dispatch 端需要新增與 `Dispatcher.dispatch()` 平行的 adoption 進場點（並點名兩個查證發現的既有 landmine：`autonomy.dispatch_ready()` 內建的 `feature/<slice_id>` 分支命名假設、`ScriptWorktreeCreator.create()` 對同名 branch 的既有「強制 reset 到 base」路徑會摧毀 continuation 要保留的既有 commit）、mid-merge 偵測與「完成 merge 視為 build step、禁止 abort」語意（論證既有 ancestry 不變量已結構性懲罰 abort，偵測本身定位為 observability／prompt 塑形而非新增強制機制）。**查證更正 issue 兩點假設**：issue 建議的「可宣告式完成 gate（測試命令＋乾淨 working tree＋commit 存在）」已由既有 `verification` 契約（`tests`／`full_suite` 欄位＋`candidate-worktree-dirty`／`candidate-not-advanced` 檢查）逐字提供，不需新 schema；mid-merge abort 已被既有 ancestry 檢查結構性擋下，不需新強制機制。核心設計張力（adopt dirty worktree 的既有 diff 與 cortex 既有 exact-candidate 純度／pinned-input 契約如何調和）：論證純度不變量管的是退出邊界（job 結束時狀態），adopt dirty 只動進入邊界，不需修改既有檢查本身；但 continuation candidate 的 `dispatch_base..candidate` 全段 diff 混合了 adopt 前（cortex 未監督）與 adopt 後（cortex 驅動）的內容，ForeignReview 該評全段還是只評增量段（需新增第二 baseline）——本票**明確不替 maintainer 決定**，完整列出兩個選項與代價。另有一項零新 commit（型 4「純驗證＋簽核」）情境是否需要對 `candidate-not-advanced` 開 opt-out 的未決問題，同樣留待 maintainer。唯一落地的 code：`paulsha_cortex/coordinator/mid_merge.py`（`detect_merge_state()`，純唯讀，偵測 worktree 私有 `MERGE_HEAD`＋未解衝突路徑，零接線）與 `tests/test_mid_merge.py`（6 個回歸測試，含真實 `git worktree add` + 真實衝突 merge fixture，核驗 worktree 私有 `MERGE_HEAD` 解析正確、與其他 worktree 互不干擾）；不觸碰 `autonomy.py`／`dispatcher.py`／`manager.py`／`verification.py`／`seams.py`／`deck/schema.py` 任一行。全套 pytest：新增 6 個測試全綠，既有測試零回歸（另有 2 個與本票無關、main 基線既已存在的環境相依 flaky 失敗：`test_fix_dispatch_spec_path.py` 的 `_infer_repo_root` fallback 案例，於本次查證的 sandbox 環境下 main 基線本身即失敗，非本票引入）。

### Fixed
- **Issue #396：conventions open-issue batch 派工實測撞到 4 個 executor/launcher 缺口**：cg（copilot API／glm-5.2）CLI 介面未經證實，維持 out-of-scope 並補上明確 extension-point 文件與回歸測試；copilot 認證缺口的 preflight probe 早於 #369 已備（未接上 cards.yaml 為既有、刻意的環境決策，留給 operator），新增 `launcher._copilot_credential_env()` 補齊 job env 缺 `COPILOT_GITHUB_TOKEN` 時從 `GH_TOKEN`/`GITHUB_TOKEN` 正規化注入的契約；claude builder sandbox 擋 git commit 修正為純 code bug（`build_claude_argv` 先前漏接 `commit_required`／linked-worktree git 寫入放行，比照既有 copilot/codex 補齊，非 harness/persona 契約矛盾）；`slice-action`/`recover slice` 的 `retry-review` 補上 `--review-executor`/`--review-model`（manager/daemon 早已支援轉發，缺口純粹在 CLI 表層）。詳見 `changelog.d/executor-launcher-gaps.md`。新增 22 個回歸測試，修復前均已確認 RED。全套 pytest：2352 passed、32 subtests passed（基線 2330/32，淨增 22；另有 2 個與本票無關的既有環境性失敗，見 fragment）。
- **Issue #416：abandon 不回滾已發佈未提交的 planning artifacts，殘留檔令後續世代 brainstorm 落盤被 authority 檢查必拒**：brainstorm define 成功發佈 spec/design/plan 到工作樹（未 git 提交）後，若 run 隨後被 abandon（例如 build 卡失敗），`_PlanningPublicationTransaction` 的 rollback 只在 define 流程內部失敗時觸發，沒人知道要回滾這些已發佈但未提交的檔案——下一世代重新 claim 後 brainstorm 對同一 destinations 再發佈時，`_publish_planning_artifacts` 對「檔案已存在但無對應 `PlanningArtifactAuthority`」一律 fail-closed 拒收（`planning artifact lacks current planning authority`），殘留檔變成死鎖地雷，operator 只能手動 rm 孤兒檔或改名重識別繞過（issue 內文記載的短期實操）。新增 `work_actions._gc_abandoned_planning_artifacts`／`_gc_one_abandoned_planning_artifact`，接在 `_abandon_action` 兩處 `_manager_abandon_workflow_run` 之後（含已 superseded 的重入分支），逐一檢視 run 的 `planning_authority`：只有「未被 git 追蹤」且「現存內容 hash 與發佈時 baseline_sha256 相符」才視為安全的發佈殘留並回滾刪除；已被 git 追蹤或 hash 不符（operator 手動改過）一律保留、只記 diagnostics log，不誤刪。GC 全程 best-effort（單項或整體失敗皆吞掉例外、只記 log），不得讓 abandon 本身失敗。另補 #393 分類映射的窄修正（issue 建議 3）：`manager._is_planning_authority_residue_failure` 辨識 `primary-artifact-write-rejected: ValueError: planning artifact lacks current planning authority: ...`／`... current authority drift: ...` 這兩個明確的 authority 殘留特徵，命中時 `apply_workflow_action` 把 evidence classification 由 #393 預設的 `content` 改記 `environment`，讓 `recover-planning` 可用；`_publish_planning_artifacts` 其餘的內容型驗證錯誤（schema 不合法、路徑逃出 governed roots、artifact 未通過驗收）不受影響，維持既有 `content` 分類與 fail-closed 意圖。新增 10 個回歸測試（`test_work_actions.py` 三個 GC 案例：hash 相符刪除／hash 不符保留／git 已追蹤保留，修正前已確認重現 RED；`test_workflow_production_wiring.py` 七個分類器與端到端案例）。全套 pytest：2321 passed、32 subtests passed（基線 2311/32，淨增 10）。
- **Issue #439：monitor server.py 的 `os.umask(0o177)` dance 冗餘且為 process-global 併發 footgun（#425 追查副產物）**：`MonitorServer.serve_forever()` 在 socket `bind()` 前後翻 `os.umask(0o177)` 再還原，其效果被緊接其後的 `os.chmod(0o600)` 覆蓋（冗餘），且 `os.umask()` process-global 非 thread-local，翻轉窗口內其他執行緒的 `mkdir(parents=True)` 會繼承 `0o600`（無 execute 位）而在該目錄下觸發 `PermissionError`——這正是 #425 CI flaky 的實際機制。移除 umask dance，僅保留 `os.chmod(0o600)`，socket 權限語意不變、race class 消除。詳見 `changelog.d/server-umask-redundant.md`。新增 2 個回歸測試，修復前皆已確認 RED（其一直接重現 #425 的 `PermissionError` 症狀）。全套 pytest：2356 passed、32 subtests passed（基線 2354/32，淨增 2；另有 2 個與本票無關的既有環境性失敗，見 fragment）。

### Added
- **Issue #384：LLM executor/provider 失敗缺 typed semantics、bounded recovery 與 policy-aware fallback**：`completion.classify_completion` 過去只有 exited/failed 兩值，job registry 只有 status/exit_code；slice lane（`manager.py`）硬編 `"builder-failed"`、workflow lane 硬編 `"job-failed"`＋`needs_human`，任何 executor 失敗（auth 失效、rate limit、暫時性網路錯誤、內容政策拒答）一律壓平成同一種無分類、無 retry、無 backoff 處置。新增 `paulsha_cortex/coordinator/provider_outcome.py`：typed `ProviderOutcome`（`auth`／`rate_limited`／`quota`／`transient`／`content`／`unknown`）分類複用 #369 的 `executor_auth.classify_cli_output` 與 #370 的 `github_rate_limit` 訊號模組；新增中間 authority 等級 `SignalAuthority`（`structured` > `text_signal` > `hint`）解開 plan 內部「stderr 訊號只做 hint」與「recovery matrix 期待 rate_limited 觸發 retry」的矛盾——`text_signal` 等級足以驅動 bounded、可逆的 retry/re-route，但不足以驅動 policy 層決策。`Dispatcher._finalize_headless` 分類一次寫回 job registry 新欄位 `provider_outcome`，slice lane／workflow lane 共用同一份結果。Workflow lane 新增 bounded durable retry（沿用既有 `run.attempts` 樣板，`terminal_contract.MAX_PROVIDER_RETRIES=2`，只有 `rate_limited`／`transient` 重試，逾限轉 `"provider-retry-exhausted"`）與 policy-aware re-route（`_provider_failure_reroute` 複用 `runtime_preflight.evaluate_dispatch_gate`，在既有 domain-filtered candidate 順序上換人，結構上不放寬 independence domain，不可 policy-shopping）；slice lane 做分類與 `cortex inspect status` 投影（無 `run.attempts` 可持久化，範圍界定不含 auto-retry）。詳見 `changelog.d/provider-failure-semantics.md`。新增 53 個回歸測試（`test_provider_outcome.py`／`test_provider_failure_recovery.py`／`test_provider_failure_slice_lane.py` 等），修正前皆以暫時移除實作確認真實 RED。全套 pytest：2266 passed、32 subtests passed（基線 2213/32，淨增 53）。

### Fixed
- **Issue #420：auto-claim 建立的 run 於 define 完成後永久卡住，periodic tick 不接手，explicit intake 卻能同步跑 define→plan→build**：`_claim_action`（`work_actions.py`）對「既有 ongoing run」的重試觸發條件過去只在字面比對 `args.get("action") == "resume"`（即人工經 `cortex work resume` 觸發）時才重呼叫 `workflow_starter`，讓卡在 `apply_workflow_action(action="start")` 之 claim→define→plan 同步續推段中途被中斷的 run 有機會重跑一次；periodic auto-claim scan（`run_auto_claim_scan`）固定帶 `args={"action": "auto-scan"}`，永遠不滿足這個字面比對，導致自己建立的、facets 乾淨但卡在 `current_phase="define"` 的 run 每輪都只被原樣反映、`workflow_starter` 永遠不會再被呼叫——無 needs_human、無錯誤，觀測面全綠但永久停滯。修復：重試觸發條件新增 `automatic and decision.action == "resume"`，讓 auto-claim scan 對自己的 define-stuck run 也能重試，等價 explicit resume 的續推行為；刻意不擴及 needs_human／blocked，維持 #373 的守衛。詳見 `changelog.d/autoclaim-define-advance.md`。新增 2 個回歸測試，修正前已確認 RED。全套 pytest：2313 passed、32 subtests passed（基線 2311/32，淨增 2）。

### Fixed
- **Issue #425：`test_stage9_project_monitor_service` 在 CI Python 3.13 job 偶發 `PermissionError`（測試隔離 flaky）**：逐行比對 CPython `pathlib` 原始碼確認 CI traceback 的兩個行號（`Path.mkdir()` 的 `os.mkdir`、`Path.stat()` 的 `os.stat`）都出自測試輔助函式 `_make_workspace()`（`tests/test_stage9_project_monitor_service.py`）未受保護的 `mkdir(parents=True, exist_ok=True)` 呼叫鏈，並非 issue 原始猜測的「測試自行 chmod 後未還原」（整檔已無任何 chmod 呼叫）；真正成因是 `MonitorServer.serve_forever()`（`paulsha_cortex/monitor/server.py`）在 `bind()` 前後暫時改動行程層級、非 thread-local 的 `os.umask()`，若同一行程另一 thread 恰好在此窗口新建目錄，該目錄會意外繼承 `0600`（無 execute bit），3.13 pathlib 重寫後的呼叫時序讓這個既有 race window 更容易被排到（3.10/3.12/3.13 的 `is_dir()`/`exists()` 對 `PermissionError` 的處理邏輯本身相同，非語意變更）。`paulsha_cortex/monitor` 的掃描／serve 邏輯經複查全數透過 `checked_stat_mode`/`checked_resolve` 與顯式 `except OSError` 防禦，對 3.13 無行為差異，故僅修測試基礎設施，未動 product code。新增 `_mkdir_resilient()`（bounded retry-on-`PermissionError`）並套用到檔案內全部同構 `mkdir(parents=True, exist_ok=True)` 呼叫；新增 `MkdirResilientTests` 3 個回歸測試直接驗證 retry 能穿越暫時性權限窗口、對持續性權限問題仍正常 raise。詳見 `changelog.d/stage9-permission-flaky.md`。因屬 CI-only、Python 3.13-only 的計時敏感 flake，本機 3.12 環境無法穩定重現原始 RED，故以上述單元驗證替代。全套 pytest：2314 passed、32 subtests passed（無回歸）。
- **Issue #389：work intake 對 issue-only work item 必然失敗，且錯誤訊息無法診斷**：lifecycle reducer 只在 work item 進到 `todo` state（有 todo-kind 來源）時才投影 `start` next_action，只 link 一個 GitHub issue 的 work item 永遠停在 `topic`。`coordinator/claim.py::_authority_from_canonical_row` 過去對這種 row 有三個 bare `return None` 出口（全 sources inferred／無 `start`＋無 active workflow／confirmed sources 無 todo-kind 來源），不留任何診斷，`load_work_authority` 找不到目標時只能落回與「row 不存在」「issue 被多個 work_id 認領」共用的泛化 `confirmed work authority missing or ambiguous`。三個出口改為各自 raise 專屬 `AuthorityValidationError`（新 reason code `authority-all-inferred`／`authority-not-startable`／`authority-no-confirmed-todo-source`），訊息含 work_id、目前 lifecycle state 與「需要 active Todo 來源」的指路；真正的 missing／ambiguous 情境維持原泛化訊息不受影響。同步補 `docs/unified-work-lifecycle.md`、`cortex work --help`（先前完全缺 `intake` 條目）與 `docs/onboarding/concepts.md` 記載 claim 的 lifecycle 前置條件。詳見 `changelog.d/intake-diagnostics.md`。新增 8 個回歸測試，修復前以新測試檔確認 RED（三個新分支皆重現泛化 missing/ambiguous）。全套 pytest：2319 passed、32 subtests passed（基線 2311/32，淨增 8）。
- **Issue #381：fanout 同時啟動 N 個 builder 打爆 GitHub `/user` 端點配額**：`autonomy.dispatch_ready`（fanout 迴圈）與 workflow lane（`manager_daemon.py` periodic tick 的 resume 迴圈，實際 spawn 點在 `manager._dispatch_workflow_card`）過去背靠背 spawn 全部就緒單位，無 sleep／gap／併發上限；copilot executor 每次啟動連續探測 GitHub `/user` 約 6-7 次，該 quota bucket 與 core rate_limit 分離，既有診斷看不到，同時派 3 個 slice 即打爆，三個 builder 同一秒全部 `builder-failed`、且錯誤訊息誤導成需要重新登入。新增 `paulsha_cortex/coordinator/spawn_admission.py::SpawnAdmissionLimiter`：per-provider 最小啟動間隔（非併發上限、非序列化整個 job 生命週期，不同 provider 互不阻塞），真正 spawn 前呼叫 `admit()`，成功即釋放。兩條 lane（`autonomy.dispatch_ready`、`manager._dispatch_workflow_card` 及其呼叫鏈 `resume_workflow_run`／`manager_daemon` 的 periodic runner 與手動 `fanout`／`dispatch`／`tick` 請求）皆接上同一個 limiter instance；`spawn_admission=None`（未注入）在每一層都是零間隔 no-op，不影響既有呼叫端／測試。`manager_daemon.main()` 新增 `--spawn-min-interval-seconds`（可用 `PSC_SPAWN_MIN_INTERVAL_SECONDS` 覆寫，支援 per-provider override），唯一建構真實 limiter 並注入 daemon 的地方。詳見 `changelog.d/spawn-admission-limiter.md`。新增 29 個回歸測試，接線前已確認 RED（`TypeError: unexpected keyword argument 'spawn_admission'`）。全套 pytest：2209 passed、32 subtests passed（基線 2180/32，淨增 29）。

### Fixed
- **Issue #379：builder 完成回報與實際驗收背離——gate 清單來源與驗收判準無機械連結**：`#261` 既有的 gate ledger cross-check（`terminal_contract.authorize_terminal`）只對照「builder 自報的 `gate_evidence`」與「manager 重跑的 ledger」，但 ledger 本身的 gate 集合完全由 operator 的 `PSC_GATE_CMD_*` env 決定，與 spec/plan 的驗收條件（deck 卡片的 `test_policy`）沒有機械連結；operator 若漏宣告某個 plan 要求的 gate，builder 自報的「超集」項目就落在 ledger 之外，`#308` 對空/局部 ledger 的既有處理又直接放行，形成無人驗證的落差。複驗過程中另外發現一個會讓修復落空的既有 bug：`manager._audit_phase_steps` 在任何一次 phase advance 時，會把全部 step（不只是被更新的那個）的 `test_policy`／`skill_ref`／`action`／`commit_policy` 重置為 `None`，使 build phase 卡片的 `test_policy` 在真正被拿來跑之前就已被抹除。三項修復：（1）`_audit_phase_steps` 改為無條件帶過這四個欄位；（2）新增 `authorize_terminal(..., expected_gate_names=...)` 與 `manager._expected_gate_names_for_test_policy`，在 `#308` 空 ledger 早退之前插入獨立檢查——spec/plan 導出的應驗 gate 若不在 ledger 實際跑過的集合內（不論 ledger 完全空還是只宣告部份 gate）一律 fail closed，純無 gate 宣告的卡片維持既有合法空 ledger 放行語意；（3）驗收判準（`test_policy`）比照既有 `_pinned_input_mismatches`／`_review_inputs_drifted` pinned-input 模式：`registry.create_job` 新增 `workflow_test_policy` 欄位供派工時 pin 住，harvest 時 `manager._workflow_acceptance_definition_drifted` 比對 pinned 值與 registry 現值，drift 一律 fail closed（`reason="workflow-acceptance-definition-drift"`）。詳見 `changelog.d/gate-provenance.md`。新增 `tests/test_gate_provenance_spec_plan.py`（16 個回歸測試），修復前皆已確認 FAIL；另修正 3 個既有測試（原本 fake launcher 遷就 `#308` vacuous pass，一律寫空 gate ledger）。全套 pytest：2191 passed、32 subtests passed（基線 2175/32，淨增 16）。
- **Issue #369：dispatch 前未驗證 executor 登入態，且 provider capability 探測路徑在生產環境為死碼**：`manager._runtime_preflight_gate` 呼叫 `evaluate_dispatch_gate` 時從未傳入 `snapshot_lookup`／`provider_prober`，`provider:` capability 探測（#262 設計）因此永遠放行，整條路徑是死碼；`cards.yaml` 也從無 `provider:` 宣告可觸發它。另外 `porcelain/bootstrap.py` 的 copilot 登入態判定對輸出做字串匹配，GitHub 限流訊息常同時帶有 "login"／"authenticate" 字樣，導致限流被誤判成「尚未登入」。接上真正的 provider 資料源（GitHub 走既有 monitor durable snapshot；executor 走新增的 `coordinator/executor_auth.py` 登入態探測，rate-limit 判定永遠先於 login 判定）；新增 `provider:executor` 動態 sentinel 讓同一張卡對不同 identity candidate 各自驗證其真正會用到的 executor；`openspec-archive`／`policy-commit` 兩張 ship-phase 卡宣告 `provider:github:...` 需求；修正 bootstrap 的 copilot 限流誤判。詳見 `changelog.d/provider-preflight-wiring.md`。
- **Issue #378：builder 產出的「實證」可能是 rigged setup，verification gate 全綠也擋不住**：驗收契約字彙只有 `persona-scope` 與 `command`，`run_result_verification` 只驗 git ancestry／worktree 乾淨度／artifacts 存在性／scope diff／exit code，不驗「結論是否由觀測導出」；2026-08-07 對 `hamanpaul/embedebuguide` 派工 issue #47 時實際發生，兩支 evidence-case probe 因對照臂共用可變狀態或判準為恆真式而拿到全綠 gate。複驗確認 `adversarial-review` 卡與 `review.py` 的 `BLOCKING_FINDING_CATEGORIES`（含 `acceptance`／`verification-bypass`）fail-closed 通道皆已存在，缺的是卡片任務描述與強制掛載：`adversarial-review` 卡新增 `execution.action`，明確指示對抗式檢視 rigged setup（對照臂是否共用可變狀態／setup-order dependency、verdict 是否為恆真式、結論是否真由觀測值導出），命中即以 `verification-bypass` 或 `acceptance` category 回報 blocking finding；本 repo 現有的 evidence-claim 類 combo `mcu-feature`（硬體證據）把 `adversarial-review` 直接掛進核心層（不透過 `band_triggered` 加掛層），不論 band 評估結果都會派工。不新建 persona、不新建機制、不動 `run_result_verification`。詳見 `changelog.d/adversarial-evidence-review.md`。新增 4 個回歸測試，修正前已確認重現 RED（`adversarial-review` 缺任務描述、`mcu-feature` 未強制掛卡）。全套 pytest：2179 passed、32 subtests passed（基線 2175/32，淨增 4）。
- **Issue #383：run_tick 因殘留 handoff manifest 靜默略過已復原的 slice**：`run_tick` 的 `already_terminal` 判定過去只看 handoff 目錄底下有沒有 `<slice_id>.json`，完全不比對 registry 實際狀態——`recover-pre-candidate` 等操作者復原動作把 slice 撥回 `state="pending"`（可重派）後，殘留的舊終局 manifest 讓這個已復原的 slice 被永久排除在 fanout 之外，且 gate_status 非 needs_human 時連回報清單都不會列出，operator 完全看不出來。手動 `cortex fanout`（daemon `request_type == "fanout"`）走完全獨立的一條路徑，未過濾即把全部 metas 餵給 `dispatch_ready_fn`，連 run_tick 既有的 in-flight active 過濾都沒有，同一份 snapshot 兩條路徑給出不同答案。修法：新增共用函式 `manager.dispatch_gate_scan()` 收斂兩層過濾（in-flight／handoff 終局），`run_tick()` 與 daemon 的 `fanout` 分支皆改呼叫它消除分歧；終局過濾改與 registry 現況對帳（`state == "pending"` 代表已復原、不再讓殘留 manifest 擋 fanout，registry 查無此 slice 或其他狀態仍保守照舊擋）；`apply_slice_action` 的 `recover-pre-candidate`／`abandon` 補上 `_supersede_handoff_manifest`，把殘留 manifest 標記 `superseded_at`/`superseded_by`/`superseded_reason`（不刪檔，純稽核可見性，不影響放行判定）。詳見 `changelog.d/handoff-manifest-reconcile.md`。新增 5 個回歸測試，修正前皆已個別確認 RED。全套 pytest：2151 passed、32 subtests passed（基線 2146/32，淨增 5）。

### Fixed
- **Issue #370：`cortex work resume` 撞 GitHub rate limit 無法優雅退避，doctor 的 gh-auth probe 把 rate limit 誤報為憑證失效**：全 repo 過去對 Retry-After／X-RateLimit／secondary rate limit 零處理。`GitHubWorkProvider.scan()`（`monitor/providers.py`）錯誤分類把 auth 字樣判定排在 rate limit 之前，GitHub 真實的 secondary/abuse-detection rate limit 訊息常提及「OAuth」／「re-authenticating」，先判 auth 會誤歸為死憑證；`claim.py::_authority_from_canonical_row` 對「provider 因 rate limit degraded」與「provider 真的損毀」共用同一個 reason code，upstream 無法不重新解析訊息文字就分辨兩者；`doctor.py` 的 `gh-auth` probe 只看 exit code，rate limit 下的非零 exit code 被誤報成憑證失效；resume 撞限流後例外一路冒到 `manager_daemon.py` 的通用 `except Exception`，無條件覆寫 `needs_human`，人工 `cortex work resume` 清掉後立即重跑，限流仍在窗口內就立刻再卡，形成「清狀態→立刻重試→立刻再卡」的迴圈。新增共用分類器 `paulsha_cortex/github_rate_limit.py`（rate limit 訊號永遠先於 auth 訊號判定），修正 `GitHubWorkProvider.scan()` 分類順序；`claim.py` 新增 `REASON_PROVIDER_RATE_LIMITED_CANONICAL` 專屬 reason code；`doctor.py` 新增 `_gh_auth_probe`，rate limit 回 `warn`（不誤報 authentication failed）；新增 `paulsha_cortex/coordinator/provider_backoff.py`（durable、跨 daemon restart 落盤的 backoff deadline，複用 `manager_daemon.py` 既有的指數退避曲線，抽成共用 `coordinator/backoff.py`）與 `manager.resume_workflow_run` 的 `_provider_rate_limit_result`：rate-limit 分類的 `AuthorityValidationError` 不再無條件覆寫 `needs_human`，改記一筆 durable backoff 並回傳 `provider-rate-limited`；backoff 視窗內的任何後續 resume（operator 或 tick）直接短路回同一結果，不再重撞。詳見 `changelog.d/ratelimit-classification.md`。新增 5 個測試檔／29 個回歸測試，皆先確認 RED 再修復轉 GREEN。
- **Issue #382：slice gate_state 落 failed 後永久不可 repin，且 record_action 非原子突變會把半突變髒 row 沖上磁碟**：真正根因是 `JobRegistry.record_action`／`update_slice` 逐欄位「驗證→立刻寫入活物件」，多欄位 transition 若後段欄位（例如 `gate_state`）驗證失敗 raise，前段已合法驗證的欄位（例如 `state`）已經寫進活物件；raise 發生在 `_persist()` 之前不會立即沖上磁碟，但下一次**任何無關**的 `_persist()` 都會把這個半突變髒 row 一併沖上磁碟。疊加 `GATE_STATE_TRANSITIONS["failed"]` 原本不允許 `failed -> pending`（與 `SLICE_STATE_TRANSITIONS["failed"]` 不對稱）、`repin_slice()` 硬編閘門只認 `pending`/`needs_human`，讓 builder 失敗後的 slice 永久卡死：`allowed_slice_actions()` 宣告的 `retry-build` 保證被拒，`recover-pre-candidate` 則因非原子突變髒寫 `state=pending`，留下 `state=pending / gate_state=failed` 無出口死路。改為兩階段（先全驗證、再全突變、單次 persist）；`GATE_STATE_TRANSITIONS["failed"]` 補 `pending`、`SLICE_STATE_TRANSITIONS["failed"]` 補 `building`（對齊既有 `needs_human` 行為，讓 retry-build 派工路徑走得完）；`repin_slice()` 併入 `failed` 為可 repin 狀態；新增共用判準 `slice_repin_eligible()`，`allowed_slice_actions()` 據此決定是否宣告 `retry-build`，不再無條件宣告。更新既有測試（原本斷言 failed 必須拒絕 repin，已反向修正為斷言復原成功）並新增 9 個回歸測試，修復前已確認重現半突變髒寫外洩（RED）。全套 pytest：2127 passed、32 subtests passed（基線 2117/32，淨增 10）。

### Added
- **Issue #372：cortex/hippo 缺排程觸發的跨系統 digest 出口**：新增 `paulsha_cortex/coordinator/digest.py`，把既有 `read_status()` 快照（`attention`／`ready`／`held`／`degraded`／`recent_done`）彙整成結構化 `cortex-coordinator/digest/v1` JSON（含人類可讀 `summary_text`）。投遞層二擇一、不 fallback：預設寫入無外部依賴的檔案 outbox（`<coordinator_root>/digest/outbox/<timestamp>-<random>.json`，時間戳採既有 `now_fn` 注入慣例）；設定 `PSC_DIGEST_DELIVERY_CMD`（typed argv，比照 `PSC_PREFLIGHT_CMD`）時改把 digest JSON 從 stdin pipe 給該命令（`subprocess`、`shell=False`、逾時保護），命令失敗直接 fail-closed，不靜默改寫檔案。刻意不 import custom-skills（維持 cortex 對外零 runtime 依賴定位），也不把孤兒 `coordinator_telegram_notifier.py`（僅單元測試 import、production 零呼叫）接上。新增 porcelain 子命令 `cortex digest emit`，供外部 timer/cron 排程觸發；`cortex --help` 自動列出。另修復 `porcelain/inspect.py` 文字模式漏印 `attention`（`--json` 早已有）。新增 22 個回歸測試，修正前已確認 RED。

### Fixed
- **Issue #371／#375：installer 的 `preserve_existing` 鎖死 managed path 錯誤值，且 manager.lock 未 instance-scoped**：`PSC_PROJECT_CONFIG_ROOT` 過去被 `preserve_existing` 鎖住，一個早期殘留的錯誤值永遠無法被 `cortex install service` 重裝修復，導致多個 instance 共用同一份 project config、掃同一組 repo，打爆共用 GitHub 配額（#371）；`manager.lock` 路徑則完全沒有 instance 成分，shell wrapper 與 Python daemon 各自硬寫一套解析規則，agents_root 相同時兩個 instance 會搶同一把鎖（#375）。合併修復：`PSC_PROJECT_CONFIG_ROOT` 移出 `preserve_existing`；新增 `PSC_CONTROL_ROOT`（`<agents_root>/control/<instance>`）進 `managed_env` 且明確不放進 `preserve_existing`；新增 `cortex control lock-path` CLI 契約，shell wrapper 改委派給它與 daemon 同源；`cortex doctor` 新增 `managed-path-drift` probe 偵測既有安裝的潛伏漂移。詳見 `changelog.d/installer-managed-env.md`。
- **Issue #373：authority-restart 每 tick 剝除 needs_human 並改寫 source_revision，claim_key 不更新導致永久重觸發**：每個 daemon tick，`run_auto_claim_scan` 判定 `canonical_run.claim_key != _expected_claim_key(authority)` 即觸發 `registry._manager_reset_workflow_for_authority_restart`：改寫 `source_revision`、`attempts["verify"] += 1`、剝除 `needs_human` facet；同一 tick 內 resume 迴圈的跳過條件只看 `blocked`、不看 `needs_human`，於是放行進 `resume_workflow_run`，撞上 `_job_for_workflow_card` 比對到舊 job 的 `source_revision` 與剛改寫的新值不符，raise `workflow job binding mismatch`；except handler 把 `needs_human` 寫回，但 `claim_key` 從未被 reset 更新，下一個 tick 觸發條件永久為真，迴圈無限重複（生產環境已累計 166k+ 筆同型 log）。根治：`_manager_reset_workflow_for_authority_restart` 現在會把 `claim_key` 同步重算為新 authority 對應的 expected key（新增共用 helper `claim.claim_key_for_authority_digest`），authority 沒有再變時觸發條件即為假，不再重複 reset；只有 authority 真的再前進才會合法地再次觸發。另加縱深防禦：`manager_daemon.py` 的 resume 迴圈跳過條件新增 `needs_human`，與 `resume_workflow_run` 自身的 early-return 契約對齊。新增 3 個回歸測試（`claim_key` 同步重算、模擬多次 daemon tick 不再重觸發、needs_human run 不被送進 resume），修正前皆已確認 FAIL。
- **Issue #380：deck compile 產出的 verification 骨架寫死 pytest**：`_verification_skeleton` 過去對 `checks[name=policy]`、`tests`、`full_suite` 三處無條件硬寫 `python3 -m pytest -q`，`name` 宣告 `"policy"` 但實際執行的是測試而非任何 policy 驗證。改吃新增的 `compile_combo(..., repo_root=...)` 參數，透過 `resolve_project_policy()` 讀 `.project-policy.yml` 的 `preflight.steps`（`kind: validation` → policy check argv/timeout，`kind: tests` → tests/full_suite argv/timeout）；偵測不到對應 step 時不留空（`validate_verification_contract` 會拒收），改填 fail-closed placeholder（誤執行必非零退出）並印醒目 `[WARNING]`，`name` 維持 `"policy"` 以滿足 auto_dispatch 前提。同步修正建議樣板 doc 與 `init_sample.py` 過時的 `target_branch`／`verification` 提示文字（自 #101 起已非 `null`）。新增 3 個回歸測試，修正前皆已確認 FAIL。
- **Issue #374：`_log_error` 的單槽去重被多筆錯誤輪替瓦解**：`_LOG_ERROR_DEDUP_STATE` 過去是單槽 `dict`，daemon 一輪 tick 交錯產生多個不同 signature（實測每輪 14 個，來自 #373 的 14 個受害 run）時，下一筆 signature 一旦不同就整槽重置，使去重恆判定為「新 signature」——每筆都印，#249 的抑制摘要在多 signature 交錯下從未觸發（實測交錯 3 signature 各 200 筆共 600 筆 → 印 600 行、抑制 0 行）。既有測試只送單一重複 signature，破損實作下仍照樣綠燈。改為以 signature 為 key 的多槽 `OrderedDict` LRU（容量上限 `LOG_ERROR_DEDUP_MAX_SLOTS=64`，超出時淘汰最久未用者，避免 signature 含 `source_revision` 時無界成長），每個 signature 各自獨立計數與抑制，交錯不再互相重置對方計數；既有週期摘要語意與對外介面不變。新增 2 個回歸測試（交錯多 signature、LRU 淘汰），修正前已確認交錯案例 RED。
- **Issue #385：README／Quickstart 預設安裝路徑追 mutable `main`，packaging CI 未覆蓋所有宣稱支援的 Python**：`README.md`／`docs/onboarding/quickstart.md` 安裝段改為預設推薦已發版、可回滾的 release（`pipx install "git+…@vX.Y.Z"` 或 GitHub Release wheel），`main` 安裝改列為明確標示 mutable/edge channel 的獨立段落；`pyproject.toml` 新增 `[project.optional-dependencies].test = ["pytest>=7"]` 與 `requires-python = ">=3.10,<3.14"` 加 Python 3.10–3.13 classifiers；`tests.yml` install step 移除吞掉失敗的 `|| true`，`smoke-install` job 加上 Python 3.10–3.13 matrix 並補 `import paulsha_cortex` 檢查；`release.yml` 新增 tag vs `VERSION` 一致性檢查，不一致即 fail-closed。本機以獨立 venv 複驗：`pip install -e ".[test]"`＋全套 pytest（2084 passed/3 skipped，skip 為環境缺 `watchdog` 的既有行為）、`python -m build` 產出 `py3-none-any` wheel 並通過 `twine check --strict`、乾淨 venv 安裝該 wheel 後 `cortex --version`/`--help` 皆成功、tag↔VERSION 比對邏輯 match/mismatch 兩案例皆驗證正確。

### Fixed
- **Issue #418：#414 materialize 出的 canonical plan 檔與 brainstorm evidence 對帳必炸**：`_validated_brainstorm_planning_authority` 過去單純用 `set(persisted) - set(scanned)` 非空即 raise，`_materialize_plan_card_output`（#414）為對齊 build 端 declared input pattern 而產生的 canonical plan 副本天生不在 brainstorm evidence 列表內，每次 resume 對帳必 `needs_human`。新增合法副本例外路徑：`kind`／`work_id`／`baseline_sha256`（byte-copy）／ref 落在 plan phase output pattern 內四條全符合才排除於 omission 之外，其餘真正的 omission 維持 raise；回傳的 authority tuple 仍保留該副本以 seed 進 build worktree。新增 3 個回歸測試，修正前已確認重現 `omits persisted authority` 的 RED。

### Changed
- **work item 重識別 `-v4` 並移除 v2 墓碑**：v3 三世代分別耗於 #408 補完前、#414 前與 #416（棄單殘留 artifacts 地雷）；三修復皆已 merge，本次改名前已先棄單（#410 順序教訓）。v2 墓碑錨點（#411）任務完成（孤兒 run 已由 #412 救援通道清除），一併移除。

### Fixed
- **Issue #414：plan 卡 deterministic pass 不驗證宣告 outputs，導致下一棒 build 的 declared input 必缺**：`assess_planning_completeness` 只看 kind 覆蓋率，workstream todo（kind=plan、accepted）就足以讓 planning 判定 complete，`manager._dispatch_workflow_card` 於是把 plan 卡（如 `writing-plans-light`）deterministic pass，卻從未檢查卡片宣告的 `produces` glob 是否真的命中檔案，todo 的 ref 通常不落在該 pattern 內，下一棒 build 卡的 declared input 檢查因此必缺（生產實測 run workflow-e18785acc54e5ad87836，`ValueError: workflow declared input missing: ...`）。新增 `_plan_card_declared_outputs_present`（比照 build 端 `_workflow_input_snapshot` 的 glob 語意）於 deterministic pass 前驗證；缺席時由 `_materialize_plan_card_output` 把已 accepted 的 kind=plan 內容 materialize 到卡片宣告的 canonical 路徑並併入 `planning_authority`（走既有 `_PlanningPublicationTransaction`，registry 提交失敗會 rollback）；不可 materialize 時 fail-closed 不跳過。新增 3 個回歸測試，修正前已確認重現生產事故的確切 `ValueError`。

### Fixed
- **missing-kind 問題的 source_refs 補 accepted fallback（#408 補完）**：`_build_default_question_pack` 對 `missing-{kind}` 只取同 kind refs——同 kind 有草稿時語意正確（重寫以草稿為本），但 todo 錨定的 work item 該 kind 完全不存在，refs 恆空，`_planning_destinations` 與 `_planning_source_material` 雙雙斷炊（PR #409 的 workstream 推導因此拿不到料，v3 gen1 實測 destinations 仍空、模型輸出裸路徑被 governed-roots 拒）。同 kind refs 為空時 fallback 至全部 accepted refs；端對端實測 brainstorm 三棒＋integration 驗證全通、destinations 正確導出。

### Fixed
- **abandon 孤兒救援窄放行（issue 410 建議 2）**：work item 改名／重識別後，舊識別 authority 失去 issue／openspec 映射，run refs 與 authority 恆不相等，嚴格相等守衛使孤兒 run 永遠不可 abandon、其 issue 認領持續與新識別相撞。僅在「authority 兩類映射皆空、run 仍留 refs」的孤兒簽名下放行（expected_run_id／actor／reason 強制項與單一 ongoing 檢查不變、evidence 照常落盤）；authority 映射非空的真 refs 漂移維持 fail-closed。

### Changed
- **v2 墓碑錨點（issue 410 改名死結短期解）**：`fix-log-error-dedup-v2` 改名 v3 時仍有 ongoing run，形成「孤兒 run 不可 abandon（authority 隨改名消失）→ 其 issue 認領與 v3 相撞 → repo provider degraded → 全域凍結」三環死結。重加 v2 tombstone row（僅 path 錨點＋明示 exclude issue 374）恢復 authority 以 abandon 孤兒；abandon 後於收尾打掃移除。

### Fixed
- **`_planning_destinations` 支援 workstream todo 錨點，修復 small-fix combo 的 artifact write 必拒**：過去只從 `openspec/changes/<slug>/…` 形 source_refs 導出目的地，todo.md 錨定的 work item（無 openspec-propose 卡的 combo）拿到空 destinations，integrator 只能發明路徑、必被 `_publish_planning_artifacts` 的 governed-roots 驗證拒收（canary v2 gen3 實測）。新增 `docs/superpowers/workstreams/<slug>/todo.md` 推導（openspec 優先、歧義維持 fail-closed 空 dict）；並補上 #397 漏掉的第四個裸吞分支——artifact-write 失敗的 reason 現在附例外摘要。另附 work item `-v3` 重識別（v2 三世代同樣全數耗於基礎設施缺陷）。

### Fixed
- **integrator prompt 補結構語意，修復必然的空 `artifact_refs` 驗證失敗**：`build_production_planning_runtime` 的 integrator prompt 過去只列欄位名，未說明 `artifact_refs` 須為非空的 destination path 清單、`artifact_kind` 須對應 question kind 去掉 `missing-` 前綴、artifacts path 集合須恰等於 refs 聯集、每題恰一 resolution——模型在無語意指引下把不確定欄位留空，`validate_primary_integration` 必然拒收（canary v2 gen2 實測）。prompt 補上四項約束並以回歸測試釘住關鍵語句；validator 不動。

### Changed
- **work item `fix-log-error-dedup` 重識別為 `-v2`**：v1 的三個 run 世代全數消耗於基礎設施缺陷（#390/#397/#399/#401），觸發 #218 語意重宣告熔斷（`semantic-reclaim-budget-exhausted`）；依熔斷設計的逃生門改用新識別，issue 374 連結與 workstream todo 隨遷。此案例同時佐證 #331（-v2 重識別摩擦）所述成本。

### Fixed
- **Issue #404：planning claude 呼叫繼承 operator 全套 Claude Code 配置，且 plan 模式與回聲任務衝突**：`planning_runtime._planning_argv` 的 claude 分支移除 `--permission-mode plan`（plan 模式系統提示與「必須回傳純 JSON」的確定性回聲任務衝突，實測模型會拒絕直接回 JSON），安全層改由 no-tools（`--tools ""`）＋既有 disposable sandbox＋operator 樹快照比對承擔；新增 `_seed_hermetic_claude_env`，`_invoke_json` 對 claude 身分注入一次性 `CLAUDE_CONFIG_DIR`（只播種 `~/.claude/.credentials.json`，0700/0600），同時隔離 operator `~/.claude` 下的 user MCP servers／plugins／hooks／使用者層 CLAUDE.md，不影響登入態；缺憑證時不猜測、維持不設 env，讓 CLI 自行回報 not logged in；codex／agy 維持不帶 env 覆寫。probes 與 questioner／secondary planner／integrator 皆經同一個 `_invoke_json`，自動受益。新增 4 個回歸測試，修正前 3 項已確認 FAIL。
- **Issue #401：`_extract_json` 在 result 非 JSON 時把 CLI envelope 當模型輸出餵進驗證，且 planning prompt 未強制純 JSON**：`_extract_json` 對 envelope（claude CLI 含 `api_error_status` 等 20+ 鍵的成功回傳）巢狀 `result`/`content`/`message`/`text` 欄位解析失敗時，過去會 fall through 把整個 envelope dict 當模型輸出回傳，讓下游驗證報出 `unexpected key: api_error_status` 這種完全誤導的診斷。新增共用 helper `_find_json_object`（頂層維持既有嚴格整串語意；envelope 巢狀欄位改用平衡大括號掃描從散文抽取內嵌 JSON），全部抽取失敗時明確 `raise ValueError` 而非回傳 envelope 本體；questioner／integrator／secondary planner 三處 prompt 統一附加純 JSON 輸出契約字句。新增 5 個回歸測試，修正前已確認 4 項新行為測試 FAIL。
- **Issue #399：planning 完整性檢查把 gitignored 的 `runtime/` daemon 狀態目錄雜湊進去，handoff churn 誤判為 planner 汙染**：`.gitignore:8` 明列 `/runtime/` 為本機部署常見拓撲（manager daemon 以 repo 為 `WorkingDirectory` 常駐）下的狀態殘留，`runtime/handoff/wf-*.json` 被 daemon 每個 periodic tick 整份重寫（issue #373 的迴圈使其每 ~55 秒必然發生一次），內容含時間戳必變。`planning_runtime._tree_snapshot`（PR #398 後已排除 `__pycache__`／`*.pyc`）仍雜湊 `runtime/`，被 `_invoke_json` 的 operator 快照前後比對誤判成「planner 修改了 operator worktree」而 fail-closed rollback；`_copy_planning_sandbox` 同樣照抄複製這棵目錄。修法：`_tree_snapshot` 只跳過快照 root 直下的 `runtime/`（以 relative path 判斷，避免誤跳 `pkg/runtime/` 等深層同名目錄）；`_copy_planning_sandbox` 改用自訂 `ignore` callable 與其同語意排除。新增回歸測試釘住雙向行為：root 層級 handoff churn 不觸發 mismatch，深層同名目錄變動仍觸發 mismatch。
- **Issue #390：resume 不凍結 combo override，導致 canonical workflow manifest conflict**：`work_bridge.start_canonical_workflow` 過去每次呼叫（含 resume 路徑）都重新以 `select_combo(mapped_issue_titles(authority), ...)` 選 combo；resume 依 contract.py 的 fail-closed 設計不會轉發 combo，`combo_override=None` 使其改由 issue 標題自動選擇，與 claim 時的顯式 `--combo` 覆寫不同，`default_workflow_manifest` 產生不同 bytes，被 `_write_manifest` 的 byte 比對誤判為 `canonical workflow manifest conflicts with persisted claim` 而炸掉。修法比照既有 `model_chain_override` 的凍結語意：`existing_run`（仍在 `define` phase 的既有 ongoing run）已知後，若本次呼叫沒帶 `combo_override`，改以 `existing_run.combo` 作為 effective override 再進 `select_combo`——走既有 `explicit-override` 分支，不需另開 schema 不認得的新 `source` 字面值。新增回歸測試釘住「claim 帶非 auto 值的 combo override → 同 run 再走一次不帶 override 的 resume 路徑 → 不得 raise、combo 維持凍結值」。
- **Issue #391：define/brainstorm 失敗時 needs_human reason 只活在回傳值裡、daemon periodic tick 觸發時蒸發**：`manager.apply_workflow_action` 的 define/brainstorm 三條 needs_human 路徑（`runtime_factory` 初始化失敗、runtime 元件缺失、`run_heterogeneous_brainstorm` 未收斂到 ready）過去只把 reason 放進回傳值，daemon 背景觸發時無人消費、底層 exception 被 `except Exception` 整段吞掉，run row 只留下查不出原因的 `needs_human` facet。最小修：三條路徑各補一筆結構化 `logger.error`（`paulsha_cortex.coordinator.manager` logger），含 run_id、reason，以及 runtime_factory 失敗時的 `type(exc).__name__: str(exc)[:200]`；未動 `WorkflowRun` schema（既有 `retry_classification` 為完成態 retry 分類的受限值域欄位，語意不符，不適合承載本次的自由文字 reason，故未挪用）。
- **Issue #393：recover-planning 的 evidence 生產端不存在，`cortex-planning-failure/v1` 全庫僅有 reader**：define 三條 needs_human 靜默失敗路徑（同上 #391 的三個點位）過去沒有任何一條寫 evidence，`recover-planning` 對它最該覆蓋的場景結構性不可用，resume 的 next_actions 只剩不可逆的 `abandon`。新增 `manager._write_planning_failure_evidence`（原子寫入模式與 `work_bridge._write_json_evidence` 一致：tmp、fsync、rename、0400）與 fail-open 呼叫端 wrapper，三條路徑各落一筆 `cortex-planning-failure/v1` evidence 並與既有 facets 更新合併成同一次 `_manager_update_workflow_run`；runtime 初始化例外／元件缺失歸 `environment`，brainstorm 未 ready 歸 `content`。未做歷史遺留無 evidence run 的例外放行通道，維持 fail-closed。
- **Issue #397：planning 完整性檢查把共享工作樹的 `__pycache__` churn 誤判為 planner 汙染**：daemon 與 planning launcher 共用同一棵 operator 工作樹時，daemon lazy import 隨時重編的 `__pycache__/*.pyc` 過去被 `planning_runtime._tree_snapshot` 一併雜湊，被 `_invoke_json` 的前後快照比對誤判成「planner 修改了 operator worktree」而 fail-closed rollback。修法：`_tree_snapshot` 跳過 `__pycache__` 目錄與 `*.pyc` 檔名（其他任何檔案異動仍觸發 mismatch，fail-closed 不變）；`_copy_planning_sandbox` 的 `ignore_patterns` 同步排除，避免 sandbox 複製過程的 race read。另修正 `run_heterogeneous_brainstorm` 三處 `except Exception` 把底層例外壓平成單一字面值 reason 的問題，改為透傳例外型別與訊息片段，供 #393 的 planning-failure evidence／`recover-planning` 直接讀出。

### Added
- **open-issue 批次 workstream todo 錨點**：為 14 個 work item 新增 `docs/superpowers/workstreams/<work-id>/todo.md`（frontmatter `status: accepted` + `work_item`），並在 `.cortex/work-items.yaml` 補上對應 `path` 連結。lifecycle reducer 需要 active todo 來源才會把 work item 推進 `todo` 態並開放 `start`——issue-only 連結停在 `topic` 不可 claim。各 todo 的任務清單取自對應 issue 2026-08-10 獨立複驗 comment 的修復標的，同時作為 ship gate 的勾選要件。

### Added
- **open-issue 批次（369–385）work item 進件登錄**：`.cortex/work-items.yaml` 新增 14 個 work item 條目，將 15 張 open issue（369、370、371、372、373、374、375、378、379、380、381、382、383、384、385）連結為可 claim 的 work authority；371 與 375 因同動 `installer.py` 的 `managed_env`／`preserve_existing` 而合併為單一 work item（`fix-installer-managed-env`）避免平行修改互撞；386 為 tracking gate 不建 work item。各 work item 的規劃權威為對應 issue 上 2026-08-10 的獨立複驗 comment（含 root cause 更正與修復標的）。純進件登錄，不含任何實作。

## [0.1.4] - 2026-08-08

### Changed
- **lifecycle 詞彙表改為治理平面與記憶平面的聯集，修復 `claim`／`research` 分歧造成的跨平面對齊 FAIL**：`persona/contract.PHASES` 由 7 個擴充為 8 個，新增 hippo 的首階段 `research`。`claim`（cortex 的 work item 認領，manager 決定性執行）與 `research`（hippo 的記憶 slice 調查階段）語意不同、不可互相改名，故採聯集而非改名，兩平面詞彙得以逐字相等且無需資料遷移。`PHASES` 在兩平面都只做成員資格檢查、不決定順序，聯集不影響既有行為；實際執行序列 `coordinator/workflow.WORKFLOW_PHASES` 維持 7 個、自 `claim` 起不變，並新增測試釘住這條界線。三套套件之間維持零 import 依賴，相等性續由 paulshaclaw 的消費端對齊測試守。

## [0.1.3] - 2026-08-07

### Fixed
- **Issue #366：install service 身分守衛改比對身分真值，PSC_REPO_ROOT 補上守衛——F44 復發修復（#148 未完成的一半）**：新增 `PSC_REPO_IDENTITY` 身分戳記（由 `git remote origin` 正規化而來，SSH/HTTPS 視為同一身分；非 git/無 origin 退回路徑指紋），取代 `#198` 遺留的「既有 PY 比對呼叫者」守衛，改為「既有身分比對新解析出的身分」，解掉腐化一次後守衛永久失效的根因；新增 `--rebind` 顯式繞過旗標（installer 與 `porcelain/service.py` 對稱透傳），既有 env 缺戳記時放行並補寫（遷移路徑，不 fail-closed）；`cortex doctor` 新增 `repo-identity` probe，能在潛伏期內偵測 `PSC_REPO_ROOT` 與實際身分不符；同步修正 `README.md:597` 舊敘述。

## [0.1.2] - 2026-08-07

### Added
- **Issue #210：sizing 難度與能力封套校準設計文件——以 cortex 自身 run 歷史取代手估**：新增 `openspec/changes/2026-08-07-sizing-envelope-calibration/`、`docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md` 與 `docs/superpowers/plans/sizing-envelope-calibration.md`，定案 `calibration_source`／`calibrated_at` 只掛在 `#209` 的 `invariant_ceiling` 欄位（非全部四個供給側欄位）；定案難度後驗 estimator 改讀 `CompletionRecord.work_authority.merge_commit` 本地 diff，取代粒度不符（模組數 vs LOC）的 `sizing_declaration_drift`；**查證發現新缺口**——`invariant_ceiling` estimator 所需的 `invariant_count` 歷史值從未被 `CompletionRecord` 持久化，需先補一張前置票；定案「一次通過率」排除非 `model_repair` 的 `retry_classification`；定案 estimator 觸發時機比照 `cortex stat` 既有四個彙總旗標即時查詢；裁定不採納 issue 對 `consistency_scope` 的 glob 化建議，維持 `#209` 已凍結的產物種類集合契約。更新 `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 更正 `#210` 舊有「零外部前置」註記。純設計文件，未實作任一 estimator、未改任何 `.py`。
- **Issue #279：跨 repo ad-hoc 一次性派工——設計文件（design-doc）**：新增
  `openspec/changes/2026-08-07-design-adhoc-oneshot-dispatch/`（proposal／
  design／tasks／`specs/trusted-dispatch-completion/spec.md`）與
  `docs/superpowers/specs/adhoc-oneshot-dispatch-{design,spec}.md`，定案
  D1-D6：`cortex run once` 繞過 control queue、直接組裝既有
  `JobRegistry`/`Dispatcher`/`manager.run_tick()` 於呼叫行程內完成派工，
  job 狀態落 ephemeral tmp 路徑與宿主 `~/.agents` 物理隔離（不擴充
  `PSC_INSTANCE`/`_installed_environment()` 機制）；repo-root 沿用既有
  `_infer_repo_root()`，worktree／branch 建立行為不變，「呼叫方既有
  branch/worktree 內工作」明確列為 v1 非目標；combo 重用 #324 的
  `small-fix`，不新增更輕量 combo（`validate_manager_spine()` 七 phase
  涵蓋為不可放寬的治理憲法）；builder identity 臨時放行透過既有
  `load_model_identities()` 的 packaged+instance-local 合併機制，不改
  registry 驗證邏輯。另發現 `depends_on` 列的 #338（persona catalog gate
  對外部 repo 派工必炸）症狀已由 #341（commit `0264f3f`，早於本票查證
  基準 main）解掉，判定其現況為「症狀已消失、issue 未關閉」。本票不動
  任何 `paulsha_cortex/` 程式檔；code 落地拆為四張候選後續票（見
  `tasks.md` 文末拆票建議）。
- **Issue #138：交付成本治理 judge（cost-aware dispatch + 控速分流，不擋）設計文件**：新增
  `docs/superpowers/specs/cost-governance-judge-{spec,design}.md` 與
  `docs/superpowers/plans/cost-governance-judge.md`。凍結 `rate` 自追資料契約
  （`RateSnapshot`）與新模組落點 `rate_tracker.py`；凍結控速分流層 `filter_ready()` 介面
  契約，掛點為 `autonomy.ready_units()` 與 `dispatch_ready()` 之間，並與 `#136` 已落地的
  `capacity_gate.py`（daemon-idle 布林閘）劃清「並行兩把閘、不同稀缺資源軸」的邊界；429
  回授裁定重用 `manager_daemon._tick_backoff_seconds()` 的指數封頂公式、不重用其
  daemon-level 狀態；凍結 judge MVP 四因子合取判斷式（`rate_available × quota_remaining
  × capable() × track_record()`）與四個 interim stub 契約——`#137`／`#209` 尚未
  code-landed 期間全恆真，行為與現況等價；串接 `#137` `session_health` opaque
  pass-through 邊界，凍結 `should_terminate()` 五類終止觸發契約。裁定 MVP 不新增
  `resource-inventory.yaml`，遵循 `#209` 既定路徑。本票不實作任何程式碼、不開
  `openspec/changes/**`。複驗訂正：`#137` 的設計文件實際只存在於未合併分支
  `feature/137-oneshot-lesson-loop-design`（main 上不存在），初版誤標其為「已落地
  設計」已訂正；另補上與已落地票 `#325`／`#324` 的介面關係查證。
- **Issue #137：交付 one-shot 成效閉環（lesson-loop + 棘輪計分）設計文件**：新增
  `docs/superpowers/specs/oneshot-lesson-loop-{spec,design}.md`。凍結 `task_type ×
  outcome` 計分 schema（計分鍵沿用 `#139` taxonomy 的 `(type, scope)` tuple、`outcome`
  三態 `clean`／`fixup`／`fail`、`cost` reserved 並定義由 `#325` 已落地的 usage 聚合投
  影）；定案 session-health 為診斷特徵、不進 reward；定案 cortex 端只產出 lesson
  payload、不觸碰 `paulsha-hippo` `knowledge/` 目錄的跨 repo 邊界；定案棘輪介面契約與
  `#209 capable()` 既有簽章相容，建議掛點為 `capable()` 判準之一而非另開
  `autonomy.py` 內部路徑。
- **Issue #340：builder persona 契約新增 `completion_obligations`（結束前必須 commit）**：`PersonaContract` 新增 `completion_obligations` 欄位（fail-closed schema 檢查），`personas.yaml` 的 `builder` 角色新增義務宣告「完成前必須 git add＋git commit，worktree 不乾淨不得回報完成」，由 `render_contract_prompt` 注入實際派工的 dispatch prompt，補上既有 `commit_policy: required`（只管寫入權限）與 manager 端事後 dirty-worktree 安全網之間「事前宣告義務」的缺口；空清單角色不受影響。
- **Issue #275：發布 canonical engineering outcome contract 供外部 learning systems 消費**：新增
  `paulsha_cortex/coordinator/engineering_outcome.py`——append-only、一 repo 一檔的
  `engineering-outcomes/<repo-slug>.jsonl` outbox，`work_actions._ship_action`／
  `_abandon_action` 在既有的 `status="done"`／`status="superseded"` terminal transition
  之前 durable 寫入一筆 `shipped`／`abandoned` record（`outcome_id` 由 run_id／outcome／
  該次轉換的內容位址 digest 決定性推導，daemon restart 或 request retry 重複 tick 不會
  產生第二筆）。record 含 per-job `card`／`persona`／`workflow_phase` 展開欄位，
  `execution_provenance` 誠實標示 `correlation_confidence: "weak"`（job record 目前沒有
  存 executor 自身 session UUID，只有 worktree-path＋時間窗可用）。`rejected`／`failed`／
  `rolled_back` 是 schema 保留值，v1 沒有對應的 run-level 終局轉換點可掛，尚無 emitter。
  新增 `cortex outcome list/show/replay` 唯讀 CLI surface。設計決策見
  `docs/superpowers/specs/engineering-outcome-contract-{spec,design}.md`。
- **Issue #324：combo 搜尋改支援 instance-local override，新增 small-fix 輕量 combo**：`deck/schema.py` 新增 `resolve_combo_path()`／`iter_combo_files()`／`combo_search_dirs()`，一律先查 `$PSC_AGENTS_ROOT/config/combos/<id>.yaml`，找不到才 fallback 到套件內建目錄（同 id 時 instance-local 優先、reinstall 不覆寫自訂檔）；`deck/selector.py`／`deck/cli.py`／`coordinator/work_bridge.py`／`porcelain/init_sample.py` 全數改走這兩個入口。另新增卡片 `writing-plans-light`（只吃 spec/design doc、不依賴 openspec proposal）與參考 combo `small-fix`（7 張卡、2 條核心 gate_spine，覆蓋全 7 個 phase 各恰一張），打斷小任務不需要的 openspec requires 全鏈；`small-fix` 只能經 `--combo small-fix` explicit override 使用，不進自動選牌映射。
- **Issue #204：新增 skill usage ledger、proposal-first park janitor 與 core/emergency 永久豁免**：新模組 `paulsha_cortex/coordinator/skill_ledger.py`（append-only、去重的 `~/.agents/registry/skill_usage.jsonl` terminal 執行事件記錄，欄位固定白名單不含任何自由格式 payload）與 `skill_janitor.py`（cold-skill 判定＋proposal-first park／restore，比照既有 `gc.py` 只分類不執行的精神）；`manager.run_tick` 新增 `ledger_recorder`／`skill_janitor` 兩個注入點（與既有 `reaper` 同款預設不啟用、例外不破壞 tick）；新 CLI `cortex skill inspect|list-proposals|propose|approve-proposal|park|restore`。`class: core`／`class: emergency`（`deck/schema.py` 既有 `CARD_CLASSES` schema 欄位的首個治理消費點）於 cold 判定與所有 park 入口皆二次防呆強制豁免。
- **Issue #203：`cortex work intake` 把 link+start 合成單一「拿到一個 issue/task 就進件」入口，不復活低階直派**：新增 `work-action` 動作 `intake`——帶 `--issue`／`--kind`+`--ref` 且尚未反映在受監控快照時先建立 override link（等價 `cortex work link`），再原樣轉交既有 `start` 語意（`claim_key` 去重、`--combo` override 皆比照 `start`）；省略時直接沿用 work_id 現有的 confirmed authority。Intake 不會憑空建立新 authority——work_id 必須已在受監控權威快照中存在，且最終仍要求 confirmed Todo 或已授權的 issue/openspec/path 來源，否則 fail-closed，不建立 WorkflowRun。`contract.py`／`work_actions.py`／`manager.py`／`manager_daemon.py`／`cli.py`／`porcelain/run.py` 六處同步放行 `intake`；已停用的低階 `dispatch` 與既有 Telegram `/dispatch <slice_id>` 維持原樣，不在本次範圍內改動。
- **Issue #325：job record 收斂 token usage——per-lane 成本歸屬的最小底座**：新增
  `usage_extractors.py` 依 executor（codex／claude／copilot／agy）從 headless
  session log 抽取 token 用量，各自處理累計值 vs 逐行累加、欄位語意易混淆
  （如 claude 的 `cache_read_input_tokens` vs `cache_creation_input_tokens`）與
  copilot `result.usage` 不含 token 數的誤讀陷阱；全程 fail-soft 不影響 job 的
  status/exit_code 判定。job record 新增 `usage`／`usage_raw`／`usage_reason`／
  `started_at`／`exited_at` 欄位，並新增 `cortex stat --usage-by-run` 依
  workflow run 彙總用量。
- **Issue #136：新增 `cortex capacity-gate check` porcelain 命令與 `claude.json` PreToolUse 模板**：補上「agent 手動呼叫 `Task`/`Agent` 或以 `Bash` 啟動 `codex exec`/`claude -p`/`copilot -p` headless session」這條完全繞過 manager daemon 既有 fanout idle gate 的 ad-hoc 破口。純函式 `classify_tool`/`evaluate_gate` 讀既有 `control.client.read_status()` 的 `daemon.idle` 布林，忙碌或 `degraded`（保守視為忙碌，避免讀不到狀態時靜默放行）時回傳 Claude Code PreToolUse hook 協定的 `ask` 決策；`claude.json` 新增 `PreToolUse` 區塊（`Task`／`Bash` matcher）僅為模板，寫入使用者 live `~/.claude/settings.json` 的切點屬 paulshaclaw thin install，本 repo 不自動生效。
- **Issue #331：`cortex work migrate` 原子動詞設計（ADR-0002）**：新增
  `docs/adr/0002-work-identity-migration.md`，定義用單一 atomic override
  transaction＋寫入前凍結 authority 的 abandon CAS，把識別遷移（如 `-v2`
  世代熔斷）收斂成 1-2 次 CLI 呼叫，取代現況要靠 5 個 PR、跨近 9 小時手動
  拉鋸 `.cortex/work-items.yaml` 的流程（`#326`–`#330` 實測記錄）；刻意維持
  `claim.py` 既有碰撞不變量與 source-owner-transfer 守門不變。純設計文件，
  不含程式碼變動。
- **Issue #276：builder 派工依 plan Task 邊界分段——設計文件（design-doc）**：新增
  `openspec/changes/2026-08-07-builder-task-boundary-segmentation/` 與
  `docs/superpowers/specs/builder-task-boundary-segmentation-{design,spec}.md`，
  定案 per-Task fan-out（同 worktree 續派原語）、Task 邊界解析
  （`planning.list_plan_tasks()`）、`build_dispatch_prompt()` 反漫遊／
  commit 斷點語句、`classify_completion()` 新增 `context-exhausted` 分類、
  commit log 續跑進度帳，以及與 #277 的介面邊界；本票不動任何
  `paulsha_cortex/` 程式檔，code 落地拆為三張後續票。
- **Issue #209：模型能力封套設計文件——定案 `capable()` 六項判準與 `resource-inventory` 四欄位契約**：新增 `openspec/changes/2026-08-07-design-model-capability-envelope/` 與 `docs/superpowers/specs/design-model-capability-envelope-{spec,design}.md`，定案 `#138` judge「能力配得上」謂詞的六項合取式與供給側四個靜態欄位契約；定案短期落地位置為既有 `model-identities.yaml`；定案三閘序（eligibility／admission／routing）並記錄與既有 `claim_readiness.CHECK_ORDER` 的落差；更正 issue §4 roster 現況——registry 全文只有一個身分，連 issue 自身修正 comment 的三身分表都對不上 main。純設計文件，未實作、未改任何 `.py`。
- **Issue #323：`cortex jobs`／`stat` 對 workflow lane job 補 work_id／primary issue 歸屬欄**：`wf-xxxxxxxx-<card>-<n>` job 輸出新增 `workflow_work_id`／`workflow_primary_issue` 兩欄，於輸出端以既有 `workflow_run_id` join registry 的 workflow run，零額外持久化狀態；card 已由既有 `workflow_card` 欄位提供。非 workflow lane job 與其餘既有欄位皆不受影響。
- **Issue #178：新增 `cortex work gc` 交付後產物回收命令**：proposal-first 回收殘留 build worktree 與已 merge 的 repo local branch；預設 dry-run 只輸出候選清單與逐項 `reclaim`／`keep`＋reason code，`--apply` 才執行且逐項重驗（TOCTOU-safe）。merged 判定改走內容層驗證鏈（`git merge-base --is-ancestor` → `git cherry` 內容等價），修正 squash-merge 後 ref-ancestry 失真、`git branch -d`／`--merged` 誤拒已合併分支的既有陷阱；任何疑義一律 `keep` 並附 reason code，closed-unmerged PR 分支保留。新模組 `paulsha_cortex/coordinator/gc.py` 由 umbrella CLI 攔截路由，不經 manager daemon、對 registry 唯讀、不動 remote。
- **Refs #294：slice spec 可宣告 executor/model_id 並於派工前強制 registry 驗證**：`dispatch_ready` 支援逐 slice 的 builder identity 覆寫，unknown identity fail-closed 並列出可用 candidates；同時 `cortex fanout`／`tick` 的明確 `(executor, model)` 與 periodic tick 預設 model 也改為先查 `model-identities.yaml`，避免 typo 直到 session 內才失敗。
- **Issue #202：task_type 自動選牌與 fix-standard combo**：新增 deck taxonomy loader／selector、`fix-standard` workflow combo、`WorkflowRun.combo_selection` provenance、`cortex work start --combo` authoritative override，以及 `cortex stat --combo-selections` 彙總。Refs #202。
- **Issue #260：新增 `recover-repair-commit` work action**：repair job 失敗終止但已在 builder worktree 留下合法 descendant commit 時，以雙 CAS（`expected_run_id`＋`expected_candidate`）確定性 bind 為新 candidate；判準全部取自系統事實，不啟動任何 model session，冪等回報 `already-recovered`；`retry-build` 既有 CAS 與窄化入口原封不動。
- **批次 W2 planning artifacts（#294、#263、#202）**：為三個 work item
  （`feat-slice-executor-model`、`fix-preflight-closeout-order`、`feat-task-type-combo-selector`）
  新增 spec／design／plan／todo 與 `openspec/changes/2026-08-04-<wi>/` planning artifacts，
  並登錄 `.cortex/work-items.yaml` 作為 confirmed authority；只提供 planning authority，
  不含實作。
- **批次 W1 planning artifacts（#295／#291、#260、#178、#139）**：為四個 work item
  （`fix-persona-catalog-portability`〔#295 primary＋#291 duplicate 一修多關的 multi-issue
  Work Item〕、`fix-repair-commit-recovery`、`feat-work-gc`、`design-task-type-taxonomy`）
  新增 spec／design／plan／todo 與 `openspec/changes/2026-08-04-<wi>/` planning artifacts，
  並登錄 `.cortex/work-items.yaml` 作為 confirmed authority；只提供 planning authority，
  不含實作。
- **Refs #292：實作 subagent / agent 派工收尾的六項確定性機械驗收檢查**：提供零 model session 的確定性收尾檢查 (`paulsha_cortex.mechanical_acceptance` 與 `cortex mechanical-acceptance`)，包含 1. 自我宣稱 vs 產出比對、2. 輸出內部一致性、3. 摘要 vs 內文一致性、4. 事實新鮮度 (涵蓋 PR body 與 commit message 的 closing keyword 雙重檢查)、5. 語言規範、6. 禁止無依據量化。提供 `--pr <N>`/`--unresolved-issues`/`--repo-root` 自動 context 蒐集、缺 context 標為 `SKIPPED`（exit code 2）而非 `PASS`、`policy-exempt:*` 白名單豁免與全套正負向測試。
- **Issue #262：dispatch 前驗證 runtime capability 與 provider snapshot 新鮮度**：新增
  `coordinator/runtime_preflight.py`，在建立 worktree／sandbox／job row／model session 之前，
  於「即將實際被使用的 executor 環境」執行低成本 preflight。card 契約新增 `runtime_capabilities`
  資料宣告（`module:` / `executable:` / `bridge:` / `provider:`），preflight 為通用執行器，新增
  card 不需修改實作；非法宣告在 deck 載入時 fail-closed。module 檢查透過 executor 的 interpreter
  以子行程 import、executable 只查 executor PATH，皆不使用 manager 這側的 import 或 host PATH；
  `SubprocessLauncher.executor_environment()` 沿用與 `launch()` 相同的 `_git_scope_env()`／
  `_review_scope_env()`，確保 preflight 與正式 job 的 interpreter／PATH／HOME／sandbox policy 一致。
  provider 健康改採「快照 + TTL + 有界 probe」三層，snapshot 帶 `observed_at`／TTL／source／reason，
  超過 TTL 的 degraded 判斷不再被當成當前事實；`capability missing`／`provider unavailable`／
  `stale snapshot`／`probe inconclusive` 四種結果各自獨立表達，只有前兩者是 hard block。live probe
  以 provider identity 為鍵共用 TTL 快取與 rate-limit 額度，同批次同 provider 不重複探測。preflight
  失敗時優先在既有 identity 順序與 independence domain 規則內 re-route，無合法替代才進入帶具體
  reason 的 `needs_human`，全程 model invocation 維持 0；`cortex inspect status` 顯示缺少的
  capability、使用中的 executor environment 與 snapshot 新鮮度。
- **Issue #205：per-work planner/builder/reviewer 模型鏈覆寫**：`WorkflowRun` 新增 `model_chain_override`（run-scoped 覆寫，claim/首次 dispatch 時凍結，只作用於本 run，不動共享 `model-identities.yaml`）與 `resolved_model_chain`（三段實際解析結果與來源 override/registry，供事後稽核）兩個 provenance-only 欄位；`_select_workflow_identity` 逐段優先讀凍結覆寫、未指定段落回退共享 registry，覆寫仍須通過既有 capability 與 builder/reviewer independence domain 檢查，違反時 fail closed 並列出可用 identity；`cortex run work start/resume/retry-build/retry-verify/retry-review/...` 新增 `--planner-executor`／`--planner-model`／`--builder-executor`／`--builder-model`／`--reviewer-executor`／`--reviewer-model` 六個 run-scoped 覆寫參數。
- **批次 B planning artifacts（#261／#256／#262／#205／#135）**：為五個 issue 各新增 spec／design／plan／workstream todo 與 openspec change（`2026-07-30-<work_item>`），並登錄 `.cortex/work-items.yaml`，作為 cortex work-item lifecycle 的 confirmed authority。五組皆通過 `assess_planning_completeness`（`status: accepted`、必要章節齊備、無 blocking marker）。本 PR 只提供 planning authority，實作由後續 cortex 派工的 build phase 完成。

### Changed
- **封存批次 W2 三個已交付的 OpenSpec changes**：#294／#263／#202 的 change 已隨 PR 合併，但本批改由人工管線收尾未經 cortex ship，故 change 目錄仍 active；以官方 archive 折入 canonical specs。
- **feat-work-gc 與 design-task-type-taxonomy 重識別為 -v2（#178／#139）**：三代 run 因基礎設施缺陷鏈 superseded 觸發 #218 世代熔斷，依「-v2 識別」慣例重識別於修復齊備的 main 重跑。
- **W1 canary work item authority 補強（#295／#291）**：`fix-persona-catalog-portability` 於 `.cortex/work-items.yaml` 補綁 design／plan path 連結，並於 workstream todo 記錄首次 claim 環境性 stall 的 abandon 審計附註；使 authority digest 前進以重新 claim。
- **W1 canary v2 檔名對齊（#295／#291）**：build 卡 declared inputs 以 `*<work_id>*` glob 檔名，v2 僅改 frontmatter 導致 declared input missing；檔名與 workstream 目錄補 `-v2` 並同步引用。
- **W1 canary 重識別為 fix-persona-catalog-portability-v2（#295／#291）**：三代 run 因 #299／#302／#303 基礎設施缺陷 superseded 觸發 #218 世代熔斷，依「-v2 識別」慣例重識別續作（檔案路徑不動）。
- **Issue #135：persona enforcement shadow → enforce**：切換前先以
  `python -m paulsha_cortex.persona.replay`（新增，可重跑）回放最近已合併 PR 的
  實際檔案清單，證明對現行 `builder` 派工慣例零誤殺，才把
  `paulsha_cortex/persona/personas.yaml` 的 `enforcement` 由 `shadow` 切為
  `enforce`。`persona-scope.yml`（`scope_ci.py`）現依 `enforcement` 動態決定放
  行：違規時輸出含 persona／觸及路徑／違反規則的可定位 verdict 並 `exit 1`；
  套用 `policy-exempt:persona-scope` label 時不阻擋，但違規內容仍完整輸出（不
  靜音）。`persona-scope` 設為 main required status check 屬 GitHub repo 設定，
  設定步驟見 `docs/persona-scope-enforcement.md`。

### Fixed
- **Issue #339：run tick 對已有 needs_human 終局紀錄的 slice 不再重複 fanout**：`run_tick` 原本的冪等防護只排除 registry 中仍在 `dispatched`/`running` 的 job，job 一旦 poll 到 exited 就離開這個集合，不論其 `gate_status` 是 `needs_human`／`failed`／`passed`；`ready_units`/`default_is_satisfied` 只檢查「別人 depends_on 我」是否滿足，從未檢查「我自己是否已經跑過」，導致下一趟 tick 對已完成待人工的 slice 重新 fanout，撞 `ScriptWorktreeCreator.create` 的 `"worktree target already exists"`。現在派工前會掃描每個 slice 是否已有 handoff 終局紀錄，併入排除集合；此掃描不受 idle gate 影響，`require_idle` 擋下新工作時 `needs_human` 清單仍會回報。summary 新增 `needs_human: [{slice_id, gate_reason, handoff_path}, ...]` 欄位。
- **paulshaclaw#264：status 條目補上明確 project 歸屬**：`recent_done`／`attention`／`slices` 現在投影明確的 `repo`；缺少來源時保留 `null`，不從 worktree 或 branch 猜測 project。
- **Issue #295（primary）／#291（duplicate）：persona catalog 改以套件內建為 canonical 來源，非 cortex repo 的 slice 不再確定性卡 `persona-catalog-unreadable`**：`run_result_verification` 原本無條件從**目標 repo**讀 `paulsha_cortex/persona/personas.yaml`，該檔只存在於 paulsha-cortex 自身，跨 repo 治理必然卡 `needs_human`，且 `dispatch: auto` 又強制要求該 check 無法拿掉。改為先以 `git cat-file -e` 探測 `dispatch_base` tree 是否宣告 repo-local override：存在即維持既有 pin/fail-closed 行為；不存在則回退讀取 `paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH` 套件內建 catalog 完成 scope 判定。override 壞損（不可讀／不合法）仍 fail-closed 不靜默回退；cortex repo 自身行為不退化。evidence 新增 `source`（`repo-local`／`packaged`）欄位可稽核判定依據。
- **Issue #303：三個測試直讀 production coordinator 狀態檔，環境洩漏使本地 pytest gate 被宿主狀態污染**：`test_porcelain_inspect.py::test_inspect_missing_targets_exit_one[argv0-missing-job]`／`test_work_actions.py::test_auto_without_issue_mutates_every_mapped_issue`／`test_auto_without_issue_fails_closed_if_any_label_mutation_fails` 未隔離 coordinator root，未顯式覆寫 `PSC_*` 時經 `resolve_runtime_root()` 落回 `$HOME/.agents`，直讀宿主真實 `~/.agents/coordinator/jobs.json`；production 狀態異常時三測試連帶 fail-closed。同根因擴大排查後，`tests/conftest.py` 的 autouse `_clear_runtime_env` 改為同時把 `PSC_AGENTS_ROOT`／`PSC_CONFIG_ROOT` 指向每測試獨立的空 tmp 目錄（作為 fail-safe 安全網，覆蓋 coordinator／control／specs／monitor／project-config／run root 整個家族），並補上 5 支既有測試（`test_paths.py`／`test_install_service.py`／`test_coordinator_manager_daemon.py`）刻意驗證「未覆寫時落回 `$HOME`」語意所需的顯式 `PSC_AGENTS_ROOT` delenv。以 audit-hook 稽核與偽造 corrupted `jobs.json` 重現 W1 batch 情境驗證修復前後行為差異。
- **Issue #260：resume／dispatch 不再重選 stale failed job**：`resume_workflow_run`／`_dispatch_workflow_card` 的 stale-terminal 判定補上「`exited` 且 exit code 非 0」，第一次 operator resume 即 dispatch replacement，不再空轉一輪；失敗回報附掛唯讀 `terminal_diagnostics`，不授予 candidate authority。
- **Issue #139：`task_type` taxonomy 契約補齊測試覆蓋並確認驗收面**：`paulsha_cortex/deck/data/task-types.yaml`（雙鎖值域＋scope 受控詞典）與 `paulsha_cortex/deck/task_types.py`（fail-closed loader、`classify_title` 五類判定）已隨 #202 提前落地，本票確認其符合 spec 的 R1–R6，並補齊 `tests/test_deck_task_types.py` 缺口測試（值域漂移拒載、空描述拒載、未知 combo 引用拒載、五類處置映射全稱驗證）；R7（統一 log reader／status view 介面契約）維持只定契約不實作。
- **Issue #296：builder tick tasks.md 與 reviewer authority-proving 凍結
  baseline 矛盾——確認已由 #310 修復，補 production-fidelity 迴歸測試**：
  #296 與 #310 為同一起 2026-08-04 hippo 事故的獨立提報；#310 的修法（PR
  #311／#312）已在 #296 提報後數小時落地，但 #296 未被關閉核實。新增
  `tests/test_builder_tasks_tick_verify_dispatch.py` 以真實 git repo 重現
  `_dispatch_workflow_card` reviewer 分支（verify／review 共用），涵蓋
  checkbox-only 通過、tasks.md 文字改動仍擋、proposal.md 等 spec 檔改動仍擋
  三種情境；無需再改動 production code。
- **Issue #307：gate ledger 一致性檢查消費 `test_policy=red-required`，解除與 tdd-red 卡的結構性互斥**：`_assert_terminal_gate_consistency` 從 job 綁定的 `WorkflowRun.steps` 查出目前 card 的 `test_policy`，交給 `terminal_contract.authorize_terminal` 對 red-required 卡的 pytest gate 做精準語意反轉——只有 exit code 精確等於 `1`（測試如預期失敗）才視為合格 RED；exit code `0`（全綠）或 `2`／`3`／`4`／`5`（collection error／internal error／usage error／no tests collected）一律維持 fail closed。其他 gate 與一般卡不受影響。
- **CI 測試閘門形同虛設（tests.yml 偵測誤判）**：`ls tests/test_*.py tests/*_test.py` 只要任一 glob 沒配到就回傳非零，本 repo 因此恆判為「無測試套件」而跳過整段 pytest 卻回報 success；改用 `find -print -quit`。
- **測試套件在 Python 3.10／3.11 無法 parse**：`tests/test_coordinator_manager.py` 與 `tests/test_coordinator_candidate_verification.py` 的 `_persona_catalog` 在 f-string 表達式內嵌含反斜線的 f-string，PEP 701（3.12）之前不允許，導致宣稱支援 3.10 的專案在該版本連 collect 都失敗。改為先組好字串再內插，輸出等價。
- **openspec 整合測試在缺 CLI 時硬失敗**：`tests/test_openspec_archive_purpose.py` 依賴 npm 套件 `@fission-ai/openspec`，不在 Python 依賴樹內；改為 `skipif` 明確標示並附原因，取代 `assert shutil.which(...)`。
- **Issue #263：ship validator 重排為本地 closeout 先於 PR metadata preflight**：archive commit 不再內嵌 push；pre-PR metadata preflight 失敗改回可 resume 的 `pr-preflight-blocked` typed stop、通過後照舊自動建立 PR；slice-based review worktree 補上 frozen authority materialize 與 hash 驗證。
- **Issue #263 補遺（PR #336 code review）**：review worktree authority materialize 的路徑檢查改為先驗證後動作（拒絕 `..`／絕對路徑 ref 於任何 mkdir 之前）；`work_bridge._manager_archive_applied()` 改委派 `manager` 版避免與 `any(...)` 舊語意漂移；`_slice_review_authority_inputs()` 相對 plan/spec path 改以 repo_root 解析，對齊 `_pinned_input_mismatches()` 既有語意。
- **Issue #202 補遺：durable snapshot 不可用時 combo 選擇改走 fail-soft**：`claim.mapped_issue_titles` 先前只在 snapshot hash mismatch 時 bypass；`_load_snapshot` 因 snapshot 不存在／不可讀／schema 損壞 raise 的 `ValueError`（含 `AuthorityValidationError`）未被攔截，會炸穿 `work_bridge.start_canonical_workflow`。現在一併回傳 `None` 落回 bypass-default combo，`load_work_authorities`／`load_work_authority` 維持 fail-hard 不變。
- **Issue #202 code review 修復：override 驗證改用 `load_combo`、`combo` 收斂只在 start 可用**：`deck.selector.select_combo` 的 override 先前只靠 taxonomy 反查判定未知，會誤判 repo 內實際存在但無 task_type 映射的 legacy combo（如 `mcu-feature`）；改為直接以 `load_combo` 驗證。另外 `--combo` 雖標「start 專用」，CLI／porcelain／manager 先前對所有 work action 都會轉交 `combo`，`resume` 在特定時序下可能被未經驗證的 combo override 影響；四層（`control/contract.py` fail-closed 為收斂防線）同步收斂為只在 `action == "start"` 才夾帶／轉交 `combo`，並清除 `work_bridge.start_canonical_workflow` 內與 selector 重覆的驗證死碼。
- **abandon 尋址窗口放寬至全額認領**：abandon 校驗 run refs 與 authority 全等；窗口期舊識別全額認領（撤 openspec exclude）、-v2 暫撤 openspec link。
- **舊識別墓碑 todo（abandon 尋址窗口）**：authority 需檔案級來源，-v2 遷移後舊識別無檔化致 abandon 不可尋址；暫置墓碑 todo，abandon 後移除。
- **-v2 issue links 暫撤（abandon 尋址窗口）**：解 issue contested → authority ambiguous → abandon 無從尋址的死鎖；隨後還原並補 excludes（abandon 先於 exclude 的正確時序）。
- **-v2 excludes 收窄至 openspec ref**：保留舊識別可尋址性（abandon 需 authority），僅排除實際碰撞的 openspec 認領。
- **-v2 重識別補 excludes 斷開舊識別的 source 認領**：消除 confirmed source collision 造成的 repo provider degraded（比照 dispatch-reliability-batch 先例）。
- **封存 14 個 7/25–26 遺留 active OpenSpec changes**：功能已 merge 但缺 specs delta，validate --all 14 fail 擋所有 ship preflight；官方 archive 後 0 failed。
- **build 卡指引明令 pinned tasks/todo 僅可切換 checkbox**：修正 builder 註記 plan 文字超出 checkbox 容忍造成 drift 卡死。
- **Issue #315 補遺 3：review StructuredOutput 工具 schema 開放 authority_hashes**：additionalProperties:false 下模型無法交出驗證器要求的攻證欄位（#219 佈線缺口）；工具 schema 開放屬性，必填與比對仍由 manager 驗證。
- **Issue #315 補遺 2：review 派工 schema 把 authority_hashes 列入 fixed 逐字照抄**：修正 sonnet reviewer 條件性解讀導致整組省略、review terminal 恆 schema invalid；harvest 精確比對不變。
- **Issue #315 補遺：retry-review 重置時同步失效舊 exited review job**：比照 retry-verify，reset 時標記 failed 讓 resume 走 replacement dispatch。
- **Issue #315：retry-verify 重置時失效舊 exited verification job**：沙箱已清的舊 job 維持 exited 會讓 dispatch 先 terminalize 而永遠 `input snapshot file missing`；reset 時標記 failed，resume 走 replacement dispatch。
- **Issue #313：verify phase 移出 gate ledger 必要集**：verification 卡的 review-only 沙箱依設計不寫 ledger，要求 ledger＝verification 卡結構性永不可過。`GATE_LEDGER_REQUIRED_PHASES` 收斂為 `{build}`；verify 的獨立證據層是 deterministic verification report 管線。
- **Issue #310 補遺：reviewer frozen authority 驗證沿用 checkbox 容忍**：`verify_authority_in_input_snapshot` 的 pinned 期望值改由 `_authority_map_with_checkbox_tolerance` 提供，checkbox 容忍成立的 tasks/todo 以候選實際 hash 比對；其他差異維持 fail-closed。
- **Issue #310：pinned planning input 對 task checkbox 更新的 drift 容忍**：kind=plan 的 `tasks.md`／`todo.md` 於 raw-hash 不符時做 checkbox-insensitive 比對（baseline 取自 operator_root 並先驗 hash）；其他差異維持 fail-closed。修正卡片契約要求勾選 checkbox 與 verify 派工 drift 檢查的互斥。
- **Issue #308：零 gate 設定下模型自述 gate_evidence 不再觸發 fail-closed**：ledger `gates: []` 時 `authorize_terminal` 跳過 unknown-gate 對照（#261 文件：零 gate＝無 R2 保護）；ledger 非空維持 fail-closed。
- **Issue #302：registry 載入層 claim_key 唯一性改為只約束 ongoing runs**：與 abandon→reclaim（#256 D4／#299）語意對齊；重 claim persist 後 manager 重啟不再無法載回狀態檔。run_id 唯一性維持全域。
- **批次 W1 openspec design.md 補件（#295／#291、#260、#178、#139）**：design kind 的 authority 來源是 `openspec/changes/<change>/design.md`，缺檔時 planning completeness 永遠 incomplete、claim 後 define 繞進 brainstorm 並靜默 needs_human（7/30 批次全卡 define 的根因）。為四個 work item 補上 design.md，使 define 走 planning-complete deterministic 路徑。
- **Issue #299：planning_released 釋放後同 claim_key 可重新 claim**：`work_bridge.start_canonical_workflow` 的 existing-run reuse guard 原對 `superseded` run 無條件短路，未 honor #256 D4 釋放語意，abandon→reclaim 永久死路。新增 `_claimable_existing_runs` 過濾已釋放 run，未釋放 superseded／done／ongoing 行為不變。
- **Issue #277：pre-candidate 失敗恢復與 stale candidate 重評**：新增 `recover-pre-candidate` work/slice action 以處置 candidate 為 null 時的 builder 失敗並回收殘留 worktree；修復 completion 對 `candidate-worktree-dirty` 的快照競態，改為在 tick 時以當前 branch HEAD 動態重評。
- **Issue #286：fanout plan pinning 以 spec 檔自身所在 repo 解析**：修復 `coordinator/autonomy.py` 中 `_infer_repo_root(spec_path)` 於 `PSC_REPO_ROOT` 環境變數存在時盲目回傳 manager host repo 的問題。調整為優先以 `spec_path` 所在目錄向上推導專案 Git repository root；當 spec 位於 manager host 外部的其他 repository（如 `serialwrap` 或 worktree）時，能正確將 relative plan glob 解析至該專案目錄，解決跨 repo ad-hoc 派工觸發 `DispatchReadyError: plan file unreadable` 的問題，並使 `ready` 與 `fanout` 階段對專案 repo_root 的判定維持一致。
- **Issue #273：修復 Monitor refresh 靜默失敗、同 Repo 多 Checkout 衝突與 Source Collision 歸零缺陷**：
  - 缺陷一：`ProjectMonitorService._refresh_work_model` 不再靜默吞掉 `ValueError` / `OSError` 例外，加入 log 紀錄並於 store / status / snapshot 記錄 `last_refresh_error` 與連續失敗計數；`cortex work show` / `work start` 在 snapshot 停更或 refresh 失敗時直接回報真正原因。
  - 缺陷二：`WorkModelRefresher` 掃描專案時依 git 身分去重同 repo 的多個 checkout，優先選擇 canonical checkout 避免 duplicate work item ID 錯誤，並在 status diagnostics 中明確標示碰撞目錄。
  - 缺陷三：`correlate_work_sources` 與 `project_work_items` 發生 source collision 時，將影響限縮於相關 Work Item 並標記 `degraded` 診斷，不再導致整個 repo 的 Work Item projection 歸零。
- **Issue #277：repo rebind 後 monitor 投影與 work authority 跟隨 `PSC_REPO_ROOT`**：
  - monitor 設定解析遇到 deprecated legacy 設定（`PAULSHACLAW_CONFIG` 或 `paulshaclaw.yaml`）時改為 fail-loudly 拋出 `ValueError`，不再靜默降級至舊 repo 設定。
  - monitor config 載入時自動將 `PSC_REPO_ROOT` 納入監控專案集，確保 instance 換綁 repo 後 monitor 投影能正確解析當前 repo 的 work items。
  - `work link` / `unlink` 重寫 `.cortex/work-items.yaml` 時保留原始鍵（key）排序，避免無謂重排導致 git status 變 dirty，並於寫入後新增 readback 讀回驗證。
- **Issue #284：persona 歷史回放測試改釘固定錨點**：原以浮動的 `main` ref 回放，merge／rebase 進行中 `prs_scanned` 可能落到 0 而讓斷言失敗（「掃不到」被誤判為「有誤殺」，實測 merge 期間出現過一次隨機紅）；且 `actions/checkout` 預設 shallow（實測 depth 1 的 clone `git log --merges -n 30` 回 0 個），CI 實際回放範圍遠少於宣稱的 30 個 PR 卻仍以「歷史回放零誤殺」通過。改釘固定 commit 錨點（`6813058`，即 #135 切換 enforce 當下的 main），使結果不隨 HEAD 移動而改變，並新增「錨點不可解析時明確 skip」的分支，避免在淺 clone 環境靜默宣稱零誤殺。偵測新 PR 誤殺仍由 CI `persona-scope` workflow 對 PR diff 負責，`replay.py` CLI 的動態回放能力不受影響。
- **Issue #261（收口）：gate ledger 由 manager 掌控的 wrapper 產生，canonical envelope 實際生效**：
  新增 `paulsha_cortex/coordinator/gate_ledger.py`，由 `launcher.build_wrapper_script` 產生的
  headless wrapper 在模型行程結束**之後**執行——`<模型 argv>; printf %s "$?" > <sentinel>;
  python3 -m ...gate_ledger --out <ledger> --worktree <wt> >/dev/null 2>&1`。三段以 `;` 串接，
  模型失敗時 sentinel 與 ledger 仍會產生；sentinel 早於 gate 階段寫入，模型 exit code 不被
  gate 耗時污染；gate 輸出導向 `/dev/null`，不污染 terminal evidence 解析。gate 清單由 operator
  以 `PSC_GATE_CMD_<NAME>` 宣告（沿用 `PSC_PREFLIGHT_CMD` 的 typed-argv 規範、拒絕 shell wrapper），
  exit code 來自真實 subprocess，模型既不能選 gate、不能定 exit code，也拿不到 ledger 路徑
  （由 job `log_path` 推導、位於 manager 的 log_dir），因此 R2 的重驗不再是「拿模型的話驗模型的話」。
  跑不起來或逾時的 gate 一律記為 `failed`。`_workflow_job_prompt` 改發 `schema_version: 2` 的
  canonical envelope（含 `diagnostics` 與 `gate_evidence`），舊形狀維持相容讀取；build／verify
  的 `passed` 在缺少 ledger 時 fail closed。schema retry 計數經 workflow provider observations
  投影到 Monitor work item envelope，`cortex inspect work` 會列出 `schema_retry[<card>]:
  <count>/<limit>`；計數沿用既有 `attempts` 欄位而非新增 `WorkflowRun` 欄位，避免 #205 那類
  「新欄位讓每個 run row 變 unsupported、整份 projection degraded」的 regression。
- **Issue #261：terminal/result contract 誠實表達 gate failure，消除 fail-open 破口**：
  新增 `paulsha_cortex/coordinator/terminal_contract.py` 作為 terminal/result 契約的單一
  真相源——帶 `schema_version` 的 canonical envelope 讓 `passed`／`failed`／`needs_human`
  三種終局狀態在 build／verify／review 三類 card 上對等可達，舊形狀 payload 走相容讀取
  路徑並帶 legacy 標記（不拒收既有 run）。`terminalize_workflow_job` 在任何狀態採信之前
  先做確定性 cross-check：manager 重讀自己 evidence 目錄下的 gate ledger，只要有任何
  gate 實際失敗（含 ledger 自身矛盾，例如記了非 0 exit code 卻標 passed），terminal 自稱
  的 `passed` 一律 fail closed，並保留「哪一個 gate、期望值、實際值」的可操作原因；模型
  文字、exit code 為 0、無明確錯誤三者皆不再構成成功授權。StructuredOutput 的 wrapper
  正規化改採明確白名單且同一確定性 mismatch 只嘗試一次，未知形狀終止為可操作錯誤而非
  被寬鬆解析吞掉；`resume_workflow_run` 的 malformed-terminal 重派改為有上限、有計數器
  （持久化於 `run.attempts`，逾限轉 `needs_human` 並回報 `schema_retry_count`／
  `schema_retry_limit`／`last_validation_path`／`last_validation_reason`），終結同一格式
  錯誤反覆回派模型的 retry storm。terminal parse 失敗時保留 observed HEAD／job id／
  失敗原因的唯讀診斷，但與授權欄位分離儲存，可觀測不等於可授權。verifier 與 reviewer 的
  StructuredOutput schema 與 prompt contract 同步放開非通過狀態，非通過狀態由 manager
  fail closed 為可操作錯誤，而不是被誤判成 schema 壞掉。
- **Issue #256：planning claim 前向恢復與放行語意修正**：`recover-planning` 新增環境/內容分類判定與 CAS 重入保護，`abandon` 加入 `planning_released` 釋放標記讓 `needs_human` 的同識別 work item 可重 claim，並讓 `resume` 對 `needs_human` 回傳合法 `next_actions`（停在 `define` 的環境類 planning 失敗才含 `recover-planning`，內容類仍只給 `abandon`）與具體 `blocking_reason`，恢復稽核紀錄亦保存前後 run 狀態；`work_actions`、`control/contract`、`coordinator/cli`、`porcelain/run`、`work bridge/registry` 均同步放行與解讀新動作。
- **Issue #273（實例修正）：openspec change 內 frontmatter `work_item` 不一致**：`openspec/changes/fix-systemctl-install-failure/tasks.md` 的 `work_item` 與同目錄 `proposal.md`／`design.md` 不同，使同一 openspec source 被兩個 work item 宣告擁有，觸發 confirmed source collision，導致 `hamanpaul/paulsha-cortex` 的 monitor work item projection 由 43 個降為 0、任何 work item 皆無法 claim。本次對齊 frontmatter 使 projection 恢復；靜默吞例外與 collision 影響範圍過大的根本問題見 #273。
- **Issue #270：CLAUDE.md 的 changelog 要求對齊 engine R-09**：agent 指引原先三處（改 code 時、
  claim done 前）都只要求 `CHANGELOG.md [Unreleased]`，與 R-09 實際檢查的 `changelog.d/*.md`
  fragment 不一致，導致照指引交付的 PR 必然掛在 `policy / check`（#266／#267／#268／#269
  四個 PR 同時實證）。改以 fragment 為硬性 gate、寫明檔名 slug 慣例與「fragment 須 commit
  才進 diff」；claim-done checklist 的 policy_check 一項補上帶 `--pr-title`／`--pr-body`／
  `--pr-labels`／`--pr-base-ref`／`--pr-head-ref` 的完整命令形式（裸跑會給出假的 `fail: 0`，
  因為 CI 會傳這五個參數並啟用一批 PR／diff-aware 規則）；並移除指向不存在檔案的
  `.github/pull_request_template.md` checklist 項目。`CLAUDE.md` 為 canonical 真檔，
  `AGENTS.md`／`GEMINI.md`／`.github/copilot-instructions.md` 的 symlink 自動同步。
- **Issue #155：修補 install/upgrade 未遷移 Codex 全域 relay hook**：新增
  `paulsha_cortex.deploy.hooks.reconcile_codex_hooks()`，於 `cortex install
  service` 流程中 idempotent 改寫 `$HOME/.codex/hooks.json` 內
  `managedBy: psc-coordinator-relay` 的 legacy entry（指向已不存在的
  `paulshaclaw/scripts/coordinator/psc-relay-hook.sh` 絕對路徑）為 canonical
  的 `cortex relay-hook` 指令，改寫前自動備份原檔（`hooks.json.bak-<hex>`），
  只動 Cortex 自管的 entries，其餘 owner（例如 `paulsha-memory`、
  `psc-bro-return`）與已是 canonical 的設定維持不變，修補 Codex Stop hook
  exit 127 的 install migration gap。全程 fail-open：套件內建 manifest
  本身損壞/缺失時也不拋例外中斷 `cortex install service`，改回傳
  `changed=False` 並附可辨識原因的 detail；`daemon-reload`／`enable` 等
  systemctl 步驟失敗時，若 hook 遷移已先行發生，回報訊息會一併帶出遷移
  結果，避免副作用（改檔＋備份）發生卻未讓 operator 知情。
- **Issue #255：AGY_MODEL_ID 改用 agy 實際輸出的 kebab id**：`agy models` 現在輸出
  kebab id（如 `gemini-3.1-pro-high`），但 `model_identities.AGY_MODEL_ID` 仍寫死
  顯示名 `Gemini 3.1 Pro (High)`，導致 `probe_agy_capability` 字面比對必然
  miss，套件預設下唯一的 planning identity 永遠 probe 失敗、work-item workflow
  卡死在 `define/needs_human`。`AGY_MODEL_ID` 改為 `gemini-3.1-pro-high`；
  `probe_agy_capability` 新增正規化容錯比對（`_normalize_model_token` /
  `_resolve_agy_cli_token`），顯示名與 kebab id 之間的格式落差不再是硬性
  依賴，且一律用 `agy models` 實際列出的字面值呼叫 `--model`；`model-not-listed`
  失敗時 `diagnostic` 帶出實際可用清單方便除錯；v1 schema 設定檔沿用舊顯示名
  的既有寫法仍會被正確識別為 canonical agy planning identity（向後相容）。同步
  更新套件內建 `data/model-identities.yaml` 與 `README.md` / active openspec
  spec 的範例值。
- **Issue #264：workflow phase job 收工不再誤走 slice lane gate**：`paulsha_cortex/coordinator/manager.py` 的 completion sweep 新增 `_is_workflow_lane_job()`（以 job 的 `workflow_run_id` 是否存在機械判定 lane 歸屬），completion sweep 對帶 `workflow_run_id` 的 job（workflow lane 的 phase job，本就不註冊進 `slices` 表）不再查 `slices` 表、不再產出 `needs_human`/`missing-slice-proof`，改寫入新的 `gate_status="workflow-tracked"`／`gate_reason="workflow-lane-job"`（job 失敗則 `gate_status="failed"`，`gate_reason` 仍為 `workflow-lane-job`，與 slice lane 的 `missing-slice-proof` 機械區分）；`missing-slice-proof` 保留給真正屬於 slice lane、但 slice 關聯缺失的情形，新增 regression test 釘死其 fail-closed 行為未被放寬。修正前受影響的 30 份誤判 manifest（`~/.agents/coordinator/handoff/*.json`，`gate_reason=missing-slice-proof` 且 `slice_id` 為 `wf-<hash>-<phase>` 形式）皆為歷史殘留、其對應 workflow 早已交付合併，建議標記為歷史誤判並保留供稽核（下次 completion sweep 對相同 job_id 為冪等 skip、不會自動覆寫；如需清理由使用者自行對 runtime state 執行 retention 操作，不在本次程式修改範圍內）。
- **Issue #230／#265：`recent_done` 補投影欄位並加入 recency window**：`manager_daemon.py` 的
  `recent_done_provider()`（`build_runtime_status_provider()` 內）除既有 `slice_id`／`gate_status`／
  `at` 外，多投影 handoff manifest 既有的 `gate_reason`／`job_id`／`branch`（manifest 缺欄位時為
  `null`，不拋錯），讓 `needs_human` 條目在 consumer 端（paulshaclaw cockpit）不再只顯示「待裁決 ·
  原因未知」（#230）；範圍不含 `next_actions`——它是 `manager.slice_status_entry()` 算出來的衍生欄位，
  不在 manifest 欄位集合裡，不屬本次修法範圍。同時新增可設定的 recency window（新常數
  `RECENT_DONE_WINDOW_SECONDS`，預設 86400 秒／24 小時，可用 CLI `--recent-done-window-seconds`
  或環境變數 `PSC_MANAGER_RECENT_DONE_WINDOW_SECONDS` 覆寫），過期 manifest 不再進入 `recent_done`；
  window 內無資料時回空陣列，不回退撈更舊的紀錄（#265）。handoff manifest 檔案本身的
  retention／prune 途徑不在本次範圍內，屬 #178 program teardown GC 負責。
- **Issue #254：legacy monitor config 警告去重**：在
  `paulsha_cortex.monitor.config._resolve_config_source` 中加入單一 process 內 per-key
  去重機制，保留既有 legacy 警告文案與設定解析順序，避免 legacy env 與 legacy
  file fallback 在同一 process 重複輸出警告。
- **Issue #253：系統 Service 安裝失敗改為可結構化回報**：`cortex install service` 在 `daemon-reload`、`enable monitor service` 或 `enable manager timer` 失敗時，不再拋出 traceback；改由回傳 `mode=systemd` 的非零 result，訊息固定帶出 systemd stderr、unit directory 與重試指令，並在第一個失敗步驟即停止後續流程。
- **Issue #252：安全化 `cortex doctor` preflight 與 identity 失敗診斷**：對 `PSC_PREFLIGHT_CMD` 與 `model-identities` 失敗做 allowlist 分類，輸出可執行修復方向且不外洩敏感細節，並補齊 fresh-install 相關 preflight 契約文件。

## [0.1.1] - 2026-07-28

### Added
- **Issue #211：新增 pre-claim readiness 檢查與凍結集**：新增 `coordinator/claim_readiness.py`，依成本排序執行六項 pre-claim 檢查（heading/OpenSpec/changelog scope → base SHA → monitor snapshot → GitHub owner → capability → live probe，live probe 最後且帶 TTL 快取），輸出為可序列化的凍結集（frozen SHA/hash 組）而非布林值；失敗分終局（policy scope 契約互斥）與可重試兩類。`work_actions._claim_action` 新增可注入的 `readiness_checker`，任一檢查失敗即在建立 workflow job/worktree/model session 之前擋下。
- **Issue #212：新增 plan review gate 三項判定**：`planning.py` 新增 `plan_review_gate()`，依 cost order 跑完整性（每個 acceptance surface 有對應 task）／契約相容性（plan scope 與呼叫端算好的 R-09/R-16/R-19/R-22 相容，明確排除項目與規則衝突時是 hippo #18 第 9 條的 terminal case）／封套相符（plan 宣告的 `invariant_count`／`artifact_classes` 落在 #209 builder 封套內，封套資料缺席時記 `envelope_unavailable` 可觀測 bypass）三項判定，任一不過即 fail closed；`completion.py` 新增 `final_defect_locus` 訊號欄位，記錄 final 才發現問題出在 plan 而非 candidate 的訊號（供 #137 度量 plan review 漏檢），純 provenance 不影響 semantic match。
- **Issue #213：凍結點移至 plan review 通過之後**：`claim.py` 新增 `claim_identity_digest()`（不含 `mapped_openspec`／`mapped_todo_paths`／`source_revisions` 的穩定 identity）與 `ClaimCandidate.active_plan_review_passed`／`active_claim_identity_digest`；`_existing()` 在 `active_plan_review_passed=False`（plan review 尚未通過）時改用穩定 identity 比對，plan 修訂造成的產物欄位飄移不再被誤判為 authority 變更、不再觸發 supersede（hippo #18 第 3、7 條 v3→v4→… 世代增長 regression）。`planning.py` 新增 `plan_review_freezes_authority()`，把 `plan_review_gate()`（#212）的判定結果對應到「是否可以 freeze」，供呼叫端串接。
- **Issue #214：stage 級 content-addressed execution key**：新增 `registry.compute_stage_execution_key`（涵蓋 repo/work_id/card/phase/executor/model/base_sha/candidate_sha/frozen_input_hashes/action/test_policy）與 `JobRegistry.find_reusable_stage_evidence`（fail-closed reuse 查詢，建立在既有 phase 級 checkpoint 之上、與 `bind_workflow_evidence` 並存不改語意），`manager_daemon.py` 的 workflow-action/start 觸發處可消費此查詢在相同 key 已有可重用 evidence 時短路 dispatch、不增加 model invocation；`CompletionRecord` 新增 `reused_from`（run/job/evidence hash）provenance 欄位，並排除在 semantic match 之外避免良性 reuse 誤觸衝突 quarantine。
- **Issue #215：retry 分類骨架**：`work_actions.py` 新增 `RetryClassification` enum（`model_repair`／`orchestrator_retry`／`authority_restart`／`review_handoff_failure`／`source_owner_repair`，enum 定案，後波不得改名）與 `_classify_retry()`，依 run.current_phase 與 builder job 的乾淨終止／evidence 綁定狀態判斷一次 retry-build 屬 candidate 內容缺陷（`model_repair`）還是 provider/stale base/claim sequencing 等非模型原因（`orchestrator_retry`），不再只看 vN 世代數；`_retry_build_action` 回傳結果攜帶分類。`completion.py` 的 `CompletionRecord` 同步新增可選欄位 `retry_classification`（比照 `reused_from` 的 provenance 模式，排除於 semantic match 之外），供未來 `cortex stat` 依分類彙總。`authority_restart`／`review_handoff_failure`／`source_owner_repair` 的判準留供 #216 補齊。
- **Issue #216：補齊 retry 分類與精準 invalidation**：新增 `work` action `retry-verify`／`retry-review`——`retry-verify` 只重跑 verification（不重建 candidate、不動 build phase），`retry-review` 只重跑 foreign review（不重跑 builder，缺冷凍 plan authority 時 pre-dispatch fail-closed），`registry.py` 新增對應的局部 reset helper（`_manager_reset_workflow_for_retry_verify`／`_manager_reset_workflow_for_retry_review`），只清各自 phase 的 gate_result，其餘 phase 保持不變。`_classify_retry` 擴充 `trigger` 參數（不改既有 MODEL_REPAIR／ORCHESTRATOR_RETRY 狀態推論），補上 AUTHORITY_RESTART／REVIEW_HANDOFF_FAILURE／SOURCE_OWNER_REPAIR 三類判準；`_claim_action` 於 WorkAuthority 宣告變更（claim_key mismatch）時，新增 `_manager_reset_workflow_for_authority_restart` 只 invalidate verify/review gate（build phase 已產出的 Candidate 保持不變），並在 source-owner transfer 尚未完成（#217 防線）時把 `start_canonical_workflow` 的 RuntimeError 轉成結構化 blocked 結果（`retry_classification: source_owner_repair`），確保從未派過任何 builder。`cli.py`／`coordinator/cli.py`／`control/contract.py` 同步補上兩個新 action 的 CLI choices 與 control queue 白名單、`expected_candidate` 驗證。`WorkflowRun` 新增可選欄位 `retry_classification`（provenance-only，比照 `pr_candidate`／`merge_revision` 的 sticky 語意，一般 phase 推進不清除），`work_bridge._completion_draft` 在組裝 `CompletionRecord` 時一併帶出（#215 遺留的接線點），`monitor/providers.py` 的 workflow row 欄位白名單同步放行。
- **Issue #218：work-item 級 repair budget 與 circuit breaker**：`delivery.py` 的 `MAX_FIX_ROUNDS` 依 sizing band 參數化——新增 `repair_budget_for_band()`（green=1、yellow=2、band 未掛時 fail-soft 回退現行值=2、red 防禦性拒絕），`ReviewLoop` 新增 `max_fix_rounds` 欄位取代寫死的模組常數，`ShipOrchestrator.merge_if_ready()` 同步改讀 loop 自帶的預算。`work_actions.py` 的 ship 迴圈 repair round 計數由 `active["ship"]["fix_rounds"]`（會在 multiple-delivery-targets-unsupported 復原路徑被整包 pop 掉）提升到 `active["repair_rounds"]`（work-item 頂層，與 `delivery_binding`／`snapshot_hash` 平級），確保計數跨 ship-state reset 仍存活；第 3 次 model_repair 觸發 needs_human 時，回傳結果一併附上剩餘 repair scope、已重複 stage、合法下一步（`maintainer-review`）與預估 invalidation 範圍。
- **Issue #219：reviewer input attestation**：`review.py` 新增 `verify_authority_in_input_snapshot()`，在 reviewer job 派工前證明 frozen plan/authority 的 exact path＋hash 已在 input snapshot 中（缺席或 hash drift 皆 fail closed）；`validate_review_verdict()` 新增可選 `expected_authority_hashes`，要求 reviewer verdict 回填其實際讀到的 authority hash，缺漏或不符即拒收 PASS。`manager.py` 新增 `_reviewer_input_patterns()`，即使 review card（如 `code-review`）宣告 `requires: []`，仍會把 run 的 frozen planning authority 併入 reviewer 的 input snapshot 派工前驗證，並在 `_workflow_job_prompt`／`terminalize_workflow_job` 同步要求與驗證 `authority_hashes` 回填，堵住 hippo #41 v3（reviewer 未取得 frozen plan 卻誤判 PASS）同型缺口。
- **Issue #221：五維 sizing 評分（三維機械算＋二維宣告）**：`planning.py` 新增 `compute_sizing_score()`／`SizingScore`，純函式計算 acceptance_surfaces／spec_stability／orchestration，並讀取 plan frontmatter 宣告的 `domain_breadth`／`state_consistency`；`deck/schema.py`／`deck/compile.py` 同步落地 gate_spine 兩層制（`band_triggered` 加掛層，預設 Yellow 起掛，band 未知時保守全含），`feature-oneshot` combo 的 `adversarial-review` 移入加掛層。
- **Issue #222：band 判定與 CompletionRecord 記錄**：`claim.py` 新增 `sizing_band()` 純函式，把 #221 `compute_sizing_score()` 的五維總分（0–10）依門檻（Green 0–3／Yellow 4–6／Red 7–10，沿用 `deck.schema.BAND_LEVELS`）換算成 band，供 `claim.py`／`registry.py`／`completion.py` 三處共用同一份門檻；`workflow.py` 的 `WorkflowRun` 新增可選欄位 `sizing_score`／`sizing_band` 作為 work item 快照（`registry.py` 的 `_manager_create_workflow_run`／`_manager_update_workflow_run` 同步支援，每次 repair／re-claim 都由呼叫端重新算過寫入，不沿用 claim 當時判定）；`completion.py` 的 `CompletionRecord` 同步新增可選欄位 `sizing_score`／`sizing_band`（比照 `reused_from` 的 provenance 模式，band 需與 score 門檻一致，排除於 semantic match 之外）與 `sizing_declaration_drift`（記錄宣告模組數 vs candidate 實際變更數，供 #210 後驗）。
- **Issue #223：Red band 轉 needs_decomposition 與 planner 回派路由**：`workflow.py` 新增 `WORKFLOW_FACETS` 成員 `needs_decomposition` 與 `WorkflowRun.decomposition_depth`（0–2 層快照，逾限 fail-closed）；`claim.py` 新增純函式 `decomposition_route()`（Red band 拆分深度達上限即回 `needs_human`，否則回 `needs_decomposition`），`_resume_decision()`／`_validate_candidate()` 讓已標記 needs_decomposition 的 run 在 claim/resume 掃描時原樣浮現、不再以原身分繼續重試；`work_bridge.workflow_status()` 新增對應 facet→status 分支。`manager._dispatch_workflow_card` 在 planner/plan phase 完成、即將推進到 build 前掛載此路由：Red band 攔下並改設 facet（不推進 phase、不得跳階/倒退），Green／未掛 band（舊 plan fail-soft）維持既有行為直接進 build。`decomposition_depth` 進可觀測面：`cortex stat --decomposition-depths` 依深度彙總，`monitor/providers.py` 的 workflow row 欄位白名單同步放行。「Yellow 先 plan review 再派」與 Red 之後由 planner 實際產出新子 work item（supersede + 各自重新 claim）的收斂路徑，因上游 sizing 計算與 plan review gate 的生產接線尚未就位，本卡僅完成 band→路由的狀態機部分，留待後續銜接。
- **Issue #208：sizing／plan-review／凍結集五條生產接線收口**：`#211`–`#223` 的 13 張子單已把各自機制落地，但 verifier 逐卡確認後留下五條殘留接線，本次一次收口——(1) `work_bridge.start_canonical_workflow` 在 claim 建 run 前嘗試（fail-soft）算好 sizing：能取得既有 plan artifact（`_artifact_rows` 已找到的 `openspec/changes/<change>/tasks.md` 等）與 deck combo 資訊時，呼叫 `planning.compute_sizing_score()` → `claim.sizing_band()` 並透過新增的 `work_bridge.current_sizing_snapshot()` 共用 helper 傳入 `_manager_create_workflow_run(..., sizing_score=, sizing_band=)`；`applicable_contract_rules` 固定餵 `planning.ACCEPTANCE_SURFACE_RULES` 全集（R-09/R-16/R-19 對任何程式碼變動類工作項目皆適用，不反推 scope/code_paths）。拿不到就維持 `None`，`work_actions._claim_action` 回傳的 run dict 新增可觀測標記 `sizing_unavailable`。(2) `manager._dispatch_workflow_card` 的 plan phase 完成掛載點（#223 Red 攔截同一位置）新增 Yellow 分支：`run.sizing_band == "yellow"` 時呼叫 `planning.plan_review_gate()`（`acceptance_surfaces` 取自 plan 自己宣告的 `artifact_classes` frontmatter），ready 才放行進 build 並把新增的 `WorkflowRun.plan_review_passed` 欄位寫回 True；terminal 失敗轉 `needs_human`，non-terminal 失敗不推進、原樣可重試；Green／band None 完全不呼叫 gate，維持現行為。`work_actions._claim_action` 組 `ClaimCandidate` 時同步接上 #213 的 freeze 欄位：`active_plan_review_passed`（非 Yellow band 或無 active run 一律視為已通過，比照 pre-#213 立即凍結）、`active_claim_identity_digest`（`claim.claim_identity_digest(authority)`）。(3) `work_actions` 的 `_retry_build_action`／`_retry_verify_action`／`_retry_review_action` 成功路徑重算 sizing（同一份 `current_sizing_snapshot` helper），算得出來就 `_manager_update_workflow_run(..., sizing_score=, sizing_band=)` 寫回，算不出來維持現值。(4) `work_bridge._completion_draft` 比照 `retry_classification` 的既有模式，把 `run.sizing_score`／`sizing_band` 非 `None` 時寫入 `CompletionRecord`。(5) `manager._dispatch_workflow_card` 建 builder worktree 時（#211 閉環）：`run.frozen_readiness`（新增欄位，持久化 #211 的 `FrozenReadinessSet`）存在時以其 `base_sha` 為基底呼叫 `ScriptWorktreeCreator.create(..., base_sha=)`，不存在時完全不傳這個關鍵字引數、維持現行為。`WorkflowRun` 新增 `plan_review_passed`（bool，預設 False）與 `frozen_readiness`（可選 dict，驗證 `base_sha` 格式）兩個持久化欄位，`registry.py` 的 create/update 方法與 `monitor/providers.py` 的 workflow row 欄位白名單同步放行。
- **Issue #177：driving-cortex skill**：新增 `skills/driving-cortex/SKILL.md`，提供 agent 編排 coordinator 視角下的 cortex dogfood 批次驅動與交付操作指南（含 seven-section 實務骨架）。
- **Issue #177：driving-cortex skill**：新增 `skills/driving-cortex/SKILL.md`，提供 agent 驅動 cortex dogfood 派工與交付的操作指南（含心智模型、批次啟動、驅動桿、每批部署與已知坑）。

### Changed
- **同步 policy 1.0.14 → 1.0.15**：`.project-policy.yml` 與 `CLAUDE.md`（`managed-by`／`policy_version`／profile 段）皆 bump 至 `1.0.15`；`Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@a764806046c410eb4f254ac0b6a8aec8b7559dab`（= engine tag `v1.0.15`，尾註 `# v1.0.15` 供 R-23 對齊）；`README.md` 開發備註的引擎版號字樣同步更新。本次升版 1.0.14→1.0.15 未新增規則編號（僅新增 tag 觸發的 runtime bundle release workflow），故 `CLAUDE.md` 不需新增「新增規則」段落。
- **本機部署 paulsha-conventions v1.0.15 runtime bundle**：依 release 說明下載 `paulsha-conventions-v1.0.15-cp312.tar.gz`、驗證 SHA-256 後執行 `install.sh`，`~/.agents/skills/preflight-ci` 改為指向新 runtime release 的受管 symlink；既有 skill 目錄已先備份（不影響本 repo 版控內容，屬本機環境變更）。
- **同步 paulsha-conventions 1.0.14 → 1.0.15**：`.project-policy.yml` 與 `CLAUDE.md`（`managed-by`／`policy_version`／profile 段）皆 bump 至 `1.0.15`；`Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@a764806046c410eb4f254ac0b6a8aec8b7559dab`（= engine tag `v1.0.15`，尾註供 R-23 對齊）；`README.md` 開發備註的引擎版號字樣同步更新。1.0.14→1.0.15 未新增規則編號，僅新增 tag 觸發的 runtime bundle release workflow，故 `CLAUDE.md` 不需新增規則段落。

### Fixed
- **Issue #217：source-owner 轉移原子化**：`work_bridge.start_canonical_workflow` 新增顯式斷言——同 repo 下若有其他 work_id 的 ongoing WorkflowRun 其 `issue_refs` 與本次 `mapped_issues` 重疊，且該 run 尚未 terminal（`status` 不在 `{superseded, done}`），一律拒絕新 claim，避免 hippo #41 v3→v4 owner 轉移競態重現的 `missing_issue`/`human-intervention-required` run。`claim.load_work_authorities` 同步補上「同一 repo 下每個 issue 至多一個 work_id owner」的結構性不變量，讓轉移中途若快照仍出現雙 owner，任何 claim/ship/abandon 呼叫都會在載入 authority 時即拒絕，而非悄悄挑一個贏家。
- **Issue #220：final attestation 必須先於 merge mutation**：`github_delivery.py` 的 `GitHubDeliveryClient.merge_if_ready()` 拆成兩段——`evaluate_final_gate()` 只重讀 remote facts 並評估閘門，回傳可持久化的 `FinalGateVerdict`（綁定 repo／PR／candidate head／authority digest）；`commit_merge()` 要求傳入該 verdict，且 repo／PR／candidate／authority digest 須與呼叫當下完全相符，否則 fail closed，從不下 merge 指令。`delivery.py` 的 `ShipOrchestrator.merge_if_ready()` 改為先呼叫 `evaluate_final_gate()` 取得 verdict、綁定 `work_authority_digest(authority)`，才呼叫 `commit_merge()`，結構性堵住「先 merge 再補 attestation」的倒置情形（hippo #18 實案）。`merge_if_ready()` 維持既有相容行為。
- **Issue #98：修正 dispatch spec root 推斷**：`_infer_repo_root()` 在 `PSC_REPO_ROOT` 設定且 spec 位於 repo 外部時，改回傳 `paths.repo_root()`，避免沿 spec 路徑 `.git` 向上尋找導致誤判。
- **Issue #118：跨 repo 派工 builder scope 修正**：`builder` persona 的 `write_paths` 已改為 `**`，不再綁定 `paulsha_cortex/**`，避免跨 repo 派工時對目標 repo 路徑誤拒，保留 worktree 邊界與其他 persona 安全限制原則。
- **Issue #148：service install 不可覆寫既有 manager env 的 Python 指向**：`cortex install service` 當既有 `PY` 指向不同有效 venv 時會拒絕安裝，並修正既有相對/無效 `PSC_AGENTS_ROOT` 的覆寫邏輯，避免既有 runtime 設定被破壞。
- **Issue #175：reclaim PR inheritance**：`start_canonical_workflow` needs_human 與 start 兩條路徑都不再帶入 `pr_refs`，`GitHubTerminalProvider` 不會將 `state=CLOSED`（未合併）PR 加入 `closing_links`，避免 closed PR 異常延續到 delivery authority。
- **Issue #101：deck emit frontmatter 補齊 auto dispatch 合約**：`paulsha_cortex/deck/compile.py` 讓 `--emit` 預設輸出非空 `target_branch`（`feature/<change>`，缺 `change` 時 fallback `feature/<slug>`）並補齊 `verification` skeleton（含 `persona-scope`、`name=policy` command、`tests` 與 `full_suite.baseline=no-regression`）。
- **Issue #158：openspec archive 產出規格 Purpose 改進**：`openspec archive` 會以 change proposal 的 `## Goals`（無法取得時備援其他段落）填補 archived spec 的 `## Purpose`，同時補齊本次變更關聯九份既有 specs 的 `Purpose` 內容。
- **Issue #169：onboarding-docs 規範檢查 regex 韌性**：`tests/test_onboarding_docs_contract.py` 的 `BASH_FENCE_RE` 改容忍 CRLF 與 ` ```bash ` 後空白，`PERSONAL_ABSOLUTE_PATH_RE` 擴充 Windows `C:\Users` 絕對路徑偵測。
- **Issue #134：multi-issue build 支援最小 issue 為主 branch 且以 run repository 建立 worktree**：builder 會在 run 含多個 issue 時，使用編號最小的 `feature/<主issue>-<work_id>`，並由 run repo 作為 `ScriptWorktreeCreator` 的 git 工作目錄。
- **Issue #152：mutation request 逾時改採分級 timeout + pending 回報**：`coordinator/cli.py` `_submit_mutation_request` 依 `req_type` 套用分級 timeout（`fanout`/`tick` 60 秒、`complete`/`work`/`work-action`/`run` 30 秒、其餘 5 秒），`poll` timeout 時保留 `req_id` 與追蹤指引並回傳 `EXIT_SUBMITTED_PENDING`（exit 3）避免成功派工被誤報為失敗。
- **Issue #153：failed slice 可恢復與外部 jobs.json 重載**：`manager.apply_slice_action` 現在可對 `failed` slice 進行 `retry-build`，`JobRegistry` 也會偵測 `jobs.json` 外部修改並重載持久化狀態。
- **Issue #99：git runner 與 systemd 單元以 repo root 為基準**：`dispatcher._default_git_runner` 現在以 `git -C <repo_root>` 執行 git 呼叫；`cortex-manager.service` 與 `cortex-monitor.service` 渲染時皆加入 `WorkingDirectory=<repo_root>`，避免 daemon 於非預期目錄執行 git 或長駐服務命令。
- **Issue #182：monitor workflow provider 跳過 superseded run 的 issue authority**：`WorkflowRegistryProvider` 建立 workflow_links 時不再對 `superseded` run 主張 issue/pr/openspec authority，避免廢棄 run 的 issue_refs 與其他 work item 衝突使 provider degraded；degraded 會凍結 `project_work_items` 於舊 snapshot，讓新以 work-items.yaml `github_issue` link 綁定的 source 永遠不進入 monitor read model。
- **Issue #100：tick 的 dispatch 失敗回報與 manager daemon log 時間戳**：`DispatchReadyError` 現在輸出每 slice 的 `slice_id`、例外型別與訊息摘要；`tick` 在 fanout 失敗時保留已成功 dispatch 的 jobs 並回傳 per-slice 錯誤欄位；`manager_daemon` 的錯誤 log 改以可解析 ISO-8601 時間戳為行首。
- **Issue #246：auto-claim 單一 work item 失敗不再中止整個 periodic tick**：`work_actions.run_auto_claim_scan()` 的逐 authority 迴圈新增 try/except，任一 authority 的 claim 流程 raise（例如 `resolve_trusted_repo_root` 對不在信任清單的 repo fail-closed）改為 append 一筆帶 `repo-root-unresolved`／`claim-failed` reason 與不含機密的錯誤摘要的 blocked 結果並處理下一個 authority，不再讓整批掃描中止；`manager_daemon.build_periodic_tick_runner` 的 `execute()` 同步包住 auto-claim 呼叫，失敗時 `_log_error` 記錄安全脈絡（action／work_id／repo／source revision）並讓 `auto_claims` 降級為空 list，後面的 workflow resume 迴圈與 `run_tick` 照常執行，回傳 summary 新增 `auto_claim_failed`／`auto_claim_error` 供 operator 觀察降級（現場證據：17,013 次同一則 `ValueError` 每 4-5 秒重複，持續 22 小時）。
- **Issue #249：daemon tick 失敗不再退化為熱迴圈**：`manager_daemon.run_loop` 的 periodic tick 失敗時同樣推進排程時鐘並採指數退避（`tick_interval * 2^min(連續失敗數, 4)`，上限 16 倍），成功後即重置為正常間隔；連續失敗達門檻（`TICK_CIRCUIT_BREAKER_THRESHOLD = 6`）後熔斷暫停 periodic tick 一小時冷卻期（`TICK_CIRCUIT_BREAKER_COOLDOWN_SECONDS`），request 佇列（含人工 tick request，操作者的救援管道）處理完全不受影響，且人工 tick 成功會自動重置熔斷狀態。`status.json` 的 `daemon` 區塊新增 `consecutive_tick_failures`／`tick_circuit_open`／`last_tick_error`（已去識別化的型別＋原因摘要）觀測欄位。`_log_error` 對同一錯誤簽章的連續重複改為每 50 次輸出一則彙總，不再逐行全文重刷（第一次仍完整輸出）。
- **Issue #206：durable GitHub provider authority invalid 仍復發且缺診斷**：`claim.load_work_authorities()` 改為逐 row 隔離解析——單一 row 的 provider/欄位驗證失敗不再中止整批載入，只把該 row 標記為不可用並繼續解析其餘 row；`load_work_authority(repo=, work_id=)` 因此不再被無關 repo 的壞 provider 誤阻斷，同時對「目標本身就是壞 row」的查詢改拋出帶 reason code 的精準錯誤，維持既有 fail-closed（壞 row 仍不出現在回傳結果中）。新增 `AuthorityValidationError(ValueError)`，攜帶不含機密的 `reason_code`／`repo`／`work_id`／`provider_id`／`field`，並讓 canonical（`provider-authority-*-canonical`）與 legacy（`provider-authority-*-legacy`）schema 的失敗 reason 各自可分辨；#217 的 identity 重複／雙 owner 完整性檢查維持原 raise 行為未動。
- **Issue #158：`openspec archive` 產出規格 Purpose 初始化缺失**：`openspec archive` 現在在 archive 產生 `openspec/specs/*/spec.md` 後，會用對應 change proposal 的 `## Goals`（或備援欄位）填補 `## Purpose`，不再保留 `TBD` 預設文字。
- **Issue #118：跨 repo 派工的 builder 可寫入範圍修正**：`paulsha_cortex/persona/personas.yaml` 將 `builder` 的 `write_paths` 由 `paulsha_cortex/**` 調整為 `"**"`，避免非本體 repo 派工時 write-path 被誤拒。`changelog.d` 與 `CHANGELOG.md` 也同步更新。
- **Issue #101：deck emit frontmatter 補齊 auto dispatch 合約**：`paulsha_cortex/deck/compile.py` 讓 `--emit` 產生 frontmatter 時帶入非空 `target_branch` 並補齊 `verification` skeleton（含 `persona-scope`、`name=policy` command、`tests` 與 `full_suite.baseline=no-regression`）。
- **Issue #100：tick 的 dispatch 失敗回報與日誌時間戳**：`DispatchReadyError` 改為輸出每個 slice 的詳細錯誤摘要，`manager.tick` 將失敗切面轉為包含 `slice_id`/`type`/`message` 的錯誤回傳，並保留已啟動 `jobs`；`manager_daemon` 錯誤輸出改為 `ISO-8601` 時間戳前綴。
- **Issue #98：修正 dispatch spec root 推斷**：`_infer_repo_root()` 在 `PSC_REPO_ROOT` 設定且 spec 路徑位於 `paths.repo_root()` 之外時，改回傳 configured repo root，避免沿外部路徑 `.git` 向上掃描導致錯誤的 repo 判斷。
- **Issue #99：git runner 與 service units 使用 repo root 定位**：`_default_git_runner` 改為以 `paths.repo_root()` 呼叫 `git -C <repo_root>`，並將 `cortex-manager.service` 與 `cortex-monitor.service` 渲染加入 `WorkingDirectory=<repo_root>`，避免系統d 啟動目錄漂移。
- **Issue #152：mutation request 分級 timeout 與 pending 回報**：`_submit_mutation_request` 依 request 類型套用分級 timeout（`fanout`/`tick` 60 秒、`complete`/`work`/`work-action`/`run` 30 秒、其他 5 秒），`poll` 超時時保留成功派工的可追蹤結果（含 `req_id`），回傳 `EXIT_SUBMITTED_PENDING`（3）避免將成功派工誤判為失敗。
- **Issue #148：service install 不可覆寫既有 manager env 的 Python 指向**：`cortex install service` 當既有 runtime env 中的 `PY` 指向不同有效 venv 時，改為直接中止並回報清楚錯誤；同時修正既有相對/無效 `PSC_AGENTS_ROOT` 的覆寫行為，避免因既有參數異常而中斷或誤覆蓋。
- **Issue #153：支持 failed slice 恢復與 registry daemon 外部 jobs.json 重載**：`apply_slice_action` 現在可對 `failed` Slice 執行 `retry-build`，`registry` 會支援偵測 `jobs.json` 外部改動並重載，並保留恢復 action 供運維介面重入。
- **Issue #182：monitor workflow provider 不再對 superseded run 主張 issue authority**：`WorkflowRegistryProvider` 建立 workflow_links（issue/pr/openspec authority）時跳過 `superseded` run，避免廢棄 run 的 issue_refs 與其他 work item 的 run 衝突導致 provider degraded；degraded 會使 `project_work_items` 凍結於舊 snapshot，讓新以 work-items.yaml `github_issue` link 綁定的 source（如 #100）永遠不進入 monitor read model。superseded run 仍作為來源顯示，只是不再主張 issue authority。
- **Issue #134：multi-issue build 不再限制只允許單一 issue**：Builder 在 build phase 會採用排序後最小的 issue 編號作為 `feature/<issue>-<work_id>` 的主 branch，並改用 run workspace repo 作為 worktree 建立來源。
- **Issue #175：reclaim PR inheritance**：`coordinator` 在 needs_human 與 start phase 都不再帶入任何 `pr_refs`，並且 `GitHubTerminalProvider` 不會把 `state=CLOSED` 但未合併的 PR 轉為 `closing_links`，避免 closed PR 仍污染關聯主線閉環。
- **Issue #169：onboarding-docs 規範檢查 regex 韌性**：放寬 `BASH_FENCE_RE` 以支援 CRLF 與 ` ```bash ` 尾隨空白的 code block，並擴充 `PERSONAL_ABSOLUTE_PATH_RE` 偵測 Windows 使用者絕對路徑。

### Documentation
- **Issue #143：`load_config` explicit/ambient 邊界明確化**：新增 `docs/monitor-config.md` 記錄 `load_config` explicit mode 與 ambient mode 的差異，說明 `load_config(config_path=<explicit>)` 僅載入指定設定檔，不合併 ambient `project-hippo.yaml`，未帶 `config_path` 則仍合併可見的 ambient hippo projects。
- **Issue #208 收口文件**：`CHANGELOG.md [Unreleased]` 彙整 `#211`–`#223`＋接線收口共 14 條 fragment entries；workstream `cost-governance-cluster/todo.md` 更新收斂結果與殘留事項。
- **Issue #143：`load_config` explicit/ambient 語義明確化**：補齊 `docs/monitor-config.md` 記錄 `config_path` 指定時不合併 ambient `project-hippo.yaml`、未指定時仍保留 ambient 合併行為。
- **新增 `cost-governance-cluster` workstream**：紀錄成本治理／派工決策叢集的跨 issue 收斂結果——`#208` 拆分為 13 張可派工子單（`#211`–`#223`）、`task_type` 主軸定案、freeze point 位移、三種閘的邊界、gate_spine 兩層制，以及封套與 registry 的現況事實。

## [0.1.0] - 2026-07-24

### Added
- **release-pipeline 發版工作流**：新增 Python 3.10–3.13 測試矩陣、build/smoke-install CI，以及 tag `v*` 觸發的 GitHub Release 資產發佈流程。
- **B9 release-pipeline 規劃產物**：批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **onboarding-docs 新手文件集**：新增 Quickstart、Upgrade、Rollback、Troubleshooting、Concepts、Admin、Runbook 七份 onboarding 文件與 README 導覽段。
- **release 版本安裝指引**：Quickstart／Upgrade 補上從 release tag（`@v0.1.0`）或 GitHub Release wheel 安裝特定版本的說明，避免只裝到 `main` HEAD。
- **B8 onboarding-docs 規劃產物**：批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **B7 porcelain-init-sample 規劃產物**：init-sample 批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-init-sample 導引式 sample CLI**：新增 `cortex init-sample`，包裝 `deck compile --emit`，固定維持 `dispatch: hold`，並輸出必補欄位、`deck verify` 指令與手動翻 auto 指引。
- **porcelain-run-recover 高階執行與復原 CLI**：新增 `cortex run tick/fanout/complete/work` 與 `cortex recover slice/work/brokers/service` 家族、顯性 request ID、`--wait`，以及 versioned JSON 輸出。
- **B6 porcelain-run-recover 規劃產物**：run/recover 家族批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-bootstrap 單步上手 CLI**：新增 `cortex bootstrap`，把 preflight、`service install/start`、`inspect status/doctor` 摘要、`--dry-run` 與非阻斷 `--sample` 串成單一 `cortex-porcelain/bootstrap/v1` 入口。
- **B5 porcelain-bootstrap 規劃產物**：bootstrap 批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-service service 管理 CLI**：新增 `cortex service install/start/stop/restart/status/logs/uninstall` 家族、versioned `cortex-porcelain/service/v1` JSON 輸出，以及 systemd/fallback runtime 與 log source 切換。
- **B4 porcelain-service 規劃產物**：service 家族批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-inspect 唯讀檢查 CLI**：新增 `cortex inspect status/job/ready/work/doctor/service` 家族、versioned `cortex-porcelain/inspect/v1` JSON 輸出，以及 systemd unit exec path stale 偵測。
- **B3 porcelain-inspect 規劃產物**：inspect 家族批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-request request 追蹤 CLI**：新增唯讀 `cortex request list/show/wait/logs` 家族、versioned `cortex-porcelain/request/v1` JSON 輸出，以及 request timeout 後的顯性追蹤面。
- **B2 porcelain-request 規劃產物**：request 家族批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **porcelain-skeleton registry 與頂層路由骨架**：新增 stdlib-only porcelain 命令註冊表、`cortex --help` 動態 porcelain commands 區段，以及 coordinator 透傳前的外掛分派點，供 B2+ 家族各自登記子命令。
- **porcelain-skeleton rollout 補充規格**：B1 驗收要求與 B2+ 接軌契約。
- **B1 porcelain-skeleton 規劃產物**：路由骨架批次的 Work Item 綁定、規劃四件套與 OpenSpec change。
- **cortex CLI 版本輸出**：新增頂層 `cortex --version`，輸出已安裝套件版本；若無 package metadata 則回退為 `0.0.0+unknown`。
- **canary follow-ups workstream todo**：記錄 dogfood canary 實跑發現的後續事項，並作為 work authority 的新增 confirmed 來源。
- **dogfood canary 規劃產物（add-cortex-version-flag）**：為 `cortex --version` canary 批次建立 confirmed Work Item 綁定、accepted 規劃四件套與 active OpenSpec change（`cli-version-reporting`），供 coordinator dogfood 派工消費。
- **Porcelain CLI UX 規格與 v0.1.0 release plan**：凍結七家族（bootstrap/request/run/inspect/recover/service/init-sample）命令詞彙、exit code 契約、`--json` schema 穩定策略、request_id 顯性化 UX 與 TUI 邊界契約；並定義 v0.1.0 批次順序、release 程序（GitHub Release）、升級回滾與 KPI。

### Fixed
- **pyproject 補 readme**：`[project]` 宣告 `readme = "README.md"`，使 build 產物通過 `twine check --strict`。
- **ship source_revisions 漂移容忍**：`completion_records_semantically_match` 現在把 `work_authority.source_revisions` 視為揮發欄位，讓跨多次 main 前進的長期 in-flight run 在 ship 時不再因 source_revisions 合法漂移誤判 `completion record reread WorkAuthority mismatch`。
- **CompletionRecord immutable 重用與 provider 缺檔韌性**：已完成/已合併 run 的 CompletionRecord 一旦已有可驗證舊檔，後續 reconciliation 會直接重用既有 record，不再因 `work_authority.source_revisions` 等合法漂移欄位重推導後隔離舊檔；`WorkflowRegistryProvider` 遇到缺失、symlink、越界或內容損毀的 completion 檔時，也只跳過該列並留下 diagnostic，不再讓整個 provider degraded。
- **WorkflowRun completion record 冪等與 provider 韌性**：已完成 run 若因 snapshot/provider revision 漂移而重播 closure，現在會重用既有 CompletionRecord 的揮發 `work_authority` metadata，不再隔離合法舊檔；`WorkflowRegistryProvider` 遇到單筆 completion record 驗證失敗時也只跳過該列，其他 run 的 sources/links 與 validated completions 仍可正常輸出。
- **porcelain-run-recover CLI JSON 邊界修正**：`cortex recover service restart` 不再暴露會誤導 schema 的 `--json` 旗標，`cortex run work --payload` help 也明確改成要求 JSON 檔案路徑。
- **porcelain-bootstrap executor preflight**：`bootstrap` preflight 改為檢查實際 executor 登入態（`copilot` / `claude` / `codex`），移除未列入凍結設計的額外 `gh-auth` gate，且 `copilot` 探測不再要求 `--allow-all-tools`。
- **GitHub terminal PR 分頁**：`GitHubTerminalProvider` 現在會以 cursor 逐頁讀取最多 20 頁 pull requests，完整聚合超過 100 筆的 repo；若頁數仍未收斂則顯式失敗，避免第 101 個 PR 讓 terminal provider 永久 degraded。
- **porcelain-service lifecycle guardrails**：所有 `service` 子命令現在共用 instance 驗證，`logs --follow` 改為 systemd-only 串流且 fallback 明確拒絕，systemctl/journalctl 失敗時 `--json` 也會回傳一致的 `cortex-porcelain/service/v1` 錯誤 envelope。
- **repair 派工注入 bot review findings**：delivery journal 現在會保留 blocking review threads 的檔案/行號/摘錄，repair builder 的 commit-required prompt 會直接附上 needs-fix findings，避免 fix-round 在缺少 reviewer 上下文時盲修。
- **builder workflow identity 先過濾 build capability**：`_select_workflow_identity()` 現在會先排除不具 `build` 能力的 builder 候選，再套用 `primary_domain` 偏好，避免 google primary domain 把 build 卡誤派給僅支援 planning 的身分而陷入 malformed 重派。
- **plan-phase planner 卡可在產物完備時由 Manager 決定性通過**：`writing-plans` 等規劃卡若其 persisted planning authority 對應的 accepted spec/design/plan 已完整存在，manager 會直接把當前 planner step 標記為 `passed` 並推進到下一 phase，不再多派 planner executor。
- **planner workflow identity 不再被 `primary_domain` 釘死**：`_select_workflow_identity()` 現在只對非 planner、非 reviewer persona 套用 domain 偏好，避免 primary domain 是 google 時規劃階段被 agy 單點綁死。
- **model identities custom 優先 packaged 預設**：`load_model_identities()` 合併 packaged 與 custom registry 時改為先放 custom 條目，避免本機不可用的 agy 永遠搶先 operator 在 custom 設定的 planner 而卡在重派迴圈。
- **builder archive gate 交付路徑與 tasks 勾選指示補齊**：builder persona 現在可寫 active OpenSpec、changelog、CHANGELOG 與 superpowers plan 路徑，commit-required workflow prompt 也會明示更新對應 `tasks.md` 勾選且不得改動 pinned input。
- **archive gate 不再把 R-22 advisory WARN 視為阻斷**：`policy_check` 的 doc reference gate 現在只以 return code 判定，避免既有 diff-aware R-22 advisory WARN 讓所有 archive change 永遠卡在 `doc-reference-invalid`。
- **Copilot commit-required 卡補 scoped 檔案/工具權限**：workflow builder 派工現在會在 commit-required 模式只開 `--allow-all-tools` 與必要的 linked worktree Git 寫入目錄，避免 headless Copilot 因無法互動授權而卡在 commit。
- **malformed workflow build 卡改走可重試 recovery**：Manager 現在會把 malformed 的 passed terminal（含 build candidate 缺失）辨識為可重派卡片，避免 operator resume 永久卡死，並在 prompt 明示 build/plan candidate 的回報契約。
- **canary 規劃產物補齊 completeness gate 要求**：openspec change 三件與 workstream Todo 補 `status: accepted` frontmatter 與各 kind 必要章節，`assess_planning_completeness` 全數通過。

### Changed
- **canary Work Item 收斂為單一 todo 交付目標**：followups 內容併回主 todo，符合 ship v1 恰一組 PR/OpenSpec/Todo 的閉環要求。
- **canary follow-ups 移至獨立 workstream todo**：符合 Monitor repo provider 固定 todo glob，確實成為 confirmed todo 來源。
- **canary Work Item 補齊 spec/plan path links**：`.cortex/work-items.yaml` 的 canary 條目補 superpowers spec/plan path links，work authority 完整涵蓋規劃產物。
- **Unified work lifecycle OpenSpec完成正式封存**：所有33項實作與canary工作已有可驗證證據，official CLI將change搬入日期archive，並把governed delivery、persona workflow與unified read model三份規格發佈為canonical specs。
- **舊 lifecycle canary 以 abandoned 語意封存**：兩個未進入delivery的canary在Manager exact-run abandon後搬入日期archive，保留未勾選tasks並綁定immutable abandon evidence；不建立CompletionRecord，也不宣稱completed或done。
- **Canonical ship audit保留Job發證時source revision**：evidence reader改以Job自身persisted dispatch revision驗envelope，不再因WorkflowRun current source於PR/provider refresh後前進而誤報binding invalid；run/claim/repo/card/phase與locator/hash仍完整fail-closed重驗。
- **Cached done可用current semantic draft刷新CompletionRecord**：default snapshot前進時，Manager為`done` journal產生新draft，terminal validator優先完整驗證replacement並於成功後更新cached record ref/hash；沒有replacement仍重驗既有record，conflict或remote mismatch維持fail-closed。
- **Cached done closure可在Manager finalization前安全重入**：delivery journal已完整記錄`done`但WorkflowRun尚未綁定CompletionRecord時，explicit resume與`merged`相同略過已消失的active planning path；journal只作routing hint，terminal validator仍完整fail-closed重驗所有closure authority。
- **Post-archive repair保留可驗證的Manager ship audit**：`openspec-archive` job可在registry仍保存passed authority且Git ancestry成立時綁定final Candidate的ancestor；`policy-commit`仍要求exact final Candidate，unrelated commit、ambiguous evidence或ancestry error維持fail-closed。
- **CompletionRecord統一綁定WorkflowRun closure evidence**：Manager先重驗各自per-card slice的verify/review canonical envelope，再以共同run ID派生closure evidence；strict reader現在可同時驗證slice、Candidate與builder/reviewer jobs，不再因合法的不同card identity誤報slice mismatch。
- **Completion Draft依closure語意建立immutable revision**：檔名改以排除`completed_at`的normalized payload hash版控；相同語意retry沿用首份draft，default branch或authority前進則保留舊檔並建立新revision，malformed或symlink collision維持fail-closed且不覆寫audit evidence。
- **舊pre-delivery WorkflowRun可由Manager明確abandon**：新增queued `cortex work abandon`，要求exact run ID CAS、current WorkAuthority、actor與bounded reason；active Job、PR ref、passed ship step或CompletionRecord一律拒絕。成功只寫immutable audit evidence並將run設為`superseded`，不勾未完成tasks、不建立CompletionRecord或投影done。
- **Delivery GitHub pagination 相容未提供 `--slurp` 的 gh**：checks、statuses 與 reviews 改用 shell-free `gh api --paginate --jq '.'` JSONL page stream；空輸出或任一 malformed page 仍 fail-closed，避免 current-HEAD ship validator 永久停在 `needs_human`。
- **Monitor 完成統一 Work Item correlation、lifecycle 與 read API**：`.cortex/work-items.yaml`／scalar `work_item` frontmatter 提供 confirmed authority，雙訊號 heuristic 僅供 inferred display，collision、path escape、provider degraded 與 partial closure 全部 fail-closed；四態 reducer 支援 strict done/reopen 與 `on-going` 公開拼法。Unix socket 新增 list/get/explain/work subscription、支援 repo-scoped 同名隔離，且保留既有 ProjectState API；CLI 新增 read-only `cortex list` 與 `cortex work show`。
- **Monitor 新增統一工作來源與 durable last-good foundation**：repo provider 以固定 artifact globs 掃描 Todo／superpowers／active OpenSpec，排除 archive 並對 active/archive 同名 fail-closed；GitHub provider 只用 typed `gh api` argv/JSON，auth、rate-limit、timeout 或 malformed response 一律 degraded。`work-items-snapshot/v1` 以 0600、file/directory fsync 與 atomic replace 保存 per-provider last-good，失敗 scan 不移除既有 sources，權威 project removal 會清除對應 provider，restart 可先提供 degraded read model，且 GitHub entity／terminal closure providers 均受 900 秒 freshness gate 約束。
- **Define/Plan completeness gate 加入安全的異質 brainstorm**：planning artifacts 現在必須有 `status: accepted`、必要章節且無獨立 TBD 或真正 `Open Questions` 清單；不完整時由 primary planner 產生完整 question pack，依 `agy/google → claude/anthropic → codex/openai` 選擇異質 secondary 只回證據，再由 primary 整合。新增 schema v2 packaged model identities 與 `agy --print --mode plan --sandbox` launcher/live capability probe；unknown、same-domain、unavailable 或 malformed output 一律 fail-closed，immutable brainstorm evidence 會綁 repo/work/source revision 與 artifact hashes，且不能替代 ForeignReview/Copilot gate。
- **Manager接管workflow單一寫入與嚴格phase spine**：Deck emit會同步fsync持久化persona-preserving workflow manifest；control queue新增`workflow-action`，production dispatcher會依manifest逐card建立綁定run/claim/source/repo/phase/persona/model的durable Job，periodic terminal poll只從job log建立canonical coordinator-root evidence並原子綁回job，拒絕caller-supplied path/hash，全部card通過後phase才前進。Define/Plan與manifest plan card一律在disposable checkout以strict read-only模式執行（Claude `plan`且無tools、Codex `--sandbox read-only`）；成功、nonzero或snapshot `PermissionError`皆會先恢復安全traversal，再依baseline還原entries、mode與xattrs，restore fault一律fail-closed。Structured artifact scan會持久化canonical ref/kind/work ID/content hash authority；新檔no-clobber，既有TBD檔只接受同一authority的baseline CAS replacement。Artifact、immutable/idempotent brainstorm evidence、expected gate ref與registry phase以durable intent journal組成recoverable transaction；registry已commit時restart逐operation重驗type/hash/mode/evidence後保留產物，任何drift改設`needs_human`並保留journal。Verification/ForeignReview在dispatch時保存output directory baseline，terminal report必須是新檔或相對baseline已更新，且frontmatter精確綁run/card/Candidate；canonical evidence也保存baseline hash，舊report不得重播。Ship缺可信current-HEAD validator時維持`needs_human`等待後續delivery automation；registry在atomic replace後directory fsync失敗時會以fsync backup復原檔案並重載記憶體。
- **Coordinator registry升級為schema v2 workflow persistence**：首次載入合法v1 state時，會先以timestamp與content hash建立read-only、no-clobber、fsync完成的原檔backup，再atomic replace為v2；舊jobs/slices只保存於`legacy_records`且不推測work item。新增typed `WorkflowRun`/`WorkflowStep`持久化、claim-key restart冪等、phase transition、facet/gate/attempt/evidence refs，並讓Deck manifest逐step保留persona binding。
- **Work lifecycle mutation 統一由 Manager daemon 單一 writer 執行**：新增 queued `work link|unlink|start|resume|auto|ship` control action與 periodic auto-claim scan；claim/resume 會重驗 persisted semantic authority，display-only inferred rows不會授權或阻塞其他confirmed authority，whole-fleet snapshot refresh noise 只更新 provenance 而不另建 run。Auto scan 讀取全部 mapped issues、任一 issue API 失敗即 fail-closed；`work auto` 未指定 legacy `--issue` 時會 mutation 全部 mapped issues，任一 API 失敗整體報錯。Typed link API 支援 `github_issue|github_pr|openspec|path` canonical refs。V1 `ship` 只接受唯一且 exact 的 PR/OpenSpec/Todo target，多 target 轉 `needs_human`，並依序執行 change-specific archive/PR metadata gates、authenticated PR update+reread、official OpenSpec archive、exact-tree preflight、遠端 archive gate、持久化 current-HEAD Copilot fix epoch、ForeignReview、merge authorization/reconciliation 與 remote closure。
- **Workflow 與 delivery 改用同一份 canonical run truth**：`jobs.json/workflows` 是唯一 lifecycle truth，delivery 僅保留以真實 `run_id` 為 key 的 ship journal。Public `work start/resume` 會建立並續跑 `feature-oneshot` persona cards；Monitor 直接投影同一個 `WorkflowRun`。Manager 由 trusted repo registry 解析 root，依 foreign-review evidence 精確綁定 reviewed builder/base，先以 zh-TW metadata 跑初次 preflight，再冪等建立 PR 並原子寫回同一 run；merge 後的 CompletionRecord 也由該 run 的 canonical verify/review evidence 產生並綁回同一 run。
- **Preflight skip evidence 改由實際 full suite 產生**：只有 runner 成功且 HEAD/tree 前後不變時才會在 canonical coordinator state path 寫入 immutable、hash-bound evidence；`run_preflight()` 自行依 exact tree 載入並以 trusted finite clock 驗 freshness，不再接受 caller 提供的 passed/path/hash。
- **Delivery gate 綁定 exact PR HEAD 與遠端閉合證據**：新增 shell-free preflight、Copilot current-HEAD review epoch、known unable-to-review error detection、terminal-green checks、thread/closing/archive/mergeability final reread與merge後 strict closure evaluator；單一 ShipOrchestrator 同時要求 fresh provider、乾淨且未競態的 exact-tree preflight、異質 ForeignReview 與本 epoch Copilot review，merge argv 固定目標 repo 並使用 `gh pr merge --merge --match-head-commit`，remote Todo revision及既有 CompletionRecord hash 重驗全部通過後才閉合。
- **Work claim 預設改為 manual 且以 source revision 冪等**：只有 confirmed Todo、confirmed issue 與 fresh provider 可建立 claim；auto 額外要求 `cortex:auto-on-going` label，missing issue 轉 `needs_human`，已 active workflow 即使移除 label 仍只 resume 而不取消。
- **新增 unified lifecycle doctor 與 migration 操作文件**：`cortex doctor --probe-live --repo owner/name` 會以 production validator 與 secret-safe 診斷檢查 gh auth、Contents/Issues/Pull requests write capability proof、auto label、typed preflight、model identities、agy safe plan/sandbox capability、systemd effective bootstrap env及 Monitor state/socket；socket 必須通過 production `list_work_items` 的 `cortex-work/v1` handshake。installer 保留 operator path overrides，README 與 migration guide 同步說明四態 read model、correlation authority、manual/auto claim、schema v2 identity、snapshot/registry 升級及 exact-HEAD delivery gate。
- **README Usage 與 CLI help 對齊實際 runtime**：頂層 `cortex --help` 現在列出 umbrella/coordinator 公開命令，coordinator/deck/monitor help 統一使用真實 `cortex` invocation，並明示低階 `dispatch` 停用、unsafe/model/control timeout 語意；README quickstart 同步區分 installer enable 與 service start、deprecated timer interval 與 daemon tick、monitor config 前置、Deck hold→auto、foreign review、preserving-commit merge 與 completion 流程。
- **builder `exited` 不再直接走 completion shadow path**：coordinator 現在會先固定 Candidate、重新驗 pinned inputs，並以 deterministic ResultVerification 執行 required artifacts、persona scope、typed argv checks、task tests 與 base/candidate full-suite 比較；只有成功驗證才把 slice 推進 `verified` 或 `reviewing`，其餘一律 fail-closed 到 `needs_human`，不再讓 `exited` 單獨滿足 DAG。
- **review-required slice 改為 exact-HEAD foreign review gate**：manager 現在會依 `PSC_PROJECT_CONFIG_ROOT/model-identities.yaml` 選擇不同 independence domain 的 reviewer，建立固定 Candidate 的 detached reviewer worktree，並把 `passed|rejected|absent` verdict 以 immutable GateEvaluation 落盤到 `evidence/review/`；缺 model / 同 domain / malformed verdict / stale HEAD / reviewer failure 一律 fail-closed，且只有 formal category enum 中的 blocking finding 會拒絕通過。
- **dependency release 改為 CompletionRecord + target ancestry gate**：manager 在 `passed|verified` 後會先對 target ref 做 fetch/ancestor 檢查，寫入 immutable CompletionRecord 並把 slice 標記 `completed`；readiness 只接受 `slice_state=completed` 且 CompletionRecord/hash 對齊、Candidate 仍是當前 target ancestor 的 upstream，dispatch 下游 worktree 也改以 target ref SHA 當 base 並在發車前重驗 ancestry，避免未合併或 stale-head upstream 被提前釋放。
- **新增 persisted `slice-action` recovery 與 attention status**：`cortex slice-action <slice-id> <retry-build|retry-verify|retry-review|abandon> --actor <text>` 會透過既有 control request queue 交給 manager 單一 writer 執行；action entry 會保存 `requested_at/consumed_at/result`，`status` 快照新增每個 slice 的 job/gate/ancestry/evidence 摘要與 `next_actions`，並額外彙整 `attention` 一次列出全部 `needs_human` slice。
- **新增 dispatch discipline disposable canary 與 README 操作契約**：新增 `tests/test_coordinator_dispatch_discipline_e2e.py`，集中覆蓋 missing artifacts、same-domain foreign review absent、stale reviewer input audit-only、candidate merge ancestry、dependency base pin、completion restart 補完與 reaper negative safety；README 同步補齊 Job/Slice/Gate 語意、verification/frontmatter trust boundary、identity 設定、completion/restart 以及 operator action/status 用法與 reaper best-effort 限制。
- **deck frontmatter emit 契約與 runtime parser keyset 對齊**：`EMITTED_FRONTMATTER_FIELDS`、deck compile frontmatter 與 `parse_spec_frontmatter()` 現在一致包含 `target_branch` / `verification` / `parse_error`；compile 產生 hold spec 時固定輸出 `null` 欄位，runtime 僅接受 `parse_error: null`（non-null fail-closed），避免 deck contract alignment 漂移。

### Fixed
- **Done WorkflowRun可重驗並刷新terminal CompletionRecord**：explicit `cortex work resume`會唯一選取同work的`done/ship` run，使用current WorkAuthority重跑既有ship validator並只在完整closure通過後更新CompletionRecord；PR provider已有`closed` revision時不再額外注入`state:open`，pending、needs-human或malformed結果皆保留舊completion且不重派workflow card。
- **Terminal closure接受合法的WorkAuthority revision前進**：`merged`／cached `done`重播會保留immutable merge authorization的pre-terminal digest，並以current closed/archived authority重驗remote closure；merge-authorized與merge前gate仍要求current digest精確相等，tampered wrapper、review或其他binding不會被放寬。
- **Post-merge closure不再要求已archive的active planning path**：delivery journal完整綁定merged run/Candidate/merge commit/authorization時，operator resume會直接重驗CompletionRecord與remote closure；official archive移走active OpenSpec後不再誤報planning-authority reconciliation failure，malformed journal仍走原本fail-closed路徑。
- **CompletionRecord接受typed maintainer review authority**：remote closure現在保留merge authorization實際使用的`copilot`或`maintainer-review` evidence kind/ref/hash，並要求兩者恰好一種；maintainer-authorized merge不再因舊的Copilot-only白名單卡住，缺失或雙重authority仍fail-closed。
- **Typed maintainer review可安全接手Copilot stop**：delivery journal停在`copilot-finding-budget-exhausted`、timeout或其他`copilot-*` needs-human reason時，只有WorkflowRun已綁定且path/hash完整的exact-HEAD maintainer evidence可重入；後續仍重驗immutable evidence，external merge與其他stop不會被旁路。
- **Plan/build terminal output prompt對齊manifest ref契約**：structured prompt現在明示`outputs`只能是符合declared outputs的repo-relative artifact path字串；manifest未宣告outputs時固定要求`[]`，避免builder把adoption摘要放入outputs後被Manager正確拒絕而無法綁定tested descendant Candidate。
- **Retry-build可採用已提交且已測試的descendant Candidate**：delivery preflight與post-archive review recovery prompt現在明示先檢查worktree既有repair commit，允許builder提交或採用tested descendant；Manager仍以exact舊Candidate CAS與單調ancestry獨立驗證，不接受caller evidence或倒退HEAD。
- **Copilot workflow terminal evidence 可由 Manager 正式綁定**：terminal parser 現在只在 typed `assistant.message` event 讀取 `data.content` 的完整 workflow payload，保留既有 discriminator 驗證；不再把 Copilot 已成功產生的 exact-Candidate terminal 誤判為缺少 JSON evidence。
- **Exact PR metadata不再無條件重寫**：`ensure_pr_metadata`先以authenticated PR/issue reread驗title、body與完整labels；全部精確一致時直接保留remote state，不發PATCH/PUT。任一欄drift才執行冪等metadata writes並再次完整reread，降低GitHub write degradation期間的無效side effect且不放寬identity gate。
- **Delivery PR metadata可承受暫時gateway故障**：既有PR的title/body PATCH、labels PUT與兩筆identity reread只在HTTP 502/503/504時做finite bounded retry；這些操作皆為冪等metadata transaction。PR create、push、merge與其他delivery side effect不共用此retry路徑，auth、rate-limit及malformed response仍立即fail-closed。
- **Terminal closure provider不再被GitHub暫時gateway故障或無關PR拖垮**：remote Todo改以default revision精確綁定的Contents API讀取，並逐筆重驗path/blob SHA/encoding；只有HTTP 502/503/504會依有限backoff重試，auth、rate-limit與malformed response仍立即fail-closed。Production ancestry compare只對canonical WorkflowRegistry已連結的PR執行，保留完整PR remote truth但不再為整個repo的歷史merged PR逐筆查詢。
- **既有PR可由Manager推進至fresh exact Candidate**：post-archive修復等流程若WorkflowRun已綁PR但remote branch仍停在舊HEAD，ship adapter現在會先以PR context在乾淨checkout重跑preflight，再冪等push並重讀授權的`feature/*` ref；remote HEAD未精確對齊時維持fail-closed，不再必然卡在`ship HEAD differs from authenticated GitHub PR`。
- **Official archive 後可安全修復 Candidate finding**：`cortex work retry-build` 現在可在唯一已通過的 ship authority 是 identity 精確的 Manager official archive 時，保留 archive step並重開最後builder與fresh verify/review；brainstorm authority會以同hash的唯一official archive artifact重證，任何其他已通過ship card仍拒絕rewind。新Candidate繼續要求單調延伸，讓archive後才暴露的stale path/test defect可經正式workflow修正。
- **Terminal canary accepted planning authority 保持 immutable**：Todo bootstrap 與 delivery recovery 不再改寫已由 WorkflowRun hash-bound 的 OpenSpec proposal/design 與 accepted superpowers spec/design/plan；恢復 canonical bytes，讓同一 run 可由 Manager 重驗而不需直接修改 registry。
- **Delivery target-cardinality stop 可在 authority 收斂後安全續作**：terminal canary 補齊唯一 confirmed Todo 與 repo-local mapping；若 delivery 尚未建立 immutable binding，只因 PR／OpenSpec／Todo target 數量不符而進入 `needs_human`，operator 修正 authority 後可 explicit resume 同一 WorkflowRun。Manager 會先重綁既有 PR run 的 delivery journal authority，再只清除此特定 stop；其他 `needs_human` 與已建立 binding 的 delivery 仍維持 fail-closed。
- **Initial delivery可重綁fresh WorkAuthority**：若review完成後因default branch或provider refresh使WorkAuthority digest前進，Manager會在exact-Candidate push前只更新同一WorkflowRun的current `source_revision`，並冪等建立或重綁delivery journal authority；`planning_source_revision`、claim、Candidate與既有verify/review evidence保持不變。Registry已更新但journal仍為舊digest的crash window可於resume重播，不再誤報`delivery WorkflowRun does not match current WorkAuthority`。
- **Delivery preflight隔離Manager runtime authority**：Manager執行quick policy與configured CI-parity command時會移除所有繼承的`PSC_*`，並改用完成後刪除的disposable `HOME`／`XDG_CACHE_HOME`；只有Python user-site與GitHub config等必要工具／認證root被顯式保留，故測試不會從installed bootstrap重新取得production coordinator、executor或repo authority。Manager systemd unit另固定`UMask=0022`，讓exact-Candidate suite的檔案mode假設不受operator service umask影響。
- **Delivery preflight後可由Manager重開Candidate build**：新增queued `cortex work retry-build`，只接受exact `expected_candidate` CAS；僅在唯一ongoing `needs_human` verify/review run、無active Job且既有build全passed時，由窄化registry recovery重開最後一張builder card、清除舊verified/review/delivery authority並立刻建立fresh builder Job。新HEAD仍須單調延伸舊Candidate，stale CAS、caller evidence或一般phase update都不能取得倒退權限。
- **Delivery preflight不再被Manager自己的report publication阻斷**：進入delivery前只清除hash與canonical verify/review evidence完全吻合、且未被Candidate追蹤的Manager-owned report；清除前先建立hash-addressed immutable cleanup intent，故crash/retry evidence replay只接受已授權的missing path。Symlink、tracked path、可寫或malformed intent、未授權缺檔或任何drift仍fail-closed。尚未建立PR時會先驗delivery branch符合`feature/<slug>`，再於exact Candidate的乾淨暫存`feature/preflight-*` checkout執行metadata preflight，避免accepted planning artifacts等未提交overlay污染exact-tree gate，同時保留branch policy且不放寬原worktree的一般dirty gate。Review已完成後的ship validator若拋錯，也會先持久化`needs_human`與failed gate再回報，避免只留下CLI error而run看似可繼續。
- **Rejected workflow review改為可審計的needs-human stop**：合法且exact-bound的`state=rejected` GateEvaluation不再誤報成binding mismatch；Manager會保存blocking evidence、將當前card標成`needs_human`並停止，periodic不得重派，只有operator explicit resume可在Candidate與immutable evaluation重驗後建立fresh reviewer Job。Reviewer terminal schema亦明示report-only精度問題不得冒充Candidate correctness，既有category-based blocking規則不放寬。
- **Passed review evidence支援crash replay**：review已canonical bind但step audit/save尚未完成時，operator resume會重驗exact `passed` evidence並冪等重播，不建立fresh Job；forged、stale或unknown state仍保留`needs_human`。
- **Claude reviewer StructuredOutput綁定terminal schema**：`--json-schema`不再只宣告無欄位的generic object；Manager依typed workflow phase明確傳入verification或review exact schema，強制必要binding fields、verified status、inline reports與typed findings，避免Claude合法回傳空物件卻留下`exited-0`無evidence terminal。
- **Claude reviewer sandbox canonicalize runtime socket aliases**：Linux/WSL 上的 `/var/run` 若指向 `/run`，review policy只保留canonical `/run/docker.sock` deny，避免sandbox runtime對同一socket建立重複bind而讓所有Bash在啟動前失敗；home deny只重開解析後的官方SRT package root供`apply-seccomp`執行，live doctor改用實際review filesystem policy執行SRT與Unix-socket smoke。
- **Reviewer explicit recovery綁定production builder worktree**：Exact-bound無payload reviewer不再把disposable checkout的原始Candidate root誤比成WorkflowRun主workspace；改以已驗證Builder Job worktree為唯一authority，並只在recovery classifier命中後進入pre-dispatch cleanup，讓真實multi-worktree canary可由operator重派。
- **Claude workflow reviewer改用可執行測試的fail-closed read-only sandbox**：Reviewer不再誤用會阻止`pytest`與terminal JSON的Plan Mode；改以`dontAsk`、safe-mode、僅Bash工具面、structured JSON output與CLI-only settings執行，不載入Candidate customization、MCP或remote session。OS原生sandbox拒讀home/runtime sockets且只重開Candidate/Python user-site，Candidate clone明確deny-write、credential paths/env隔離；review subprocess改用最小正向環境allowlist與非login shell，不再以變數名稱denylist猜密鑰。缺`claude`/`bubblewrap`/`socat`/`srt`、版本、native smoke或Unix-socket seccomp smoke不符、或要求unsandboxed fallback時直接拒絕。Manager把Claude protected-path bind targets放在deterministic disposable session root，exact Candidate則固定置於無污染的`candidate/` checkout；exact-bound、`exited-0`但無terminal payload的reviewer Job只可由operator明確`work resume`保留舊Job後重派，periodic仍無重試權。
- **Workflow reviewer terminal 契約改為 capability-bound isolated read-only transport**：Verify/Review只選schema v2明示`review`且不同Builder domain的identity，launcher在exact Candidate disposable clone以enforced read-only mode執行，Manager以原Candidate tree snapshot防寫；agent只回substantive result/findings與inline report body，由Manager依durable Job建構Candidate/job/identity binding、finding state與frontmatter。Report限phase專屬Markdown root，durable publication journal使multi-report CAS、canonical evidence與registry bind可安全rollback/roll-forward。Parser只接受整份單一JSON fence；升級前planning-only canonical Agy generic terminal僅可由exact explicit operator resume保留舊Job後重派，periodic與一般retry仍fail-closed。
- **Builder worktree 可安全續用已合併的 issue branch**：若目標 local branch 已存在，只有在它是 requested base 的 ancestor 時才 fast-forward 後掛入 worktree；含未合併 commits、已被 checkout 或 ancestry 無法驗證皆 fail-closed，避免 canary bootstrap branch 阻斷正式 workflow build。
- **Workflow terminal evidence 支援真實 Codex JSONL event stream**：Manager 會略過 `turn.completed` 與 tool envelopes，只從 final `item.completed/agent_message` 或帶明確 workflow discriminator 的 payload 解析 JSON；任意 command output 不再可能被誤當 canonical evidence。
- **失敗的 planner card 可安全重建 disposable sandbox**：retry 只會清理 canonical coordinator boundary 內、名稱精確綁定同一 run/card、且持久化狀態為 failed planner job 的 sandbox；symlink、identity mismatch 或清理不完整仍 fail-closed。
- **唯讀 workflow planner 可在 disposable checkout 啟動 Codex**：Coordinator launcher 僅對 `read_only` card 加上 `--skip-git-repo-check`，保留 `--sandbox read-only`；builder 路徑仍維持原本的 git trust gate，避免放寬可寫入執行階段。
- **Active workflow 不再被自己新增的 planning sources supersede**：auto claim 與 explicit resume 會先以穩定 repo/work/issue/OpenSpec identity 找出唯一 ongoing run；accepted superpowers spec/plan 加入 authority 時維持同一 run，由 active workflow 優先，不再建立新 claim。Codex planner 同時在無 `.git` 的 disposable read-only checkout 使用官方 `--skip-git-repo-check`，避免安全 sandbox 被 CLI trust check 誤擋。
- **`work resume` 現在會真正重入既有 `needs_human` workflow**：claim router 不再把既有 `needs_human` 誤當成只讀結果直接回傳；operator resume 會以同一 claim key 呼叫 canonical starter，讓 define／brainstorm retry 可以在不新建 run 的前提下繼續。
- **Headless 異質 brainstorm 不再因讀取 evidence 觸發互動 permission**：缺 accepted artifact 時，question pack 現在保留同 kind 的既有 repo refs；Manager 以 bounded、UTF-8、無 symlink 的 read-only方式把來源內容直接嵌入 secondary prompt，明確禁止 tool/command call，並提供 manifest-compatible planning destinations 與 exact JSON contract。`agy --mode plan --sandbox` 因此不需 unsafe bypass 或全域 command allow rule。
- **`cortex work resume` 不再覆蓋 define retry 的真實結果**：canonical starter 已負責重跑 define／brainstorm 時，Manager daemon 不再緊接著呼叫只支援 card phase 的 resume；planning runtime 仍失敗時會保留原始 reason 與 `needs_human`，成功進入 plan 後才續跑 workflow card。
- **Active workflow authority 可在 `start` action 消失後繼續 resume/closure**：canonical loader 現在把 confirmed `workflow_run` 視為持續 authority；display-only topic/orphan仍忽略，避免 `on-going` WorkItem 因 next actions 為空而變成 authority missing。
- **Work authority loader 不再讓 display-only topic/orphan 阻塞 confirmed 工作**：canonical loader 會忽略沒有 `start` authority 或沒有 confirmed Todo 的 read-model rows；repo/work/source/action malformed 仍 fail-closed。Repo override 同步補上 unified lifecycle issue/OpenSpec/superpowers confirmed links，消除重複 display groups與錯誤 missing-issue workflow。
- **Live GitHub provider 不再因 scan 前固定 clock 而被立即標 stale**：freshness reference 改為 provider scans 完成後再讀取，避免成功 snapshot 的 `last_success_at` 晚於 pre-scan clock 形成負 age、永久關閉 auto claim/merge；新增 deterministic clock regression 與 live projection probe。
- **GitHub Work provider 相容不支援 `gh api --slurp` 的 gh 版本**：pagination 改用 typed `--paginate --jq '.[]'` JSONL entity stream，保留 shell-free argv 與 malformed response fail-closed；避免 live Monitor 永久 degraded 並阻止 auto claim/merge。
- **統一 `cortex work` umbrella routing**：`show` 仍由 Monitor read API 回應，`link`／`unlink`／`start`／`resume`／`auto`／`ship` 則正確進入 Manager control queue；共同 help 同步列出完整 command family。
- **Project Monitor 不再把暫時掃描失敗發布成假移除**：workspace／project subtree 無法可靠讀取時保留 last-good `ProjectState` 並附加 `degraded` scan signal；只有成功掃描父層後才允許 removal。`poll_interval_seconds`、`rescan_interval_seconds` 與 `watch_debounce_ms` 也改為拒絕非正值，避免錯誤設定被 clamp 成緊迴圈。
- **CompletionRecord 會重新驗證並綁定全部 evidence**：readiness 現在會嚴格驗證 GateEvaluation schema，並要求 verification/review evidence 的 Slice、Candidate、builder/reviewer job、狀態與 CompletionRecord 一致；target ref 也必須對應宣告的 remote/branch，避免以跨 Slice 或跨 Candidate 的合法 hash 證據繞過 dependency gate。
- **Work delivery terminal cache 與 merge crash window 改為 fail-closed reconciliation**：terminal run 不再當 active workflow；cached done 必須以 fresh WorkAuthority 重跑 remote closure。CompletionRecord 額外綁定 repo/work/run/workflow step、snapshot/provider/source、唯一 mapped delivery refs、merge commit與 trusted preflight/Copilot/ForeignReview/merge-authorization refs。Authorization hash 只納入 stable semantic preflight 結果與 immutable evidence hashes，排除 stdout/stderr/duration；Manager 在 merge 前 atomic no-clobber、fsync 並設為唯讀，restart 可先 reconcile 已授權 merge 而不重跑漂移輸出，其他 external merge 一律轉 `needs_human`。
- **Work action repo boundary 改為 canonical GitHub identity**：所有 action 先驗 `owner/name`；`repo_root` 必須恰好等於無 symlink 的 canonical git top-level realpath，且 `origin` 必須解析為同一 GitHub repo。Nested directory 與 Path/OpenSpec/Todo refs 穿越 repo 內 symlink皆拒絕，避免跨 repo 或越界 mutation。
- **manager 會重新驗證 verification evidence 後才套用結果**：`complete_tick()` 現在會自行驗 schema、candidate、證據檔 path/hash 與落盤內容一致性；`verification_runner` 回傳 forged payload/path/hash 時一律 fail-closed 到 `needs_human`，不再把 Slice 或 handoff manifest 誤推進 `reviewing` / `verified`。
- **Task 3 剩餘 fail-closed 缺口已補齊**：pinned-input mismatch 重讀 spec 時若遇到 non-UTF-8 / parse failure，現在會回傳明確 mismatch reason 並照常把 slice 轉進 `needs_human`；verification evidence finalize 改為 no-clobber，若 create-after-check race 期間冒出衝突檔案，會隔離既有證據並 fail-closed 拒絕覆寫。
- **slice repin 不再繞過合法狀態轉移**：`JobRegistry.repin_slice()` 現在只允許 `pending` / `needs_human` slice 重派；它會保留 slice state、透過 validator 合法地把 `gate_state` 重設為 `pending`，並拒絕 terminal slice 的非法 rewind。
- **Task 3 review fixes now fail closed on contract drift and full commit IDs**：`verification.required_artifacts[].must_change` 現在只接受實際 boolean；verification evidence candidate 只接受完整 40-char commit SHA；manager 對 builder `exited`/`failed` 兩種終態都會先做 pinned-input drift 檢查，drift 一律升級為 `needs_human`。
- **Task 3 verification follow-up 會嚴格 fail-closed**：spec frontmatter 的 `target_branch` 只要存在就必須是非空字串，`dispatch:hold` 不再默默吞掉 malformed value；既有 verification evidence 若是可解析 JSON 但 schema 無效，現在也會先隔離到 quarantine 再拒絕覆寫。
- **Task 2 review follow-up 對齊 notifier/registry state contracts**：`coordinator_telegram_notifier` 改以 `exited|failed` 判定 Task 2 終態；`JobRegistry` 現在會拒絕持久化或更新指向不存在 job 的 `builder_job_id` / `reviewer_job_id` slice 參照。
- **coordinator slice read path 不再回傳共享 history refs**：`JobRegistry.get_slice()` / `list_slices()` 現在會複製 history/action entries 內的巢狀 `refs` 清單，避免呼叫端 mutate 回傳資料時污染 live registry state。
- **control queue 會正確尊重 request override 與 dead-daemon 狀態**：queued `dispatch`/`fanout`/`tick` 現在以 request 自帶的 `handoff_dir` 建 readiness predicate，`complete` 在未提供 `specs_dir` 時不再多做 spec scan；`control.client.read_status()` 若看到 daemon pid 已死亡，會立即回報 `degraded_reason=dead`，不再短暫誤報健康。
- **`cortex reap-brokers` 失敗時改回 non-zero exit**：操作員手動執行 cleanup 時，若腳本缺失、無法 exec，或腳本以非零碼結束，CLI 仍會印出 JSON summary，但現在會回傳 exit 1，避免把未執行/失敗的 cleanup 誤報成成功。
- **service installer 會持久化 manager Python 解譯器**：`cortex install service` 現在會把 `PY=<sys.executable>` 寫入 `~/.agents/core/runtime/<instance>-manager.env`，避免 pipx / venv 搭配 user systemd 時落回系統 `python3` 而找不到 `paulsha_cortex` 模組。
- **service installer 會持久化正確 repo root**：`cortex install service` 新增 `--repo-root`，會先驗證目標是否為 git repo，再把解析後的 top-level 路徑寫入 `PSC_REPO_ROOT`，避免 manager daemon 在 systemd cwd 下把 worktree 建到錯誤目錄。
- **hook 模板改為透過 `cortex relay-hook` 定位封裝腳本**：三份 hook JSON 不再硬編不存在的 repo 內路徑，也移除了不屬於 cortex 的 `psc-bro-return` glue；`relay-hook` 子命令會直接執行封裝內的 `psc-relay-hook.sh`，安裝位置改變時仍可正確解析。
- **停止 periodic automatic reaper，改為 scoped operator cleanup**：`tick` 與 manager daemon 不再自動回收 codex broker；新增 `cortex reap-brokers` dry-run/operator 路徑，`--apply` 必須搭配 `--cwd-root`，腳本會在送 `SIGTERM` 前重驗 `ppid/start-time/cmdline/cwd`，只清理同 project scope 內、身份未變的 broker。

### Changed
- **dispatch 會固定 v1 verification contract 與輸入 hashes**：`parse_spec_frontmatter()` 現在嚴格解析 `target_branch` / `verification` v1 contract、對未知鍵與非法 check 回報 structured parse error 並強制 `dispatch=hold`；spec-driven `dispatch` request 會把 spec/plan/verification SHA-256、target branch/remote 與 review policy 釘進 Slice，再由 manager 在 builder 結束時檢查 pinned-input mismatch 並 fail-closed 到 `needs_human`。同時新增 versioned verification evidence writer，對相同內容冪等重讀、對衝突內容隔離後拒絕覆寫。
- **coordinator state 改為 versioned `jobs+slices` foundation**：`jobs.json` 現在要求 `schema_version`/`jobs`/`slices` 根結構，legacy `done` 狀態與無版本舊檔會 fail-closed 要求 clean start；headless 完成語意改為 `exited|failed`，SliceRecord 會持久化 spec/plan hash、branch/base、builder/reviewer、candidate 與 evidence/action history。
- **mutable coordinator CLI 全改走 control request queue**：`fanout`/`tick`/`complete` 不再本地寫 registry，daemon 未就緒時會明確失敗；低階 `cortex dispatch --task ...` 因缺少 spec metadata 已拒用，只保留 `jobs`/`stat`/`ready`/`status` 為讀取路徑。
- **同步 policy 1.0.6 → 1.0.7（R-24 moc-alignment）**：`policy_version` 1.0.6 → 1.0.7；`Policy Check` workflow re-pin 引擎到 1.0.7 SHA `e24fbd6`（尾註 `# v1.0.7` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.7 新增規則段（R-24）與白名單 `policy-exempt:moc-alignment`。
- **採用 policy 1.0.6 新模型（agent 慣例檔 symlink 單一真檔 + 引擎 pin attestation）**：`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向 canonical `CLAUDE.md` 的 symlink；`.paul-project.yml` 設 `agent_files.mode: symlink` 與 `conventions_engine.repo`，`policy_version` 1.0.2 → 1.0.6；`Policy Check` workflow re-pin 引擎到 1.0.6 SHA `261f3f6`（尾註 `# v1.0.6` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.3–v1.0.6 新增規則段。修正 P0 傳播漂移（本 template 先前停在 1.0.2）。

### Added
- **新增 `tests.yml` CI 骨架**：生成的新 repo 出生即帶測試 gate——`tests/` 尚不存在時 job 自動跳過（綠燈），加入測試套件後 pytest 自動成為 PR gate，同時滿足 policy R-19 的 workflow 偵測
- 建立 `hamanpaul/new-project-template` 新專案 bootstrap skeleton
- 新增釘選到 `hamanpaul/paulsha-conventions` 的 `Policy Check` reusable workflow
- 新增同步的 agent convention files 與基本 policy metadata

### Changed
- **同步 policy 1.0.2**：bump `policy_version` 1.0.1 → 1.0.2（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.2`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@98487868a098e22647074c677a58633ce4fa19be`（= engine tag `v1.0.2`，含 R-19 / R-20）；agent 檔追加 R-19（CI 必須跑測試）/ R-20（workflow policy_version 同步）說明與 `policy-exempt:ci-tests` 白名單項
- **同步 policy 1.0.1**：bump `policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.1`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17（PR↔issue closing-keyword）、R-18（docs 對齊 WARN）與語言規範說明
- `Policy Check` workflow 改為雙重釘選 `hamanpaul/paulsha-conventions@8454aa1967b752ea38c82edd79a8439b5bde915b`，同步設定 reusable workflow `uses:` 與 `policy_engine_ref`

### Fixed
- 移除超出需求範圍的 `pyproject.toml` 與相關 package 化敘述
