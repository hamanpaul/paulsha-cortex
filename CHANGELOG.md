# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.15。

## [Unreleased]
### Added
- **Issue #340：builder persona 契約新增 `completion_obligations`（結束前必須 commit）**：`PersonaContract` 新增 `completion_obligations` 欄位（fail-closed schema 檢查），`personas.yaml` 的 `builder` 角色新增義務宣告「完成前必須 git add＋git commit，worktree 不乾淨不得回報完成」，由 `render_contract_prompt` 注入實際派工的 dispatch prompt，補上既有 `commit_policy: required`（只管寫入權限）與 manager 端事後 dirty-worktree 安全網之間「事前宣告義務」的缺口；空清單角色不受影響。
### Fixed
- **Issue #339：run tick 對已有 needs_human 終局紀錄的 slice 不再重複 fanout**：`run_tick` 原本的冪等防護只排除 registry 中仍在 `dispatched`/`running` 的 job，job 一旦 poll 到 exited 就離開這個集合，不論其 `gate_status` 是 `needs_human`／`failed`／`passed`；`ready_units`/`default_is_satisfied` 只檢查「別人 depends_on 我」是否滿足，從未檢查「我自己是否已經跑過」，導致下一趟 tick 對已完成待人工的 slice 重新 fanout，撞 `ScriptWorktreeCreator.create` 的 `"worktree target already exists"`。現在派工前會掃描每個 slice 是否已有 handoff 終局紀錄，併入排除集合；此掃描不受 idle gate 影響，`require_idle` 擋下新工作時 `needs_human` 清單仍會回報。summary 新增 `needs_human: [{slice_id, gate_reason, handoff_path}, ...]` 欄位。
- **paulshaclaw#264：status 條目補上明確 project 歸屬**：`recent_done`／`attention`／`slices` 現在投影明確的 `repo`；缺少來源時保留 `null`，不從 worktree 或 branch 猜測 project。
- **Issue #295（primary）／#291（duplicate）：persona catalog 改以套件內建為 canonical 來源，非 cortex repo 的 slice 不再確定性卡 `persona-catalog-unreadable`**：`run_result_verification` 原本無條件從**目標 repo**讀 `paulsha_cortex/persona/personas.yaml`，該檔只存在於 paulsha-cortex 自身，跨 repo 治理必然卡 `needs_human`，且 `dispatch: auto` 又強制要求該 check 無法拿掉。改為先以 `git cat-file -e` 探測 `dispatch_base` tree 是否宣告 repo-local override：存在即維持既有 pin/fail-closed 行為；不存在則回退讀取 `paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH` 套件內建 catalog 完成 scope 判定。override 壞損（不可讀／不合法）仍 fail-closed 不靜默回退；cortex repo 自身行為不退化。evidence 新增 `source`（`repo-local`／`packaged`）欄位可稽核判定依據。
- **Issue #303：三個測試直讀 production coordinator 狀態檔，環境洩漏使本地 pytest gate 被宿主狀態污染**：`test_porcelain_inspect.py::test_inspect_missing_targets_exit_one[argv0-missing-job]`／`test_work_actions.py::test_auto_without_issue_mutates_every_mapped_issue`／`test_auto_without_issue_fails_closed_if_any_label_mutation_fails` 未隔離 coordinator root，未顯式覆寫 `PSC_*` 時經 `resolve_runtime_root()` 落回 `$HOME/.agents`，直讀宿主真實 `~/.agents/coordinator/jobs.json`；production 狀態異常時三測試連帶 fail-closed。同根因擴大排查後，`tests/conftest.py` 的 autouse `_clear_runtime_env` 改為同時把 `PSC_AGENTS_ROOT`／`PSC_CONFIG_ROOT` 指向每測試獨立的空 tmp 目錄（作為 fail-safe 安全網，覆蓋 coordinator／control／specs／monitor／project-config／run root 整個家族），並補上 5 支既有測試（`test_paths.py`／`test_install_service.py`／`test_coordinator_manager_daemon.py`）刻意驗證「未覆寫時落回 `$HOME`」語意所需的顯式 `PSC_AGENTS_ROOT` delenv。以 audit-hook 稽核與偽造 corrupted `jobs.json` 重現 W1 batch 情境驗證修復前後行為差異。詳見 `changelog.d/fix-test-production-state-leak.md`。
### Changed
- **封存批次 W2 三個已交付的 OpenSpec changes**：#294／#263／#202 的 change 已隨 PR 合併，但本批改由人工管線收尾未經 cortex ship，故 change 目錄仍 active；以官方 archive 折入 canonical specs。
### Added
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
  不含程式碼變動。詳見 `changelog.d/work-identity-migration-design.md`。
- **Issue #276：builder 派工依 plan Task 邊界分段——設計文件（design-doc）**：新增
  `openspec/changes/2026-08-07-builder-task-boundary-segmentation/` 與
  `docs/superpowers/specs/builder-task-boundary-segmentation-{design,spec}.md`，
  定案 per-Task fan-out（同 worktree 續派原語）、Task 邊界解析
  （`planning.list_plan_tasks()`）、`build_dispatch_prompt()` 反漫遊／
  commit 斷點語句、`classify_completion()` 新增 `context-exhausted` 分類、
  commit log 續跑進度帳，以及與 #277 的介面邊界；本票不動任何
  `paulsha_cortex/` 程式檔，code 落地拆為三張後續票。
- **Issue #209：模型能力封套設計文件——定案 `capable()` 六項判準與 `resource-inventory` 四欄位契約**：新增 `openspec/changes/2026-08-07-design-model-capability-envelope/` 與 `docs/superpowers/specs/design-model-capability-envelope-{spec,design}.md`，定案 `#138` judge「能力配得上」謂詞的六項合取式與供給側四個靜態欄位契約；定案短期落地位置為既有 `model-identities.yaml`；定案三閘序（eligibility／admission／routing）並記錄與既有 `claim_readiness.CHECK_ORDER` 的落差；更正 issue §4 roster 現況——registry 全文只有一個身分，連 issue 自身修正 comment 的三身分表都對不上 main。純設計文件，未實作、未改任何 `.py`。詳見 `changelog.d/model-capability-envelope-design.md`。
- **Issue #323：`cortex jobs`／`stat` 對 workflow lane job 補 work_id／primary issue 歸屬欄**：`wf-xxxxxxxx-<card>-<n>` job 輸出新增 `workflow_work_id`／`workflow_primary_issue` 兩欄，於輸出端以既有 `workflow_run_id` join registry 的 workflow run，零額外持久化狀態；card 已由既有 `workflow_card` 欄位提供。非 workflow lane job 與其餘既有欄位皆不受影響。
- **Issue #178：新增 `cortex work gc` 交付後產物回收命令**：proposal-first 回收殘留 build worktree 與已 merge 的 repo local branch；預設 dry-run 只輸出候選清單與逐項 `reclaim`／`keep`＋reason code，`--apply` 才執行且逐項重驗（TOCTOU-safe）。merged 判定改走內容層驗證鏈（`git merge-base --is-ancestor` → `git cherry` 內容等價），修正 squash-merge 後 ref-ancestry 失真、`git branch -d`／`--merged` 誤拒已合併分支的既有陷阱；任何疑義一律 `keep` 並附 reason code，closed-unmerged PR 分支保留。新模組 `paulsha_cortex/coordinator/gc.py` 由 umbrella CLI 攔截路由，不經 manager daemon、對 registry 唯讀、不動 remote。
- **Refs #294：slice spec 可宣告 executor/model_id 並於派工前強制 registry 驗證**：`dispatch_ready` 支援逐 slice 的 builder identity 覆寫，unknown identity fail-closed 並列出可用 candidates；同時 `cortex fanout`／`tick` 的明確 `(executor, model)` 與 periodic tick 預設 model 也改為先查 `model-identities.yaml`，避免 typo 直到 session 內才失敗。
- **Issue #202：task_type 自動選牌與 fix-standard combo**：新增 deck taxonomy loader／selector、`fix-standard` workflow combo、`WorkflowRun.combo_selection` provenance、`cortex work start --combo` authoritative override，以及 `cortex stat --combo-selections` 彙總。Refs #202。
- **Issue #260：新增 `recover-repair-commit` work action**：repair job 失敗終止但已在 builder worktree 留下合法 descendant commit 時，以雙 CAS（`expected_run_id`＋`expected_candidate`）確定性 bind 為新 candidate；判準全部取自系統事實，不啟動任何 model session，冪等回報 `already-recovered`；`retry-build` 既有 CAS 與窄化入口原封不動。

### Fixed
- **Issue #260：resume／dispatch 不再重選 stale failed job**：`resume_workflow_run`／`_dispatch_workflow_card` 的 stale-terminal 判定補上「`exited` 且 exit code 非 0」，第一次 operator resume 即 dispatch replacement，不再空轉一輪；失敗回報附掛唯讀 `terminal_diagnostics`，不授予 candidate authority。

### Fixed
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
- **Issue #263：ship validator 重排為本地 closeout 先於 PR metadata preflight**：archive commit 不再內嵌 push；pre-PR metadata preflight 失敗改回可 resume 的 `pr-preflight-blocked` typed stop、通過後照舊自動建立 PR；slice-based review worktree 補上 frozen authority materialize 與 hash 驗證。
- **Issue #263 補遺（PR #336 code review）**：review worktree authority materialize 的路徑檢查改為先驗證後動作（拒絕 `..`／絕對路徑 ref 於任何 mkdir 之前）；`work_bridge._manager_archive_applied()` 改委派 `manager` 版避免與 `any(...)` 舊語意漂移；`_slice_review_authority_inputs()` 相對 plan/spec path 改以 repo_root 解析，對齊 `_pinned_input_mismatches()` 既有語意。
- **Issue #202 補遺：durable snapshot 不可用時 combo 選擇改走 fail-soft**：`claim.mapped_issue_titles` 先前只在 snapshot hash mismatch 時 bypass；`_load_snapshot` 因 snapshot 不存在／不可讀／schema 損壞 raise 的 `ValueError`（含 `AuthorityValidationError`）未被攔截，會炸穿 `work_bridge.start_canonical_workflow`。現在一併回傳 `None` 落回 bypass-default combo，`load_work_authorities`／`load_work_authority` 維持 fail-hard 不變。
- **Issue #202 code review 修復：override 驗證改用 `load_combo`、`combo` 收斂只在 start 可用**：`deck.selector.select_combo` 的 override 先前只靠 taxonomy 反查判定未知，會誤判 repo 內實際存在但無 task_type 映射的 legacy combo（如 `mcu-feature`）；改為直接以 `load_combo` 驗證。另外 `--combo` 雖標「start 專用」，CLI／porcelain／manager 先前對所有 work action 都會轉交 `combo`，`resume` 在特定時序下可能被未經驗證的 combo override 影響；四層（`control/contract.py` fail-closed 為收斂防線）同步收斂為只在 `action == "start"` 才夾帶／轉交 `combo`，並清除 `work_bridge.start_canonical_workflow` 內與 selector 重覆的驗證死碼。
- **abandon 尋址窗口放寬至全額認領**：abandon 校驗 run refs 與 authority 全等；窗口期舊識別全額認領（撤 openspec exclude）、-v2 暫撤 openspec link。
- **舊識別墓碑 todo（abandon 尋址窗口）**：authority 需檔案級來源，-v2 遷移後舊識別無檔化致 abandon 不可尋址；暫置墓碑 todo，abandon 後移除。
- **-v2 issue links 暫撤（abandon 尋址窗口）**：解 issue contested → authority ambiguous → abandon 無從尋址的死鎖；隨後還原並補 excludes（abandon 先於 exclude 的正確時序）。
- **-v2 excludes 收窄至 openspec ref**：保留舊識別可尋址性（abandon 需 authority），僅排除實際碰撞的 openspec 認領。
- **-v2 重識別補 excludes 斷開舊識別的 source 認領**：消除 confirmed source collision 造成的 repo provider degraded（比照 dispatch-reliability-batch 先例）。
### Changed
- **feat-work-gc 與 design-task-type-taxonomy 重識別為 -v2（#178／#139）**：三代 run 因基礎設施缺陷鏈 superseded 觸發 #218 世代熔斷，依「-v2 識別」慣例重識別於修復齊備的 main 重跑。
### Fixed
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
### Changed
- **W1 canary v2 檔名對齊（#295／#291）**：build 卡 declared inputs 以 `*<work_id>*` glob 檔名，v2 僅改 frontmatter 導致 declared input missing；檔名與 workstream 目錄補 `-v2` 並同步引用。
- **W1 canary 重識別為 fix-persona-catalog-portability-v2（#295／#291）**：三代 run 因 #299／#302／#303 基礎設施缺陷 superseded 觸發 #218 世代熔斷，依「-v2 識別」慣例重識別續作（檔案路徑不動）。
### Fixed
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

### Changed
- **Issue #135：persona enforcement shadow → enforce**：切換前先以
  `python -m paulsha_cortex.persona.replay`（新增，可重跑）回放最近已合併 PR 的
  實際檔案清單，證明對現行 `builder` 派工慣例零誤殺，才把
  `paulsha_cortex/persona/personas.yaml` 的 `enforcement` 由 `shadow` 切為
  `enforce`。`persona-scope.yml`（`scope_ci.py`）現依 `enforcement` 動態決定放
  行：違規時輸出含 persona／觸及路徑／違反規則的可定位 verdict 並 `exit 1`；
  套用 `policy-exempt:persona-scope` label 時不阻擋，但違規內容仍完整輸出（不
  靜音）。`persona-scope` 設為 main required status check 屬 GitHub repo 設定，
  設定步驟見 `docs/persona-scope-enforcement.md`。
### Added
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
### Added
- **Issue #205：per-work planner/builder/reviewer 模型鏈覆寫**：`WorkflowRun` 新增 `model_chain_override`（run-scoped 覆寫，claim/首次 dispatch 時凍結，只作用於本 run，不動共享 `model-identities.yaml`）與 `resolved_model_chain`（三段實際解析結果與來源 override/registry，供事後稽核）兩個 provenance-only 欄位；`_select_workflow_identity` 逐段優先讀凍結覆寫、未指定段落回退共享 registry，覆寫仍須通過既有 capability 與 builder/reviewer independence domain 檢查，違反時 fail closed 並列出可用 identity；`cortex run work start/resume/retry-build/retry-verify/retry-review/...` 新增 `--planner-executor`／`--planner-model`／`--builder-executor`／`--builder-model`／`--reviewer-executor`／`--reviewer-model` 六個 run-scoped 覆寫參數。詳見 `changelog.d/per-work-model-chain.md`。

### Fixed
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

### Added
- **批次 B planning artifacts（#261／#256／#262／#205／#135）**：為五個 issue 各新增 spec／design／plan／workstream todo 與 openspec change（`2026-07-30-<work_item>`），並登錄 `.cortex/work-items.yaml`，作為 cortex work-item lifecycle 的 confirmed authority。五組皆通過 `assess_planning_completeness`（`status: accepted`、必要章節齊備、無 blocking marker）。本 PR 只提供 planning authority，實作由後續 cortex 派工的 build phase 完成。

### Fixed
- **Issue #270：CLAUDE.md 的 changelog 要求對齊 engine R-09**：agent 指引原先三處（改 code 時、
  claim done 前）都只要求 `CHANGELOG.md [Unreleased]`，與 R-09 實際檢查的 `changelog.d/*.md`
  fragment 不一致，導致照指引交付的 PR 必然掛在 `policy / check`（#266／#267／#268／#269
  四個 PR 同時實證）。改以 fragment 為硬性 gate、寫明檔名 slug 慣例與「fragment 須 commit
  才進 diff」；claim-done checklist 的 policy_check 一項補上帶 `--pr-title`／`--pr-body`／
  `--pr-labels`／`--pr-base-ref`／`--pr-head-ref` 的完整命令形式（裸跑會給出假的 `fail: 0`，
  因為 CI 會傳這五個參數並啟用一批 PR／diff-aware 規則）；並移除指向不存在檔案的
  `.github/pull_request_template.md` checklist 項目。`CLAUDE.md` 為 canonical 真檔，
  `AGENTS.md`／`GEMINI.md`／`.github/copilot-instructions.md` 的 symlink 自動同步。

### Fixed
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

### Fixed
- **Issue #253：系統 Service 安裝失敗改為可結構化回報**：`cortex install service` 在 `daemon-reload`、`enable monitor service` 或 `enable manager timer` 失敗時，不再拋出 traceback；改由回傳 `mode=systemd` 的非零 result，訊息固定帶出 systemd stderr、unit directory 與重試指令，並在第一個失敗步驟即停止後續流程。

### Fixed

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
