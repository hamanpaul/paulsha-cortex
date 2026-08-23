# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.17。

## [Unreleased]

- **#692：downgraded job 的 HOME 契約改為 fail-closed**：launch 前拒絕 missing/blank/relative/symlink/wrong-owner HOME，PATH+HOME 雙缺會一併點名，shim 也不再回退到 unit/daemon HOME；HOME `lstat` 診斷不以 chained traceback 洩漏路徑。
- **Phase 2 Docker RC credential projection**：protected Codex credential import 後再次執行 production `trust_root scaffold`，以短暫非機密 reviewer optional fixture 維持 scaffold contract，並把新憑證投影到 Manager-owned canonical authority，讓後續 legal builder runtime provisioning 使用實際部署 authority。
- **Phase 2 Docker RC legal-job identity**：qualification harness now derives the systemd template instance from the raw job identity through the production helper, keeping `%i` and every per-job writable surface byte-for-byte aligned.
- **Phase 2 Docker RC AGY preflight**：AGY 1.1.18 now uses its machine-readable `/quota` response for live quota validation instead of rejecting the supported slash-command path as an unknown status subcommand.
- **Phase 2 Docker RC Codex preflight**：Codex 0.149.0 now uses the pinned app-server JSON-RPC `account/read` and `account/rateLimits/read` contract, validating authenticated account and usable rate-limit windows without inferring quota from `doctor --json`.
- **Phase 2 Docker RC Copilot preflight**：Copilot 1.0.80 now uses its pinned headless SDK server JSON-RPC `account.getCurrentAuth` and `account.getQuota` contract, rejecting absent or exhausted snapshots without relying on interactive output.

- **Phase 2 Docker RC qualification 修正**：R9 現在依 trust-root `writer_accounts` 驗證合法 producer mutation，transactional install plan 同步套用 registry 推導的父層 traverse ACL，避免合法 spool／worktree 寫入被 0700 parent 錯誤阻擋。

- **Phase 2 Docker RC qualification 邊界固化**：`review-verdict` 僅保留為 Phase 2a legacy fallback，R9 將其列為 deny-only asset，權威寫入路徑固定為 `review-verdict-spool`；job-visible 的 manager-only `handoff-manifest` 與 legacy verdict probe 各自使用隔離 parent，避免 builder worktree default ACL 汙染測試；rootless Docker fixture 的 restore 也改由合成 owner 還原，不放寬 production ACL。

- **Phase 2 Docker RC qualification systemd 穩定性**：T3 每次 enforcement probe restart 前重置 Manager 的 start-rate counter，避免連續合法 restart 觸發 `StartLimitBurst` 造成 harness 假紅。

- **Phase 2 Docker RC qualification legal-job probe**：gate Manager negative control 改用 production `prepare_job_log_spool()` 預建 canonical `job.jsonl`，讓 namespace probe 與實際 builder template contract 一致。
- **Phase 2 Docker RC qualification legal-job surfaces**：legal builder control 啟動前改用 production `prepare_commit_spool()` 與 `provision_runtime_surfaces()` 建立 commit、monitor event、Codex home/cache 與 job-log per-job surfaces，確保實際 systemd template namespace 完整。
- **Phase 2 Docker RC qualification Codex controls**：reference container 在首次 installer apply 建立帳號後，以非機密 root-owned legacy fixture（保留 installer 產生的 enforcement `hooks.json`，並在 scaffold 後清除 canonical 與 legacy placeholder credential authority）執行 production `trust_root scaffold`，讓 real template 的 Codex read-only bindings 有完整 deployment input；真正 credential 仍只走 protected stdin import。

- **Copilot foreign-review verdict spool permissions are now file-scoped**：headless
  reviewer argv 只授予 exact `verdict.json`、`rg` 與 `python3` checks，不再以整個
  spool directory 或 broad bypass flags 放行。

- **#501 修復 verification contract hash 被 evidence hash 覆寫**：slice registry 將 pinned
  contract hash 與 current verification evidence hash 分欄保存；verification/status evidence
  不再污染 pinned contract，既有被覆寫的 state row 會在載入時可判定地復原。

- Preserve exact template-instance authority during isolated Codex credential
  harvest in the Trust-root Phase 2 runtime path, joining persisted instances
  byte-for-byte while keeping raw job-id fallback separate for legacy callers.

- **#623:** doctor 的 service-path discovery 現在接受 generated trust-root
   manager/monitor units 所宣告的受保護 deploy `EnvironmentFile`，維持 repo/runtime
   identity 可見，並對缺失、分歧或不可驗證的安裝狀態維持 fail-closed。
- **#718 repair:** prompt slots now live below a Manager-owned, non-renameable per-principal root with durable prelaunch cleanup tracking; typed runtime metadata governs Codex harvest and direct/non-Codex lanes cannot enter it.
- **#718 repair:** template-job harvest now persists the exact Manager-issued runtime instance as durable spool authority, consumes only that validated slot byte-for-byte, and fails closed instead of re-deriving from internal job ids or sibling paths.
- **#718 repair:** canonical Codex migration now copies only `config.toml`, `hooks.json`, `plugins/`, and `skills/`, rejects symlink/special descendants, atomically installs normalized root-owned 0644/0755 controls, and generated builder/reviewer units publish atomic `auth.json` refreshes with a named Manager read ACL before harvest.
- **#718 repair:** isolated Claude/CG workflow prompts now use bounded Manager-created per-job files rather than argv or template stdin; Manager exit accounting cleans them after termination, Codex credential harvest is typed and durably fail-closed, and `gpt-5.6-luna`/`gpt-5.3-codex-spark` carry explicit `model_reasoning_effort` argv pins.
- **#718 repair:** Codex-capable units derive isolated per-job homes and caches from real R1 writable-surface assets; canonical control and credential authorities are now registered deployment assets seeded byte-for-byte from both deployed roles without stubs, and builder/reviewer terminal paths publish and harvest refreshes; reviewer units no longer receive the builder-only monitor event slot.
- **#718 repair:** headless hook event writes now use the authoritative per-job
  slot (including `--spool-root`), monitor harvest covers isolated child slots,
  and repeated Codex control scaffold installs preserve deployed policy content.
- **#718 repair:** template jobs no longer bridge the job-writable log spool to
  Manager controls with a cross-mount hard link; Manager reads the preseeded
  canonical log, completion controls use an explicit Manager-only anchor, and
  Codex last-message output is published readable before Manager completion
  accounting without becoming terminal evidence.
- **#718 repair:** template job log slots now reuse the exact systemd-safe `%i`
  / spec `instance` while the raw dispatch log name stays an explicit
  Manager-only control anchor, so long slice ids no longer desynchronize the
  mounted log slot from completion controls.
- **#718 repair:** systemd-template launch now pre-provisions every typed
  builder/reviewer writable slot before start, surfaces malformed rows with
  the surface id and exact slot path, and keeps write-only rows on their
  deployment ACL instead of the runtime-cache ACL widening path.
- **#718 repair:** Copilot/Agy 的 controlled-egress canonical allowlist 現在補上
  2026-08-21/22 live proxy observation 量到的 exact hosts，並由 trust-root
  regression test 釘住 per-executor membership 與 no-wildcard rows。
- **#718 repair:** downgraded Copilot template jobs now require a
  Manager-selected canonical OAuth `config.json`, seed a private per-job
  `COPILOT_HOME` under the runtime-cache slot, disable auto-update in the
  spec, and refuse broad GitHub token env passthrough.
- **#718：建立五面向 per-job writable surface 單一真相與 instance-scoped slot helper；Codex `exec` 固定帶 `--ignore-user-config`，並同步更新 argv golden pins。**
- **#718（RED）：新增 per-job trust-root writable-surface isolation regression contract tests**，鎖定 canonical surface table、instance-scoped slot、foreign-slot boundary 與 fail-closed slot shape。
- **#776 補遺：archive 前補齊舊 manifest 世代的 openspec change scaffold**（canonical-specs-invalid 修復）。
- **#776 補遺：ship adapter refs 守衛改走 openspec 相容判定**（helper 下沉 claim.py；refs-differ 誤擋修復）。
- **#776 補遺：舊 manifest 的 openspec-propose 卡視慣例名 change 為 run 自產**（85114100 二度被誤殺的根因）。
- **#776 補遺：`recover-superseded` 補進 control contract 白名單**（驗證分支與 porcelain choices 同步）。
- **#776（#765 第八處）：resume 穩定識別容忍 run 自產 openspec 落地 authority**——不再 supersede 已驗證 run；新增 `work recover-superseded` 撿回被誤作廢的 run（official authority-restart 語意）。
- **#765（intake 回寫）：`fix-read-repo-tier-fail-closed` openspec change 回寫 main**——monitor 只掃 main，change 僅在 candidate 導致 `mapped_openspec` 恆空、ship 卡 target 計數；檔案與 PR #764 byte-identical。
- **#765 補遺：review 對 builder job 的綁定改跨 era（#216 AC5 落實）**——build 產物不再因 authority 前進成孤兒。
- **#765 補遺：registry reset evidence 守衛以 claim era 定錨。**
- **#765 補遺：dispatch reuse／retry 判定走同 era `reusable` 子集**（第五個出口，binding 必炸的真正回傳點）。
- **#765 補遺：recovery 選擇器（最後一個 era-blind）以 claim era 過濾。**
- **#765 補遺：delivery journal rebase 連 claim_key 一起帶**（era 雙店面不一致修復）。
- **#765 補遺：binding mismatch 錯誤帶 job_id 與兩側值。**
- **#765 補遺：retry-card 的 target-jobs 亦以 claim era 過濾**（舊 era evidence
  不再擋死新 era 重派）。
- **#765 補遺：daemon `_log_error` 首次出現附完整 traceback**（重複維持抑制）。
- **#765（部分）：advance 的 terminal-job 選擇以 claim era 過濾**——authority
  restart 後前代 job 不再造成每 tick 綁定炸裂，新 era 正常重新派工。
- **#759：pr-preflight evidence 補 backend stdout/stderr 有界尾段**——失敗原因
  進 evidence，不再只靠實機重現定位。
- **#760：`--skip-tests` 的 FullSuiteEvidence 契約接上 production**——gate 綠的
  build 候選在採信點落 tree-hash 定址 evidence，delivery preflight 消費之；
  manager 環境不再第三跑全套。tdd-red 的 RED 天然排除、記錄失敗不影響採信。
- **#757：operator 裁決改為 run 級獨立 prompt 區塊**（verify/review 的 matching 以
  candidate 定錨，掛在 retry_context 下會在 candidate 換新時整組消失）。
- **#755：`--reason` 擴到 `retry-build`（共用 #752 的 adjudication evidence 落地），
  operator 對 repair 回合的指示有通道；retry-card 收斂到同一支 helper。**
- **#752 補遺：retry-card 的 adjudication evidence 改接 `resolved_state_path`**
  （原始參數在 daemon 路徑恆為 None，人裁通道上線即不可用）。
- **#752：verify 階段的人裁通道。** `retry-card --reason` → Manager-owned
  immutable evidence（`cortex-operator-adjudication/v1`）→ retry_context 的
  `operator_adjudications` 鍵（builder 與 reviewer 卡都吃）。design/todo 矛盾
  這類 needs_human 判定，operator 裁決終於有可信路徑進 prompt。
- **#750：repair 回合帶上打回它的 verification 判定（`retry_context.review_rejection`），
  盲修不收斂的迴圈關掉。** 跨卡回饋機械組裝、有界、標注 reviewer-terminal 來源；
  首派 prompt 不變、採信端零改動。
- **#748：三分模式 reviewer settings 補 `allow: ["Bash"]`。** #746 關內層後
  `autoAllowBashIfSandboxed` 的放行消失，`dontAsk` 下 pytest 全拒、零 gate 可跑；
  deny 優先於 allow、憑證拒絕不變、direct 模式不變。
- **#746：claude reviewer 內層 sandbox 依 runner mode 分岔（#714 的 reviewer lane
  版）。** bubblewrap 與加固剖面硬性互斥；三分模式關內層、外層＋採信端完整性
  檢查為邊界，direct 模式逐字不變；`permissions.deny` 兩模式相同。
- **#743：auto 路徑的 ancestry baseline 與採信端同一條導出（`run.candidate_head or
  dispatch_head`），中段 build 卡不再每張都要人工 `regenerate-gates`。**
  `dispatch_head` 是 run 層級凍結值、後續卡逐字繼承首張卡的，#738 首版拿它當
  基線使中段卡的 ledger 被「baseline 不符視同缺席」守衛正確拒絕。
- **#742：reviewer sandbox 交接（#710 的 reviewer lane 版），三分部署下 verify／
  review 卡派得出去。** 容器收斂 0701；per-job sandbox 由 owner 顯式
  `setfacl -R u:<reviewer>:rwX`（default ACL 繼承在 `UMask=0077` 下會被 mask
  歸零，#736 同族）。帳號單一導出、direct 模式零回歸、回收不受影響。
- **#740：誠實紀律補環境維度，builder sandbox 的環境紅不再使 focused 寫入卡確定性
  自報 failed。** 判準來自 Manager 在 gate 環境的重跑；sandbox-only、與變更無關的
  失敗改為「省略該 gate＋diagnostics 記錄＋照常交付 candidate」，宣稱綠仍禁止、
  ledger 紅照樣 fail-closed。`status_policy` 失敗條款改為「because of your change」。
- **#738：candidate 驗證下放 gate ledger，三分部署下帶 candidate 的 build 卡終於
  可被採信。** gate 身分在快照副本上收集 `worktree_state`（head／dirty／
  ancestry），Manager 只消費權威 ledger、不再以自己的身分 `git -C <builder 樹>`
  （#641 收掉唯讀 ACL 後那條路結構上必死）。baseline 經封閉 argv
  `--assert-ancestor` 由 job 記錄導出；ledger 缺席時逐字退回既有路徑（direct
  模式零回歸）。#629 後半／#641 預留的那張票。
- **#736：gate snapshot 依名跳過可再生快取目錄（`__pycache__`／`.pytest_cache`／
  `.mypy_cache`／`.ruff_cache`），寫入卡不再結構性必死。** builder unit 的
  `UMask=0077` 讓 pytest 產生的 `.pytest_cache/` 以 group bits 0 落地 ⇒ ACL
  `mask::---` ⇒ gate 帳號讀不到 ⇒ `snapshot_worktree` 整格 `SnapshotError` ⇒
  `gate-spool-empty` crashloop（exit 74）。快取不是候選樹內容（.gitignore 排除、
  gate 的 pytest 會自行重建）；清單外的不可讀項目維持 fail-closed。#723 一族第三例。
- **#734：wrapper 斷言改為逐 token 語意判定，不再對整串 argv 做 substring 搜尋。**
  gate 執行帳號名（`cortex-gate`）出現在 pytest tmp 路徑裡，讓
  `test_planning_wrapper_has_no_gate_bundle_verdict_sentinel` 的 `"gate" not in joined`
  只在 gate 環境紅並以 `GateContradictionError` 擋死演示鏈。新判準：token basename
  不得是 shell／git、旗標名不得含 gate／bundle／verdict／sentinel／exit、裸字 token
  不得是 wrapper 詞彙、任何 token 不得以 `.exit` 結尾；路徑值與 `--skip-git-repo-check`
  這類合法旗標不進比對範圍。mutation 驗證四種自產形態仍會紅。`#723` 環境可攜性一類的
  第二個實例（第一個是 umask，`#724`）。
- **#731 (A)：候選 git base 補上可稽核的重新凍結入口 `cortex work refreeze-base`——凍結是對的，缺的是重新凍結。** 長壽 work item 的候選基底永遠停在第一次 claim 的 commit，`abandon` ＋ `reset-reclaim-budget` ＋ `work start` 換不掉（`work start` 對還有 active workflow 的 work item 回 `action=resume / reason=active-workflow`，不走新 claim）；0819 實測 mirror 已是 `7eb707b`、候選樹仍是 `59a7a9b`，於是已在 main 的修法**結構上永遠到不了**正在跑的 run。**查證到的權威來源**：候選基底只有一處被消費——`manager._dispatch_workflow_card` 建首張 build 卡工作區時讀的 `WorkflowRun.frozen_readiness["base_sha"]`，傳進 `ScriptWorktreeCreator.create(..., base_sha=…)`；該欄位為 `None` 時 `create()` 退回 `self._base`，而 dispatch 傳的是字面 `base="main"` ⇒ 實際基底是**來源樹的本地 `refs/heads/main`**，而 `readiness_checker` 在 production 從未接線，故實機 run 的 `frozen_readiness` 恆為 `None`——`git fetch` 只動 `refs/remotes/origin/main`，這就是「mirror 已更新、候選樹沒動」的機制。新動作把新基底寫進 dispatch 真的會讀的那一格，不新造第二份真實來源。形狀比照 `reset-reclaim-budget`：`--expected-run-id` CAS ＋ bounded `--actor`／`--reason`（由 `control/contract.py` 在所有入口收斂點強制）、mirror fetch 走 claim 用的**同一支** `claim_readiness.base_sha_probe`、落 immutable `cortex-work-candidate-base-refreeze/v1` evidence（舊／新基底、fetch 結果、全部 fast-forward 基準、build branch 位置、actor／reason）。入場條件 fail-closed：唯一 `ongoing` run ＋ exact run id、phase ∈ `claim`/`define`/`plan`/`build`、`candidate_head`／`verified_head` 皆 `None`（否則 base 改由 handoff 決定，重新凍結會是**靜默 no-op**）、無 in-flight job、無已發佈交付物，且新基底必須是**每一條已記錄基準**的後代（非 fast-forward 一律拒絕）。其中 build branch 那一條就是 **#613** 的前置檢查——判準與 `create()` 的守衛是同一個 `git merge-base --is-ancestor` 述詞，且在改任何狀態**之前**就問，不製造「refreeze 成功、下一拍才炸」；branch 名推導抬成 `manager.workflow_build_branch()` 單一導出點。**出口狀態 == 入口狀態**（#728 同紀律）：不動 phase／facets／candidate，唯一變更是 `frozen_readiness["base_sha"]` ＋ 一筆 evidence ref。迴歸釘住走**正式** dispatch 路徑（真 `ScriptWorktreeCreator`、真 git repo、真 `retry-card`）驗新派工 worktree `rev-parse HEAD` 逐字等於新的 `origin/main`，並另釘住「未重新凍結時不得跟著漂」證明 hermetic pinning 沒被放寬。詳見 `changelog.d/731-refreeze-base.md`。
- **#731 (C)：run 的候選 git base 攤到 `cortex status`／`cortex work show` 上，過舊時給具名診斷。** 0819 現場逐字：候選 worktree `rev-parse HEAD` ＝ `59a7a9b`、mirror `refs/remotes/origin/main` ＝ `7eb707b`（落後 13 支 PR），而兩個介面的**任何欄位都看不到上面任何一個**——這個採信鏈的關鍵事實只存在於檔案系統上；run 上唯一顯眼的「版本」欄位 `source_revision` 是 64-hex 的 authority digest（work item 來源材料的 sha256），與 git base 無關，那晚它把診斷帶偏了兩次。新模組 `coordinator/candidate_base.py` **不新造第二份事實**，只把既有欄位接到曝光面：優先 `run.frozen_readiness["base_sha"]`（#211 凍結集），沒有凍結集時退回該 run **第一張 build 卡的 `job["dispatch_head"]`**（實機 0820 量測：29 個 run 的 `frozen_readiness` 全為 `null`，唯一記著基底的正是它）。距離由 mirror（`PSC_REPO_ROOT`）上**現有的** remote-tracking ref 以 `rev-list --count` 算出，**絕不 fetch**（那是 claim 的職責），輸出以 `fetched: false` ＋ `measured_against` 誠實標示比較基準；有測試釘住 status 路徑只發 `rev-parse`／`rev-list`。落後達門檻（`CANDIDATE_BASE_STALE_THRESHOLD_COMMITS`，預設 10，可用 `PSC_CANDIDATE_BASE_STALE_THRESHOLD_COMMITS` 覆寫，只有一處定義）時給**機器可讀**的 `candidate-git-base-stale`，不是自由文字；讀不到 mirror／算不出距離時 fail-soft 落 `<unresolved:MirrorRootUnset>`／`<unresolved:MirrorMainUnreadable>`／`<unresolved:BaseNotInMirror>` ＋ `candidate-git-base-distance-unresolved`，run 還沒有基底時為 `candidate-git-base-absent`，一律不靜默省略。欄位命名為 `candidate_git_base`（字面寫著 git base，不與 `source_revision` 共用詞彙），`work show` 印出時一併點明 `source_revision` 是 authority digest；`WorkflowRun.source_revision` 與 `claim.semantic_source_revision` 的 docstring 同步補註——那個誤導本身就是缺陷的一部分。Monitor 側走既有 `observations` 通道，**不新增 WorkflowRun 欄位**（#261／#527 已付過 projection degraded 的學費）。與 #731 (A)（#733 的 `cortex work refreeze-base`）**共用單一導出點**：`frozen_readiness["base_sha"]` 的正規化／驗證抬成 `candidate_base.frozen_base_sha()`，寫入端與讀取端呼叫**同一支函式**（測試以 `is` 斷言同一物件）；凍結集為 `None` 時兩側的後續處置刻意不同且寫進 docstring——(A) 退回本地 `refs/heads/main`（下一張卡**會**用什麼基底）、(C) 退回第一張 build 卡的 `dispatch_head`（候選**已經**坐在哪），是同一條時間軸的前後兩點；(A) 重新凍結成功後 (C) 自動改讀凍結集、`behind_origin_main` 歸零，兩半機械接合（有測試釘住）。本 PR **只做 (C)**，hermetic pinning 一字未動。詳見 `changelog.d/base-visibility.md`。
- **#727：codex planner safe probe 永遠 not-ready——`-o` 的落點是第二份決定、串流備援從來沒能用過、而 diagnostic 只有 `ValueError` 五個字。** 0820 實機在真實加固面複本（`psc_run_under cortex-reviewer-job-jit`，`unit_replica_properties()` 全量導出 **52 條 property**）下跑一次 `_planning_argv()` 產出的 production argv：rc=0、`agent_message` 逐字等於 `expected` ⇒ **模型端完全正常**，失敗全在解析路徑。**(1)** `_planning_argv()` 原本自組 `Path(temp_dir)/"last.json"`，job 模式下 `temp_dir` 被硬填 `"/tmp"` ⇒ 落在 `PrivateTmp` ⇒ Manager 讀不到 ⇒ 第二候選恆為 `None`；改由 `job_workspace.job_last_message_path()`（＝#714 缺陷 2 已寫下的那條規則）機械導出，`last_message_path` 成為**必填關鍵字參數**，落點嚴格落在模板 unit 既有的 `ReadWritePaths=` 之內、由 Manager 預建 `0620`（**零部署動作**），design 的 D-j／R-2 退步解除。**(2)** `_find_json_object()` 的頂層是嚴格「整串就是一個 JSON 物件」而 codex `--json` 是 JSONL ⇒ **串流備援名義上存在、實際從來沒能用過**；新增 `_extract_stream_json()` 比照 `manager._extract_terminal_json()` **由尾端往回找**，開頭那筆 `item.type=error` 因此結構上遮不住輸出本體，排在既有兩候選之後 ⇒ 「兩者皆有」行為逐字不變。**落檔那條刻意保留**——兩條都在才分得出「落檔斷了」與「模型沒輸出」。錯誤事件一律零產出（codex 錯誤訊息常內嵌 JSON，實機逐字 `unexpected status 400 …: {"detail":…}`）。**(3)** `safe-probe-failed`／`models-probe-failed` 改落**有界**診斷（`PLANNING_DIAGNOSTIC_LIMIT=2000`＝`manager.RETRY_CONTEXT_EVIDENCE_LIMIT`）：`rc=` ／ `stdout=<節錄>` ／ `last_message=<路徑>|<absent>|bytes=N|<unresolved:…>`；型別名**錨在第 0 個 token**，`classify_probe_failure()` 只看那一格 ⇒ 分級輸入逐字不變、沒有任何 probe 因此從 not-ready 變 ready，stderr 仍然一個位元組都不取。同一份實機串流在 main 上逐字 `ready=False diagnostic='ValueError'`、在本 PR 上 `ready=True`。順帶修掉 probe cache `_stat_marker()` 把 EACCES 報成 `<absent>` 的假話（**憑證輪替仍不會讓快取失效**，那條要動部署面，只記錄）。⚠️ 另記一條量測陷阱：`systemd-run <bare command>` 用 systemd 內建 path 解執行檔、不是複本的 `Environment=PATH=`，量到的是系統層 codex 0.42.0 而非 production `execvpe` 解到的 toolchain 0.147.0。詳見 `changelog.d/727-planner-probe.md`。
- **#728：`recover-planning` 不再把沒有 brainstorm 背書的 run 推進到 `plan`——recover 的出口狀態與 planning-authority 對帳的入口狀態改為共用同一組前置條件斷言。** 現場 run `workflow-ef40fb2793c5b83818d9`（`brainstorm_required=true`）recover 後逐字回 `phase=plan`，下一拍 `manager.resume_workflow_run:planning-authority` 必定 `ValueError: workflow brainstorm evidence missing`，且 attention 的 `next_actions` 為空 ⇒ 只剩 abandon 整代重來的確定性死結。**裁決 (B)**：`recovery_basis: "planning-runtime-retry"` 是「解除封鎖、讓下一拍重跑」——`_recover_planning_action` 全程沒有任何 planner／runtime 呼叫，而唯一產生 brainstorm gate evidence 的 `manager.apply_workflow_action` define 段守衛逐字是 `if run.current_phase not in {"claim", "define"}`，推進到 `plan` 等於永久關掉產生背書的唯一入口。前置條件抬成單一導出點 `workflow.brainstorm_authority_bound()`，對帳側與 recover 側共用同一個函式物件；recover 以「打算寫進去的 phase」發問，不合法就退回 `define` 由正常流程重跑。順帶修掉同一條在 `define`／`operator_resume` 上的既有 wedge（實測未修改的 main 兩個 phase 都 wedge）。另把 `needs_human` run 的基礎動作集合抬成 `claim.needs_human_next_actions()`（`abandon` 永遠合法 ⇒ 不可能為空），由 `_resume_decision` 與 `manager.workflow_status_entry` 共用，`planning-authority-reconciliation-failed` 不得再出現 `next_actions: []`。詳見 `changelog.d/728-recover-brainstorm.md`。
- **#716（選項 B 的後半）：寫入卡的 argv 切換——`builder-workspace-write` 那一列改發 `-s danger-full-access`，且不再附 `--enable use_legacy_landlock`。** `workspace-write` 在 legacy landlock 下 100% panic rc=101（`linux_run_main.rs:318`），對寫入卡「內層沙箱」不是防護是必死卡；出口管制（PR #725）已部署驗證，「不補出口就不要採 B」成立，該列殘餘防線＝**systemd 外層 ＋ 出口管制**（七面向外層零成本接得住；#718 記著今天就存在的四個缺口）。**只動導出表與其消費端**：表上新增 `attaches_inner_sandbox` 欄，附掛條件跟著契約走——read-only 族**維持** legacy landlock（真的在擋）、`danger-full-access` 不附、bypass 不附；`workspace-write` 不得再上表、`grants_filesystem_write` 的誠實語意（True＝連內層都沒有）皆為 import 期斷言。**刻意不用** `--dangerously-bypass-approvals-and-sandbox`（它與 `--dangerously-bypass-hook-trust` 綁在一起，會連 #698 的 hook 信任閘一起關掉）；planner／reviewer／write-forbidden 三列 argv **byte-identical 不變**（`tests/test_write_card_argv_716.py` 黃金釘子）。**核可閘已量**（真實加固面複本 54 條 property ＋ 真實 `codex exec` 一次）：headless `-s danger-full-access` 不卡核可閘、模型自主命令 rc=0、零 approval 請求。探針分兩族：read-only 族四步矩陣照舊；`danger-full-access` 列**沒有內層可驗**，改斷言 (a) 命令執行得了 (b) 出口管制在（`env -u HTTPS_PROXY` 直連必須 `TimeoutError`，該列僅存的網路防線），a/b 已實機跑過與期望逐字一致。`emitted_sandbox_modes()`＝`('read-only','danger-full-access')`。**B 至此兩半齊備，但端到端（真實派工跑會寫檔的卡）尚未驗**。詳見 `changelog.d/write-card-argv.md`。
- **#723(a)：`test_tree_snapshot_covers_empty_directories_directory_links_and_modes` 對 umask 不可攜——寫死的 `chmod(0o700)` 在 `UMask=0077` 的 unit 下是 no-op。** gate 的 job 跑在 `cortex-gate-job@.service`（逐字 `UMask=0077`），該 unit 底下 `empty.mkdir()` 建出來的目錄**已經**是 `0700`，接在後面的 `empty.chmod(0o700)` 因此什麼都沒改，`_tree_snapshot()` 前後兩個雜湊逐字相同，`assert ... != baseline` 必紅；實機 ledger 逐字 `AssertionError: assert '687b1390…' != '687b1390…'`，而同一份程式在 CI 與 operator 本機全綠。**修法**：判準改成「chmod 成一個**與現況不同**的 mode」而非 chmod 成某個字面值——`mutated_mode = baseline_mode ^ 0o001` 由實測到的 `baseline_mode` 導出，在任何 umask（0077／022／0 皆已實跑）下都必然與現況不同；翻的是 other-execute 位元，owner `rwx` 不動，走訪不受影響。**斷言沒有被拿掉也沒有放寬成恆真**：把 `_tree_snapshot()` 的 `metadata.st_mode` 換成 `stat.S_IFMT(metadata.st_mode)` 做 mutation 驗證，三種 umask 下這條都轉紅。**同族掃描**（281 個 `tests/*.py`，四類判準）另找到兩處同因：`tests/test_per_job_workspace_acl_710.py:650`／`:720` 的 `pool.mkdir(mode=0o701)`——`mkdir(2)` 的 `mode` 引數**會**被 umask 遮罩（`chmod` 不會），`UMask=0077` 下實際是 `0700`，pool 根的 other-execute 被靜默拿掉，而「借來的帳號 traverse 得進 pool」正是那兩條 ACL 隔離測試的前提；同檔 `acl_tree` fixture 用 `os.chmod(root, 0o755)` 正是同一個理由，pool 漏掉了。改成 `mkdir()` ＋ `chmod(0o701)`。這兩條在 operator umask 下今天是綠的，屬潛伏而非現行紅燈。⚠️ jit 剖面也是 `UMask=0077`，故本修正與 `#723`(b) 的剖面裁決無關，兩條路都受益；`#723`(b)（strict 剖面 MDWE 殺 node，`test_openspec_archive_purpose.py`）屬 operator 裁決，本次不動。詳見 `changelog.d/umask-portability.md`。
- **#716（選項 B 的前置）：出口網路管制落地——`IPAddressDeny=any` ＋ 專屬 loopback 位址上的主機名白名單 proxy。** 實測 `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` 之下 job 一次跑完逐字 `CREDS-READABLE / EGRESS-OPEN`（模型跑的任意命令可讀出自己的 token 並外傳，一條命令兩步）。位址級白名單表達不出「只連得到模型 API」（`api.openai.com` 解到 Cloudflare、CIDR 會漂），因此改為兩層：核心層的 `IPAddressDeny=any` ＋ `IPAddressAllow=<proxy>/32`（模型命令 `env -u HTTPS_PROXY` 繞不過），加上 root-owned unit 上的 `Environment=HTTPS_PROXY=`。codex 自帶的 `network_proxy` 查證後不採用——它逐字是「套在 sandboxed sessions 上」的限制，而 B 的前提就是關掉那一層。新增 `cortex-egress-proxy.service`（`User=cortex-egress`，非任何 job 帳號、無 root、零 ReadWritePaths）與 `paulsha_cortex/trust_root/egress_proxy.py`；白名單由 `EXECUTOR_TOOLS.api_hosts` → `permgen.egress_allowlist()` 單一來源導出。shim 端補上把 unit 宣告帶進 job 環境的第二層（少了它整件事是無聲的 no-op）；`unit_replica_properties()` 對半套的出口管制 fail-closed。圍堵條款（`InaccessiblePaths`／`TasksMax`／`MemoryMax`＋`MemorySwapMax` 成對）套到六份 job unit；gate 明示不受出口管制、Manager／monitor 完全不動。`SocketBindDeny=any` 重量之後**刻意不加**（本機 `-BPF_FRAMEWORK`，量到不生效），登記在 `permgen.HARDENING_DEFERRED` 並印進 unit 註解。真實 `codex exec` 在該加固面下 rc=0、模型逐字回 `PSC716-EGRESS-OK`。詳見 `changelog.d/716-egress.md`。
- **#721：派工 prompt 的 gate 適用範圍由 harvest 端同一支判準導出，`test_policy=none` 的卡不再被告知「Manager 會重跑 pytest 並用它判你的 passed」。** 現場 job `wf-6c37c77ca1-worktree-isolation-8`：`worktree-isolation` 的 `test_policy` 逐字是 `"none"`，`terminal_contract.expected_gate_names_for_test_policy()` 對它回 `frozenset()`（docstring 逐字點名這張卡），但 `manager._workflow_job_prompt` 的 `allowed_names` 是 operator **全部** `PSC_GATE_CMD_*` 宣告、與 `effective_test_policy` **無關**，整段 prompt 只在 `red-required` 分岔。模型讀到「the Manager re-runs exactly these commands ("pytest" = `python3 -m pytest -q`), and a passed status is judged against those real results」就去跑 pytest，在 `#716` 選項 F 之後的 `-s read-only` 沙箱下死於 `No usable temporary directory available`，terminal 回 `failed`，Manager 自動重派 ⇒ **確定性無限迴圈**。`#540` 機械化了 gate **名稱**，本票是同一個錯誤的另一半——**適用範圍**沒有機械化。**修法**：新增 `gate_ledger.card_requires_gate_evidence()`（轉呼叫 harvest 端那支判準，prompt 端不寫第二份）與 `gate_ledger.card_gate_names()`，`allowed_names`／`status_policy`／`gate_evidence.description` 三段都由它導出；不要求 gate 結果的卡 `allowed_names` 為空、逐字要求 `gate_evidence: []`、不出現任何逼模型自己跑 gate 的句子，`focused`／`full`／`red-required` **逐字不變**（原文複本常數釘住）。**收窄只做布林、不做名稱集合 ∩**：`expected_gate_names_for_test_policy()` 回的是**測試**這一個訊號，拿它 ∩ ledger 名稱會讓多 gate operator 收到「Manager 只重跑 pytest」而 harvest 照樣拿 `openspec` 的失敗打掉 `passed`——那是本票的鏡像缺陷；不變式「dispatch 講的判定範圍 == harvest 真正判的範圍」由多 gate 測試釘住。**none 卡仍據實揭露 Manager 自己那一次 gate 執行**（`gate_runner.ensure_gate_ledger()` 只看 phase 不看 `test_policy`，實機 ledger `wf-6c37c77ca1-worktree-isolation-3.gates.json` 內確有 `pytest` 一列，且 `authorize_terminal` 對 ledger 內任何非 passed gate fail closed）。順帶把 `#716` 選項 F 的隱性邊界寫進 `trust_root.SANDBOX_MODE_DERIVATION` 的 `builder-write-forbidden` 那一列：`-s read-only` 之下任何需要暫存檔的命令都會失敗，`/tmp` 也不例外。詳見 `changelog.d/gate-scope.md`。新增 `tests/test_gate_scope_test_policy_721.py`（17 測試，修復前 12 紅）。
- **#716（選項 F）：sandbox mode 由**卡片契約**機械導出，不由 persona 一刀切；順帶修掉 #715 探針的假綠。** `#714`／PR #715 落地之後 builder **仍然**一條命令都跑不了，逐字 `permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock`。**病因是 argv 上的 `--sandbox workspace-write`**（不是 git、不是 `$CODEX_HOME`、不是 `$TMPDIR`——四條都已逐條否證）：codex 由它導出 `:workspace` 族 permission profile，該族要求 direct runtime enforcement，而 legacy landlock 路徑不實作它（`linux-sandbox/src/linux_run_main.rs:318` 的 fail-closed 檢查）。**判準是一條性質而不是某個具名 profile**——profile 只要攜帶**任何** filesystem 寫入授權就要求 direct runtime enforcement（`-P` 實測：`extends=":read-only"` rc=0／`":none"` rc=0／`":workspace"` panic／`":read-only"` **加一條** `filesystem={"<路徑>"="write"}` **panic**）。這是 **session 層級**判定，發生在任何命令執行**之前**，所以模型的唯讀 `git rev-parse HEAD` 與一條寫入命令 panic 得一模一樣。而 `build_codex_argv` **完全不看 `commit_policy`**（`read_only` 是 launcher 維度 `as_read_only()`，builder 一律走 `else` 分支）⇒ 一張 `commit_policy=forbidden` 且 `declared_outputs` 為空的唯讀 build 卡拿到的是**寫入授權**——**那是獨立成立的最小權限缺陷**，與 landlock 無關；legacy landlock 只是讓它從「權限給多了」變成當場 panic。 **修法**：新增 `registry.SANDBOX_MODE_DERIVATION`——五種 `JobWriteContract`（`unsafe-bypass`／`planner-read-only`／`reviewer-review-only`／`builder-write-forbidden`／`builder-workspace-write`）× mode × `grants_filesystem_write`（**這一欄記的就是 codex 那條判準**）× 量測 note，mode 由 `derive_job_write_contract()` ＋ `sandbox_mode_for()` 兩步機械導出，**import 當下強制**（漏一格模組載不起來；導出函式對整個布林定義域窮舉，可解組合必須落在表上、表上不可有死格；planner／reviewer 恆為 `read-only`；`grants_filesystem_write` 與 mode 矛盾即 fail）。形狀比照 #708 的 `JOB_LOG_SPOOLS`／#710 的 `JOB_WORKSPACE_REACH`／#712 的 `JOB_GIT_WORKSPACE_TRUST`——「只修一格」在**結構上做不到**，`build_codex_argv` 裡**沒有**第二個 `if`（那條 `if/elif/else` 整個被登記表取代）。**保守方向**：只有 `commit_policy` 逐字 `forbidden` **且** `declared_outputs` 為**空序列**才降為 `read-only`；契約缺欄／型別不對／非空一律維持 `workspace-write`（**不猜**——「解不出來就降」會把一張真的要寫檔的卡靜默弄壞，而那種遠距症狀本 repo 已經付過四次診斷成本）。判準由 `registry.card_contract_forbids_workspace_write()` 出，接線在 `manager._specialize_workflow_launcher()`（吃的是 `_LEGACY_CARD_EXECUTION` 補值後的**有效**契約，因此 `worktree-isolation` 這張原症狀卡真的降得到）。**planner／reviewer 的 argv 逐字不變**（測試釘住 byte-identical，且導出優先序刻意讓 read-only 族壓過 `write_forbidden`）；write-forbidden 的 build 卡與今天**只差 mode 一個 token**（`--skip-git-repo-check` 刻意不加——那張卡跑在 per-job clone 裡）；**job 角色不變**（仍以 `cortex-builder` 起跑，`_is_review_persona()` 的三個判準一個都沒動）；preflight 的剖面名同步成 `codex:write-forbidden`（design D2：報錯剖面就只是安慰劑）。**加固面 diff 為空**——八份 unit 逐字不變，零部署動作。 **實測（一次真實 `codex exec`，prompt 極短）**：`--sandbox read-only --enable use_legacy_landlock -o <path>` → rc=0、`turn.completed`、`job.last.json` 有內容 ⇒ **harvest 路徑必要的 `-o` 產物在唯讀模式下仍寫得出來**（`-o` 由 codex 進程自己寫、不經模型沙箱——這是量到的，不是推論）。 **誠實邊界**：F **只**解得了唯讀卡；同一個 build phase 的下一張會寫檔的卡仍會撞同一面牆，#716 票上的 A／B／E 裁決仍要做，只是適用面從「所有 build 卡」縮到「真的要寫的卡」。
- **#716 第 4 節：`permgen.build_inner_sandbox_probe` 是假綠——它從沒碰過 builder 的 `workspace-write`。** #715 的兩條驗收都跑 `codex sandbox -- <固定命令>`，**不帶** `-c sandbox_mode=`，而那時 codex 導出的是**唯讀族** profile ⇒ 它驗到的是 planner／reviewer 的形態。同一份加固面複本、同一次量測（0819，`psc_run_under` 全量導出 42 條 property，instance 用真 worktree）：不帶 mode → rc=0（**綠**）；`-c sandbox_mode='"workspace-write"'` → **panic rc=101**（真實回歸）。**改成**驗「**`build_codex_argv` 會發出的每一個 `--sandbox <mode>`** 在真實加固面下都裝得上內層沙箱」：mode 清單由 `registry.emitted_sandbox_modes()` **機械導出**（手抄就會再抄成只有 `read-only` 那一格，正是原症狀本身）；**產生器與產出的 shell 各自**先斷言清單含 `workspace-write`，不含就當場停並印出理由；每個 mode 都配**負向對照**（不帶旗標必須**仍然**失敗且逐字 `bwrap: Can't read /proc/sys/kernel/overflowuid`，rc=0 ⇒ 外層被放寬了，當場停）。⚠️ **第二種假綠的陷阱寫進註解與 runbook**：`codex sandbox` **忽略 `config.toml` 裡的 `sandbox_mode`**，只吃 `-c` 覆寫（實測：config 寫 `workspace-write` 不帶 `-c` → rc=0 **什麼都沒驗到**；config 空、`-c sandbox_mode='"workspace-write"'` → panic rc=101）⇒ **探針每一條命令都帶 `-c`**。⚠️ **第三種假綠**：「內層真的在擋」那一段只量得到 `read-only` 那一列（`workspace-write` 在任何命令執行之前就 panic，結構上量不到），因此那幾條逐字帶 `-c sandbox_mode='"read-only"'`，拿掉當成「量了兩種 mode」是假的。產生器**一行 `--property=` 都不自組、一個 `--setenv=` 都不帶**（D13），沿用既有 `psc_run_under`。 **⚠️ 落地之後這條探針仍然是紅的，那是誠實狀態**：0819 實跑逐字——`1[read-only]` rc=1 bwrap／`3[read-only]` **rc=0**／`1[workspace-write]` rc=1 bwrap／`3[workspace-write]` **rc=101 panic**（`linux_run_main.rs:318:9`）／旗標仍在 rc=0。**那條紅逐字代表「#716 的寫入卡那一半未解」**，**不得**為了讓它綠而放寬判準——#715 就是這麼綠的。另新增 `tests/test_card_contract_sandbox_mode_716.py`（42 測試＝41 通過 ＋ 1 條具名 skip：真的起一份模板 unit 並讓 codex 真的導出一次 permission profile 需要 root ＋ systemd ＋ 四個 job 帳號，單 UID 的 CI 上 bwrap 起得來、`workspace-write` 也不會 panic，兩個方向都重現不了）與 runbook 第 4e-2h 步。
- **#717：build 卡要說「我需要人」得先交出 40-hex candidate——當失敗原因正是「一條命令都跑不了」時結構上做不到，模型 diagnostics 全被丟掉。** 實機 job `wf-6c37c77ca1-worktree-isolation-7`（0819 17:28，`exit_code=0`）：模型**正確地**回了 `needs_human`，還把病因逐字寫進 envelope 的 `diagnostics`（`唯讀 git 檢查連續兩次遭執行環境 sandbox runtime panic，無法確認 worktree 隔離狀態或執行 pytest。`／`permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock`）。Manager 端落成 `ATTENTION: build/card-terminal-schema-retry-exhausted` ＋ `workflow terminal payload did not satisfy the result contract`——**模型寫的病因一個字都沒進 attention**，operator 要看病因只能 `sudo cat` job 的 `.jsonl` 往回翻。同一族診斷缺陷的第六輪（#672 #679 #701 #704 #707）。 **(1) 表達力（採票上的裁決 (a)）**：`manager._retryable_nonpassing_workflow_terminal()` 是「模型明示要求停止」的唯一入口，而 build phase 的入場券過去是「交得出 `git rev-parse HEAD` 的 40-hex SHA」；走不進去就掉進 `_malformed_workflow_card_terminal()` 的 `if raw.get("status") != "passed": return True` ⇒ **唯一一種本契約無法表達的失敗模式，恰好是最需要被表達的那一種**（#716：模型結構上取不到 HEAD，只能填 contract 裡看得到的 64-hex `source_revision`）。非通過狀態（`failed`／`needs_human`）下 `candidate` 改為**只收斂型別**（`null` 或字串），不再驗 40-hex；plan phase 一併放寬（判準的理由與 phase 無關，#578 的另一半）。`passed` 的 40-hex 判準**一個位元都沒動**。放寬安全的依據是逐條查證下游：回 `True` 之後的三個消費點（`_discard_failed_planner_sandbox` 的 admission、`_dispatch_workflow_card` 的 `retryable_latest`、`resume_workflow_run` 的 `retry_failed` 分支）**都只把它當布林旗標**，沒有一個讀那個 candidate；唯一會把 terminal 的 `candidate` 寫進 run state 的 `terminalize_workflow_job()` 在讀它**之前**就已經對 `status != "passed"` 擲 `ValueError`，`_is_exact_legacy_agy_recovery()` 同樣要求 `passed`。 **(2) 診斷不落地**：`_terminal_parse_diagnostics()` 過去只保留 `reason`／`observed_head`／`validation_path`；`_canonicalize_card_terminal()` 的註解宣稱 `diagnostics`／`gate_evidence` 的語意「已經在 `_assert_terminal_gate_consistency` 消費完畢」——那對 `gate_evidence` 成立，對 `diagnostics` **不成立**：malformed／schema-retry 分支根本走不到那個函式。現在唯讀診斷新增 `model_diagnostics`，直接從**原始** envelope 讀（刻意不經那個投影——它正是把欄位丟掉的地方），內容**有界**（`TERMINAL_MODEL_DIAGNOSTICS_LIMIT = 2000`，沿用 #606 `RETRY_CONTEXT_EVIDENCE_LIMIT` 的同一個理由與量級：這段字**完全來自模型**，一個亂寫的模型可以把 attention 欄位與狀態檔一起撐爆），預算是**全體**的、被截的項目以 `…` 明示。診斷仍**不授予任何 authority**（`authority_granted` 恆為 `False`，R4／D6 不變）。 **明示停止的落地分支**：`resume_workflow_run` 新增 `card-terminal-explicit-stop`——模型明示停止時直接落 `needs_human`，`D=` 逐字帶模型病因，**不消耗** schema retry 額度（那份額度是給「模型寫壞 JSON」的，不是給「環境壞掉」的），也不自動回派（模型已經講清楚它要人，再派一次只是把同一句話再買一次）。此前這一組 job 要一路走到 `terminalize_workflow_job()` 才被 `workflow card terminal evidence did not pass` 擋下，operator 收到的是離病因兩層遠的 `terminalize-workflow-job-failed`，而且例外會往上擲。 **(3) 額度與重派語意（票上追加觀察的 (i)+(ii)）**：`registry._manager_reset_workflow_for_retry_card()` 重置時只 bump `attempts[phase]`，**沒清**同一個 dict 上的 `schema-mismatch:<card>` ⇒ 0819 實機逐字後果是「計數在 `-5`／`-6` 就累到 2 → operator 下 `retry-card` 重派 `-7` → `-7` 只產生一份不合契約的 terminal 就 `seen=2 >= MAX_SCHEMA_RETRIES` 判 exhausted，**這一輪一次自動重試都沒有**，attention 卻寫「已達上限（2/2）」」。現在 `retry-card` **清本輪額度**（operator 的顯式重派＝重新給一輪），值搬到新的 `schema-mismatch-total:<card>` 累加、**永不清零**；attention 文案改為「本輪 n/N，該卡累計 m 次」，兩個數字不再共用同一個 `(n/N)`。**本 PR 不新增任何熔斷**：`retry-card` 本身仍無次數上限（#555 仍 open），清那個鍵並未移除任何**既有的**熔斷，只移除了跨世代的意外殘留——該值除了 `resume_workflow_run` 的額度判定，全庫只有 `monitor.providers._schema_retry_rows()` 這個唯讀呈現面在讀。 **既有測試修改（逐條理由）**：`tests/test_workflow_production_wiring.py` 兩處——`test_malformed_workflow_card_terminal_detects_unterminalizable_card_results` 原本逐字斷言「非通過狀態 + `candidate` 為 null ⇒ malformed」，那正是本票要修的缺陷**本身**被釘成迴歸保護，改為釘 `needs_human`／`failed` × null／64-hex／40-hex 六格全部不得被判 schema mismatch，另補「型別根本不對（數字／物件／陣列）仍 fail closed」；`test_nonpassing_terminal_retry_authority_requires_exact_schema_and_binding` 的 `("candidate", "not-a-sha")` 反例改為 `("candidate", 7)`，並補上四種合法 candidate 形狀的正例。另新增 `tests/test_terminal_diagnostics_717.py`（10 測試，含逐字取自 `wf-6c37c77ca1-worktree-isolation-7` 的實機 envelope 當 fixture；未套用修正時 10 條有 9 條紅）。
- **#714：`ProcSubset=pid` 讓 codex 的 bubblewrap 起不來——builder 一個命令都跑不了，`card-terminal-schema-retry-exhausted` 只是症狀。** `#712`／PR #713 落地後 builder job 跑了 **30 分鐘**，19 行 job log 裡 **5 個 `command_execution` 全部 `status: failed`**，逐字都是 `bwrap: Can't read /proc/sys/kernel/overflowuid: No such file or directory`——模型於是合理地回 `needs_human`，Manager 端落成 terminal envelope 不符合契約。**病因離症狀四層遠。** **0819 逐條量測（`psc_run_under` 全量導出，D13；其餘 property 固定，每次只加一條）**：保留 codex 預設的 bubblewrap 形態要付**四條**放寬——(1) `ProcSubset=pid` ⇒ `Can't read /proc/sys/kernel/overflowuid`；(2) `ProcSubset=all` 之後 `RestrictNamespaces=yes` ⇒ `No permissions to create a new namespace`；(3) 再放寬之後 `RestrictAddressFamilies` 沒有 `AF_NETLINK` ⇒ `loopback: Failed to create NETLINK_ROUTE socket`；(4) 再加之後 `SystemCallFilter=@system-service` 擋掉 `mount` ⇒ `Failed to make / slave: Operation not permitted`；(5) 加 `@mount` 才 rc=0。**第 2、4 條放寬的正是 user namespace ＋ mount**——`RestrictNamespaces` 那一列的註解逐字寫著「user namespace 是 unprivileged 提權的常見起點」，而第 4 條的鍵還在 `PROFILE_LOCKED_KEYS` 上。0819 裁決因此由「A＝具名剖面放寬 `ProcSubset`」**更正為票上的 C**：A 的前提（「殘餘風險有界＝只放寬 `/proc/sys` 的可見度」「無等價非放寬解」）被量測否證。 **採用的形態**：codex 的 **landlock ＋ seccomp** 路徑（`--enable use_legacy_landlock`）。外層加固面**一條都不動**——`ProcSubset=pid`／`RestrictNamespaces=yes`／`RestrictAddressFamilies` 逐字還在；唯一的變動是 `SystemCallFilter=@system-service` **加上 `@sandbox`**，**全域**、八份 unit 一致。`@sandbox`＝`landlock_create_ruleset`／`landlock_add_rule`／`landlock_restrict_self`／`seccomp` 四支，能力上限是**讓呼叫者把自己關得更緊**，方向與放寬相反；而「全域一次可稽核的決定」正是 `PROFILE_LOCKED_KEYS` 那條理由要的形態，因此 `SystemCallFilter` **仍是鎖定鍵、沒有任何剖面分岔它**（`PROFILE_LOCKED_KEYS` 一個字都沒改）。 **落法**：新增與 `needs_node`（#643 ⇒ 具名剖面）／`filtered_syscalls`（#673 ⇒ 過濾語意）**正交的第三個維度** `ToolchainProgram.inner_sandbox`（`InnerSandboxSpec`：形態、argv、需要的 syscall 群組、`accepted_loss`、量測 note），需求由它**機械導出**並在 **import 當下強制**（`_validate_inner_sandbox_support()`：把 `@sandbox` 從 `_HARDENING` 拿掉 ⇒ 模組載不起來；`SystemCallFilter` 不在 `PROFILE_LOCKED_KEYS` 上 ⇒ 同樣載不起來）。argv 由 `launcher.build_codex_argv` 從**同一張表**取（lazy import `trust_root.permgen`，與 `planning_job`／`planning_probe_cache` 既有做法一致），不寫第二份字面量。判準是 **executor 維度不是 principal 維度**：需要內層沙箱的是 codex 這支程式，不是「builder 這個角色」——後者會把「planner 不跑命令」這個**行為假設**寫成加固決定，哪天 planner 真的跑了一條命令，症狀與本票逐字相同。未量過的 executor（`copilot`／`claude`／`agy`）那一格是空的，未知 executor `fail-closed`。 **誠實邊界做成機制，不是註解**：(a) `use_legacy_landlock` 是對 codex 0.147.0 的**觀察**，不是不變式——新增反向不變式探針 `trust_root inner-sandbox-probe`（四個方向：不帶旗標必須**仍然失敗**〔外層沒被偷偷放寬〕、旗標不得回 `Unknown feature flag`、帶了就通、且寫工作區外／對外連線**必須被擋**）。上游拿掉旗標時 codex 逐字回 `Error: Unknown feature flag: …` 並以非零收場，因此失敗是吵的、不是靜默退回「沒有內層沙箱但一切看似正常」（PR #713 的教訓，方向相反：那次是把某版 git 的行為寫成不變式）。**而上游已經在宣告要拿掉它**：0819 真實派工的 `--json` 串流裡逐字收到 `` `[features].use_legacy_landlock` is deprecated and will be removed soon. ``——**這是倒數，不是穩態**，旗標消失的那天只剩 bubblewrap ⇒ A／B／C 會整個回到桌上，因此探針第 2b 步把那句話印出來當早期警報。那句話以 `item.type=error` 進串流，**不影響** terminal 契約（`manager._extract_terminal_json()` 由尾端往回找 `agent_message`，開頭的 error 項會被跳過），另補一條測試釘住這件事——`#714` 的原症狀正是「Manager 端看到契約錯誤、病因在四層之下」，再多一個會混淆契約的雜訊等於替下一次同型誤診鋪路。探針的「內層真的在擋」那一段**每一條都配對照組**：0819 第一版探針拿「寫 job HOME 被擋」當證據，實跑才發現那是 `ProtectSystem=strict` 回的 `Read-only file system`——**它在證明外層**，內層裝沒裝上完全看不出來；改成「同一格路徑，沒有內層沙箱時 `OUTER_ALLOWS`、帶了就 `Permission denied`」。(b) landlock 形態**沒有 PID／mount namespace**（bwrap 有），這條寫進 `InnerSandboxSpec.accepted_loss` 與 runbook，並由測試斷言它存在——跨 UID 那一面由外層 `ProtectProc=invisible` ＋ `ProcSubset=pid` 覆蓋，mount 那一面由 `SystemCallFilter` 沒放行 `@mount` 覆蓋。 **缺陷 2：`last.json` 的落點沒跟著 #708 搬。** codex 的 `--output-last-message` 仍指著舊的 `<coordinator_root>/logs/workflow/last.json`（實機 `Permission denied (os error 13)`），**且是共用路徑**（不帶 job id）⇒ 即使有授權，並行的兩個 job 也會互相蓋掉。改由**該 job 自己那份 log 機械導出**（`job_workspace.job_last_message_path()`＝log 的兄弟檔）：降權模式落在 `<build-logs>/<job>/job.last.json`（`registry.JOB_LOG_SPOOLS` 導出的既有資產，掛在既有通道底下 ⇒ 模板 unit 的 `ReadWritePaths=` **逐字不變、零部署動作**），direct 模式落在 `<log_dir>/<slice>.last.json`（同目錄，但帶了 slice id）。**不再決定第二次落點**——#708 的破口逐字是「三個 principal 的 log 落點各自被決定」。argv 用的路徑與真正建出那一格的 `prepare_job_log_spool()` 回傳值**逐字比對**，漂移即 fail-closed。 **既有測試修改（逐條理由）**：`tests/test_trust_root_syscall_profile_673.py` 三處——(1) `_REJECTED_FILTER_TOKENS` 把 `@sandbox`／`landlock` 移出、把 `@mount`／`pivot_root` **移入**：#673 的結論「`@sandbox` 對症狀完全無效」在**它量的那個症狀**（V8 的 `pkey_alloc`）上至今成立，#714 量到的是另一個症狀（內層沙箱裝不上），兩個結論不衝突；而 `@mount` 是路線 A 第 4 道牆的解法，把它寫成拒絕清單等於把「我們沒有走 A」變成機器擋得住的事實。(2) `test_every_unit_keeps_the_untouched_system_service_filter` → `…keeps_one_and_the_same_…`：改為「與加固表相等 ＋ 八份逐字一致」，對準真正承重的性質（白名單只有一個值＝沒有剖面分岔它）；釘死字面量會讓「全域加一個群組」與「某份剖面偷偷多開一支」在測試上長得一樣。字面量本身改由逐 token 的拒絕／必要兩條清單守住（新增 `test_every_unit_allows_the_measured_inner_sandbox_syscalls`）。(3) `UnitReplicaTests` 與 `ProfileDerivationTests` 的字面量改為與 `_HARDENING` 比對，避免同一個值在三處各抄一份。`tests/test_manager_authored_job_accounting_604.py`：該檔留著一條「codex 的 `-o` 仍指向同一個目錄……另票處理」的註記，本票就是那一票——註記換成真正的回歸斷言。 另新增 `tests/test_inner_sandbox_714.py`（29 測試，1 條具名 skip：真的起一份模板 unit 並讓 codex 真的裝一次 landlock/seccomp 需要 root ＋ systemd ＋ 四個 job 帳號，單 UID 的 CI 上 `landlock_restrict_self` 不會被過濾、bwrap 也起得來，兩個方向都重現不了 ⇒ 跑了只會得到與 production 無關的綠燈，#638／#657 逐字記錄過這種假綠），與 runbook 第 4e-2g 步。
- **#712：per-job clone 跨 owner，git 的 dubious-ownership 擋死 builder——`gitconfig` 的 note 宣稱涵蓋但靜態檔裝不下動態路徑。** `#710` 的 ACL 補上之後 builder job **真的跑起來了**，然後死在 git 自己那一層（`fatal: detected dubious ownership in repository at '/var/lib/cortex/worktree/wf-…'` ＋ `fatal: Need a repository to create a bundle.`）。**檔案系統層是通的**（實機 `getfacl` 逐字 `user:cortex-builder:rwx`／`mask::rwx`）——擋住的是 **owner 判準，不是權限判準**；clone 的 owner 必然是 Manager（交出去要 `CAP_CHOWN`，#710 已論證）。`builder-gitconfig` 的 note 逐字宣稱「per-job clone 的 `safe.directory`」，而產生器實際只出**來源樹**兩條，per-job 路徑是動態的、靜態檔裝不下（萬用字元已被實測否決：`<repos>/*` 仍被拒，字面 `*` 是 opt-out 不是授權）。修法：**逐 job 由 Manager 算出那一格、隨 spec 的 env 下去**（`GIT_CONFIG_COUNT=1`／`GIT_CONFIG_KEY_0=safe.directory`／`GIT_CONFIG_VALUE_0=<這一格>`），**已實測 git 認**（0819／git 2.43.0：那是與 `git -c` 同級的 command scope，`--show-scope` 逐字 `command`；`git status`／`git bundle create` 皆 rc=0，同一份 env 對別的 repo 仍 rc=128）；值一律取**已解析（physical）路徑**且與 spec 的 `working_directory` 是同一個字串（shim `chdir` 之後 git 由 `getcwd()` 取路徑，恆為 physical path）——這是**支配性選擇，不是對 git 的斷言**：「git 拒不拒絕 symlink 路徑」隨版本而異（本機 2.43.0 拒、CI 上較新的 git 接受），本 PR 初版誤把它寫成硬斷言並因此紅過一輪。**三個降權 principal 由同一條規則導出**（`registry.JOB_GIT_WORKSPACE_TRUST`：builder／reviewer 要、gate 因為副本是自己 `copytree` 出來的所以**零動作**），且該表與 #710 的 `JOB_WORKSPACE_REACH` 對「誰建那一格」的宣告**必須一致**——兩條 import 期斷言強制，「只修一格」結構上做不到。⚠️ **只放行 `safe.directory` 一個鍵**：這條管道與 `git -c` 同級，實測 `alias.pwn=!echo …` 之下 `git pwn` **真的執行了外部命令**——那正是三份 `.gitconfig` root-owned 的理由；守衛寫端讀端共用同一支，`GIT_CONFIG_GLOBAL`／`GIT_CONFIG_PARAMETERS` 等五個「同一扇門的另外把手」一併進 `DENIED_ENV_NAMES`；放行值另由 `build_job_spec()` 綁死等於 spec 的 `working_directory`。一併更正兩則 `RunDependency` note 與兩份模板 unit 的陳舊宣稱（#696 的教訓，本票是第三個實例），新增反向不變式探針 `trust_root git-trust-probe`（基線走 D13 全量導出，**正向／反向走真實派工**——`psc_run_under` 複製的是加固面不是派工路徑，#709）與 runbook 第 4e-2f 步（#712）。
- **#710：per-job clone 建好之後沒有交給 job 帳號——builder job `cd` 不進自己的工作區。** per-job clone 是 **Manager** 用 `git clone` 建的 ⇒ `cortex-manager:cortex-manager 700`、**零具名 ACL**；模板 unit 的 `ReadWritePaths=<pool>/%i` 在 mount 層放行、**DAC 層擋死**。`#708` 修好 log 之後 `shim-error.json` 第一次交出這條逐字原因（`[Errno 13] Permission denied: '/var/lib/cortex/worktree/wf-…'`）。`cortex-reviewer-job@.service` 的註解宣稱「整個 clone 由本 job 帳號擁有」，而全 `coordinator/` **零個 `chown`**——而且 Manager 結構上做不到（`chown` 給另一個使用者要 `CAP_CHOWN`，Manager unit 帶 `CapabilityBoundingSet=`）：**不是漏寫一行，是方案與降權模型衝突**。採方案 A：owner 維持 Manager，job 拿**具名 ACL**（`setfacl -R -m u:<帳號>:rwX` ＋ default `rwx`，由目錄 owner 執行、不需要任何 capability，且 `gc`／`worktree_reclaim` 仍 `rmtree` 得掉整棵樹）。**三個降權 principal 的工作區可達性由同一條規則導出**（`registry.JOB_WORKSPACE_REACH`：builder 具名 ACL／reviewer 繼承 pool 根的 default ACL／gate pool 根 owner 就是自己），兩條 **import 期斷言**強制——缺一格 registry 載不起來、宣告與權限計畫對不上則 permgen 載不起來（先例：#698／#708）。⚠️ 授權**只下在 per-job 那一格**：pool 根的 default ACL 會讓每個 job 帳號進得去每個 job 的目錄（裁決 10-2 當場歸零），由 import 期斷言 ＋ 執行期 fail-closed ＋ 真 ACL 樹上的交叉實測三面釘住。⚠️ ACL **遞迴**套用且 `chmod` 一律排在 `setfacl` 之前，判準是 `getfacl` 的 `mask::`／`#effective:` 而非「ACL 行存在」（陷阱本身有一條實測釘住）。一併更正三份模板 unit 與登記表三則陳舊 note（#696 的教訓），把 `setfacl` 補進窮舉盤點（#666 雙向封閉），新增反向不變式探針 `trust_root workspace-probe`（零額外 env、D13 全量導出、工作區由**真實 provisioning** 產生不手工前置，#645／#709）與 runbook 第 4e-2e 步（#710）。
- **#708：builder／gate 的 job log 目錄沒有寫入授權——define 首次收斂後 builder job 立刻死在 shim 開 log 之前。** `job_shim._take_over_stdio()` 在**接管 stdio 之前**就 `os.open(log_path)`，而 builder 的 log 落在 Manager 的 dispatch log 目錄（`<coordinator_root>/logs/workflow/`，`0700 cortex-manager`、零具名 ACL）⇒ 實機 `[Errno 13] Permission denied`、`78/CONFIG`——**失敗發生在它能記錄失敗之前**。修法比照 #686 的 planner 那一格，但**三個 principal 由同一條規則導出**（`registry.JOB_LOG_SPOOLS` ＋ 兩條 import 期斷言：缺一格 registry 載不起來、掛的不是既有通道或沒有嚴格落在通道之內則 permgen 載不起來），「只修一格」在結構上做不到（先例：#698 的 `EXECUTOR_ENFORCEMENT_LEAVES`）。三格分別掛在 `commit-spool`／`review-verdict-spool`／`gate-ledger-spool` 底下 ⇒ `_minimize()` 全部吃掉 ⇒ 三份模板 unit 的 `ReadWritePaths=` **逐字不變、default ACL 自動繼承、零部署動作**。Manager 端的 harvest 路徑改以 **hard link** 指向同一個 inode，因此 `log_path` 字面量、exit sentinel、gate ledger、spool key 推導、`usage_extractors` 全部不變（刻意不是 symlink：shim 以 `O_NOFOLLOW` 開 log，且 symlink 由名字解析、job 換得掉指向；也刻意不把 log 目錄加進 RWP——那一層住著 gate ledger 與 exit sentinel，#604）。gate 的 log 一併移出 ledger 那一格並改由 Manager 預建（`0620`）：舊形態由 job 自己建、帶 `UMask=0077` ⇒ `0600 cortex-gate`，Manager 讀不到，失敗的逐字原因只存在於一個看不見的檔裡（#638 缺陷 2）。另新增 shim 失敗的機器可讀紀錄（`shim-error.json`，**可偽造、不進採信路徑**）與反向不變式探針 `trust_root job-log-probe`（零額外 env、走 `psc_run_under`／`unit_replica_properties()` 全量導出，D13）＋ runbook 第 4e-2d 步（#708）。
- **#698：`cortex-reviewer-planner` 可植入 codex hooks（R9 T3.9 實測攻破）已修——採 operator 裁決的方案 A：`$CODEX_HOME` 改成 root-owned ＋ **sticky bit** 的真目錄，job 以具名 access ACL 取得整棵寫入權，樹裡放一個 root-owned 的 `hooks.json`（unlink／rename 由 sticky 擋、改內容由檔案 mode 擋、mount 層另有巢狀 `ReadOnlyPaths=`）。permgen 的 §R2 安全網改為讓 sticky 通過（group／other 不可寫一行未放寬，setuid／setgid 改為明文清除），新增 `OwnerClass.STICKY_SHARED`。**兩個帳號的形狀由 `EXECUTOR_ENFORCEMENT_LEAVES` 一條規則導出並在 import 當下強制**，builder 一併遷移（它舊形態下 codex 在降權 unit 內根本起不來）。U-9 因此關閉、`reviewer-planner-codex-hooks` 從 deferred 升為登記表資產。實測：T3.9 兩個 subject 皆 `denied (OK)`，且 builder 的 `codex exec` 從 `Read-only file system` 翻成 `turn.completed`（#698）。
- questioner 的 prompt 動詞改對：舊句 `Return only the exact question-pack JSON **required to resolve** this completeness report` 是創作型動詞，而驗證要求逐位元等於輸入裡已附的 `default_question_pack`——指令要創作、驗證要謄寫，模型合理地把模板題目特化到 work item 並追加約 599 字，define 因此在 `question-pack-malformed` 上擲骰子。新句明說「這是謄寫不是創作」並逐一點名 `pack_id`／`question_id`／`kind`／`prompt`／`source_refs`，且**由判準機械產生**（`planning.question_pack_echo_hint()`，欄位名來自 `QUESTION_PACK_KEYS`／`QUESTION_FIELDS`、輸入鍵名來自 `QUESTIONER_INPUT_PACK_KEY`），比照 #520 的 `required_heading_hint()`，prompt 端不再持有第二份真實來源。同型掃描一併補上 secondary 與 integrator 的 `question_id`／secondary 的 `question_pack_id` 抄寫語意（`planning.echoed_identifier_hint()`）；`invoke_primary` 無自有 prompt 故不動。**驗收判準逐位元不變**（#704）。
- planning 的模型輸出驗證失敗改為**逐欄可診斷**：question-pack 的六種以上失敗不再塌縮成同一句話（改報「第一個差異的索引／欄位／expected／got」與列數），secondary evidence／primary integration／plan frontmatter／required headings 的同型塌縮一併掃過；三個 adapter 共用的 `_invoke_json` 於 rc≠0 時沿用 #674 的 `stdout_excerpt()` 保存模型輸出（只讀 stdout、不讀 stderr）；差異文字經產生端遮罩，模型無法偽裝成分類標記（#701）。
- R9 的 T1.3 斷言改為驗「operator CLI 做得到事」而非「binary 跑得起來」（兩個 subject 同時假失敗）；runbook 記錄 0818 兩 subject 實測結果並標明 T3.9 對 reviewer-planner 為已知追蹤項（#698）（#699）。
- runbook 的「M2′ 之後仍未涵蓋的」清單更正兩條陳舊項（gate 執行身分 #629 已完成、reviewer 憑證 refresh 已由 #685 解決），並加上「not-covered 清單本身也是宣稱」的約束（#696）。

### Fixed
- **#687（#672 票 F）：planner 的 define／brainstorm 正式離開 Manager 行程——切換、
  逐條宣稱更正，以及切換當下才撞得到的那一個阻斷**。四分部署的
  `PSC_JOB_RUNNER=systemd-template` 讓 `planning_runtime._select_planning_invoker()`
  恆回 `JobPlanningInvoker`（票 E／#686 land），planning 的每一次模型呼叫落成一個
  `cortex-reviewer-job@`／`-jit@` 實例、`User=cortex-reviewer-planner` 由 root-owned
  unit 決定。實機一輪 define 的證據：6 個模板 instance（`probe-1`／`probe-2`／
  `probe-3`(jit)／`questioner-5`／`secondary-6`／`integrator-7`），全程
  `systemd-cgls -u cortex-manager.service` **零 executor**（只有 daemon ＋ #604 的
  `systemctl start --wait` 記帳 shell 兩層）；probe 快取五格的指紋 `job_runner_mode`
  全為 `systemd-template`；`no-heterogeneous-planner` 消失
  （primary＝`claude`/anthropic、secondary＝`agy`/google）。
- **切換當天才浮出來的阻斷：`claude` 的 planning argv 結構性派不出 job**。
  `_planning_argv()` 對 `claude` 產出 `["claude", "-p", …, "--tools", "", …]`，而
  `job_runner.build_job_spec()` 與 `job_shim.load_spec()` 兩端都以 `all(argv)` 要求
  「每個元素都是非空字串」⇒ **每一次** define 都落
  `job-runner-job-spec-invalid`，且經 `question-pack-malformed` 被歸成 `content`。
  `--tools ""` 是 CLI 的成文 API（`claude --help` 逐字 `Use "" to disable all tools`）
  也是 #404 之後 planning「模型完全沒有工具」的唯一保證，不能改。判準因此收斂成
  **`argv` 非空且 `argv[0]` 非空**（`job_runner.malformed_job_command()`，兩端各呼叫
  一次同一支函式，比照 `forbidden_spec_keys()`）。
  **「等價寫法」`--tools=` 已實測否決**：在真實 reviewer unit 的完整加固面下
  （`psc_run_under`，38 條 property 全量導出）三臂對照，`--tools ""` 回 `NOTOOLS`、
  `--tools=` **讓模型發出 Bash 工具呼叫**、不帶旗標則三個 turn。`<tools...>` 是
  variadic，`--tools=` 不等於空清單——把它當等價寫法會讓吃 untrusted issue 內容的
  planner 在降權 job 內拿回 Bash，而症狀是「planning 跑起來了」。
- **這個阻斷為什麼躲過票 E 的驗收矩陣（可推廣的教訓）**：D13 的機械複本
  （`permgen.unit_replica_properties()`／`psc_run_under`）複製的是**加固面**，
  它證明得了「executor 在那個沙箱下跑得起來」，證明不了「**Manager 派得出那個
  job**」。票 E 的 3/3 全綠與 `job-specs/reviewer/` 是空目錄這兩件事同時為真。
  runbook 新增第 **5-6c** 步（planner／define 端到端）補上第二維，其 5 條檢查刻意
  走 daemon 自己的派工路徑而不是手工 spec。

### Changed
- **`permgen.deferred_run_dependencies()` 移除 `manager-claude-credential`**——這是
  票 D（#685）刻意留下、由本票收尾的一項。它從來不是「還沒補的憑證」，而是
  「Manager 在 direct 模式下自己 exec `claude`」的登記表投影；切換之後 Manager 不再
  exec 任何 executor ⇒ 本項**消失**，而不是被登記成一格資產（Manager 是 durable
  state owner ＋ spawn 授權持有者，passwd 註記逐字寫著 `no model code`）。
  測試同步翻成正向形態並**多守一條**：`manager_account not in CREDENTIALED_ACCOUNTS`
  ——只驗「清單少一項」的話，「刪掉逾期項」與「把它登記成資產」看起來一模一樣。
  逾期清單自 4 項（#666）→ 3 項（#685）→ **2 項**。

### Documentation
- **逐條更正「reviewer/planner 啟動面降權完成」這一族宣稱**（#672 明列，屬「不得順手
  宣稱」紀律）。更正的原則是**改成精確描述，不是把「未完成」改成「完成」**：
  - runbook 的 M1／M2 表拆成 **M2（reviewer，#615）** 與 **M2′（planner，#682-#687）**
    兩列；「M2 之後可以宣稱的」那三句（「三個 persona 全部離開 Manager 的 UID」／
    「injection 可達的進程皆無 spawn 授權**全稱**成立」／「D6 三分已生效」）逐句標明
    **在 M2 之後仍是假的**，並註記這正是本 repo「為了收尾而宣稱過頭」的樣本案例——
    下面那份「不得順手宣稱」清單看起來窮舉，卻漏掉最大的一項。
  - 5-8 殘餘風險表的 `~~M2 未完成~~` 那一列拆成 reviewer／planner 兩列，並**保留原
    那一列作為紀錄**：一句過寬的「已關閉」讓這個缺口在殘餘風險表上隱形了三天。
  - `job_runner.JOB_ROLE_REVIEW` 的 rationale：從「M2：在此之前它們仍在 Manager
    行程內」（對 planner 一直是**現在式**）改成兩張票、相隔三個月的分述。
  - `launcher._downgraded_mode()`／`_job_role()`／`_is_review_persona()`：三處都補上
    **範圍限定**。`_job_role()` 的「唯一決定點」與 `planning_runtime` 的「全庫唯一的
    執行後端選擇點」原本互相矛盾——兩者其實是兩條 code path 各一個選擇點，全庫唯一
    的是共用的 `resolve_runner_mode()`。`_is_review_persona()` 的 `read_only` 判準只
    涵蓋 **workflow lane 的 planner 卡**，涵蓋不到 define／brainstorm；這個 docstring
    是「planner 已經降權了」這個假直覺的來源之一。
  - `permgen.DOWNGRADED_JOB_PRINCIPALS` 與 `registry` 的同一段（成對）：補一句
    「本表是**產得出哪幾份 unit／spool**，不是**哪些執行路徑真的走上它**」——這兩件事
    在 #615～#686 之間分岔了三個月，而產生器面永遠不會發現：unit 產得出來，只是沒有
    人拿它起 job。
  - runbook 的帳號表、A/B 兩層論述、5-1 邊界表、5-5 的 `PSC_JOB_RUNNER` 說明、5-6b
    標題：逐處補上「哪一票讓這句成立」。
  - README 的 `PSC_JOB_RUNNER` 段落補上 `systemd-template` 的四條涵蓋路徑，並點名
    planning 是 #687 才接上的那一條。

### Fixed
- **#685（#672 票 D）：permgen 把 planner／reviewer 的憑證面 codify——`executor_credential_relpath`
  從**單一部署決定**擴成 **per-(account, executor) 表**（U-5 裁決）**。0818 的三份登入態是
  手工 `install` ＋ 手工 `ln -s` 落位的，**重跑 runbook 不會產生它們**；本票讓它可重現：
  兩軸（`permgen.CREDENTIALED_ACCOUNTS` × `EXECUTOR_CREDENTIALS`）展開成登記表資產，
  `asset_paths()`／`scaffold_directories()`／`IN_PLACE_CONTENT_WRITE_ASSETS`／unit 的
  `ReadWritePaths` 全部由它機械導出，加一格憑證不必改產生器。
  **U-4 追認**：`cortex-reviewer-planner` 同時持有 openai／google／anthropic 三份登入態為
  核可狀態，design 的安全退步 **R-3**（該帳號被攻陷時多邊 token 一起失，而 planner 正是吃
  untrusted issue 內容的角色）是**明文接受的有界殘餘風險**，在後續任何「planner 攻擊面」
  討論中不得被當成未知。
- **U-7 裁決落地：agy 的可寫狀態樹以 symlink 類資產進登記表（design 的選項 (a)）**——
  登記表新增 symlink kind（`permgen.SYMLINK_ASSETS`／`PermissionEntry.is_symlink`），命令
  形態是 `ln -sfn` ＋ `chown -h`，**刻意不出 `chmod`**：Linux 沒有 `lchmod`，而
  `chown`／`chmod` 對 symlink 一律跟著走 ⇒ 裸用會改到 `cache/gemini` **那棵樹**的 owner，
  而那棵樹歸 job 帳號正是本形狀的全部重點。守衛掛在**父目錄**（`[ ! -e <HOME> ] ||`）而
  不是自己身上——`ln` 是建立動作，「不存在就跳過」對它沒有意義，真正要跳過的是
  「本方案沒有這個帳號」。
- **驗收條件因 #686 的實測而改寫，這一條必須逐字讀。** issue 原文要求「reviewer 模板 unit
  的 RWP 逐字含憑證**檔案**」，前提是「codex 的登入態＝一個 `auth.json`」。#686 在完整
  reviewer unit 沙箱下實測推翻了那個前提：codex 需要 `$CODEX_HOME`（預設 `~/.codex`）
  **整個目錄**可寫（`state_5.sqlite`／`logs_2.sqlite`／`sessions/`／`skills/`／`plugins/`／
  `thread-writer-locks/`……檔名帶版本序號），唯讀時回
  `failed to initialize in-process app-server client: Read-only file system`，且**與 cwd
  無關**。照字面滿足原驗收會產出一個 **codex 仍然跑不起來**的部署。因此
  `cortex-reviewer-planner` 的三格改走新形狀 `CredentialShape.HOME_REDIRECT_TREE`：HOME 底下
  一條 **root-owned symlink** 導進該帳號既有的 `cache`（`~/.codex → cache/codex`、
  `~/.gemini → cache/gemini`、`~/.claude → cache/claude`），不變式換成**更強的那一條**——
  **模板 unit 的 `ReadWritePaths=` 逐字不變、零新增可寫面**（`cache` 早已在其中，
  `_minimize()` 吃掉子路徑），而 symlink 放在 root-owned 的 HOME 裡，job 換不掉指向。
- **claude 的憑證缺口一併關掉**——#686 的驗收矩陣裡 claude 是「CLI rc=0、卻回
  `Not logged in · Please run /login`」的那一列，而 **reviewer 的預設 executor 就是
  claude**：缺它時「reviewer 已降權」（#615 M2）買到的是一個跑不動的 job。新增登記表資產
  `reviewer-planner-claude-state`。job 模式下 `CLAUDE_CONFIG_DIR` 在
  `job_runner.DENIED_ENV_NAMES` 內（design D-g 的帳號隔離取代了 in-process 的一次性
  config dir），因此 claude 解到的就是 `$HOME/.claude`。
- **`deferred_run_dependencies()` 縮短一項，另兩項的理由整段換掉**——
  `reviewer-planner-executor-credential`（#640 寫「M2 落地時補第二列」、M2 早已落地 ⇒
  逾期未做）**已關閉**；#671 釘住這條逾期事實的測試按其設計意圖翻成正向形態
  （`test_the_reviewer_credential_gap_is_closed_without_widening_the_write_surface`）。
  `reviewer-planner-codex-hooks` **留著但升為 U-9**：它與 codex 的可用性在 `$CODEX_HOME`
  這一層**互斥**（要 hooks 就要有一個 job 換不掉的 root-owned 檔在一棵 job 必須整棵可寫的
  樹裡）——原本「補第二列即可」的理由已被推翻，不是「還沒補」。
  `manager-claude-credential` **留著**：U-5 解除了它的機械阻礙（表達得了了），但「要不要給
  Manager 一份模型憑證」的答案是**不要**（Manager 是 durable state owner ＋ spawn 授權
  持有者，passwd 註記逐字寫著 `no model code`），它由票 F（#687）切換後隨 direct 路徑
  一起消失，本票不預先刪掉一件還沒成立的事。
- **票 C（#684）的已知限制解除**——`planning_probe_cache._credential_path()` 跟著
  `executor_credential_of()` 的新簽章走（多一個 `executor` 參數），並改 `stat`
  **token 葉檔**而不是資產節點：`stat` 一條 symlink 只看得到目標目錄的 mtime，token
  就地覆寫時它不變 ⇒ 「憑證換了」偵測不到。agy 的 `~/.gemini` 與 claude 的 `~/.claude`
  因此**現在進得了指紋**（票 C PR body 明列的待補項）。未登記的 (account, executor)
  fail-closed，由 `compute_fingerprint` 的 `_safe` 收成穩定的
  `<unresolved:UnregisteredExecutorCredentialError>` 標記——**取不到答案本身也是一個會變的
  答案**（與 PATH 那一格同一條原則）。
- **`PSC_REVIEWER_HOME` 是本票的成對前置，不是別張票的事**——三份登入態的路徑**全部以
  `$HOME` 為根**，而模板模式下 shim 以 `os.execvpe` 整份換掉環境，unit 的
  `Environment=HOME=` 到不了模型（#686 實機更正）。`HOME` 沒宣告時它們在 job 內一條都解
  不到，而症狀（`$HOME is not defined`／`Not logged in`）與「憑證沒放好」長得一模一樣。
  新增 `permgen.JOB_HOME_ENV_BY_PRINCIPAL` 與 `PathLayout.job_home_value()`（與
  `job_runner.JOB_ROLE_CONFIG.home_env` 的成對契約由測試釘住，比照 #679 的 PATH），
  模板 unit 的憑證段直接印出 operator 要落進 EnvironmentFile 的那一行。
- **#686（#672 票 E）：`JobPlanningInvoker`——planning 的模型呼叫改走
  `cortex-reviewer-job@.service`，Manager 行程樹不再出現任何 executor**——`cortex-manager`
  的 passwd 註記逐字寫著 `no model code`，而 planning（define／brainstorm）的四個 adapter
  與**全部** probe 至今仍在 daemon 行程內以該身分 `subprocess.run` 模型 CLI（#672）。
  #615（M2）只把 reviewer 導上模板 unit，planner 走的是完全不同的一條 code path。本票補齊
  那半條：新增 `coordinator/planning_job.py` 的 `JobPlanningInvoker`（票 B 立的
  `PlanningInvoker` 介面的第二個實作），一次 planning 呼叫＝一個模板 unit 實例，身分由
  root-owned unit 的 `User=cortex-reviewer-planner` 決定、剖面由 `identity.executor` 單一
  決定、spec 結構性不得攜帶身分／剖面欄位。**不複製任何一份 `job_runner` 的邏輯**：
  preflight／instance 推導／spec 形狀／env 白名單／起動確認全部走既有函式。
  選擇點仍只有 `job_runner.resolve_runner_mode()` 一個；`systemd-run`（A 案）**fail-closed
  而不退回行程內執行**——A 案下加固面由呼叫端而非 root-owned unit 決定，而退回 in-process
  的失敗**看起來像成功**，那正是 #672 要消除的失效模式。
- **U-2 裁決＝planner scratch 對 job 唯讀，且它是登記表機械導出的性質、不是一個 `if`**——
  新增登記表資產 `planning-scratch-pool`（writers 只有 `Principal.MANAGER`），
  `required_write_targets()` 因此機械地不收它，它不出現在**任何** job 模板 unit 的
  `ReadWritePaths=`，`ProtectSystem=strict` 下模型連寫都寫不進去。design 標記的安全退步
  **R-1**（「模型弄髒自己的拋棄式 sandbox」的偵測在 job 側 Manager 做不到）因此從「失去
  行為訊號」升級成「結構上不可能」。executor 的可寫落點改指向 unit 的 `PrivateTmp=yes`
  私有 `/tmp`（per-invocation、job-owned、unit 結束即消失、Manager 看不到）。
- **planning 的輸出通道不新開寫入面**——新增登記表資產 `planning-job-log-spool`，路徑掛在
  既有 `review-verdict-spool` **底下**（`<spool>/planning-logs/<instance>/planning.log`）。
  那個帳號今天本來就對這棵樹有 `wx`，而 `read_write_paths()` 的 `_minimize()` 會吃掉被涵蓋
  的子路徑 ⇒ 模板 unit 的 `ReadWritePaths=` **逐字不變、零部署動作**、default ACL 自動
  繼承。design D3 第一句是「不新開通道」、U-3 更把新開 job→Manager 寫入面列為未決，本票
  因此不動用它。log 檔由 **Manager 預先建立且 mode 為 `0620`**：job 建的檔由 job 擁有
  （`UMask=0077`）Manager 讀不到（#638 缺陷 2），而用 `0600` 建檔會把繼承來的
  `user:<planner>:wx` 的 ACL mask 壓成 `#effective:---`（#638 缺陷 1 的同一個機制）。
- **失敗語意三分在 job 側落地**——`PlanningJobError` 攜帶票 A 的族名，
  `_probe_identity` 讓它**原樣**成為 `CapabilityProbe.reason`（票 A 的
  `_PROBE_REASON_FAMILIES` 已預留這條路），拒因表因此看得到「job 起不來」與「executor 死」
  的差別，而不是一律退化成型別名。`executor-silent-exit`（rc≠0 且輸出全空）的診斷指名
  `unit`／`hardening_profile`／`resolved_binary`／**`--version` 字串**／
  `permgen.seccomp_filter_is_fatal()` 的機械答案——最後一項正是 #673 整張票走偏的原因
  （當時沒有任何地方回答得了「該不該懷疑 seccomp」），版本字串則是 #681 那類「只比路徑會
  漏掉」的缺陷唯一看得見的地方。逾時走 D4 的 Manager 側 `wait(timeout)` →
  `systemctl stop` → **確認 unit 離開 active**（不確認的話下一輪會撞
  `job-runner-template-instance-busy`，而那個症狀與逾時完全無關）。
- **#682（#672 票 A）：planner 失敗的錯誤語意三分 ＋ `no-heterogeneous-planner` 攜帶逐候選
  拒因表**——修法前 `select_secondary_planner()` 失敗時只回一個沒有任何附加資訊的字面值
  `no-heterogeneous-planner`，而迴圈裡四個 `continue`（同 domain／probe 缺席／probe 沒
  ready／probe 身分不符）**全部靜默**，於是三類結構上完全不同的失敗（job／executor 起不來、
  executor 異常退出、輸出不合約）被壓成同一個「拓撲問題」。#670 就是這樣被誤診的：真因是
  `agy models` 兩欄漂移造成 100% `model-not-listed` ＋ code fence 造成 parse 失敗，blocking
  reason 說的卻是「沒有異質 planner」，最後靠人工重跑六遍才看出來。修法四件：(1) **三分的
  具名族**（`planning-job-start-failed`／`planning-executor-failed`／
  `planning-output-malformed` ＋ `executor-silent-exit` 子類 ＋ fail-closed 的
  `planning-probe-unclassified`）與 `classify_probe_failure()` 的單一明表，票 C／票 E 直接
  消費同一組常數；`probe_agy_capability` 的非零退出改帶 `_exit_diagnostic()`，rc≠0 且
  stdout／stderr 皆空時就地標記 `executor-silent-exit`（stderr 內容本身不入 diagnostic）。
  (2) **逐候選拒因表** `CandidateRejection` ＋ `SecondarySelection.rejections`，四個
  `continue` 各記一筆；`SecondarySelection.reason` **刻意維持原字面值**——它是下游既有的
  機器判準，拒因表走新欄位而不是把那個字串改長。(3) `run_heterogeneous_brainstorm` 經
  `render_secondary_rejection_reason()` 把表渲染進 blocking reason
  （`no-heterogeneous-planner grade=<environment|content> candidates=<N> (<逐條>)`，可用
  正規表示式釘住），**PR #674 的 probe stdout 節錄自此端到端活著抵達 blocking reason**，
  不再被 `continue` 吃掉；截斷只犧牲 diagnostic、身分列永不被丟，單條超限就地記帳 `…+Nc`、
  全表超預算改成 `<detail-elided:Nc>`——「哪一條被截掉、少了多少」永遠讀得出來。
  (4) `manager._classify_planning_failure()` 增第四條 environment 例外（比照 #416／#533／
  #554 的同一個模式）：拒因表含 environment 級拒因時整體改判 `environment`，
  `_resume_decision` 得以浮現 `recover-planning`；全部是拓撲／格式級時仍為 `content`
  （反向誤報同樣不可接受）。判準讀的是渲染端算好、**錨在字串開頭**的 `grade=` 欄位而非對
  整串 reason 做 substring-search——拒因表的 diagnostic 帶的是模型輸出，否則一個回
  「planning-executor-failed」的模型就能把 content 失敗偽裝成 environment。機密面：
  `CandidateRejection` 六個欄位沒有一個接得到 env、argv、檔案內容或 stderr，自由文字入口
  只有 probe 的 reason／diagnostic（模型對固定 probe prompt 的 stdout 節錄、例外**型別名**），
  渲染時另剝除 C0／C1 控制字元並單行化。測試 `tests/test_planning_failure_taxonomy_672.py`；
  **既有測試一行未改**。
- **#679：job 的 `PATH` 沒有一個來源是被決定過的——兩層都補、fail-closed，並禁止驗證
  指令自帶 `PATH`**。降權模式下 job 解到哪一份 CLI 取決於三件沒人裁決過的事：六份模板
  unit 沒有一份有 `Environment=PATH=`；`build_job_env()` 對 `PSC_*_PATH` **fail-open**；
  而 `PATH` 當時還在**轉發類**白名單上，因此未宣告時 job 靜默拿到 **Manager daemon 的**
  `PATH`（daemon 自己也沒有時，`os.execvpe` 退回 `os.defpath`＝`:/bin:/usr/bin`）。終點
  一樣：`claude`／`agy` rc=127，`codex` **靜默**解到 `/usr/bin/codex`（實機 0.42.0，
  toolchain 那份 0.147.0）——不報錯，只是產出來自沒人判讀過的 CLI；三個角色全中。
  修法四件：(1) 新增 `job_runner.resolve_job_path()`，三個 `PSC_*_PATH` 未宣告即
  `job-runner-path-undeclared` fail-closed（採裁決 (a) raise，不採「退回產生器預設」——
  #453「registry 永不寫入預設值」的同一條立場）；(2) **`PATH` 移出轉發白名單**（本票真正
  的 fail-open，issue 證據鏈少的那一環：daemon 的 `PATH` 帶著 `<deploy_root>/venv/bin`，
  與 `HOME`／`VIRTUAL_ENV` 同一類錯誤）；(3) 六份模板 unit 補 `Environment=PATH=`（permgen
  機械產生、與 Manager 端變數同源），`job_shim` 在 spec 缺 `PATH` 時退回這一層（root-owned
  ⇒ 不是 fail-open），兩層都缺才拒絕 exec 且失敗發生在**接管 log 之前**；(4) 驗證方法：
  共用探針 `psc_run_under` 移除 `--setenv=PATH=`、`psc_probe_path()` 刪除，新增
  `permgen.build_path_resolution_probe()`／CLI `trust_root path-probe` 的**角色 × executor
  全列舉**反向不變式（零額外 env、斷言解到 `<toolchain>/bin/<cli>` 且版本與同一支檔案的
  絕對路徑逐字相同）。**為什麼它活過五輪驗證**：每一條驗證都自帶 `--setenv=PATH=`——
  驗證環境供應了 production 不供應的東西；runbook 4e 逐字預言了症狀、連 0.42.0 都寫對了，
  但那條是 `sudo -u … env PATH=…` 跑的。這是「綠燈不承載語意」的第五個實例、新的一類
  （前四次是複本比 production 弱或強，這次是**多**），#677 的規矩因此再推一格：**複本必須
  連「production 沒有設什麼」也一起複製**。順手收掉 runbook 手打的 `PSC_GATE_PATH`
  少 toolchain 段這條第三份真相（與 permgen／#666 不一致）；三個變數一律由產生器導出。
  runbook 新增 **4e-2**（反向不變式）與 **5-5b**（升級既有部署：補三個變數、重新落檔六份
  unit、重啟 Manager，含降級路徑警語），troubleshooting 新增 `job-runner-path-undeclared`
  一節。測試 `tests/test_job_path_fail_closed_679.py`；OS 層語意以具名
  `@pytest.mark.skip` ＋ 完整理由標示，不靜默通過。

### Changed
- `docs/superpowers/runbooks/trust-root-phase2b-setup.md`：新增第 4e-3／4e-4／4e-5 步
  （唯讀 scratch 的 EROFS 驗證與逐 executor 實測表、跨 UID log 通道的 ACL mask 驗證、
  「Manager 行程樹無 executor」的取證程序）與第 5-5c 步（補宣告 `PSC_REVIEWER_HOME`／
  `PSC_GATE_HOME`）。加固面一律走既有共用探針 `psc_run_under`（其 property 清單由
  `permgen.unit_replica_properties()` 從**落檔的 unit** 全量導出），**未新增任何手寫的
  `--property=` 清單、未自帶 `--setenv=PATH=`**（design D13）。
- `job_runner.build_job_env()` 的 docstring 更正一句**事實錯誤**：原文稱「`HOME` 未給時
  systemd 依 passwd 填入正確值（而且模板 unit 另有一行 `Environment=HOME=`）」——那對
  模板模式不成立，`cortex-job-shim` 以 `os.execvpe(command[0], command, job_env)` 把環境
  **整份換掉**，unit 的 `Environment=HOME=` 到不了模型行程。實機 0818 複驗：未宣告
  `PSC_REVIEWER_HOME` 時降權 planning job 的 agy 死在
  `resolving log directory: getting home directory: $HOME is not defined`；補上之後同一條
  呼叫 rc=0。與 #679 的 PATH 缺口**同型**（builder 那一份有宣告，另外兩個角色沒有）。
  本票只更正事實並補 runbook 步驟，**不**順手把 `HOME` 也改成 fail-closed——那會讓所有
  角色的既有派工在 EnvironmentFile 補齊前當場失敗，屬於需要獨立票的改動。

- **#683（#672 票 B）：planning 的執行方式抽象成 `PlanningInvoker`——純重構、行為零改變**
  ——修法前 `planning_runtime._invoke_json()` 把三件事揉在同一支函式裡：**怎麼跑一個
  executor**（一次性 sandbox、`cwd`、hermetic env、逾時、雙向樹快照、drift 收容）、
  **跑之前準備什麼**（prompt 組裝）、**跑完之後怎麼解讀**（JSON 抽取與 CLI envelope 處理）。
  票 E（#686）要把第一件事換成降權 job、第二／第三件事逐字不變——沒有接縫時那次改動只能
  變成 `_invoke_json` 裡的 `if degraded:`，於是「design D2 的十條防線各自在哪個模式下生效」
  變成要靠讀 `if` 才知道，而四個 adapter 與 probe 共用同一支函式，等於每個呼叫端都要重新
  論證一次自己走的是哪條路（design D1）。本票只交付接縫：新增
  `PlanningInvocation`／`PlanningOutcome`／`PlanningInvoker` 三個型別；
  `InProcessPlanningInvoker` 收下「**怎麼跑**」的全部——D2 十條防線（D-a 一次性 tempdir、
  D-b sandbox 複本、D-c `cwd=sandbox`、D-d sandbox 弄髒即 fail-closed、D-e operator 樹雙向
  快照、D-f drift 唯讀收容、D-g claude hermetic `CLAUDE_CONFIG_DIR`、D-h `subprocess` 逾時、
  D-i `capture_output`、D-j codex `-o` 第二輸出候選）逐字搬入，一行邏輯未改；呼叫端只留
  prompt 組裝、rc 判定與 JSON 抽取，而 **JSON 抽取刻意留在共用層**
  （`_extract_json_candidates`），兩個 invoker 吃同一份 envelope 處理與 fail-closed 判準
  ——本 repo 已在 #401／#516／#520 買過三次「同一件事兩份真相」的單。**選擇點只有一個**：
  `_select_planning_invoker(env)` 唯一輸入是 `PSC_JOB_RUNNER`，與 launcher 共用
  `job_runner.resolve_runner_mode()`；**刻意不新增 `PSC_PLANNING_INVOKER` 之類的第二個
  開關**——第二個開關的失效模式是「以為降權了、其實沒有」，而那種失敗看起來是成功的。
  票 B 尚無第二個實作，三種模式目前全部回 in-process，這正是「行為零改變」的意思。
  四個 adapter 與 `_probe_identity` 全部改走 `invoker.run()`；`probe_agy_capability()`
  過去直接吃裸 `runner`、**繞過 `_invoke_json` 的全部防線**，現改吃
  `invoker.capability_probe_runner()`——它不是 prompt 呼叫而是兩步 CLI 協定
  （`agy models` ＋ smoke），協定的真相留在 `model_identities`（複製一份就是第二份真相），
  invoker 只交出執行接縫，**兩次 CLI 呼叫各算一次 invocation**；direct 模式下該接縫就是
  底層 runner 本身，行為逐字不變。`subprocess.run` 在 `planning_runtime.py` 內因此**只剩
  `InProcessPlanningInvoker` 一處**（issue #683 驗收第二條，由 AST 測試機械釘住）。
  **唯一刻意的行為差異**（design D1 明文要求）：daemon 路徑（不注入 `runner`／`invoker`）
  現在會在建構 planning runtime 時解析 `PSC_JOB_RUNNER`，非法值 fail-closed 成
  `JobRunnerError`；launcher 早已對同一個值 fail-closed，因此不會產生新的「本來能跑、
  現在不能跑」的部署，`manager` 端會把它記成 `planning-runtime-initialization-failed`
  （`environment` 級）。兩個精確度細節：`-o last.json` 的讀取條件與修法前逐字相同（只在
  rc==0 且 stdout 是字串時才讀），否則失敗路徑上的讀取錯誤會換掉例外型別，而例外**型別名**
  正是 `_probe_identity` 的 `safe-probe-failed` diagnostic 唯一內容、也是票 A
  `classify_probe_failure()` 的分類輸入；`invoker` 與 `runner` 兩個注入口互斥，fail-closed。
  測試 `tests/test_planning_invoker_672.py`；**既有 planning／probe 測試一行未改、全數綠燈**
  ——那就是「純重構」的定義，也是本票行為零改變的主要證據。

### Added
- **#684（#672 票 C）：planning capability probe 的跨 tick 結果快取，指紋含模板 unit 檔本身**
  ——`build_production_planning_runtime()` 對每個 planning-capable identity 各跑一次 probe
  （`_probe_identity` 每次兩次整棵 repo 的 `copytree`；`probe_agy_capability` 兩次 CLI
  呼叫），而它由 periodic tick（實機 `PSC_MANAGER_INTERVAL_SECONDS=600`）與
  `apply_work_action()` 兩條路徑呼叫；票 E 把 planning 搬上降權 job 之後**每一次 probe 就是
  一個 systemd unit 實例**，等於每 10 分鐘起一批 job 去問模型「你是誰」。新增
  `coordinator/planning_probe_cache.py`：指紋六格＝`PSC_JOB_RUNNER` 解析後模式、roster 解析
  結果的 canonical JSON 雜湊、以該角色 PATH 解析出的 executor 絕對路徑 ＋
  `st_dev/st_ino/st_size/st_mtime_ns`、憑證檔的 `st_size/st_mtime_ns`（**不讀內容**）、
  `resolve_hardening_profile(executor)`、以及**模板 unit 檔本身**的 `st_size/st_mtime_ns`。
  放 unit 檔而不是剖面名是因為 #677 的 `PROFILE_LOCKED_KEYS`——剖面名相同不代表 unit 內容
  相同，只認剖面名會沿用一個對新 unit 不成立的 `ready`；放 unit 的 stat 則把**部署動作**與
  **快取失效**綁成同一件事。含 `PSC_JOB_RUNNER` 是因為 direct 與 job 的執行環境完全不同，
  它同時是「票 F 的切換必須一次到位」的機械保證。**fail-closed**：壞檔／schema 不符／row
  形狀不合／身分欄位被改／同時帶 ready 與失敗診斷／指紋不符／TTL 過期／時鐘倒退一律視為
  **miss 重探**，沒有任何一條路徑會因為「上次是 ready」而在無法重探時回答 ready；壞檔另落
  可辨識的 `planning-probe-cache-unreadable`，與「probe 失敗」分開。與 `not_claimable`
  （#675）**刻意的一處不同**是壞檔不 raise（輔助紀錄不該取得它不該有的否決權），ledger 形狀
  與原子寫入則逐項沿用。TTL 兩段（ready 3600s／not-ready 300s，env 可覆寫，讀取時判定因此
  調短立即生效；非法值當 0 而不落回預設）。落點是新增的登記表資產 `planning-probe-cache`
  （`<coordinator_root>/planning-probe-cache.json`，Manager-owned 0600，writers／readers 只有
  `Principal.MANAGER`），**刻意不進任何 job 模板 unit 的 `ReadWritePaths`**——job 一旦寫得動
  快取，「這個 provider 是 ready 的」就變成模型可以自證的東西。指紋計算**永不 raise**，
  算不出來的分量落 `<unresolved:<例外型別名>>`（只帶型別名不帶訊息），而那本身也是一個會變
  的值。測試 `tests/test_planning_probe_cache_672.py`（40 條）；**既有測試一行未改**。
- **#672：planner 降權的設計交付（spec ＋ design ＋ 實作切分計畫，零 code）**——planning
  （define／brainstorm）從來沒有被降權：`planning_runtime.py:830` `_invoke_json()` 以
  `subprocess.run` 在**呼叫端行程內**執行、`:43` `_planning_argv()` 回傳裸 executor argv
  （無 `systemd-run`／無 shim／無模板 unit），而呼叫端是以 `User=cortex-manager` 執行的
  daemon（`manager_daemon.py:970`／`:1241`）。#615（M2）只把 **reviewer** 導上
  `cortex-reviewer-job@.service`（`launcher.py:1239 _is_review_persona()`），planner 走的是
  完全不同的一條 code path——`job_runner.py:405-408` 的 rationale 與
  `permgen.deferred_run_dependencies()` 第四項都已把這條記成逾期項，後者的 `disposition`
  給的兩條路之一正是本票的裁決（「planning 一律走降權 job」）。**這是結構性搬遷，因此本票
  只出設計，不動一行 `paulsha_cortex/`。** 設計定案 R1–R8：執行身分（單一開關、不新增第二個）、
  一次性 sandbox 的**十條防線逐條對應表**（operator 樹保護由「事後偵測」升級為 mount 層
  不可寫；claude hermetic config 升級為帳號隔離）、probe 快取（指紋含 `PSC_JOB_RUNNER` 與
  **模板 unit 檔本身**，fail-closed）、剖面零新增判定點、憑證由登記表機械導出（沿用 #671 的
  `IN_PLACE_CONTENT_WRITE_ASSETS` 與 `inapplicable_home_anchored_assets()`，不重造）、
  錯誤語意三分 ＋ **`no-heterogeneous-planner` 攜帶逐候選拒因表**（讓 #670 那種「格式問題被
  報成拓撲問題」結構上不可能；PR #674 已修好 probe 那一端，本設計補的是「診斷活著抵達上游」
  那一層）、逾時由 Manager 側 `systemctl stop` 強制終止。**剖面面沒有前置**——現行
  `EXECUTOR_HARDENING_PROFILE` 實測可用（八份 unit 全帶 `SystemCallErrorNumber=EPERM`，
  被過濾的 syscall 回 `EPERM` 而非 `KILL_PROCESS`，codex／copilot 在 `jit` 剖面 rc=0）。
  另以 **D13** 把加固面的驗證方法定為設計驗收的依據：一律從已落檔 unit **機械讀出全部
  property**，不得手抄子集——**該機制已由 PR #677 落地**（`unit_replica_properties()`／
  CLI `trust_root unit-replica`／runbook 共用探針 `psc_run_under`），本設計消費它、不重造。
  規則的理由仍記在設計裡（planner 是下一個會做這種宣稱的地方）：判準**雙向**，本 repo
  已有四個實例、兩個方向都出現過（#638／#657 偏寬得假綠，#673 原 body 與其 repro 偏嚴得
  假紅）。並消費 #677 的第二個維度（seccomp 過濾語意在剖面**之外**）——probe 快取的指紋
  因此含**模板 unit 檔本身**而非只有剖面名，`executor-silent-exit` 的診斷帶
  `seccomp_filter_is_fatal()` 的結果。誠實標註 4 條安全退步與 8 條未決裁決；實作切分成
  六張票，其中三張不依賴任何部署面改動、可獨立 land。**另查到一條沒有票的部署缺口**：
  `/opt/cortex/etc/cortex-manager.env` 未宣告 `PSC_REVIEWER_PATH` ⇒ 降權 job 拿到 PID 1 的
  預設 PATH ⇒ `claude`／`agy` rc=127、`codex` **安靜地**解到系統層 `0.42.0`（toolchain 為
  `0.147.0`，不會失敗、只是產出來自舊 CLI），**今天的 reviewer job 已受影響**。
- **#667：R3 testpilot case 素材盤點——四路盲測 sweep 合成為 102 筆去重候選清單**——
  `docs/superpowers/workstreams/r3-testpilot-case-corpus/{todo.md,case-candidates.md}`。
  **唯一產出是文件**：不寫 case yaml、不建 harness、不動 `paulsha_cortex/`（`#667` scope
  fence）。四路（症狀家族／子系統／生命週期階段／artifact 型別）互相盲測共 **155 個原始
  條目**，以「真實事件」為單位跨軸去重後 **102 筆**；`hit_by` 分佈為四路 **1**、三路 **9**、
  二路 **31**、單路 **61**。**單路命中全數保留**——artifact 路的 24 筆純實測發現沒有對應
  issue，掃 issue 的三路在結構上不可能命中。排序依「**可以最早開始長**」而非 `hit_by` 數量
  （分 T1–T6 六個 tier）：第 1 名是單路命中、零 harness 前置、oracle 為集合相等的 `#490`；
  唯一四路命中的 `#501` 排第 59，因為它需要 tick 推進與**可控的 review-launch 失敗**
  （issue 明言 happy path 會遮蔽該缺陷，只跑快樂路徑必然是一條永遠綠的假 case）。文件保留
  三個橫向發現：**oracle 品質分級**（差分／集合相等型無法靠放寬任一邊滿足，結構上擋得住
  fail-open；閾值型與存在性型容易被放寬成空過，每筆均標注型別）、**既有陷阱必須進
  `harness_needs` 並強制拆 tier**（多 UID／root／`direct` 模式／缺 `acl` 時**標
  `unsupported`，不得標 `pass`**；且「手抄 property 子集 ＝ 驗證無效」已有四個實例，
  `#638`／`#657`／`#673` 原 body 為假綠、`#673` 的 repro 為**假紅**）、**define 八環串聯
  攻關鏈應整組存在**不得拆為八個獨立 case。另含 **`evidence-insufficient` 32 筆**（四路原始
  41 筆去重，每筆保留「缺什麼證據才能判定」；3 筆與候選重疊者為四路真實判斷分歧，兩邊皆
  保留）與**四格覆蓋缺口**（08-12 波 6 張未深讀；**ship／delivery 零條語意候選是覆蓋度與
  風險落差最大的一格**；porcelain 分不出穩定與繞過；deck-combo 自動選型零事故）。
- **#673：seccomp 過濾**語意**是加固剖面之外的第二個維度，且它是**承重**的**——
  `@system-service` 確實過濾掉 V8 啟動時要用的 `pkey_alloc`（x86_64 330，systemd 歸在
  `@pkey`；**不是** `@sandbox` 的 `landlock_*`／`seccomp`，kernel audit `type=1326 …
  syscall=330` 為證，加 `@sandbox` 實測完全無效），但同一份 unit 上的
  `SystemCallErrorNumber=EPERM` 讓被過濾的 syscall 只回錯誤碼、不殺行程，V8 走 fallback
  ——以**完整 37 條** property 複製真 unit，四支 executor **全部 rc=0**，#673 回報的
  「預設派工路徑靜默壞掉」不成立。新增 `ToolchainProgram.filtered_syscalls`（實機量到、
  有 audit record 背書的被過濾 syscall；**刻意不共用 `needs_node`**——處置方向相反，
  且 `openspec` 跑在根本沒有剖面的 Manager unit 上）、`SECCOMP_FATALITY_KEY`／
  `PROFILE_LOCKED_KEYS`（`SystemCallFilter` 與 `SystemCallErrorNumber` 任何剖面都不得
  分岔）、`filtered_syscall_surfaces()`（程式 × 執行面，由 `TOOLCHAIN_PROGRAMS` 機械
  導出）與 import 時強制的 `_validate_seccomp_tolerance()`。**沒有放寬任何 syscall**：
  八份 unit 的 `SystemCallFilter=@system-service` 逐字不動（沿用 #643 的「量到才改」，
  而這次量到的結論是不用改）。
- **#673：`permgen.unit_replica_properties()` ／ CLI `trust_root unit-replica`——加固面
  複本改為從**已落檔的 unit** 全量機械導出**。#673 的 repro 是一份手抄的十 property
  複本，抄了 `SystemCallFilter=` 卻漏抄 `SystemCallErrorNumber=EPERM`，於是**比
  production 更嚴格**，量出一個不存在的 P0——#638（單 UID 讓 ACL 斷言真空）、#657、
  #673 是同一族的第一到第三次，而**假紅與假綠一樣會發生**。新函式的契約是「全帶，
  不選」：`[Service]` 段除執行面指令外全部帶出（含 `ReadWritePaths=`／
  `WorkingDirectory=`／per-account `Environment=`），落檔的 unit 少任一加固鍵即
  `UnitReplicaDriftError` 且 stdout 保持空。runbook 4e／5-2b／5-3／5-4 的手抄子集全部
  改走它，5-2b 的驗收由「正向四段」改為 **4 executor × 2 剖面 × 2 角色 unit 的全矩陣**
  ＋**反向對照**（`claude`／`agy` 在 jit 剖面下仍須 rc=0），並更正其措辭（原宣稱是在
  弱化環境下取得的）。
- **#666：外部相依的窮舉盤點——判準從「這一類東西有哪些」改成「跑完一個 run 需要碰到
  什麼」**——`#640`（executor toolchain ＋ job 憑證）、`#661`／`#664`（`srt`／`openspec`／
  preflight backend）、#666（`pytest` ＋ Manager 的 gh 憑證）是同一族的第一到第五個成員，
  每一次都是「症狀出現才補一項」，而症狀一次比一次遠（從 `rc=127`，到 doctor 一個看不出
  原因的 FAIL，到「ledger 空 ⇒ 每張 build 卡在採信階段被拒」）。前面每張表本身都完整，
  缺的是**沒有任何一處回答「一個 run 需要碰到的東西有哪些」**。新增
  `permgen.RUN_EXTERNAL_DEPENDENCIES`（25 項，逐項標明 kind／哪些 principal／run 的哪一段
  ／登記在哪），並**雙向封閉**：`uncovered_run_dependencies()`（盤點列到但表上查無）與
  `unlisted_roster_entries()`（表上有但盤點沒列到）皆有測試釘住必須為空。**後者才是真正
  買到的東西**——它讓「加一支相依」與「說明它在 run 的哪一段被誰碰到」變成同一件事，而不是
  兩件可以只做一半的事。落位計畫（`trust_root toolchain`）把盤點逐段印出來，runbook 第 4h
  步要求每次部署複核一次。
- **#666：盤點補上三支一直在用、卻從來沒被寫下來的系統層程式**——`bash`（**每一支 job 的
  `command[0]` 就是它**：wrapper 是 `bash -c <script>`，降權模式下 shim 的 `execvpe` 執行
  的第一支程式即為它；Manager 側的 exit 記帳 shell 亦然）、`python3`（gate 宣告與
  `review-sandbox` probe 都以 `PATH` 解析它 ⇒ **系統層那一支**；#661 曾以「它是部署 venv
  自己的 interpreter」為由排除，查證後該前提不成立）、`systemctl`（B 案定案後 Manager
  派工的第一個動作就是 exec 它，解不到就是「降權派工整條不可用」而非單一 job 失敗）。
- **#666：第四種外部相依——python 發行版**（`permgen.SYSTEM_PYTHON_DISTRIBUTIONS`／
  `DEPLOYMENT_PYTHON_DISTRIBUTIONS`）。`pytest`／`PyYAML`／`policy-check` 不是落在 `PATH`
  上的可執行檔，`command -v` 對它們無解；塞進 `SYSTEM_PROGRAMS` 會讓「名冊上每一項都解析
  得到執行檔」這條既有性質變成假的——與 #661「不得把 `srt` 併進 `EXECUTOR_TOOLS`」是同一條
  論證：**盤點完整性不可以用「往別張表塞東西」來換**。兩張新表各自標明落在哪一個
  interpreter、版本約束的唯一宣告來源、以及誰需要它；原本散落的
  `PREFLIGHT_BACKEND_DISTRIBUTION` 也收進同一張表。
- **#666：`permgen.GATE_COMMAND_DECLARATIONS` ／ `PathLayout.gate_command_env()`——產生器
  出建議的 gate 宣告值**（定位同 `job_path_value()`／`preflight_command_value()`：產生器
  出值、operator 落進 root-owned 的 EnvironmentFile，不是第二份執行期真相）。買到兩件既有
  形態買不到的事：(i) **gate 宣告的每一段都可以被機械對照到某張表**——`python3` 必須在
  `SYSTEM_PROGRAMS` 上、`-m <module>` 必須在 `SYSTEM_PYTHON_DISTRIBUTIONS` 上，而 #666 的
  漂移正是這條不成立卻沒人看得見；(ii) **覆蓋率**——本表必須是 doctor `gate-declarations`
  probe 由 packaged deck `test_policy` 導出那個集合的超集，否則照 runbook 裝出來的部署一
  開機 doctor 就是紅的。另有契約測試釘住 `GATE_COMMAND_ENV_PREFIX ==
  gate_ledger.GATE_ENV_PREFIX`（兩邊刻意不互相 import）。
- **#666：`permgen.deferred_run_dependencies()`——盤點撞到、尚無歸宿的四項變成可列舉**
  （比照 #661 的 `unresolved_node_execution_surfaces()`：**不做裁決、不放寬任何一面**，但
  也不讓它靜默消失）。四項的共同形態是「per-account 的機制已就緒，登記表只登記了其中一
  份」：(1) **reviewer／planner 的 executor 憑證**——#640 說「M2 落地時補第二列」，而 M2
  （#615）已經落地，因此這條現在是**逾期未做**；實測 reviewer 模板 unit 的
  `ReadWritePaths` 不含它 ⇒ `ProtectSystem=strict` 下讀得到、改不了 ⇒ token 過期那天
  refresh 靜默失敗（有測試把這個**事實**釘住，補上第二列時該測試會紅，那正是提醒去刪掉它
  與那筆 deferred）；(2) `cortex-gate` 沒有 `.gitconfig`（預防面，目前的 gate 宣告不碰
  git）；(3) reviewer／planner 的 `codex-hooks`（`asset_paths()` 把它寫死在 builder HOME
  下）；(4) **Manager 的 claude 登入態**——`planning_runtime` 是在 **Manager 行程內**直接
  exec `claude` 的（不是派降權 job），它讀 `<HOME>/.claude/.credentials.json`，而
  `executor_credential_relpath` 是**單一**部署決定、一個帳號只表達得了一份憑證。
- **#661：`permgen.node_execution_surfaces()`／`unresolved_node_execution_surfaces()`
  ——#643 剖面推導的盲區變成可列舉、不會靜默消失的東西**——#643 由
  `EXECUTOR_TOOLS.needs_node` 機械導出加固剖面，而那條推導唯一的輸入是 **executor 名**：
  它涵蓋不了「executor 在執行途中再 exec 出來的 node 程式」，也涵蓋不了 Manager 的 system
  unit。#661 的完整盤點正好撞出兩格——`srt` 由 `claude`（`strict` 剖面）exec、`openspec`
  由 Manager unit exec，兩者目前都是 `MemoryDenyWriteExecute=yes`，而 #643 已在實機量到
  V8 的 `Runtime_CompileLazy` 在該項下直接崩。**本 PR 只讓它可列舉，不做裁決也不放寬任何
  一面**：這是 OS／systemd 層語意，本 repo 的測試環境沒有那個加固面，對應的測試**明確
  skip 並寫明理由**（#638／#657 的教訓），實機量測步驟寫進 runbook 第 4e 步（`systemd-run`
  帶該 unit 的關鍵 property，附「量到才改、不得就地放寬」的處置規矩）。
- **#661：`paulsha_cortex.preflight_ci`——`PSC_PREFLIGHT_CMD` 的 typed-argv 進入點**
  ——把 cortex 的 preflight 契約（`--pr <N>` | `--metadata <路徑>`、`--skip-tests`）翻譯成
  治理引擎 `policy_check.preflight` 的契約。**不 import 引擎**，只以 typed argv spawn；
  引擎解析走 `--offline`＋已安裝發行版並由引擎驗 `installed == policy_version`，因此不需要
  網路、不需要 cache、也不會在降權部署裡造出「服務帳號寫得到又執行得到」的執行面。
- **#658：build 卡被採信之後即時回收其工作區——回收身分 ＝ 採信身分，preserve 契約拆成
  兩個具名模型**——#648 之後一個 run 會累積 N 棵約 35MB 的 per-job clone，而 #649／#653
  （ship 段自己的樹）與 #650／#659（verify／review 的 candidate 樹）落地後**被採信的
  build 卡的工作區已無任何下游消費端**。票上的核心難點是「誰以什麼身分回收」：三分部署下
  Manager 讀不進 builder 的 `0700` clone。**查證結論改變了選路**——抵達回收必須先走完
  `_verify_exact_candidate()`，而它已經以 Manager 身分對**同一棵樹**跑過 `git -C … rev-parse
  HEAD`，因此「Manager 進得去那棵樹」**本來就是「這張卡被採信」的必要條件**，回收不需要比
  採信更大的授權面 ⇒ **不新增任何身分、不新增任何授權**。票上四個候選逐一否掉：job 自刪
  違反 #540 且不知道自己有沒有被採信（會銷毀 #601 重派要用的殘留）；#629 的 `GATE` 只有
  `rX` 無 `w`，授 `w` 等於讓跑不受信任程式碼的帳號能改尚未 harvest 的交付樹；
  `ExecStopPost=` 的時機是 job 退出不是被採信，且 `+` 前綴＝root 執行，與「cortex 永不具
  root」相斥；依 `PSC_JOB_RUNNER` 分支是 #634 反模式（決定回收成不成立的是磁碟上的 owner，
  不是旗標）。改以**能力判定**：前置條件不成立就具名 skip、收不掉就 `failed`＋診斷，
  **兩者都不擋採信**。另修正票上一處前提：preserve 讀不到時是記 warning 後繼續（真正
  fail-closed 的是 `rmtree`）；且 #641 之後降權部署的 `_verify_exact_candidate()` 就已經先
  fail-closed（`blocked on #629`）⇒ **那裡今天不存在「被採信卻沒回收的工作區」**，#629 落地
  時回收身分必須與 candidate 驗證身分一起重新裁決。**契約改動**：`worktree_reclaim` 的
  「不銷毀證據」拆成 `EVIDENCE_PRESERVE`（預設，#478 語意逐字不變，未採信路徑一律走它）與
  `EVIDENCE_HARVESTED`（採信路徑；逐條盤點後未提交／未追蹤殘渣在採信面上地位為零——commit
  在來源樹、bundle 在 spool、ledger／sentinel 由 Manager 寫、log 在 Manager-owned 樹、
  outputs 已 hash 進 immutable evidence、canonical report 在 reviewer 的樹；把它複製進
  `evidence/` 等於把不受信任內容搬進受信任的樹，且只是把要回收的位元組換個目錄）；未知值
  一律 `raise`，`archive_workspace_head()` **兩種模型下都照跑**作為模型選錯時的安全網。
  新增 `manager._trusted_build_workspace_target()`（六條 fail-closed 前置條件，核心安全閘是
  「目錄名 ＝ `job_segment(job_id)`」這個 #645 單一推導點，結構性擋掉 #549 的
  `worktree == workspace_root` 地雷）與 `_reclaim_trusted_build_workspace()`（永不 raise，
  三種結果各一行結構化 log）。呼叫點刻意在狀態落盤**之後**——掛在 harvest 旁邊會留下
  「工作區已刪、卡仍 pending」的死路。重入：#601 的未採信殘留完全不動、retry-card 的 base
  走來源樹、abandon 的回收得到 `absent`（成功）、#613 的 branch 一個位元組沒碰且 `gc` 的
  「未 merge ⇒ keep」仍頂得住。新增 `tests/test_immediate_worktree_reclaim_658.py`
  （15 條 ＋ 1 條 skip，全部跑正式路徑；突變驗證停掉回收 6 條轉紅、拿掉兩道安全閘 3 條轉紅；
  跨帳號擁有權語意單一 pytest 進程結構性測不到 ⇒ 明確 skip，可測的那一半另有 `chmod 000`
  一條並在 docstring 講明它證得了什麼）。
- **#650：verify／review 卡的 candidate 樹搬出 builder 的 clone——解開擋住「卡被採信後
  即時回收」的最後一條引用**——`_dispatch_workflow_card()` 的 reviewer 分支以
  `builder_jobs[-1]["worktree"]` 為 candidate 樹，六個用途全掛在它身上；#648 之後一個 run
  會累積 N 棵這種樹（每棵約 35MB），而 `_harvest_build_candidate()` 落地後**被採信的卡的
  工作區已無任何獨佔資訊**（bundle 已封存、commit 已在來源樹），唯一還讓它回收不掉的就是
  這條引用。**六個用途逐個查證**後只有 `_create_reviewer_sandbox()` 是「只需要 candidate
  這個 commit」（來源樹就夠）；其餘五個都要一棵**真的 checkout 在 candidate 上、且 Manager
  可寫**的樹——`_authority_map_with_checkbox_tolerance()` 要比對 builder 勾過的 `tasks.md`
  實檔（#310），`_workflow_input_snapshot()` 會往樹裡 seed 缺席的 planning authority 檔，
  `_workflow_output_baseline()` 必須與 canonical report 的發佈根同源，`_tree_snapshot()`
  的逃逸偵測對象必須活過 sandbox 拆除，而 job 記錄的 `workflow_repo_root` 還是**卡與卡的
  交接載體**（`adversarial-review.requires` 就是 `code-review.produces` 發佈進去的那份未
  追蹤 report）。因此**票上的 A／B 皆不成立**（per-job clone 與 sandbox 都留不住上一張卡
  的 report），C 是把耦合固化成回收特例；改採 **A′：per-(run, candidate) 的 Manager-owned
  clone**，形狀完全沿用 #653 的 `_manager_ship_workspace()`——新增
  `manager._reviewer_candidate_workspace_id()`（唯一推導點，
  `wf-<run 摘要>-review-<candidate 前綴>`）、`_require_reviewer_candidate_workspace()`
  （branch／HEAD＝candidate／**追蹤檔**無漂移；未追蹤的 canonical report 刻意放行，那是
  交接載體不是殘留）與 `_reviewer_candidate_workspace()`（在來源樹上 clone，重用**不**打回
  pristine）。票上點名的順序問題（input snapshot 是 sandbox 的輸入、算它時 sandbox 還不
  存在）在 A′ 下**不存在**：借 #653 的「同一次派工內結構性共用同一個 provisioning」，
  candidate 樹在 reviewer 分支之前建好一次，五個用途拿到同一棵樹。順帶收掉一個 #641 同型
  缺口——舊模型下 Manager 對 builder 的 `0700` clone 做的不只是讀（seed 檔案、遞迴
  snapshot），降權部署下必然 `Permission denied`。`_is_exact_reviewer_terminal_recovery()`
  的 candidate 樹定錨改為唯一推導點，舊形狀保留為升級當下的容忍面。**即時回收拆後續票**：
  `worktree_reclaim` 的「不銷毀證據」需要讀進 builder 的 `0700` clone，三分下必然失敗，
  「誰以什麼身分回收、abandon／retry 的重入」自成一票（**#658**）。紅線全數遵守（沒有加回 job 工作樹
  的 ACL、沒有 `--reference`／`--shared`、回收通道一個位元組沒改）。新增
  `tests/test_reviewer_candidate_tree_650.py`（10 條 ＋ 1 條 skip，全部跑正式路徑；突變驗證 7 條轉紅，
  `chmod 000` 那條逐字重現 `PermissionError: [Errno 13] Permission denied`）。
- **#653 / trust-root Phase 2b：ship 段搬出 builder 的 clone——降權模式下 canonical lane
  終於跑得完整個 run**——#654 查證出的形狀：`openspec-archive`／`policy-commit`
  **不是降權派工的對象**（persona 是 `manager`，`_dispatch_workflow_card()` 對 ship phase
  一律回 `None`），它們由 Manager 自己在 `work_bridge` 內以 `cortex-manager` 身分同步
  執行；但**全程在 `_builder_binding()` 交回來的 builder 的 clone 裡動手**
  （`resolve(strict=True)`、canonical report 清理、`openspec archive` 的 `cwd`、
  `git diff/add/commit/rev-parse`、preflight、`ls-remote/push`、`_ship_action` 連測試），
  而 #641 已把 Manager 對 job 工作樹的讀取授權全部收掉 ⇒ 三分下**第一個 `git -C` 就
  `Permission denied`**。症狀是權限不是 `226/NAMESPACE`。**修法**：新增
  `work_bridge._manager_ship_workspace()`，以 `run.candidate_head` 為 base、用
  `seams.ScriptWorktreeCreator` 在來源樹上 provision 一棵 **Manager-owned 的完整 clone**
  ——來源樹是 `cortex-manager` 擁有且可寫（0817 裁決），commit／preflight／push 全在自己的
  樹裡發生，**不需要**任何指向 job 工作樹的 ACL（#644 的紅線：那條授權唯一的消費端本身
  就是提權路徑，不得復活）。`_builder_binding()` 改為**只回 delivery branch**，選 job 的
  採信鏈一個位元組沒改。工作區識別穩定於 **(run, candidate)**：同一個 candidate 的多次
  tick 重用同一棵樹（ship phase 會 tick 很多次，每次 clone 35MB 是白燒），candidate 前進
  則換一棵、前一棵原地留著——它正是 archive 卡 job 記錄上的 `worktree`，post-archive 的
  verify／review 卡仍以它為 candidate 樹，回收交給 `cortex work gc`。
  **`archive-applied-needs-commit` 重入路徑**（#653 明載）兩層處置：同一次 `validate()`
  內套用與 commit 結構上就在同一棵樹；跨 tick 則在重用前 `checkout -f`／`reset --hard`／
  `clean -ffdx` 打回 pristine 並以 `_require_pristine_ship_workspace()` 驗 branch／HEAD／
  乾淨——取票上「在新樹裡重跑 archive」那條，讓「崩在中間」與「從沒跑過」收斂成同一個
  狀態。`_remove_canonical_untracked_reports()` **移除**：它讀／刪的正是 builder 的 clone，
  而 pristine clone 讓「report 弄髒 exact candidate」在結構上不再可能，改由開工前的不變式
  承擔同一個保證；`manager._workflow_report_cleanup_allows_missing()` 保留為向後相容容忍面
  並補上說明。**回收通道一個位元組沒改**——archive commit 仍走 #654 的 bundle ＋
  append-only spool，consumer 仍是全 repo 唯一的 `harvest_branch()`；沒有 `--reference`／
  `--shared`、沒有 `git -C <來源樹> fetch <job 的 clone>`。新增
  `tests/test_ship_out_of_builder_clone_653.py`（7 條 ＋ 1 skip，全部跑正式路徑）：核心
  不變式是把 builder 的 clone `chmod 000` 後 ship 段仍跑完（#637 範本），另含工作區身分、
  重用＋pristine、重入路徑、archive→policy-commit 接續、`matches_candidate()` ancestry
  守衛、`direct` 零回歸；OS 層語意（`0700 cortex-builder` vs `cortex-manager`、pool 零
  `setfacl`）單 UID 測不出來，**明確 skip 並說明**，skip 前先斷言可測的那一半（#638 的
  教訓）。突變驗證：把工作區改回 builder 的樹 ⇒ 8 條紅 7 條，chmod 那條逐字紅在
  `PermissionError(13, 'Permission denied')`。runbook 的 `%i` 稽核段同步改寫（ship 段已可
  在降權模式下跑完，並附上 ship 樹的實機稽核指令）。**附帶發現留給後續票**：兩張 ship 卡的
  `runtime_capabilities`（#442）因為不 dispatch 在生產環境同樣無法生效；verify／review 卡
  的 `workflow_repo_root` 仍是 builder 的 clone（#650）。
  詳見 `changelog.d/ship-out-of-builder-clone.md`。
- **#629 / gate 執行身分：第四個帳號 `cortex-gate`（`UidScheme` 三分 → 四分）**——
  `#604`／PR `#628` 把 gate ledger 與 exit sentinel 的**作者**收斂到 Manager，但刻意
  沒做執行面：operator 宣告的 gate 命令（`PSC_GATE_CMD_*`）在 **builder 完全掌控內容的
  工作樹**上執行，`pytest` 會載入該樹的 `conftest.py` 與 plugin ⇒ **執行者取得任意程式碼
  執行**。後果是 `PSC_JOB_RUNNER=systemd-template` 下 build 卡恆因 `require_ledger` 而
  `gate-ledger-missing` fail closed——安全，但不能用。本次把缺的那一半補上：
  - **`permgen.FOUR_WAY_SCHEME` 成為定案方案**（`DEFAULT_SCHEME`）。既有三個帳號逐一
    皆不可承接 gate：`cortex-builder`＝模型自證（違反 `#540` 的 acceptance chain）、
    `cortex-manager`＝把任意程式碼執行引到持 spawn 授權、擁有全部 durable state 的
    帳號（`#629` 本文的整段論證）、`cortex-reviewer-planner`＝它是寫 verdict 的那一個
    （等於把 `#638`／`#639` 剛關掉的通道重新打開）。唯一認真的無新帳號候選
    `DynamicUser=yes` **結構性不成立**：本方案兩端都靠具名 POSIX ACL 授權，而
    `setfacl -m u:<名>:…` 在產生權限的當下就要解析得到那個帳號。
  - **`account_of` 支援 `ABSENT_ACCOUNT`**（語意與 `#626` 的部署決定型欄位逐字相同）：
    `three-way`／`two-way` 明示「本方案沒有 gate 角色」，因此不產生 gate 的帳號／unit／
    ACL／polkit 字幹，而**不是**讓整個產生器拒絕輸出。缺鍵（既非帳號也非明示不存在）
    仍然 fail-closed 並在訊息裡逐條列出「為什麼不能併到既有帳號」。
  - **`coordinator/gate_runner.py`（新模組）**：Manager 起 `cortex-gate-job@<id>.service`
    執行宣告的 gate，產出經 spool 回到 Manager 手上。它是**單一進入點**——自動路徑
    （`manager.terminalize_workflow_job`，排在 `_assert_terminal_gate_consistency` 正
    前方）與 operator 明著要求的 `regenerate-gates` 走同一支。
  - **登記表兩個新資產**：`gate-ledger-spool`（`<coordinator>/gate-ledger-spool/<job>/`，
    Tier-0，gate 只獲 **`wx` 無 `r`**，per-job 生命週期整條走 `#639` 的
    `coordinator/spool_slot.py`，不另寫一份）與 `gate-worktree-pool`
    （`<agents_root>/gate-worktree/`，gate-owned `0700`）。`repo-worktree` 的 readers
    補一個 `GATE`（**`rX`，沒有 `w`**）。
  - **`job_runner` 第三個 job 角色 `gate`**：沿用 `#615` 的 `JOB_ROLE_CONFIG` 表，沒有
    任何 `if role == …` 分支。`prepare_systemd_template()` 對 gate 角色**要求
    `executor=None`**（gate 不跑模型 CLI，剖面不得跟著 `PSC_MANAGER_EXECUTOR` 漂移），
    對模型角色仍**要求 `executor` 非 None**（`#643` 的既有紀律）。
  - **polkit 沿用 `#643`／`#615` 的單一交替 pattern 擴字幹**：`3 角色 × 2 剖面 ＝ 六個
    具名模板`，**不加第二條 `addRule`**，全檔仍只有一個 `return polkit.Result.YES`；
    pattern 的字幹來源改為「**本方案實際落檔**的角色」（`downgraded_job_principals()`），
    因此 `three-way`／`two-way` 產出的規則不會提一份那台機器上不存在的 unit。
- **#649 / trust-root Phase 2b：ship phase 的成果回收——`openspec-archive` 的 commit
  沒有進來源樹，`matches_candidate()` 的 ancestry 檢查因此是條死路**——票上的第一步是
  查證，而查證結果改變了範圍。**(1) ship 卡不是降權派工的對象**：兩張 ship 卡的
  persona 是 `manager`，而 `_dispatch_workflow_card()` 對 `current_phase == "ship"`
  一律回 `None`——它們不經 launcher、不 spawn job，由 Manager 自己在 `work_bridge` 內
  以 deterministic 身分執行。沒有 template unit、沒有 `ReadWritePaths=<pool>/%i`、
  沒有 `226/NAMESPACE`；#649 票上「#648 的症狀在 ship phase 原封不動」這句不成立。
  **(2) `matches_candidate()` 的 ancestry 檢查目前是壞的**：它只在 post-archive
  repair 走到，並在 `run.workspace_root` 上跑 `merge-base --is-ancestor`；而 archive
  commit 是 Manager 在**工作區**裡做的，#623 改成 per-job 完整 clone（各自的 object
  store）之後來源樹沒有它 ⇒ git 回 **128**（`Not a valid commit name`，不是 1）⇒
  該卡被濾掉 ⇒ ship audit fail-closed。既有測試綠著，是因為它的 fixture 把兩個 commit
  都直接做在來源樹裡（#623 之前共用 object store 的形狀）。**(3) #651 讓同一個缺口多
  長一個症狀**：build 卡改 per-job 之後 base ＝ `run.candidate_head`，post-archive 的
  `retry-build` 因此拿 archive commit 當 base 去 provision，而 creator 的第一道守衛是
  在來源樹 `rev-parse --verify <base>` ⇒ `git worktree base invalid`，重派連工作區都
  建不起來。**回收模型選 (a)**（票上兩條路）：archive commit **就是**下一輪的
  candidate（reset 之後 verify／review 對著它重跑、`_builder_binding()` 以它選工作區、
  PR 推的也是它），把它排除在鏈外等於重寫整條 post-archive 語意，不是縮小範圍。沿用
  #637 的 bundle ＋ append-only spool，但 **producer 換成 Manager 自己**——新增
  `job_workspace.publish_commit_bundle()`（in-process 版的 `build_bundle_command()`），
  consumer 那一半 `harvest_branch()` 一個位元組不變，「commit 進來源樹」全 repo 仍只有
  一個實作；刻意不寫 `git -C <來源樹> fetch <那棵工作區>`，那正是 `job_workspace` 模組
  docstring 已判定在三分下結構性不成立的形狀。順序是 `git commit` →
  `reserve_job_id()` → 建 spool → 產 bundle → harvest → **回收後 branch head 必須恰等
  於新 commit** → 才 `_record_manager_ship_job()` 與 `_manager_reset_workflow_after_archive()`；
  `candidate_head` 一旦推進，整條鏈就開始假設來源樹有那個 commit，回收失敗必須擋在推進
  之前。兩道附帶守衛：commit 必須在記錄的 branch 上（detached HEAD fail-closed）、
  bundle 的排除點依來源樹有沒有那個 commit 決定（沒有就帶完整歷史，不讓缺
  prerequisite 弄垮一次合法的回收）。**`matches_candidate()` 沒改**——修的是它的前提。
  **範圍切分**：票上第 3 點「ship 卡的工作區改 per-job」**不做**，因為查證推翻了它的
  動機（`%i` 不變式落不到 ship 卡上），並換上一個更大也更真的需求——ship 段全程在
  **builder 的 clone** 裡動手（`git commit`／preflight／push／`_ship_action` 都在那裡），
  而 #641 已把 Manager 對 job 工作樹的讀取授權全部收掉 ⇒ 降權模式下會在第一個
  `git -C` 就 `Permission denied`。修法是把 ship 段搬進 Manager-owned 的樹（此時
  per-job 目錄名才有意義），連帶處理 `_builder_binding()` 的來源與
  `archive-applied-needs-commit` 重入路徑，已開後續票 #653，且**必須**建立在本 PR 的回收
  通道之上。新增 `tests/test_ship_phase_harvest_649.py`（9 條，全部跑正式路徑：真 git
  repo、真 per-job clone、真 `ScriptWorktreeCreator`），含回收不變式、`rmtree` 後仍取
  得成果、fail-closed 且不先推進、ancestry 正反兩向、`openspec-archive` →
  `policy-commit` 接續、spool 定址、`direct` 零回歸；spool 封口對 producer 的強制力需要
  不同 UID ＋ per-account ACL，單 UID 環境**明確 skip 並說明**（#638 的教訓），skip
  之前先斷言可測的那一半。突變驗證：拿掉 harvest 之後 9 條轉紅 8 條。
  詳見 `changelog.d/ship-phase-harvest.md`。
- **#615 / trust-root Phase 2b M2：reviewer／planner 啟動面降權——三分的另外一半**——
  M1（#584／#603）之後三分只在**檔案權限層**成立：`cortex-reviewer-planner` 帳號、
  HOME、cache、verdict spool 的 `wx` 無 `r` ACL、gitconfig 全部到位，但
  `launcher.SubprocessLauncher._degraded_runner()` **只對 builder persona 回 True**，
  reviewer／planner 的模型 job 仍在 Manager 行程內以 `cortex-manager` 身分執行。
  A+B 裁決的核心論述「**injection 可達的進程皆無 spawn 授權**」因此只對 builder 成立
  ——而 reviewer 正是寫 verdict 的那一個。本次把缺的那一半補上：
  - **`launcher`**：`_downgraded_mode()` 移除「只有 builder 才降權」那條判斷。persona
    不再決定「降不降權」，只決定**降到哪個角色**（`_job_role()` → `builder`／`review`）。
    降權判定同時**排到 `review_only` 之前**（`launch()` 與 `executor_environment()`
    兩處逐字一致）：`_review_scope_env()` 是「從 daemon environ 篩」的模型，降權後 job
    根本不繼承 daemon 的 environ，繼續用它只會把 daemon 的 HOME／PATH／`VIRTUAL_ENV`
    硬塞進一個跑在別的 UID 上、根本進不去那些路徑的行程。
  - **`job_runner`**：新增 `JOB_ROLE_CONFIG` 一張表（角色 → 帳號／group／HOME／PATH／
    模板 unit 的 env 變數名 ＋ 預設值 ＋ 理由），`resolve_job_account()`／
    `resolve_job_group()`／`build_job_env()`／`prepare_systemd_template()`／
    `prepare_systemd_run()` 全部改為查表，**沒有任何 `if role == …` 分支**。未知角色
    **fail-closed**（落回 builder 是最糟的失敗模式：reviewer 以 builder 身分跑起來，
    而且看起來是成功的）。`resolve_builder_account()`／`build_builder_env()` 保留為
    builder 角色的具名別名，既有呼叫端零改動。
  - **`permgen`**：`DOWNGRADED_JOB_PRINCIPALS = (BUILDER, REVIEWER)`。unit 產生器
    **一行都沒有為 M2 改**——`build_job_unit(principal=REVIEWER)` 直接產出
    `cortex-reviewer-job@.service`（`User=cortex-reviewer-planner`），`User=`／HOME／
    cache／`ReadWritePaths=` 全部由 scheme 的帳號映射導出。**planner 不另開第三份**：
    三分方案把它與 reviewer 映到同一個 OS 帳號，同帳號 ⇒ 同 unit、同 RWP、同 HOME，
    多一份只會多一個要同步維護的名字與一個要放進 polkit pattern 的字幹，換不到任何
    隔離（`JOB_PRINCIPAL_PERSONAS` 把「那份 unit 服務誰」寫成機器可讀）。
  - **polkit 沿用 #643 的單一交替 pattern 擴充字幹段**，
    `^(?:cortex-job|cortex-job-jit|cortex-reviewer-job|cortex-reviewer-job-jit)@…$`
    ——**不加第二條 `addRule`**，全檔仍只有一個 `return polkit.Result.YES`（第二條規則
    會把 subject／action／verb／明細缺席四個檢查複製一份，變成兩個要同步維護的放行
    出口，那正是這份規則檔的可審查性性質要避免的）。字幹段是**兩層列舉**
    （principal × 加固剖面 ＝ 四個具名模板），前後仍錨定、instance 段字元類一字未改。
    四份模板的 `User=` 全部是無 sudo、無 root、彼此互不可寫的降權服務帳號，因此
    「多一個字幹」擴大的是**降權目標的選擇**，不是提權面。
  - **reviewer 的可寫面由登記表機械導出**，恰好兩條：
    `/var/lib/cortex-reviewer-planner/cache`（HOME 快取，明示 extra）與
    `/var/lib/cortex/coordinator/review-verdicts`（登記表資產 `review-verdict-spool`，
    `wx` 無 `r`）。**明確不含** builder 的 per-job clone／worktree pool／commit spool、
    來源樹（唯讀 ACL）、Manager 的 durable state（coordinator／control／gate ledger／
    job-spec spool／monitor state）與部署樹。
  新增 `tests/test_reviewer_planner_downgrade_615.py`（50 測試，其中 2 條在單 UID／
  無 root 環境**明確 skip 並附理由**——#638 的教訓：那些語意測了也永遠綠）。
  runbook 的「分段落地」M2 由 ⏳ 改為 ✅，並補上第 5-2（落**四份** unit）／5-5（reviewer
  的 env）／**5-6b**（reviewer 模板正向 smoke）／5-7（四字幹反向）／8a pass 2（由
  operator sudo 模擬升級為**真實 template instance**）／**8b-2**（verdict 通道端到端）
  的實機驗證步驟。詳見 `changelog.d/reviewer-planner-downgrade.md`。
- **`trust_root unit --review-job` CLI 旗標**：產生 reviewer＋planner 的模板 unit
  （可與 `--profile jit` 併用）。`polkit` 子命令的輸出自動涵蓋全部降權角色。
- **#648 / trust-root Phase 2b：canonical（workflow）lane 的工作區改為 per-job——
  per-run 工作區使 `%i` 不變式結構上不成立，該 lane 在降權模式下不可用**——
  canonical lane 的工作區是 per-run 的（build 卡 provision 之後，同 run 後續的卡沿用
  `builder_jobs[-1]["worktree"]`，**一個工作區對多個 `job_id`**），而模板 unit 是
  per-job 定址（`ReadWritePaths=<pool>/%i`）。一個工作區不可能同時等於多個 job id ⇒
  `PSC_JOB_RUNNER=systemd-template` 下必然拿到指向不存在路徑的 RWP ⇒ `226/NAMESPACE`。
  #645／#646 只把命名收斂成單一推導點，並在程式碼裡**明文把 canonical lane 排除在
  不變式外**；本次把那個排除拿掉。**改法**：build phase 的每一張卡自己 clone 一份，
  目錄名沿用唯一推導點 `job_workspace.job_segment(job_id)`（＝
  `job_runner.template_instance_id()` 的同一個輸出）。目錄名由 job_id 導出、而
  `create_job()` 的 input snapshot 又要從工作區算，順序只能是「先配 id → 建工作區 →
  建 job」⇒ 新增 `JobRegistry.reserve_job_id(task)`（**配發即消耗**，不是預測；與
  `create_job()` 共用同一個私有配發器，`f"{task}-{seq}"` 全 repo 仍只有一份）。
  **卡與卡的交接顯式化**：沿用 #637 的 bundle ＋ append-only spool——前一張卡的成果
  harvest 回來源樹的 `refs/heads/<branch>`（`_harvest_build_candidate()` 已強制它恰
  等於被採信的 candidate），下一張卡以 `run.candidate_head` 為 base 從**來源樹**
  clone，完全不讀前一張卡的工作區。**base 推導**：首張卡＝凍結集 base（#208／#211）、
  後續／中段卡＝最後一張被採信的 candidate；`retry-card`（#545）不動 `candidate_head`，
  因此中段卡重派拿到的仍是那個 candidate，而不是 run 的原始 base，也不是失敗那次留在
  磁碟上的東西。推不出合法 SHA 一律 **raise**——退回 creator 預設（`main`）等於
  `branch -f` 把整個 run 已採信的 commit 抹掉。**成本**：每張 build 卡一次 clone
  （0.5 秒／35MB），`feature-oneshot` 三張 build 卡 ⇒ 約 1.5 秒／105MB；**刻意不用
  `--reference`／`--shared`**——那會把 object store 接回共用，正是 #623 判定與三分隔離
  互斥的東西。**`gc`／`worktree_reclaim` 不必改**：兩者都以形狀與呼叫端給的路徑為準，
  沒有「一個 run 一個工作區」的假設；per-job 反而消滅了「回收一張卡把兄弟卡的樹一起
  刪掉」的隱患，而 #601 的 `worktree target already exists` 在這條 lane 上結構性消失
  （殘留回收仍屬 #601）。**`direct` 模式零回歸**：branch 名／來源樹 ref／標記檔／
  spool key 推導／`dispatch_head`（仍是 run 層級）全部逐字不變，且本次沒有引入任何依
  `PSC_JOB_RUNNER` 的分支。**範圍切分**：ship phase 的 manager 卡與 verify／review 卡
  仍沿用前一張 build 卡的工作區——前者要先補「ship phase 成果回收」才動得了，後者不是
  降權對象（`%i` 不變式不落在它們身上），各切成後續票。新增
  `tests/test_canonical_per_job_workspace_648.py`（10 條，全部跑正式 dispatch 路徑：
  真 creator、真 git repo、真 bundle ＋ spool 交接），含「把前一張卡的工作區刪掉、
  後續卡仍拿得到 base」的不變式、突變守衛、fail-closed、中段卡重派與 gc／reclaim；
  完整 `prepare_systemd_template()` 那一條需要 OS 層前置物，單 UID 環境**明確 skip 並
  逐項列出缺哪一個**（#638 的教訓）。**bootstrap**：`cortex work intake` 走的正是這條
  lane，本票讓它的 build phase 在降權下可用，cortex 自我託管的第一段因此打通。
  詳見 `changelog.d/canonical-per-job-workspace.md`。
- **#643 / trust-root Phase 2b：per-executor 加固剖面——`MemoryDenyWriteExecute` 與
  node 型 executor 的互斥，只讓需要的那一類付代價**——#640／#642 落地後做實機驗證時
  測出來的：**加固面本身與 toolchain 相衝**，不是安裝沒裝好。以 `cortex-builder` 身分
  在真實加固面下逐項隔離（`systemd-run` 一次只加一個 property）：無加固時 `node` 正常、
  `+MemoryDenyWriteExecute=yes` 時 V8 直接崩在 `v8::internal::Runtime_CompileLazy`，
  其餘每一項（`ProtectSystem=strict`／`PrivateTmp`／`RestrictNamespaces`／
  `SystemCallFilter=@system-service` ＋ `SystemCallErrorNumber=EPERM`）單獨加上去
  `node` 都正常——**唯一的阻斷點就是它**（V8 的 JIT 必須有 W+X 記憶體）。影響面是四個
  executor 掛掉兩個（`codex` node script、`copilot` shell → node 皆空輸出；`claude`／
  `agy` 原生 ELF 正常），而預設的 `PSC_MANAGER_EXECUTOR=codex` 正是掛掉那一個。
  operator 裁決走**方向 2（per-executor 剖面）**：
  - **兩份 job 模板 unit，共用同一張 `_HARDENING` 表**：`cortex-job@.service`
    （`strict`，完整 27 項，給原生 ELF）與 `cortex-job-jit@.service`（`jit`，給 node
    型）。兩份**不是**複製貼上的兩段加固——`permgen.HARDENING_PROFILES` 只帶
    `overrides`，`_hardening_lines()` 現場套用，日後往加固表加一項時兩份自動同時拿到。
    分岔面由 `permgen.PROFILE_DIVERGENCE_KEYS`（目前＝`{MemoryDenyWriteExecute}`）框住，
    覆寫不存在的鍵或白名單以外的鍵在 **import 時**即 `ValueError`。
  - **剖面由 executor 決定，且 job 選不到**（做不到就退化成「全域移除 MDWE」）。四道
    守法：對應表由既有的 `permgen.EXECUTOR_TOOLS` 的 `needs_node` **機械導出**（不另立
    第二張清單）；唯一輸入是 `executor`，而它是 Manager 的 dispatch 決定，
    `prepare_systemd_template()` 的 `executor` 參數**必填無預設**；job spec 結構性禁止
    攜帶剖面欄位（`hardening_profile`／`profile`／`template`／`template_unit`／
    `unit_suffix`／`MemoryDenyWriteExecute` 全進 `SPEC_FORBIDDEN_KEYS`，與「身分欄位
    不入 spec」同一條原則，寫端與讀端各掃一次且掃的是同一支 `forbidden_spec_keys()`）；
    `PSC_JOB_TEMPLATE_UNIT` 只接受**基底**模板名，帶剖面後綴的值一律拒。
  - **未知 executor fail-closed**：`resolve_hardening_profile()` 不回傳任何剖面——不是
    「不確定就給嚴格的」（那會讓未盤點的 node 型 CLI 靜默起不來，症狀是空輸出，
    #643 本身就是這樣被埋掉的），更不是「不確定就給寬鬆的」（那等於沒做）。
  - **polkit 維持一條規則、一個 YES 出口**：unit pattern 的字幹段改為列舉的交替
    （`^(?:cortex-job|cortex-job-jit)@[a-z0-9][a-z0-9._-]{0,62}\.service$`），由
    `HARDENING_PROFILES` 機械導出。放行面從「一個具名模板」變成「兩個具名模板」，
    **不是**「任意 unit」；5-7 的反向測試（transient 五形式、名稱前後綴混淆）對新字幹
    逐條同樣成立，另補圍繞 `-jit` 的十種混淆形式。
  新增 `tests/test_trust_root_hardening_profile_643.py`（40 測試，其中 2 條在無 root／
  無 systemd 的環境明確 skip 並附理由）。詳見
  `changelog.d/per-executor-hardening.md`。
- **`trust_root unit --job --profile strict|jit` CLI 旗標**：剖面只對 job 模板有意義，
  用在 `--manager`／`--monitor` 上直接拒絕（靜默忽略會產出與旗標不符的內容）。
  `trust_root toolchain` 的輸出也逐支列出該 executor 的剖面與對應 unit 名。
- **#640 / trust-root Phase 2b：真實 dispatch 的最後一哩——executor toolchain 與
  per-account 憑證進登記表**——#623 那一族的第五個缺口，且比前四個都靠後：前四個解完
  之後，dispatch 會一路走到**呼叫模型**那一步才失敗。job unit 帶 `ProtectHome=yes`，
  而四個 executor 原本全在 operator 的 HOME 底下，實測
  `sudo -u cortex-builder env HOME=<job HOME> codex exec --help` →
  `/usr/bin/env: ‘node’: No such file or directory`（rc=127）；登記表對 toolchain 與
  job 帳號憑證**都沒有預留資產**，permgen 因此也不會產生它們的權限。0817 裁決落地為
  兩個新資產：
  - `executor-toolchain`（`<deploy_root>/toolchain`，root-owned 0755，全部 job／服務
    帳號**唯讀＋可執行**）：`node` 走**系統層**（通用 runtime）；四個模型 CLI 落進
    部署樹，因為「job 跑的是哪個版本的模型 CLI」**會**影響產出——那必須是可稽核的部署
    決定，而不是跟著 operator 的環境漂移。實機盤點在同一台機器上就有兩份 `codex`
    （系統層 0.42.0 vs operator 實際在用的 0.147.0），因此安裝來源一律取 operator
    實際在用的那一份，不另外 `npm install -g`。四者形態不同（`codex` 是 node script、
    **唯一硬需要 node** 且要整包搬 npm 套件樹；`claude`／`agy` 自帶原生執行檔；
    `copilot` 是 shell script），固化為 `permgen.EXECUTOR_TOOLS`——**系統層 node 的版本
    風險因此只涵蓋 `codex` 一個**。`ProtectSystem=strict` 下 `/opt` 唯讀只擋寫入不擋
    執行，故本資產機械地不出現在任何 unit 的 `ReadWritePaths` 上。
  - `builder-executor-credential`（預設 `<job HOME>/.codex/auth.json`）：**檔案由 job
    帳號擁有**（0600，能自行 refresh 過期 token），**放它的目錄維持 root-owned**
    （0755）——job 因此改得了自己那份憑證的內容，卻**建不了新檔、刪不掉、也換不掉**
    同目錄下的 root-owned `codex-hooks`（增／刪／換要的是目錄的寫入權）。
    `ReadWritePaths` 只掛憑證**檔本身**而非父目錄（新的
    `permgen.IN_PLACE_CONTENT_WRITE_ASSETS` 例外），使「目錄 root-owned」在檔案系統與
    systemd mount 兩層同時成立。已知限制（裁決刻意接受）：以「暫存檔 ＋ rename」
    refresh 的 CLI 會失敗。落點由新的部署決定欄位
    `PathLayout.executor_credential_relpath` 導出，骨架的 root-owned 保護跟著它走；
    `scaffold_directories()` 已為每一個 job 帳號建出該父目錄，登記表只掛
    `cortex-builder` 一份（與 `codex-hooks` 逐條同構）。
- **`trust_root toolchain` CLI verb ＋ `permgen.build_toolchain_plan()`**：toolchain 的
  落位步驟由產生器出（逐支 CLI 的形態／搬移方式／來源判準／統一收權／
  `PSC_BUILDER_PATH` 正規值），runbook 不手寫；與 `shim`／`gitconfig` 同一個定位。
- **#623 / trust-root Phase 2b：per-job clone 的信任根層——`repo-source-tree`、三份
  root-owned `.gitconfig` 與 `commit-spool` 進登記表，內容由 permgen 產生**——M1 之後
  實機發現「這個部署做不了真實工作」：`ProtectHome=yes` 讓 `/home` 完全不可見，而登記表
  **沒有定義 repo 源碼樹該放哪**；實測進一步證明 `git worktree` 在三分下結構性不成立
  （worktree 的 `.git` 指向**共用 object store**，builder 只要 `git add` 就必須能寫它，
  「builder 能 commit」與「三分隔離」互斥）。裁決改為 **per-job 完整 clone**
  （0.5 秒／35MB per job）。本 PR 落地信任根層：
  - `repo-source-tree`（`<agents_root>/repos`，**working checkout**——monitor 要掃工作樹
    裡的檔案，bare 沒有工作樹；同一份 checkout 兼作掃描目標與 clone 來源）。**writer 是
    Manager**（0817 裁決，推翻本票初版的 root-owned）：`git fetch` 必須把 `FETCH_HEAD`
    寫進**目標 repo**，而成果回收正是「fetch 進來源樹」，實機在 root-owned 下實測
    `error: cannot open '.git/FETCH_HEAD': Permission denied`——「Manager 唯讀」與
    「Manager 回收成果」互斥，取後者。機械落點是 `owner_class=MANAGER_STATE`
    （`cortex-manager` 0700），兩個 job 帳號各獲**唯讀** ACL（`rX`），monitor 靠 unit
    的 persona 過濾（#622）仍寫不進去；隔離未變弱（不受信任的是 job 帳號，而 Manager
    本來就擁有 gate ledger／evidence／`jobs.json`）。
  - `builder-gitconfig`／`reviewer-planner-gitconfig`／`manager-gitconfig`（root-owned
    0644，落在各自帳號 HOME 下，比照既有的 `codex-hooks`）。內容由 permgen 產生
    （`build_account_gitconfig()` ＋ CLI `trust_root gitconfig [--builder|
    --reviewer-planner|--manager] --source-repo <slug>`），每個來源 repo **兩條**
    `safe.directory`（工作樹根 ＋ `<root>/.git`——實測從非 bare 來源 clone 時 git 檢查的
    是後者）。Manager 那份是實機複驗補上的 blocking 缺口：來源樹是 root 建立後才 chown
    過去的，owner 不相符的中途狀態會讓 Manager 的每一個 git 操作失敗。來源 repo 清單是
    部署決定（比照 #626），未宣告即 fail-closed（`safe.directory` 只認逐字相等的路徑或
    字面 `*`，實測 git 2.43 不吃 `<repos>/*`，而字面 `*` 等於整個關掉該保護）。
  - `commit-spool`（`<coordinator_root>/commit-spool/<job-id>/`）＋ path resolver
    `config.paths:commit_spool_root()`：成果回收改走 **bundle ＋ append-only spool**
    （0817 裁決）——builder 在自己的 clone `git bundle create` 寫進 spool，Manager 從那個
    **bundle 檔**（不是 repo）fetch，Manager 全程不碰 builder 的樹。形態逐條比照
    `review-verdict-spool`：容器 `cortex-manager` 0700，producer 僅獲 **`wx` 無 `r`** 的
    per-account ACL；**producer 只有 builder**（登記表裡唯一以 git commit 交付的 persona）。
    本 PR 只定義資產與權限，bundle 的產生／消費在 coordinator 側，屬後續變更。

  monitor 對 Manager 的**真子集**不變式（#622）仍成立。新增
  `tests/test_trust_root_repo_source_tree_623.py`（68 測試）；runbook 補第 2c 步、
  spec §R1 補兩段裁決。詳見 `changelog.d/repo-source-tree-assets.md`。
- **#622 / trust-root Phase 2b：`trust_root unit three-way --monitor`——monitor 的
  system-level unit，同帳號、同加固段，可寫面嚴格窄於 Manager**——M1 之後 permgen
  只產生 Manager unit，實機切換後 instance **完全沒有 monitor**：舊 `--user` unit 以
  操作者身分跑、指向舊 `~/.agents/monitor`，起回來只會雙寫，且寫不進 `0700
  cortex-manager` 的新樹；`monitor-event-spool` 因此只有 builder 的 `wx` 生產端、
  沒有消費端。新增 `permgen.build_monitor_unit()` 與 CLI 旗標 `--monitor`：`User=`
  取 `durable_state_owner`（UID 方案表即「`cortex-manager`＝Manager ＋ monitor」）、
  加固段與 fail-closed 的 `EnvironmentFile` 與 Manager unit 同源、`HOME`／
  `XDG_CACHE_HOME` 走 `layout.home_of()`／`cache_of()`。`ReadWritePaths` 多一層
  **persona 過濾**（`permgen.principal_needs_write()`，規則只有兩條且都直接讀登記表：
  persona 是 `writers` 之一，或是 `INTERPROCESS` 單向 spool 的 reader——消費＝unlink，
  需要容器寫入權），因此 monitor 只拿到 `/var/lib/cortex/monitor`、
  `/var/lib/cortex/run/cortex` 與服務帳號 HOME 快取三條，是 Manager 十一條的真子集；
  `principals=None` 維持既有行為，Manager 與 job 模板 unit 的輸出**逐位元不變**。
  `ExecStart` 形態比照 #618／PR #619 用 `<venv>/bin/cortex monitor`（既有 CLI verb，
  不帶 `--once` 即長駐），不用 `python -m` 以免在部署樹裡開第二種進入點形態。
  新增 `tests/test_trust_root_monitor_unit_622.py`（40 測試，含 ExecStart 契約鎖與
  加固欄位對 Manager 的集合等式）；runbook 補第 4d 步。
  詳見 `changelog.d/monitor-system-unit.md`。
- **#584 / trust-root Phase 2b：0816 第三輪裁決 A+B 的程式碼側——三分 UID 定案 ＋
  `job_runner` 的 template-instance 模式**——**A**：`permgen.DEFAULT_SCHEME`
  改為 `THREE_WAY_SCHEME`（`cortex-manager`／`cortex-reviewer-planner`／
  `cortex-builder`），`trust_root` CLI 未指定 scheme 時一律出三分、`two-way` 需顯式
  打出（打錯字不會靜默退回較寬鬆的方案）；polkit 產生器預設方案同步改為 **B**
  （`PolkitPlan.TEMPLATE`，subject＝`cortex-manager`、verb ∈ {start, stop}、pattern＝
  `^cortex-job@…\.service$`，且**不放行任何 transient unit 形狀**）。**B**：
  `coordinator/job_runner.py` 新增第三模式 `PSC_JOB_RUNNER=systemd-template`——把
  per-job spec（command／worktree／白名單 env／log 路徑，**無任何身分欄位**）原子寫進
  Manager-owned spool `<coordinator_root>/job-specs/<instance>.json`（新登記表資產
  `job-spec-spool`，builder 唯讀），再
  `systemctl start --wait --no-ask-password cortex-job@<instance>.service`；`User=` 與
  `ExecStart=` 皆硬寫死在 root-owned 模板 unit 裡，Manager 帳號選不了 UID、也給不了
  命令列。模板的固定 `ExecStart=` 是新的 root-owned shim
  （`permgen.build_job_shim()` 產 stub，邏輯在 `coordinator/job_shim.py`：`O_NOFOLLOW`
  讀 spec → 白名單 schema 驗證 → 接管 log → chdir → `execvpe`）；新增 CLI
  `python3 -m paulsha_cortex.trust_root shim`。判活與 log 沿用既有機制（`--wait` 保住
  pid 判活、exit sentinel 不變、harvest 的 log 路徑逐字不變）。fail-fast 涵蓋模板／
  shim／spool 未安裝、同名 instance 已在跑、spec 寫入失敗，一律 `DiagnosticReason`
  fail-closed 且**不退回其他模式**。`direct` 與 `systemd-run` 逐字不變。
  **誠實邊界**：`PSC_JOB_RUNNER` 預設仍是 `direct`，template 模式生效需 Phase 2b 安裝。
  詳見 `changelog.d/ab-template-job-runner.md`。新增
  `tests/test_trust_root_job_template_ab.py`（80 測試）。

### Fixed
- **#669：claim 判定 `missing_issue` 不再建立 run；跳過改記在可查詢的 `not-claimable`
  ledger**——舊行為「先建 run 再宣告 blocked」讓自我託管首輪掃描在八秒內產出 24 個內容完全
  同型的 `needs_human` 殭屍 run（全部停在 `current_phase: claim`、`evidence_refs: []`、
  `next_actions: []`，永遠不會推進），把 `attention` 的信噪比壓成 1:24。根因是類別錯誤：
  `missing_issue` 對 `docs/superpowers/workstreams/*` 這類 work item 是**預期狀態而非異常**
  （`cost-governance-cluster/todo.md` 開頭逐字寫著「本 workstream 不對應單一 issue」），不該
  進入 durable 的 run 生命週期。`_claim_action` 現在回 `not_claimable`／`run: None`，
  **一次都不呼叫 workflow starter**；每一次跳過都在
  `<coordinator_root>/not-claimable.json`（`cortex-not-claimable/v1`）留一筆帶
  `first_observed_at`／`observations`／`next_step_hint` 的紀錄，由 `cortex status` 的新
  `not_claimable` 區塊與 `cortex digest` 計數呈現——「不建 run」不得等於「靜默略過」，否則
  只是把噪音換成盲區。work item 重新可 claim 時該筆自動清除。修正前留下的殭屍 run 不由系統
  自行清除（沿用 `#373` 守衛），而是以唯一可機械辨識的簽名認出後，回
  `reason: claim-blocked-stale-run` 並附完整的 `cortex work abandon … --expected-run-id …`
  指令交由 operator 執行。
- **#670：`probe_agy_capability()` 對「模型加了 code fence」偽失敗，並把格式問題誤報成
  `no-heterogeneous-planner`**——probe 問的是語言模型卻直接
  `json.loads(smoke_stdout.strip())`；票上實測 6 次有 1 次模型把**完全正確**的 JSON 包進
  ```` ```json ```` fence ⇒ `malformed-output` ⇒ probe not ready ⇒
  `select_secondary_planner()` 回 `no-heterogeneous-planner` ⇒ run 進 `needs_human`。約 17%
  的 define 階段憑空死掉，而 blocking_reason 指向**拓撲問題**，排查方向整個帶偏。新增可測
  的 `strip_code_fence()`（```` ``` ````／```` ```json ```` 兩種開頭、有無尾隨 fence、CRLF、
  前後空白、單行 fence），刻意只處理「整串剛好是單一 fenced block」——與
  `planning_runtime._find_json_object` 頂層語意一致，**不會把「內容真的不對」順手救成
  ready**（內容不符仍是 `identity-mismatch`，有測試釘住）。prompt 同步補上顯式輸出契約
  在源頭壓低 fence 機率。
- **#670：probe 失敗時帶出實際 stdout 節錄**——`malformed-output` 過去 `diagnostic=None`，
  現場零線索（票上成因是人工重跑六遍才看見）。新增 `stdout_excerpt()`（前 200 字元、空白
  壓成單一空格、空輸出標 `<empty>`），`malformed-output` 與 `identity-mismatch` 兩路都帶。
  節錄是模型對**寫死在本模組**的 probe prompt 的回應，argv 不帶憑證、env 不回顯。
- **#670 附帶：`agy models` 改成 `id\tDisplay Name` 兩欄輸出後，probe 100% 死在
  `model-not-listed`**——2026-08-18 實機驗證 fence 修復時撞到，比 fence 偽失敗更早更絕對
  （整行正規化成 `gemini-3-1-pro-high-gemini-3-1-pro-high`，字面與正規化雙雙落空，連 smoke
  階段都到不了）。`_resolve_agy_cli_token()` 改為比對可用整行或任一欄、但**回傳一律是 id
  欄**（`--model` 不吃顯示名）；單欄舊格式行為逐字不變。
- **#666：實機為啟用 monitor 手動補的兩項收斂回登記表——`pytest`（系統層 python 套件）與
  Manager 的 `gh` 憑證**。兩項在實機都已生效，但**產生器的計畫產不出它們**：重跑
  `permissions`／`toolchain` 不會有、換一台機器部署也不會有。

  **漂移項 1：`pytest` 裝在 operator 的 user site-packages，gate 讀不到。**
  `PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 是**相對名**，由 gate 的 `PSC_GATE_PATH`
  解析 ⇒ `/usr/bin/python3`——**系統層那一支**。gate unit 自己的 `ExecStart` 用的是
  `/opt/cortex/venv/bin/python3`，但那只涵蓋 ledger writer 本身，**operator 宣告的命令另外
  解析一次**。`ProtectHome=yes` 之後 `~/.local/lib/python3.12/site-packages` 不可達 ⇒ 每張
  build 卡的 gate ledger 為空 ⇒ 撞 #540 的 acceptance chain，而痕跡只有 `manager.log` 的
  一行。修法：進 `SYSTEM_PYTHON_DISTRIBUTIONS`，連同 **`PyYAML`**——後者是**被測樹**的
  runtime 相依而不是 pytest 的（gate 的 cwd 是被驗那棵樹的副本，pytest 把 rootdir 插進
  `sys.path` ⇒ `import paulsha_cortex` 解到被驗的樹 ⇒ 它 `import yaml`；缺它的症狀是
  pytest exit code `2`，collection error，不是「測試失敗」）。**版本是明示的部署決定**：
  約束的唯一真相在 `pyproject.toml`（測試對著它比，改一邊沒改另一邊即紅），實機解出來的
  版本由 runbook 第 4f 步記錄並與 operator 側比對。

  **漂移項 2：Manager 沒有 gh 登入態，兩個 github provider `degraded`。** 新增登記表資產
  `manager-gh-credential`（`<manager HOME>/.config/gh/hosts.yml`，服務帳號 owned `0600`、
  列入 `IN_PLACE_CONTENT_WRITE_ASSETS`）與 `manager-gh-config`（`config.yml`，**root-owned
  `0644`**），兩層目錄（`.config`、`.config/gh`）由 `scaffold_directories()` 產出
  root-owned `0755`。**兩個檔的 owner 刻意不同**：`hosts.yml` 是 `gh` 唯一寫回 token 的檔
  （`auth login`／`refresh` 就地覆寫，不歸該帳號就 refresh 不回來）；`config.yml` 不承載
  憑證，但其 `aliases` 可宣告 `!` 開頭的 shell alias——讓服務帳號改得了它等於給 Manager
  一條「把任意命令掛進每一次 `gh` 呼叫」的執行面，與三份 `.gitconfig` 維持 root-owned 是
  **逐字相同**的理由。**與 #640 的 job 憑證形狀相同、洩漏面不同級**（spec／note／runbook
  三處都明寫不得混為一談）：#640 那一份是 **job 帳號**的模型 provider 憑證，job unit 另有
  `Environment=GH_TOKEN=`／`GITHUB_TOKEN=` 清空 GitHub token、成果走 `commit-spool` 由
  Manager 代理推送；本份是給 **durable state owner** 的，這個 token 推得動 PR、關得掉
  issue、改得了 label、merge 得了分支。job 帳號因此**刻意沒有** `~/.config/gh` 這一層目錄。
  落點之所以是 `~/.config/gh`，是因為產生出來的 unit 設 `HOME=` 與 `XDG_CACHE_HOME=` 而
  **刻意不設 `XDG_CONFIG_HOME=`**；日後補上它會讓憑證落點**無聲**搬走（症狀是「未登入」，
  而檔案還在原處），這一條寫進 spec 與 runbook。
- **#661 / trust-root Phase 2 收尾：實機四分部署 doctor 剩的兩個 FAIL，同一個成因
  （job／服務需要的外部程式不在登記表上）**——(1) `review-sandbox`：`srt` 被**單檔複製**
  而它是 npm 套件樹，`dist/cli.js` 的 ESM 相對 import 解到 `<toolchain>/bin/utils/…`
  ⇒ `ERR_MODULE_NOT_FOUND`、`srt --version` rc=1 ⇒ doctor 報
  `Claude sandbox dependency execution failed`；**第二個後果無聲**——
  `launcher._srt_runtime_root()` 解不到套件根，reviewer sandbox 政策少一條 `allowRead`
  且不報錯。修法與 `codex` 同形（整包搬套件樹＋`bin/srt` 為指進 `lib/` 的 symlink），
  以修正後的形狀在實機複驗 probe 的 static／live 兩路皆 pass。(2) `preflight`：舊值
  是 shell wrapper ＋ 另一個 repo 的 shell backend，兩層都在 `/home` 底下、
  `ProtectHome=yes` 之後都不可達；**票上「整包搬進部署樹」的前提在查證中過期**——該功能
  自 conventions 1.0.17 起已上游化為 typed-argv 模組 `policy_check.preflight`，因此落點是
  既有的 root-owned 部署 venv 而非 toolchain。順帶修正票上一處前提：舊值並非被
  `shell-wrapper-not-allowed` 擋下（那個類別只在 argv 第一段真的是 `bash`／`sh` 且帶 `-c`
  時成立），實機報的是 `is required`、填回舊值則是 `executable-unavailable`。
  詳見 `changelog.d/external-deps.md`。新增
  `tests/test_trust_root_external_deps_661.py`（37 測試 ＋ 1 具名 skip）。
- **#633 / trust-root Phase 2b：`ScriptWorktreeCreator` 的 repo 解析改 lazy——Manager 不再
  「因為少一個 env 變數」啟動即崩**——#612／#630 讓 `paths.repo_root()` 在未宣告
  `PSC_REPO_ROOT` 時 fail-closed，方向正確，但它命中的位置在**啟動路徑**上：
  `manager_daemon.run_loop → ensure_dispatcher() → Dispatcher(…, ScriptWorktreeCreator())`
  在建 dispatcher 當下就實體化本類，而舊實作在 `__init__` 解析 repo。於是 Phase 2b 的
  EnvironmentFile 少了 `PSC_REPO_ROOT`（#623 缺口 2）時，後果不是「派不了工」而是
  **Manager 啟動即崩**，`Restart=on-failure` 再把它推進 crash-loop（實機 `NRestarts` 連跳
  7 次）——一台什麼都做不了、也什麼都不告訴 operator 的機器。**修法只改時機、不改性質**：
  repo 與 worktree pool 改由 `_repo` / `_wt_root` 兩個 property 在第一次真正要用時解析並
  memoize（memoize 是刻意的——舊實作在建構子解析一次、其後凍結，全部既有呼叫端都建立在
  「同一個 creator 永遠對同一棵樹動手」之上）。因此沒有宣告目標 repo 的 Manager **起得來**、
  其餘職責照常（與 `PSC_DEGRADED_OPERATION` 的精神一致），而第一次 `create()` 仍
  `RepoRootUnresolvedError` **原樣**拋出、訊息逐字不變，只是出現在**派工當下**——那也正是
  operator 看得懂它的時刻。fail-closed 一個位元組沒放寬：沒有新增 cwd 退路、沒有吞例外，
  且拒絕發生在任何磁碟動作之前。唯一新增的「不拋」入口是 `anchored_at(root)`，它回 `False`
  （＝不能拿來對 `root` 派工）而不是一個猜出來的路徑；`manager._dispatch_workflow_card()`
  的 build 分支改問它，取代直接讀 `creator.repo_root` 再比對——lazy 化之後「repo 尚未解析且
  環境沒宣告」是 dispatcher 上一個合法的 creator 狀態，直接讀會讓例外從一句比較裡漏出去
  （語意不變：錨定的不是本 run 的 `workspace_root` 就換一個錨定正確的）。#646 的必填
  `job_id`、#656／#659 provision Manager-owned 樹的兩個呼叫端都顯式帶 `repo=`／`wt_root=`，
  完全不受影響。新增 `tests/test_lazy_repo_root_633.py`（9 條；突變驗證——把解析搬回建構子
  ——9 條中 8 條轉紅），`tests/test_repo_root_fail_closed_612.py` 的對應條目改為釘死
  `create()` 而非建構子。
- **#657 / trust-root Phase 2b：gate（與 reviewer／planner）的 template unit 讀不到自己的
  job spec——spec spool 改為 per-principal，preflight 改驗「那個身分讀得到」**——#629 落地
  後實機每個 gate job 都以 `78/CONFIG` 收場：三份模板 unit 共用同一個
  `Environment=PSC_JOB_SPEC_SPOOL=<coordinator>/job-specs`，而登記表只授 builder 唯讀 ACL；
  shim 是 systemd 套完 `User=` **之後**才執行的，它以 job 身分讀 spec ⇒ 必然 `EACCES`。
  reviewer／planner 同型（已查證，#652 未驗到這層）。裁決取 **per-principal spool**：
  `<coordinator_root>/job-specs/{builder,reviewer,gate}`，各自只授自己；容器降為 owner-only
  ＋ 機械導出的 `--x` traverse。「哪個身分讀哪個 spool」因此是 root-owned unit 上可逐字稽核
  的一行，而不是共用目錄上多條 ACL 的交集，也不引入「跨 persona 互讀 spec」這個新性質。
  三個資產／三條路徑／六份 unit 的 `Environment=` 全部由
  `registry.DOWNGRADED_JOB_PRINCIPALS` 機械導出。`prepare_systemd_template()` 的 preflight
  由「目錄存在」升級為「該 job 身分的 **effective** 權限」（`os.stat` ＋ POSIX ACL xattr，
  含 mask 與整條 traverse 鏈），spec 落地後再就地複驗一次；失敗因此發生在派工之前、
  訊息在 Manager 端。新增 `tests/test_per_principal_spec_spool_657.py`：**自建真實 ACL 樹**
  並以 effective 權限斷言（與 `getfacl` 交叉核對），需要第二個 UID 的部分明確 skip 並寫出
  理由。runbook 新增 §5-3a（含以各 job 身分實測讀得到的正反向步驟）。**需 operator 重跑
  權限計畫並重落六份 unit，順序：先權限、後 unit。** 詳見
  `changelog.d/per-principal-spec-spool.md`。
- **權威 gate ledger 一律由 Manager 自己重寫，spool 內容以不受信任輸入對待（#629）**——
  讓 gate 直接寫 `gate-ledger` 會被 `#628` 的 `foreign_evidence_author()` 當場以
  `gate-ledger-foreign-author` 拒掉；而那個資產**同時**是 exit sentinel 的落點，開放
  寫入面等於讓一個跑 untrusted code 的帳號能偽造**任何** job 的完成狀態。Manager 讀
  spool 時逐項驗形狀、gate 名稱必須落在 operator 的宣告集合內、payload 有界、`status`
  由 `exit_code` 重算（與採信端 `_ledger_outcomes()` 同一條紀律）。
- **產生的 job unit 檔頭不再固定寫 `--job`**：`JOB_UNIT_CLI_FLAG` 讓「重跑用哪一行」
  跟著角色走。原本 reviewer／gate 的 unit 檔頭都寫著 `unit <scheme> --job`，operator
  照抄會拿到 builder 的 unit **覆蓋掉**這一份。檔頭裡的 `Manager（cortex-svc）` 也改為
  由 scheme 導出（二分時代的字面量殘留）。
- **spec §R3 新增「誠實的取捨：gate 執行身分買到的是圍堵，不是不可偽造」**——第四帳號
  **不**使 gate 結果不可偽造：取得任意程式碼執行的 builder 依然能影響**自己那一份**
  ledger 的內容，那是「執行對方交出來的測試碼」這件事本身的性質，換誰執行都一樣。
  買到的是那個執行**碰不到**：Manager 的 durable state（含 exit sentinel）、builder 的
  工作樹（只有 `rX`）、verdict／commit 通道、他人在 gate spool 的那一格（`wx` 無 `r`）、
  以及任何 polkit grant。任何引用本 spec 的稽核或 PR 描述 MUST NOT 把它敘述成
  「gate ledger 不可被 builder 影響」。
- **spec §R9 新增族 5「gate 執行身分」**（T5.1–T5.10，含 negative control）；runbook
  對應第 8b-3 步（族 7）。runbook 另新增第 1 步的第四個帳號、5-2c／5-2d（兩份 gate
  模板 ＋ **真實加固面下**的驗證，含「拿掉 ACL 之後 build 卡必須 fail closed」的負向
  對照）、5-5 的 `PSC_GATE_*` 設定，並把全文 scheme token 由 `three-way` 改為
  `four-way`（附三分機器的升級路徑：只需建帳號、重跑權限 script、落兩份 unit ＋ 重跑
  polkit，第 3／4 步完全不動）。
- **#615：slice lane 的 foreign reviewer 差一點被以 `cortex-builder` 起跑**——實作 M2
  時發現的真缺口。foreign reviewer 走 `manager._spool_writable_launcher()` →
  `SubprocessLauncher.as_verdict_spool_writer()`，而那支工廠產出的 launcher
  `read_only` 與 `review_only` **都是 `False`**（`__init__` 明文拒絕「read-only 契約
  ＋ verdict spool 放行」的組合，因為 read-only 的 executor 連 `--add-dir` 都拿不到）。
  只看那兩個旗標的角色判定會把它判成 builder——**而它正是寫 verdict 的那一個**，那
  等於把 verdict 通道交還給 builder 帳號，抵銷 #638／#639 剛修好的東西。角色判定因此
  收斂為 `_is_review_persona()` 的三個判準，第三條是「**被授予了 verdict spool** 本身
  就是 reviewer 的標記」（而那個授予是 Manager 在 dispatch 當下做的決定，job 側碰不到）。
  同一個判準一併修掉「foreign reviewer 會拿到一格 commit spool 並在 wrapper 裡跑
  `git bundle create`」：它從不 commit，那一格在 `direct` 模式下永遠是空的（浪費），
  而降權之後 reviewer 帳號對 commit-spool **零寫入權**，那一段會逐 job 失敗。
- **#615：`permgen.RETIRED_JOB_WRITE_ASSETS`——已除役的 verdict 寫入面不再進 RWP**。
  `review-verdict`（reviewer worktree 內的 `.psc-review-verdict.json`）是 spec §3 認定
  的最短攻擊路徑，Phase 2a 已把權威通道整個換成 `review-verdict-spool`：
  `manager._review_verdict_source()` 對任何帶 `review_verdict_channel == "spool"` 標記
  的 job **只**認 spool 落點，而 Phase 2b 部署派出的每一個 reviewer job 都帶那個標記。
  在模板 unit 上放行它買到的是**零**（沒有消費者），付出的卻有兩項：語意上等於在 OS
  邊界重新打開一條已除役的 verdict 寫入面；可用性上它的路徑是 `<worktree pool>/%i`，
  而 reviewer 的工作樹**不在** pool 底下 ⇒ systemd 對不存在的 `ReadWritePaths=` 目標會
  讓每一個 reviewer job 直接起不來。登記表仍完整記錄該資產（過渡期 legacy fallback 還
  要讀它），除役的只是「Phase 2b 的 job unit 為它開寫入面」這件事——**嚴格更緊**。
  builder 的 RWP 逐字不變（它本來就不在該資產的 writer 面上）。
- **#645 / trust-root：模板 unit 的 `%i` 與 worktree 目錄名永遠對不上——降權派工從未經
  正式路徑成功啟動過任何 job**——`seams.ScriptWorktreeCreator.create()` 以 **branch
  slug** 命名工作區（`feature/<slice_id>` → `<pool>/feature-<slice_id>`），而模板 unit 的
  `ReadWritePaths=<pool>/%i` 期望的是 `job_runner.prepare_systemd_template()` 由 **job
  id** 算出的 instance 名；兩者永遠差一個 `feature-` 前綴 ⇒ `ReadWritePaths` 指向不存在
  的路徑 ⇒ systemd 建 mount namespace 直接失敗（`226/NAMESPACE`）。M1 的正向 smoke 用的
  是**手工組的 job spec**（自然挑了與 instance 名相符的路徑），把這個 bug 繞過去了——
  與 #584／#623 同一條方法論教訓：手工 spec 只能驗隔離，驗不了功能。
  **修法（operator 裁決）改目錄名這一側，不改 instance 名**：模板 unit 只有 `%i` 可用、
  推不出 branch slug，而「job 的工作區以 job id 定址」本來就與登記表既有的 per-job 模型
  （spool／sentinel／gate ledger）一致。**branch 名完全不變**，只有磁碟上的目錄名改。
  收斂為**單一推導點** `coordinator/job_workspace.py:job_segment(job_id)`——
  `job_runner.template_instance_id()` 改為委派給它（形狀逐字不變，既有部署的 spec 檔名／
  polkit pattern／unit 名皆不改變），`seams.create()` 加上**必填**的 `job_id` 關鍵字
  （留預設值等於留一條「忘了傳就退回舊命名」的復發路徑）。新增
  `tests/test_worktree_dir_naming_645.py` 直接比對兩個真實推導函式的輸出（不對常數斷言）、
  一條突變守衛與一條接線測試；完整 `prepare_systemd_template()` 那一條需要 OS 層前置物，
  單 UID 環境**明確 skip 並列出缺哪一項**（#638 的教訓）。
  **既有殘留**：`gc`／`worktree_reclaim` 以**形狀**（標記檔／`.git` 檔）判斷、與名字無關，
  舊目錄照樣回收得掉；真正會漏的 `recover-pre-candidate` 反推路徑（`manager` 與
  `work_actions` 各一份）收斂到 `worktree_reclaim.reclaim_recorded_or_derived()`，
  新舊兩種形狀都試，而形狀不明的目錄仍一律 fail-closed **不刪**。
  **已知邊界**：canonical（workflow）lane 的工作區是 per-run 的（一個工作區對多個
  job_id），「目錄名 ＝ 該 job 的 instance 名」在那裡結構上不可能成立；要在
  `PSC_JOB_RUNNER=systemd-template` 下跑那條 lane，須先把工作區改成 per-job。
  （**已由 #648 解掉**：build phase 的工作區已改 per-job，不變式在那裡成立且有測試
  守著；ship／verify／review 卡的工作區另有後續票。）
  **附帶**：`permgen.build_job_unit()` 把 `CollectMode=inactive-or-failed` 由 `[Service]`
  搬到 `[Unit]`——放錯段只被 systemd 忽略（`Unknown key name … ignoring.`），
  「失敗的 instance 自動回收」的用意因此沒生效。（#643 在 runbook 第 5-2 步補上落檔後的
  `systemd-analyze verify | grep -i "unknown key"` 檢查與 `reset-failed` 清理，讓舊部署
  的殘骸被看見；產生器側的修正已由本條完成，#643 不重複。）
- **#641 / trust-root：`repo-worktree` 仍授 Manager 唯讀 ACL——交換面已改 bundle，
  這條授權沒有消費者，卻讓 #637 的不變式在實機上不成立**——#637 把成果回收換成
  bundle ＋ append-only spool 並加了「Manager 全程不碰 builder 的 clone」不變式測試，
  但登記表 rationale 仍停在 worktree 時代（「交換面沿用 D2 git 讀」），permgen 因此
  照樣產出 `setfacl -m u:cortex-manager:rX /var/lib/cortex/worktree/<job-id>`；operator
  實機複驗：沒有 ACL 時 Manager `ls` 得到 Permission denied（不變式成立），套上登記表
  那條之後就讀得到（同一條不變式在實機上不成立）。**三條同型授權一起收**——
  `repo-worktree` 的 `rX`、`review-verdict` 與 `work-items-yaml` 的 `r`，逐條論證過
  各自的消費者都已移到 spool 或來源樹；必須一起收是因為 traverse ACL 由跨帳號 ACL
  機械導出（#620），留任何一條 job 樹的 `--x` 就會自己長回來。收掉它同時**移除一條
  提權路徑的成立條件**：那條 `rX` 唯一還在使用的消費端 `verification.py` 是以
  `cwd=<builder 完全掌控內容的樹>` 執行宣告的 check／test／full-suite，`pytest` 會載入
  該樹的 `conftest.py`／plugin ⇒ builder 在 `cortex-manager` 身分下取得任意程式碼執行
  （與 #629 同一條路徑）。那組讀工作樹的檢查改為**明確 fail-closed 並指向 #629**：
  權限造成的失敗改回專屬理由碼 `candidate-worktree-unreadable-pending-gate-identity`，
  evidence 帶 `blocked_on: "#629"` 與可操作處置——不靜默略過、不改讀 bundle（同源會讓
  檢查退化）、不採信 builder 自報工作樹乾淨（#540／#628）。reviewer 側同型殘留已一併
  確認並處理；`dispatch-worktree-pool` 容器層（`0701`）複驗零 `setfacl`。新增
  `tests/test_manager_worktree_acl_641.py`（31 測試，含需要 root 的 OS 層不變式——
  非 root 時**明確 skip 並附理由**，不靜默通過），四組突變驗證全部實跑；理由碼的
  判定字串以 git 2.43.0 對真實的跨 uid `0700` repo 實測取得。詳見
  `changelog.d/drop-manager-worktree-acl.md`。
- **#638 / trust-root：兩個 spool 的 producer／consumer 權限模型在三分下三處失效——
  verdict 通道（Phase 2a）實際從未成立過，`commit-spool` 繼承了同樣的缺陷**——三個
  獨立缺陷全部有 operator 的實機證據：(1) per-job 目錄以明確 mode 建立會**重設 ACL
  mask**，把 default ACL 繼承來的具名條目壓成 `#effective:---`，producer 連建檔都不行
  （實機 `fatal: Unable to create '…/commits.bundle.part.lock': Permission denied`）；
  (2) `wx` 無 `r` 的那一格上，producer 建的檔由 producer 擁有，**consumer 讀不到**；
  (3)「落地後轉唯讀」實作成 `chmod` producer 擁有的**檔案**，而只有 owner 或 root
  能 chmod，該處又刻意不 raise ⇒ **無聲失敗**，實測 reviewer 可以在 Manager 判讀之後
  回頭覆寫自己的 verdict。修法：per-job 目錄**不傳明確 mode**（初始權限交給 default
  ACL，事後只檢查並收窄 `other`；有 ACL 時 `group` 位就是 mask，刻意不動）；producer
  寫完後自己 `chmod 0644`（verdict 那邊由 wrapper script 在模型結束後補一段，排在
  exit sentinel 之前且不污染 `$?`）；seal 改封**目錄**（`0500`——consumer 是目錄的
  owner，收掉 `w` 讓那一格定版，`chmod` 同時把 mask 收成 `---`，producer 具名條目的
  traverse 一併失效）。兩個 spool 的 per-job 生命週期收斂到新的
  `coordinator/spool_slot.py`——這個 bug 之所以有兩個實例，正是因為兩邊各自實作。
  pre-seed 守衛語意與 operator 看到的錯誤字串一字未變。新增
  `tests/test_spool_permission_model_638.py`：**自己建出帶 default ACL 的容器**並直接
  斷言具名條目的 **effective 權限**（不是斷言 mode），同一組不變式參數化涵蓋
  `review-verdict-spool` 與 `commit-spool`，另含一條把「修法前的形狀在同一個 fixture
  下必須是紅的」釘住的突變驗證；跨 UID 的正反向功能驗收需要 root，拿不到時（以及檔案
  系統不支援 ACL 時）**明確 skip 並說明理由**，不靜默通過。本票是 M2（#615）的前置：
  verdict 通道目前只因 reviewer 仍以 Manager 帳號在行程內跑而看似正常，M2 一落地即
  整條斷。（Closes #638）
- **#626 / trust-root：permgen 為不存在的 principal 產生 `setfacl`，`sh -e` 下中止整份
  script 留下半套用的權限樹**——`permissions --commands --paths` 會印出
  `setfacl -m u:operator:rX …` 與 `setfacl -m u:cortex-outbox:rX …`，但這兩個是
  `registry.Principal` 的**抽象角色名**、不是真實帳號（`SCHEMES` 只把服務帳號那幾個
  principal 對應到真實帳號——**對應表缺項，不是填錯**）。實機 `setfacl` 回
  `Invalid argument near character 3`，而 runbook 第 2b 步是
  `sudo sh -e /tmp/p2b-permissions.sh`：第一條就**中止整份 script**，權限樹停在半套用
  狀態（前段已 chown/chmod、後段完全沒動），而且**看起來像裝好了**——錯誤訊息完全看不出
  是「帳號不存在」。修法：`UidScheme` 新增 `operator_account`／`external_reader_account`
  兩個**預設 `None`** 的欄位，由 `--operator-account`／`PSC_OPERATOR_ACCOUNT`（及
  external reader 的對應旗標／env，旗標優先）於產生當下注入；值 `none` 是**明示**本部署
  沒有這個角色的實體，該 principal 的授權整組略去。未指定時 `plan_to_commands()`
  **fail-closed**：raise `UnresolvedPrincipalError`、**一行都不輸出**（CLI stdout 全空、
  回傳碼 2，被重導的檔案是空檔而不是半套 script），訊息指出是哪個 principal、走哪個旗標／
  env、以及「先 `getent passwd`」。另加一道輸出後自我檢查
  `assert_output_accounts_known()`：每一行的 `u:<name>:` 與 `chown <owner>:<group>`
  都必須落在方案宣告的帳號集合內（註解行一併檢查），擋的是**未來新增 principal 時再犯**。
  `UidScheme.__post_init__` 拒絕把這幾個 principal 塞回 `account_of`（會被靜默忽略而形成
  第二份真相，正是本 issue 的成因），帳號名另做 `^[a-z_][a-z0-9_-]*$` 形狀驗證（名字會被
  逐字嵌進命令字串）。env 只在 CLI 這一層讀取，`permgen` 維持純函式。新增
  `tests/test_trust_root_principal_account_mapping_626.py`（46 測試，兩 scheme 參數化，
  含「輸出不得出現任何不在帳號集合內的字面值」這條擋復發的不變式）；runbook 第 2a 步補
  稽核 6／6b（每個 ACL 帳號與 scaffold owner 都必須 `getent passwd` 得到）並明說 script
  冪等、中止後直接重跑安全。詳見 `changelog.d/principal-account-mapping.md`。
- **#608 / ledger gate 環境健壯性（#565、#586 同族第三例）：AF_UNIX socket 路徑不再
  吃 `TMPDIR` 長度，長暫存根無法再偽造出一筆 gate 失敗**——Linux 的 `sun_path` 只有
  108 bytes（含結尾 NUL，可用 107），而 pytest `tmp_path` 與 `tempfile.mkdtemp()` 都
  掛在 `TMPDIR` 下，socket 路徑長度因此由環境決定。實測 `origin/main`：`len(TMPDIR)`
  ＝47 時全套 **4 failed**、66 時 **18 failed**、91 時全綠但 AF_UNIX 家族 **36 測
  靜默 skip**（#586 探針自己也建在 `TMPDIR` 下，先超限 → `bind()` 失敗 → 被誤判成
  「sandbox 禁止 bind」，覆蓋消失而套件是綠的）。Manager gate ledger 對 candidate
  重跑全套 pytest 是採信的硬 gate（#540），那些 `failed` 進 ledger 後與「交付真的
  沒過」無法區分，合格 candidate 會被 `GateContradictionError` 拒掉。主修法比照 #565
  的 `tests/git_fixtures.py`：新增 `tests/socket_fixtures.py`（短固定根 ＋ per-uid
  `0700` 容器 ＋ 短亂數名目錄，與 `TMPDIR` 無關）與 conftest 的 `socket_dir` fixture，
  五個測試檔的 bind／connect 路徑改用它，工作區與快照仍留在 `tmp_path`。次修法：新增
  `paulsha_cortex/monitor/socket_path.py` 收攏 107／108 常數與 byte 級判定，
  `MonitorServer.serve_forever()`／`MonitorSocketClient.request()` 超限時 fail closed
  在 `SocketPathTooLongError`（`ValueError` 子類，刻意不繼承 `OSError` 以免被
  「transport 出事」的處理吸收），另補 `MonitorServer.startup_error` 與 `cortex doctor`
  在 live probe 前的長度診斷。刻意不改用抽象命名空間——它沒有權限位、會打開現行
  `chmod 0o600` 的 socket，且仍吃同一條 108 bytes 上限。
  詳見 `changelog.d/afunix-sunpath-hermetic.md`。
- **#604 / trust-root：gate ledger 與 exit sentinel 的作者收斂到 Manager——降權後
  「被隔離的一方自寫驗收證據」的第一步修法**——登記表資產 `gate-ledger`（Manager 的
  dispatch log 目錄，同時放 `<slice>.gates.json` 與 `<slice>.exit`）宣告
  `writers=(MANAGER,)`，但兩個檔一直由 `launcher.build_wrapper_script` 的 wrapper
  **在 job 進程內**寫。Phase 2b M1 實機上線（`PSC_JOB_RUNNER=systemd-template`，job
  以 `uid=cortex-builder` 跑）之後這條路同時有兩個問題：**信任面**——sentinel 是
  `poll_headless_done` 的第一判準、ledger 是 `authorize_terminal` 採信 `passed` 的唯一
  背書，卻由被驗方自報（違反 #540 的「model 既不能自證成功、也不能自證失敗」）；
  **可行性**——該目錄是 `0700 cortex-manager` 且不在 builder 模板 unit 的
  `ReadWritePaths=` 內（`ProtectSystem=strict`），job 寫進去必然 EROFS，於是降權模式
  下每個 builder job 都在「行程已死、無 sentinel」的分支被記成 failed。修法：(1) 新增
  `job_runner.build_manager_exit_recorder_argv()`，把 `--wait` 的 client argv 包進一層
  跑在 **Manager 身分**的 `bash -c` 由它寫 `$?`（**sentinel 路徑逐字不變**，harvest 端
  零改動）；(2) `confirm_*_started()` 新增 `manager_authored_sentinel=` 旗標，判準改為
  「確認窗內 client 以非零收場」（未傳＝既有語意，零回歸）；(3) `build_wrapper_script()`
  新增 `write_sentinel=`，降權模式連同 gate 階段一併移出 job wrapper；(4) 新增
  `terminal_contract.foreign_evidence_author()`，`read_gate_ledger()` 對非 Manager 產生
  的 ledger 以新 reason `gate-ledger-foreign-author` fail closed、
  `dispatcher._read_exit_sentinel()` 對非 Manager 產生／非普通檔的 sentinel 視同尚未
  寫下。direct 模式下 job 與 Manager 同 uid，(4) 永遠不會命中，既有部署零影響。
  **本票不含 gate 執行面的搬遷**：把 gate 重跑直接放進 Manager 進程會讓 builder 掌控的
  `conftest.py`／pytest plugin 取得 `cortex-manager` 的任意程式碼執行，比要修的缺口更
  嚴重；正確落點是一個既非 builder 也非 Manager 的 gate 執行身分（需動 permgen 的 UID
  scheme 與 unit 產生器），在那之前降權模式不產生 ledger，build 卡照 `require_ledger`
  fail closed。
- **#620 / trust-root：permgen 機械導出父目錄 traverse ACL——三分下 builder／
  reviewer-planner 走不到自己的 spool，正向路徑全斷**——葉節點的跨帳號 ACL 完全正確
  （`u:cortex-builder:wx` 掛在 `<monitor>/event-spool` 上），但父目錄是
  `0700 cortex-manager`；POSIX 要求路徑上**每一層**都帶 `x`（search）位，於是 Phase 2b
  實機兩條 append-only 正向路徑同時 `Permission denied`，且錯誤訊息指的是**父目錄**、
  與真正缺的授權不在同一層。`permgen` 新增 `derive_traverse_grants()`：對每個授了跨帳號
  ACL 的資產沿路徑往上走到管理樹根，逐層以 `can_traverse()` 判斷該帳號是否已經走得過去
  （owner 位相符／others 帶 x／既有 ACL 已含 x），只為真正缺的那幾層產生一條 `--x`；
  中間層的目標狀態由登記表 `PermissionEntry` ＋ `scaffold_directories()` 兩個既有真相
  合出（`directory_facts()`），沒有第二份手寫清單。**`--x` 而非 `r-x`、且一律不設
  default ACL**：前者讓 job 帳號走得到自己那格卻列不出 `coordinator/` 底下還有哪些
  Manager 資產，後者避免一條 traverse 被子物件繼承成整棵子樹的授權。命令排在輸出**尾端**
  ——`chmod` 會重寫 ACL mask，順序反了會靜默失效。另新增 `account_can_reach()`／
  `unreachable_hops()` 讓「鏈是否完整」成為可測的純函式判定。新增
  `tests/test_trust_root_permgen_traverse_620.py`（26 測試，兩 scheme 參數化，含
  「拿掉導出授權即重現 issue 斷法」的反向對照）；runbook 補稽核 5 與正／負向驗證。
  詳見 `changelog.d/permgen-parent-traverse-acl.md`。
- **#621 / trust-root Phase 2b runbook 與 M1 實機對齊：十三處逐條修正（docs-only）**
  ——#584 M1（2026-08-17 實機）逐步對照後累積的落差，**沒有一條是安全破口**，
  但每一條都會讓下一個執行者卡住或誤判。**(A) 舊佈局殘留**：`--home-dir`
  `/var/lib/cortex-svc` → `/var/lib/cortex-manager`、刪掉已由 #616 涵蓋的四行手動
  `install -d`、稽核 3 的 `grep -c cortex-svc` 期望 2 → **0**、驗證路徑同步改名。
  **(B) job-spec spool 改版**：`/var/lib/cortex/jobs/<id>/run.sh` →
  `<coordinator_root>/job-specs/<instance>.json`，且一律用
  `job_runner.build_job_spec()` 產生；連帶修正「輸出在 spec 的 `log_path` 而非
  journal」「job env 完全等於 spec 的 `env`、不繼承 unit 的 `Environment=`」兩個
  會讓 smoke 直接看不到東西的誤導；刪掉不存在的 `/var/lib/cortex/jobs` 驗證。
  **(C) R9 三條假期望**：T1.1 由「讀 EnvironmentFile 期望 denied」改為**測寫入四式**
  （登記表 rationale 明寫「對全部 headless 唯讀」，可讀是設計）；T1.5 同理改測
  spool 的寫入四式，讀取期望改為依 subject 而異，並在 5-3 表格註明
  **per-job 讀隔離需 per-job UID、不在本方案範圍**；T2 delete 的 `rm -f` 改 `rm`
  並補 `need()` 前置守衛（`-f` 對不存在的檔回 0＝必然假陽性）。
  **(D) 族 4 假綠**：`pgrep` 在 `ProtectProc=invisible` 下必回空，測到的是
  「pid 不存在」而非「權限被拒」——改由 operator 以
  `systemctl show … -p MainPID --value` 取得後注入，並新增 `test -d /proc/<pid>`
  直接證明 pid 不可見；8c 的 negative control 同步改用 `MainPID`。
  **(E) 第 4a／第 6 步 pipx 遷移缺步**：`cp -a` 後 venv 仍有兩處指回 operator 樹
  （`bin/*` shebang、`site-packages/pipx_shared.pth`），補上重寫 shebang 與移除
  `.pth` 兩步（須在 `chmod a-w` 之前）＋總驗收，並說明這同時是**安全條件**。
  **(F) 執行前提兩個硬性 gate**：`acl` 套件（缺它時跨帳號授權整段無聲 no-op）、
  `/etc/sudoers.d/` 萬用 `ALL ALL=NOPASSWD: ALL`（三個服務帳號一建立就是無密碼
  root）。**另補（issue 未列，實機對照時發現）**：R9 攻擊腳本加**身分鎖**
  （腳本會真的 truncate／`rm`／`mv "$HOME/.codex"`，在沙箱外跑會弄壞 operator
  自己的環境，而那些「成功」會被誤讀成邊界失守）；**第 3-0／3a-2**——裁決
  「legacy-imported 不得滿足任何 ship gate」針對的是**模型產出的 state／evidence**，
  `config/**`／`specs/**` 是 **operator 撰寫**的、不是 gate 的受檢對象，原本兩類
  一起 quarantine 而沒有任何一步搬內容，實測導致 `cortex monitor --once` 直接
  `錯誤: 無 project 設定`，新增分類表與明示逐檔複製段；**第 4d 的「裝好但不得啟動」
  gate（#623）**——`PSC_DEGRADED_OPERATION=per-case-approval` **不會**阻止派工
  （只 gate 四個敏感動作），monitor 一起來就會派出必然失敗的 builder job，
  4d 驗證因此拆成安裝面／執行面兩段、正確終態為 `disabled`；**第 7b 功能面檢查**
  ——M1 的驗收全是結構性的，這正是部署通過 M1 卻做不了實際工作的原因，新增
  F1（`cortex monitor --once` 載得到設定）／F2（`cortex status`／`jobs`）／
  F3（真實 intake 跑到 terminal，⛔ #623 前不得執行）；**第 2a 稽核 6（#626）**
  ——permgen 為本機不存在的 principal 產出 `setfacl`，`sh -e` 下會中止整份 script
  留下半套的樹，稽核在套用前攔下並禁止「拿掉 `-e` 硬跑」。另隨 PR #624 落地移除
  #620 的手動繞過段、M2 追蹤 issue 更正為 #615、新增「M1 實機基準值」對照表
  （自檢 `job_writable_count` **5 → 0**、R9 denied 條數等）。規模統計
  35 → **43** 個 sudo 點、133 → **156** 個驗證點。
  詳見 `changelog.d/phase2b-runbook-realign.md`。
- **#612：repo root 的 cwd fallback 是危險預設——相對路徑輸入使 production 動作
  （`git fetch` 等）落在真實 checkout**——`paths.repo_root()` 舊實作未宣告
  `PSC_REPO_ROOT` 時退回 `Path.cwd()`，而 manager daemon 的 `WorkingDirectory` 正是
  operator 的真實 cortex checkout；`autonomy._infer_repo_root` 對**相對** spec 路徑
  的 `Path.resolve()` 也接到同一個 cwd。於是「解析不出目標 repo」不是失敗，而是
  **靜默打在錯的樹上**。清點出九條這樣的呼叫路徑，其中三條有**寫入**語意
  （`worktree_reclaim` 的 `git worktree remove --force`／`prune`、
  `ScriptWorktreeCreator` 的 worktree 建立、`installer.render_units` 把錯的
  `WorkingDirectory=` 持久化進 systemd unit），另有一條會把錯 repo 的 `rev-parse`
  結果寫成 candidate SHA 進 handoff manifest。#565／#607 收掉的是 `/tmp` 那半，
  本次收 cwd 這半。改法為 fail-closed 取代 silent fallback：`paths.repo_root()` 未
  宣告即拋 `RepoRootUnresolvedError`（新增 `configured_repo_root()` 讓「有沒有宣告」
  可被分辨，cwd 語意改由 `allow_cwd=True` 在 operator 手動 CLI 上顯式表態）；
  `_infer_repo_root` 拒收相對 spec 路徑、推不出 repo 根時帶 `DiagnosticReason`
  fail-closed（`spec-path-not-absolute`／`repo-root-unresolved`），不再回
  `spec.parent`——那條退路會讓 `git` 自己向上走回被 #565 搜尋上界／`~/.agents` 名稱
  規則刻意排除掉的 repo。與 #623 的 Phase 2b 佈局相容：repo 源碼樹遷入 Manager-owned
  樹之後，唯一的目標來源就是顯式的 `PSC_REPO_ROOT` 或顯式的絕對 spec 路徑。新增
  `tests/test_repo_root_fail_closed_612.py`（13 個不變式測試，每個都把 cwd 設成真
  repo 並斷言零 git 打向它）。連帶修掉四處**非 hermetic** 測試：`conftest` 的
  `_clear_runtime_env` 從未涵蓋 `PSC_REPO_ROOT`（全套測試的 manager 目標 repo 一直是
  跑 pytest 的當下目錄，比照 #303 改指 per-test 暫存路徑）、兩個 recover-pre-candidate
  測試實際在真 checkout 上跑 `git worktree list`、`init-sample` 測試讀的是真 checkout
  的 `.project-policy.yml`。詳見 `changelog.d/repo-root-fail-closed.md`。
- **#618 / trust-root Phase 2b：補上 `cortex service run`——permgen 的 manager unit
  `ExecStart` 指向一個不存在的 verb**——產生的 system unit 寫
  `ExecStart=<venv>/bin/cortex service run`，但 porcelain 只有 `install`／`start`／
  `stop`／`restart`／`status`／`logs`／`uninstall`，unit 一 start 即
  `unsupported service command`，Phase 2b 第 4c 步 blocking。加一個薄轉發 verb：
  `run` 之後的 argv（含 `--help`）在 parse 前攔截並原樣交給
  `coordinator.manager_daemon.main()`。**不沿用 `scripts/service-manager.sh`**：它會
  `mkdir -p "$HOME/.agents/log"` 導 daemon 輸出，而 Phase 2b 的 `HOME` 為 root-owned
  且 unit 帶 `ProtectHome=yes`——system-level 的正確形態是前景跑、log 進 journald。
  新增 `tests/test_service_run_verb.py` 五條，含一條把「產生器 ExecStart」與
  「CLI 實際 verb」綁在一起的迴歸鎖。
- **#584 順修（#614 runbook 實測發現的兩個 permgen 缺口）**——(a) 帳號 HOME／cache
  改由帳號名機械導出（`PathLayout.home_of()`／`cache_of()`），不再是二分時代的字面量
  `/var/lib/cortex-svc`：三分下 Manager 的 `Environment=HOME=` 原本會指向一個沒人擁有
  的目錄；(b) `scaffold_directories()` 改由 `scheme.headless_accounts()` 導出帳號清單，
  `cortex-reviewer-planner` 的 HOME／cache／`~/.codex` 因此自動入列（原本靠列舉，漏了
  三分才出現的第三個帳號）。runbook 的手動補行 workaround 可移除。另修
  `preflight_systemd_run()` 的 `which` seam：預設值原本在 def 時就綁定 `shutil.which`，
  `mock.patch.object` 打不到，測試實際驗到的是「本機有沒有那個帳號」而非它宣稱的分支。

### Changed
- **#666：HOME-anchored 資產在不適用的方案下不再進入 `ReadWritePaths`**
  （`permgen.inapplicable_home_anchored_assets()`）。幾個掛在帳號 HOME 下的資產由
  `PathLayout` 的部署決定欄位導出路徑，而那些欄位取的是定案的三分／四分；二分把 Manager／
  reviewer／planner 併進 `cortex-svc`，同一條路徑在二分部署裡不存在，而 systemd 對不存在的
  `ReadWritePaths=` 目標會讓 unit **直接起不來**。登記表的 note 早就寫著「二分下該資產不
  適用」、權限那一半也早就以 `[ ! -e ] || …` 守衛表達了它，缺的只有 RWP 那一半——#640 當時
  的處置是「乾脆不登記第二份憑證」，#666 要登記 Manager 的 gh 憑證時同一個陷阱又出現一次，
  因此改成一條**可列舉的機械規則**（靜默扣掉一條 RWP 與漏授一條在輸出上長得一樣，而後者的
  症狀是 job 跑到一半 EROFS）。附帶效果：#640 當年那個「不要登記第二份」的阻礙已經拆掉。
- **#666：`trust_root toolchain` 的輸出擴為三段**——系統層 python 發行版的落位與版本比對、
  Manager gh 憑證的落位與**以該身分實測**的驗證、以及窮舉盤點與已知未決項。計畫仍是純字串
  （非註解行只可能是 `install -d`／`chown`／`chmod`，有回歸測試）。驗收方式是**「重跑計畫
  後零漂移」**：實機手動補過的東西若產生器出不出來，換一台機器部署就不會有它。
- **#666：runbook 新增第 4f（系統層 python 套件）／4g（Manager gh 憑證）／4h（窮舉盤點
  複核）三步**，驗證一律**以該身分實測**而不是只驗檔案存在：4f 有「gate 身分
  `python3 -m pytest --version`」＋完整加固面下的 `systemd-run` 實跑（**CPython 不是 V8，
  MDWE 對它沒有影響**，因此這條在完整加固面下就該過，失敗即為新發現、不得就地放寬）＋版本
  比對；4g 有「Manager 身分 `gh auth status`」＋不變式四條（改得了內容／建不了新檔／刪不掉
  root-owned 鄰居／改不了 `config.yml`）＋ RWP 只掛檔案不掛父目錄＋ job 側反向驗證。附錄 A
  的漂移自我檢查同步補三條。spec §R1 新增 (b2)（Manager 傳輸層憑證：同形狀、不同級的洩漏
  面）與 (c)（外部相依的盤點判準從 run 反推、雙向封閉、第四種相依、HOME-anchored 資產的
  不適用規則）兩節。
- **#661：登記表的外部程式盤點由「四個 executor」擴為完整名冊，但**刻意分成兩張表****
  ——`permgen` 新增 `SERVICE_TOOLS`（`srt`／`openspec`）與 `SYSTEM_PROGRAMS`
  （`node`／`git`／`gh`／`bwrap`／`socat`），落位計畫改由 `TOOLCHAIN_PROGRAMS`
  （＝`EXECUTOR_TOOLS ∪ SERVICE_TOOLS`）導出，`TOOLCHAIN_SYSTEM_RUNTIMES` 由寫死的
  `("node",)` 改為導出值。**不直接擴充 `EXECUTOR_TOOLS`** 的理由是它同時是 dispatch 的
  executor 名字判準（`executor_hardening_profile()` 對表外的名字 fail-closed，spec §R8），
  併進去等於讓 `executor: srt` 這種派工變成合法。另把 `doctor` 的 review-sandbox 相依清單
  由行內字面值提成常數 `REVIEW_SANDBOX_EXECUTABLES`，並加測試與登記表對照——#661 的實機
  症狀正是「probe 要求的程式」與「登記表涵蓋的程式」各走各的。盤點過程另發現同一族的
  **第三個**成員 `openspec`（`@fission-ai/openspec` node script，住在 operator 的 nvm 樹）。
- **#661：runbook 兩項實機修正**——(1) 第 2c 步來源樹建立補
  `git remote set-url origin <上游>`：從 operator 的 checkout clone 會讓 `origin` 指向本機
  路徑，除了 doctor 判 `repo-identity` drift，更要緊的是 #656 的 ship 段 `push origin` 會把
  交付安靜地推進本機那棵樹。(2) 第 4b 步 EnvironmentFile 模板移除五個顯式覆寫
  （`PSC_CONTROL_ROOT`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT`／`PSC_MONITOR_STATE_ROOT`／
  `PSC_RUN_ROOT`）：`PSC_CONTROL_ROOT` 的模板值與 installer managed_env 的
  `control/<instance>` 不相等 ⇒ `managed-path-drift`；拿掉後由 `PSC_AGENTS_ROOT` 導出的值
  **逐字等於 `permgen.PathLayout.control_root`**，也就是 unit `ReadWritePaths` 實際保護的
  那條路徑——**顯式列出反而讓解析結果與保護面分岔**。同步補上 4a 的 backend 安裝步驟與
  4b 的 `PSC_PREFLIGHT_CMD` 值。
- **#633：trust-root Phase 2b runbook——EnvironmentFile 的展開慣用法、模板引號，以及兩條
  ACL 警語**——(1) **`env $(grep -v '^#' <envfile> | xargs)` 全數改掉（10 處）**：
  `$(… | xargs)` 依**空白**切詞，因此任何值含空格的變數都會被拆成多個參數；實機補上
  `PSC_GATE_CMD_PYTEST=python3 -m pytest -q` 之後那 10 條驗證指令全數變成
  `env: ‘-m’: No such file or directory`（rc=127），改用未加引號的 shell source 也一樣。
  **systemd 自己沒問題**（`EnvironmentFile` 把 `=` 之後整段當值），壞的只有 runbook 的驗證
  指令，而 `PSC_GATE_CMD_*` 這族**天生含空格**（它們是命令列），不是邊角案例；一律改為
  `sh -c 'set -a; . <envfile>; set +a; …'`。(2) **第 4b 的 env 檔模板值一律加引號**，並補上
  八個操作變數（`PSC_REPO_ROOT`／`PSC_REPO_IDENTITY`／`PSC_MANAGER_EXECUTOR`／
  `PSC_GATE_CMD_PYTEST`／`PSC_GATE_TIMEOUT`／兩個 interval），讓同一份檔案對 systemd、對
  `sh` 的 `.`、對驗證指令三邊都讀得對（`PSC_PREFLIGHT_CMD` 刻意仍留白——舊值在
  `ProtectHome=yes` 下不可達，搬到哪裡是 #623 的未決問題）；另補第 7b 的 **F2b**
  ——lazy 化之後 `systemctl is-active` 全綠**不代表**派得了工，`paths.repo_root()` 印不印得出
  來源樹才是判準。(3) **兩條 ACL 警語**：改變 ACL **結構**時舊的 default ACL 會靜默地跟著
  新物件走（`job-specs` 從 builder 專用改成 per-principal 三格後，三格全繼承了容器上的
  `default:user:cortex-builder:r-x`，正是 per-principal 要防的事）⇒ 改結構前先
  `setfacl -k <容器>`，並新增「每格具名條目 `foreign=0`、`#effective:` 註記為 0」的機械判準；
  以及**升級既有部署重跑也要守 scaffold → permissions 的順序**（反過來的話 scaffold 的
  `install -d -m` 對既存目錄會重設 mode，那次 `chmod` 重寫 ACL mask，讓具名條目變成
  `#effective:---`——ACL 還在、實際權限是零）。兩條的驗收判準一致：看 `mask::` 與
  `#effective:`，**不是**「有沒有那條 ACL」。
- **`work_actions._regenerate_gates_action` 的執行面收斂到 gate 執行身分（#629）**——
  它原本在 **Manager 進程內**直接呼叫 `gate_ledger.write_gate_ledger()`，等於以
  `cortex-manager` 的身分在 builder 完全掌控的工作樹上跑 `pytest`。`direct` 模式下
  builder 與 Manager 同 UID，這件事本來就沒有邊界可言；OS 隔離上線後它是一條**真的**
  提權路徑，而且是最容易被忽略的那一種（不在派工熱路徑上，只在 operator 手動救援時
  才走到）。改為與自動路徑共用 `gate_runner.run_declared_gates()`，因此不會出現「自動的
  那條降權了、手動的那條還在 Manager 進程裡跑」。降權模式下 gate 起不來時**不退回
  Manager 進程內執行**，診斷碼原樣回報。
- **`gate_ledger` 新增 `--snapshot-from`／`--publish`**：gate 命令一律在**拋棄式副本**
  上執行。唯讀不可行（`pytest` 要寫 `.pytest_cache`／`__pycache__`，`npm test`／`make`
  更是必寫，掛成唯讀只會讓每個真實 gate 以 EROFS 收場＝#629 要修掉的「安全但不能用」）；
  副本另外買到「gate 的寫入不污染 builder 交付的樹」與「快照在單一時點取得，builder
  留下的背景行程改不了跑到一半的樹」。symlink **原樣複製、絕不跟隨**（跟隨會把樹外
  內容複製進 gate 的可寫區，或走進無界遞迴）。
- **`UidScheme.headless_accounts()` 的來源改為 `registry.UNTRUSTED_EXECUTION_PRINCIPALS`**
  （headless persona ＋ headless hook ＋ **gate**），因此「Manager-owned／deployment 樹
  對這些帳號零寫入」這條核心不變式自動涵蓋 gate。同時新增
  `model_job_accounts()`——只有**跑模型 CLI** 的帳號需要 root-owned `~/.codex` 與
  executor 憑證骨架，gate 兩者都不要。
- **#643 / `permgen.EXECUTOR_TOOLS` 的 `copilot.needs_node` 由 `False` 改為 `True`**
  ——#640 落表時只知道它是 shell script、還沒查它內部 exec 什麼（表上的 note 當時就
  寫著「安裝時務必 `head -n 20` 查一次」）。#643 在真實加固面下量到 `copilot
  --version` 在 `MemoryDenyWriteExecute=yes` 下**空輸出**、拿掉即正常，與 `codex` 的
  症狀逐字相同——它內部 exec 的就是 node。因此「系統層 node 的版本風險只涵蓋 `codex`
  一個」在 spec／runbook／表註解三處同步改為**涵蓋 `codex` 與 `copilot` 兩個**。把量
  到的事實回填既有那張表，而不是為剖面另開一張。
- **#643 / spec §R3 新增「per-executor 加固剖面」段並明載誠實的取捨**：走 `jit` 剖面
  的 job **失去 `MemoryDenyWriteExecute` 這一層**（取得任意程式碼執行的攻擊者可在該
  job 自己的位址空間內配置 W+X 記憶體，JIT 型 shellcode 在此可行）。**沒有失去的部分
  同樣寫明**：其餘 26 項逐項不變，`User=` 一樣寫死在 root-owned unit 檔裡——W+X 只讓
  攻擊者在自己這個 UID 內執行程式碼，跨 UID／跨檔案系統／提權那幾層完全沒有鬆動，而
  §R2／§R3 保護的 Tier-0／Tier-1 資產靠的正是後者。**換到的是**保住 `codex`／
  `copilot` 兩個 provider，即 §R5／§R8 的 `independence_domain` 仍有可選空間。spec 因此
  **明文禁止**把本系統敘述成「所有 job 都有完整加固」，準確敘述是「原生 ELF executor
  的 job 有 27 項；node 型的有 26 項，少的那一項是 `MemoryDenyWriteExecute`」，風險表
  補兩列（被讀成完整加固／剖面被改成可由呼叫端選擇），並明載退出條件（node 型能在無
  W+X 下執行時 `jit` 剖面 SHALL 被移除，而非長期保留）。
- **#643 / Phase 2b runbook**：第 5-2 步改為落**兩份** unit（含「兩份差異必須恰好
  兩行」的落檔前 gate 與 `systemd-analyze verify` 未知鍵檢查），新增第 5-2b 步「在
  **真實加固面下**驗證兩種剖面」——形態比照 #640 第 4e 步且**含負向對照**（node 型
  executor 在 `strict` 剖面下**必須失敗**；只驗寬鬆環境的 `--version` 會整個溜過去，
  只驗 `jit` 成功也證明不了剖面分岔是必要的）。5-7 新增第 12 條（config 選不了剖面／
  未知 executor fail-closed／spec 帶剖面欄位被讀端拒），並讓 (5)(7)(8)(9)(10) 對兩個
  字幹各跑一次 → 合計 **50** 個 sudo 點、**184** 個驗證點。
- **#640 / job 的 `PATH` 沿用既有的 `PSC_BUILDER_PATH`，並由「選配」改為「必填」**
  ——`PathLayout.job_path_value()` 給出正規值（`<toolchain>/bin` **排最前面**，尾段是
  系統層，不含任何 `sbin`）。**刻意不在模板 unit 裡寫 `Environment=PATH=`**：模板
  unit 的 `ExecStart` 是 root-owned shim，shim 以 `execvpe(argv[0], argv, spec['env'])`
  整份換掉環境，job 解析命令用的 `PATH` 來自 **spec 的 env**——寫在 unit 上只會是一個
  看起來承載作用、實際被 shim 丟掉的設定。toolchain 排最前面是必要的：否則系統層那份
  舊版會蓋掉它，症狀是「跑得起來但版本不是預期的那個」。取捨連同理由寫進產生出來的
  job unit 註解裡。
- **#640 / spec §R1 明載一個誠實限制**：把 operator 的憑證複製給 job 帳號，代表 job
  用的是**同一個 provider 帳號**。三分買到的是**檔案系統層**的隔離（job 偷不到
  Manager 的 token、改不了 Manager 的 state、讀不到另一個 job 帳號的憑證），**不是**
  provider 層的獨立——與 `independence_domain`（§R5／§R8 的 anti-collusion 控制）不是
  同一件事，兩者不得互相當作證據。真正的 provider 層獨立需要每個 job 帳號各自的
  provider 帳號，屬未來選項。
- **#640 / Phase 2b runbook 新增第 4e 步**「executor toolchain 落位 ＋ per-account
  憑證」（4 個 sudo 點、9 個驗證點）：系統層 node（版本本身是部署決定）、逐支 CLI 的
  搬移方式、**以 job 帳號實跑一次 `--help` 期望 rc=0**、**版本與 operator 側逐字相同**
  （只驗 rc=0 不夠——系統層那份舊的一樣 rc=0）、在真實加固面下以 `systemd-run` 複跑
  （`MemoryDenyWriteExecute=yes` 對 node 的 V8 是第一嫌疑），以及憑證「能改內容／
  建不了新檔／刪不掉／換不掉鄰居」的反向不變式。5-5 的 `PSC_BUILDER_PATH` 與
  「builder 自己 `login`」兩段一併改寫，附錄 A 補兩條漂移自我檢查 → 合計 **49** 個
  sudo 點、**174** 個驗證點。
- **#623 / trust-root Phase 2b：成果回收由「Manager 伸手進 builder 的 clone fetch」
  改為 git bundle ＋ append-only spool**——#634 的回收做法
  `git -C <來源樹> fetch <builder 的 clone>` 在三分下**行不通**（operator 0817 實機
  驗證）：clone 是 builder-owned `0700`，Manager 直接 `cannot change to '…':
  Permission denied`；而「對每個 job 的 clone 補 `safe.directory`」也不可行——實測
  git 2.43 **不吃路徑 glob**，只認逐字相等或字面 `*`。改成 builder 在自己的 clone
  產 bundle → 寫進 Manager-owned 的 append-only spool
  （`<coordinator_root>/commit-spool/<job-id>/commits.bundle`，形態比照
  `review-verdict-spool`：容器 `0700 cortex-manager`、producer 只獲 `wx` 無 `r` 的
  per-account ACL、dispatch 當下 pre-seed、落地後 seal）→ Manager 從**那個檔案**
  fetch。關鍵在 Manager 讀的是一個普通檔而不是一個 repo，dubious-ownership 與父鏈
  traverse 兩個問題同時消失，回收全程**不存取 builder 的樹**（不變式測試以
  `chmod 000` 釘住，並已做突變驗證）。bundle 在 wrapper 的模型 argv 之後、**exit
  sentinel 之前**產生（sentinel 一出現 Manager 隨時可能開始回收），並以
  `exit "$rc"` 還原模型的 exit code（降權模式下 unit 的 exit code 就是它，#604）。
  `^<base>` 取自 provisioning 在 clone 內 pin 的 `refs/cortex/base`＝來源 repo 自己
  `rev-parse --verify` 出來的 `exact_base`，因此「來源樹一定有 prerequisite」由單一
  推導點對每條 lane 成立。bundle 缺席／prerequisite 缺席／帶錯 branch／非
  fast-forward 四類全部 fail-closed 且訊息可操作（git 原文＋該怎麼辦）。bundle 成功
  回收後保留並封存，取代 `refs/cortex/reclaimed/**` 對 clone 形狀的證據角色（該機制
  本身也要 `git -C <clone>`，三分下同樣不可行）。`direct` 模式沿用 #634「以形狀判斷、
  不依 `PSC_JOB_RUNNER` 分支」的原則，`commit_bundle=None` 時 wrapper 輸出逐字不變。
  登記表資產與 OS 權限由 #636（已 merge）在 `trust_root/` 定義，路徑契約的權威
  resolver 是 `config/paths.py:commit_spool_root()`；本次只做 coordinator 側。
  新增 `tests/test_bundle_commit_harvest_623.py`（30 測試）。
  詳見 `changelog.d/bundle-harvest.md`。
- **#623 / trust-root Phase 2b：job 工作區模型由 `git worktree` 改為 per-job 完整
  clone——provisioning、成果回收與回收層**——M1（#584）之後 builder 以
  `cortex-builder`、Manager 以 `cortex-manager` 執行，實測顯示 `git worktree` 在三分
  下**結構性不成立**：linked worktree 的 `.git` 是指向 Manager-owned 樹的指標檔，把
  gitdir 也 chown 給 builder 之後 `git status` 過了但 `git add` 仍失敗——寫 object
  需要寫**共用** object store，而能寫共用 object store，隔離邊界就在 git 這一層漏掉。
  新增 `coordinator/job_workspace.py` 作為工作區模型的單一真相（標記／識別／列舉／
  刪除／成果回收／封存）；`coordinator/seams.py` 的 provisioning 改為
  `git clone --no-hardlinks`，四條守衛（target 已存在、base 必須是既有 commit、既有
  branch 必須位於 base ancestry、fast-forward 後重掛）與錯誤訊息逐條等價，且失敗會把
  已做的變更全部還原；工作區的 `origin` 指向**真正的上游**、指向來源 repo 的暫時
  remote 一律移除、`refs/remotes/origin/*` 與本地 git identity 一併複製過去，因此
  delivery 的 `git -C <工作區> push origin` 行為不變。新增成果回收
  `job_workspace.harvest_branch()`——Manager 以 `git -C <來源 repo> fetch <clone>`
  單向拉回（沿用 D2「git 讀」的方向，builder 永遠不 push 進 Manager 的樹；refspec 不帶
  `+`，非 fast-forward fail-closed），掛在 canonical lane 的
  `_verify_build_candidate_transition` 之後與 slice lane 的
  `verification.run_result_verification` 讀 branch head 之前。`coordinator/gc.py` 的掃描
  與 `--apply` 同時涵蓋 per-job clone 與升級前既存的 linked worktree，依工作區**自己的
  形狀**分派回收方式；`coordinator/worktree_reclaim.py` 的安全閘擴充為認得兩種形狀，
  並在刪除 clone 前把工作區 HEAD 封存進 `refs/cortex/reclaimed/**`（clone 的 `rmtree`
  會連 object store 一起刪掉，與該模組「不銷毀證據」的契約相牴觸）。clone 模型對
  `direct` 與降權模式走同一條 code path，所有新行為都以工作區標記檔為前置條件，既有
  部署與測試裡的假路徑完全不觸發。新增
  `tests/test_per_job_clone_provisioning_623.py`（27 測試，全部以真 git repo 驗證）；
  全套 `python3 -m pytest tests/ -q`：3644 passed，零回歸。
  詳見 `changelog.d/per-job-clone-provisioning.md`。
- **#584 / trust-root Phase 2b runbook：A／B 兩案並列收斂為 A+B 單一路徑（docs-only）**
  ——落實 operator 0816 第三輪裁決。polkit 的 `manage-units` 只暴露 unit 名與 verb、
  不暴露 `User=`（#603 實測），因此「誰持有授權」與「授權能做什麼」兩層一起收：
  **A＝UID 三分**（`cortex-manager` 不跑模型且是唯一 polkit subject／
  `cortex-reviewer-planner`／`cortex-builder`，`THREE_WAY_SCHEME` 由備選轉定案，
  全文 `two-way` → `three-way`，二分縮為一行歷史註記）＋
  **B＝root-owned template unit**（`cortex-job@.service`，`User=cortex-builder` 寫死）＋
  **C 由 root-owned shim 承接**（`/opt/cortex/bin/cortex-job-shim` 讀 Manager-owned
  job-spec spool 導出 argv），切換點 `PSC_JOB_RUNNER=systemd-template`。第 5 步由
  10 小節的兩案並列收斂為一條九節路徑；反向測試由「4＋7 條、其中 1 條期望成功」
  收斂為 11 條**全部期望被拒**。殘餘風險重寫為「僅剩 `cortex-manager` 帳號的
  supply-chain 類」，並誠實標註 M1／M2 分段（`_degraded_runner()` 目前只對 builder
  persona 降權，reviewer／planner 的行程面降權屬後續工項）。**R9 攻擊矩陣四族 →
  五族**：族 1–4 各跑 builder 與 reviewer-planner 兩個 subject，新增族 5
  privilege-boundary——(a) 以 `cortex-manager` 請求 transient unit `--uid=root`
  的五種形式（含 `--user` bus 與 `busctl` 直打 `StartTransientUnit`）必須被 polkit 拒、
  (b) 三個 headless 帳號 × 九種手法改寫 template unit／shim／polkit 規則必須被
  root-owned 拒（27 條），兩族各附 negative control。執行前提補三帳號與
  `RUNNER_MODES` 檢查、回滾段補齊 template／shim／polkit／切換點／三帳號各自的回滾，
  並重新統計為 9 步 ＋ 3 附錄、**32 個 sudo 點、122 個驗證點**（逐段落明細表）。
  transient 路徑降為附錄 B 的降級備援並標明殘餘風險。
  詳見 `changelog.d/ab-runbook-converged.md`。

### Added
- **#606：重派 prompt 機械附上前次採信失敗證據——無回饋的重試不再是決定論的重複**
  ——現場 run `workflow-7812abefede9d9b5d601` 的 subagent-build（job 492／493）：builder
  兩次自稱 `pytest: passed`，Manager 的 gate ledger 兩次獨立重跑抓到**同一個**失敗，兩次
  `GateContradictionError` 逐字相同。根因不是重派錯誤（`retry-card` 刻意用原卡 prompt，
  契約不可竄改是對的），而是 prompt 沒有任何通道攜帶「上一次為什麼被拒」。新增
  `manager._workflow_retry_context()`：輸入是 `_dispatch_workflow_card` 既有的 `matching`
  （同一張卡的先前 job），輸出機械組出一個 `retry_context` 區塊塞進 dispatch contract，
  內容全部來自 **Manager 自產證據**、一個字都不取自模型輸出（與 #540 的不可竄改性同一條
  紀律）——**採信錯誤類別＋canonical 訊息**（不讀敘事欄位，而是對舊 job 重跑既有採信路徑
  的前兩段，`GateContradictionError` 與「log 無 JSON envelope」兩型都覆蓋）、**gate ledger
  的 failed gates**（名稱＋exit code＋截尾輸出，「哪些算 failed」複用採信端的
  `terminal_contract._ledger_outcomes`，不另立第二份判準），加上明示語句「前次嘗試因以下
  Manager 獨立證據被拒；先重現並修復，再完成本卡」。`retry-card` 與 daemon 的 forced retry
  都收斂在同一個 prompt 組裝點，兩條路徑同時拿到回饋。**首派逐字不變**：`retry_context`
  預設 `None`，首派 `matching` 為空即不產生任何差異（測試釘住，含走真正 dispatch 路徑的
  那條）。截斷上限 `RETRY_CONTEXT_EVIDENCE_LIMIT=2000`（全體 gate `detail` 合計預算，保留
  尾段、被截明示 `detail_truncated`）與 `RETRY_CONTEXT_MESSAGE_LIMIT=600`，並 fail-soft
  ——證據讀不到不得害死一次合法重派。附帶收斂 issue 的第二個觀察：
  `terminal_schema.status_policy` 末段接上新的 `gate_ledger.gate_scope_honesty_hint()`，
  明說「focused 綠不得推定宣告的 gate 綠」，且**實際會被 Manager 重跑的命令逐字進 prompt**
  （與 #541 的 `allowed_names` 同一條機械生成紀律）。`retry_context` 另帶 `attempt`／
  `redispatch_count`（由這張卡已燒掉的 job 數機械導出）作為 #555 per-card 熔斷的計數鉤子
  ——**本票不實作熔斷**。詳見 `changelog.d/retry-feedback-context.md`。新增
  `tests/test_retry_feedback_context_606.py`（14 測試）。
- **R0.5 D6 / trust-root 隔離 Phase 2b：permgen 產 systemd unit ＋ polkit 規則，
  runbook 收斂為可執行版（仍不需 root）**——`permgen.py` 新增 `PathLayout`，把
  operator 0816 第二輪裁決的路徑（`/var/lib/cortex`、worktree pool
  `/var/lib/cortex/worktree`、部署樹 `/opt/cortex`）固化為機器可讀 config，對 R1 登記表
  每一項給出真實絕對路徑（等式測試：無遺漏、無多餘），runbook 因此不再有 placeholder。
  `build_manager_unit()`／`build_job_unit()` 機械產生 system-level unit：`User=` 服務
  帳號、`ExecStart` 指 root-owned 部署樹、`EnvironmentFile=` **無 `-` 前綴＝fail-closed**、
  27 項加固指令**逐項附「為何」註解**，而 **`ReadWritePaths=` 由 R1 登記表機械導出**
  （未決 5 定案）——等式測試同時釘住無遺漏／無多餘／最小性／與 `ProtectSystem`
  `ProtectHome` 的一致性，非登記表的額外可寫路徑只能經 `ExtraWritePath` 明示宣告且
  **每條必須附理由**。降權 job 走 **root-owned 模板 unit**（`User=cortex-builder`
  硬寫死、token 清空、命令來自 Manager-owned spool 且 job 唯讀）。`build_polkit_rule()`
  以同一套邏輯產出**兩個降權方案**的規則：**A（`--transient`，預設）**對應 #603 的
  `systemd-run`，unit pattern 與 `job_runner.UNIT_NAME_PREFIX` 是成對契約，且把
  「polkit 只暴露 unit 名稱、**不暴露 `User=`／`--uid=`**」導致的殘餘風險由 `UidScheme`
  機械導出並逐條寫進規則檔開頭（二分下 reviewer／planner 與 Manager 併帳，三分下該條
  自動消失）；**B（`--template`）**配合 root-owned 模板 unit 把 `User=` 硬寫死、
  transient unit 一律拒，殘餘為零。兩案骨架相同（**unit／verb 明細缺席即拒**、只放行
  `start`/`stop`、錨定 pattern）且互不放行對方的 unit 形狀；`evaluate_polkit()` 是與
  JS 共用常數的 Python 鏡像，兩案各跑一份決策矩陣。`transient_unit_properties()` 把
  同一套加固表 ＋ 同一份 RWP 展開成 A 方案的 `systemd-run --property=` 對照清單。
  三者**只產生內容字串**，靜態測試把
  `open(`／`write_text`／`mkdir`／`shutil` 也納入禁用字串。CLI 新增
  `python -m paulsha_cortex.trust_root {unit,polkit,scaffold}`（含
  `unit --job-properties`、`polkit --transient|--template`）與
  `permissions --commands --paths`。runbook `trust-root-phase2b-setup.md` 由 draft
  改為 **executable**：7 個 `⚠️ 未決` 全數替換為裁決定案表，結構為執行前提 ＋ 9 步
  ＋ WSL2 風險段 ＋ 附錄（24 個 sudo 點、94 個驗證點）。第 5 步把 A／B 兩案**都**寫成
  完整可執行（共用前提含「polkit 能／不能強制什麼」對照表與 A/B 比較表，各自的正向／
  反向驗證，A 方案另含「移除 polkit 規則後 dispatch 必須 fail-closed」的負控制與一條
  **預期會成功**的殘餘風險實測），並在開頭標明這是全 runbook 唯一還需 operator 拍板
  的一點（且與「是否提前三分」連動）。第 3b 節吸收 #599 的 review verdict 受控通道；
  第 8 步 R9 手動抽驗給出四族 43 條攻擊命令與預期輸出＋三組 negative control；
  第 9 步含每階段回滾與「全部退回 Phase 1 降級運轉」的總回滾；WSL2 段補上開機拉起
  驗證與 `ProtectSystem=strict` 誤擋診斷。**本票只交付程式碼與文件**：不建
  UID、不 chown、不動 systemd／polkit／pipx／`~/.agents`。詳見
  `changelog.d/p2b-runbook-executable.md`。新增 `tests/test_trust_root_permgen_p2b.py`
  （61 測試）。
- **R0.5 D6 / trust-root 隔離 Phase 2a：review verdict 受控通道（per-job 單向 spool）**
  ——堵掉 spec 背景 §3 的最短攻擊路徑：verdict 原本由 reviewer 模型寫在**自己的
  worktree 內**（`.psc-review-verdict.json`），同 UID 下 builder 可直接覆寫／預埋，
  不需偷任何 capability 就能為自己的 candidate 產出一份「通過」的 foreign review。
  依 §R2 把 verdict 落點搬到 Manager-owned 樹的 per-job 單向 spool
  `<coordinator_root>/review-verdicts/<reviewer_job_id>/verdict.json`（未決 10-6 的
  **per-job spool 案**，待 operator 最終拍板）：新增
  `config/paths.py:review_verdict_spool_root()` 與 `coordinator/review.py` 的
  `review_verdict_spool_dir()`／`prepare_review_verdict_spool()`（dispatch 前
  pre-seed 守衛、目錄 `0700`）／`read_spool_review_verdict()`／
  `seal_review_verdict_spool()`（落地後轉 `0444`）；登記進 R1 資產登記表為
  `review-verdict-spool`（permgen 產出：owner＝Manager 帳號、reviewer 僅
  **write-only** ACL、**builder 零寫入**，二分／三分皆然）。**reviewer 身分由 Manager
  job registry 推導**——`builder_job_id`／`reviewer_job_id`／`candidate`／
  `launch_identity` 四個綁定欄位不看 verdict payload 自述（與 workflow lane 早已成立
  的形狀對齊），reviewer 只貢獻 `findings`。相容遷移以 job row 新欄位
  `review_verdict_channel`（唯一合法值 `"spool"`）判定：有標記的 job **只認 spool、
  不回退**；本修法之前派工的 in-flight job 才讀 legacy worktree 落點並記 WARN ＋
  `DiagnosticReason`（`review-verdict-legacy-worktree-source`）。另新增
  `SubprocessLauncher.as_verdict_spool_writer()`，沿用 `--add-dir` 只放行該 job 的
  那一格 spool（未授權時 argv 與改動前逐字相同）。**範圍**：只改 slice lane；
  workflow lane 從終局 JSON 收割、本來就不在 worktree 內，未動。**誠實邊界**：
  Phase 2b（分 UID／chown）之前同 UID 下 builder 技術上仍寫得進 `coordinator_root`
  （Phase 1 自檢已知），本階段交付的是**通道結構**（路徑／守衛／登記／身分推導），
  OS 強制等 Phase 2b。詳見 `changelog.d/p2a-verdict-channel.md`。新增
  `tests/test_review_verdict_channel_p2a.py`（47 測試）。
- **R0.5 D6 / trust-root 隔離 Phase 2a（降權啟動器，預設關閉）**——新增
  `paulsha_cortex/coordinator/job_runner.py`：Manager spawn headless **builder** job 時
  改經 `systemd-run --uid=cortex-builder` 的 transient unit 執行（operator 0816 第二輪
  對「未決 1 降權機制」的裁決）。`PSC_JOB_RUNNER` **預設 `direct`＝現行行為逐字不變**，
  設為 `systemd-run` 才降權；值非法時 fail-closed，不靜默當成 direct。builder job 的
  環境改為**白名單**而非黑名單 scrub——transient unit 本來就不繼承呼叫端 environ，因此
  job 只看得到轉發類 7 項（`PATH`／`LANG`／`LC_ALL`／`LC_CTYPE`／`SSL_CERT_FILE`／
  `SSL_CERT_DIR`／`NODE_EXTRA_CA_CERTS`，每項在 `BUILDER_FORWARDED_ENV` 帶「為何需要」
  的 rationale）加合成類 5 項（`PSC_JOB_ID`／`PSC_SLICE_ID`／`PSC_REPO_ROOT`／選配
  `PSC_RELAY_TARGET`／選配 `HOME`），**gh token、daemon 的 `CLAUDE_CONFIG_DIR`／
  `GH_CONFIG_DIR` 一律不在其中**（issue #588 第 1 點）；降權模式的 shell 改
  `bash -c`（非 `-lc`，#588 第 2 點——login shell 會讓 `~/.profile` 在 env 約束建立後
  重新匯入），**direct 模式維持 `-lc` 不動**。FD 只交出 stdin/stdout/stderr 且 stdin
  顯式接 `/dev/null`。判定點與既有 persona 分支對齊：reviewer／planner 在二分方案裡與
  Manager 同帳號，**不經降權**。fail-fast 走 #570 `DiagnosticReason` 契約——systemd-run
  缺席／未以 systemd 開機／builder 帳號或 group 不存在在任何副作用前擋下，polkit 拒絕
  與 unit 名衝突由起動確認（「client 已結束**且** exit sentinel 不存在」）擋下並帶回
  systemd-run 的實際錯誤訊息，**任一條都不退回 direct**。unit 名前綴 `cortex-job-` 是與
  Phase 2b polkit 規則成對的契約。**本項是機制不是生效**：實際降權要等 Phase 2b 建好
  帳號＋polkit 後把 `PSC_JOB_RUNNER=systemd-run` 寫進 Manager env。同步改寫
  `docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 5 步（含 polkit 只暴露 unit
  名、不暴露 `--uid=` 的誠實標註）與 README 的 env 說明。詳見
  `changelog.d/p2a-systemd-run-launcher.md`。新增
  `tests/test_trust_root_job_runner_p2a.py`（61 測試，systemd-run 本體全程 mock）。
- **v4 R1：shadow telemetry 的 aggregation reader ＋ TTL retention（Go/No-Go 的直接
  輸入）**——PR #590 落地了 coverage validator shadow 的 sink（一次比對一檔），但沒有
  讀端；R1 的 Go/No-Go 判準是「兩週 telemetry 中所有 disagreement 可解釋」，沒有統計
  就無從判讀。新增**唯讀** aggregation reader `build_shadow_report()` 與 on-demand CLI
  `python -m paulsha_cortex.coordinator.coverage --report [--json]`（比照
  `python -m paulsha_cortex.trust_root ...` 的模組入口慣例，不動 `cortex` 傘狀 CLI）：
  總筆數、agreement／disagreement 計數與比例、觀測窗、disagreement 依 `kind` 分組
  （理論上只有 `topology-fail-coverage-pass`），每組附 combo／task_slug／callsite／
  missing-responsibility 分佈與逐筆樣本（含 `context` 與 `satisfied_by`，足供人工
  逐筆解釋）。同時加 TTL 清掃（`DEFAULT_SHADOW_TTL_SECONDS`，預設 30 天，比照 D4
  event spool 慣例）——**只在 reader 執行時順帶清**、無 daemon 常駐邏輯，以
  `recorded_at` 判齡、缺漏時降級用 mtime（壞檔亦隨時間退場），刪不掉只計數不 raise；
  `--ttl-days` 可調、`--no-sweep` 純唯讀。單筆 JSON 壞損跳過並計數，絕不炸掉整份報告。
  只做 reader ＋ retention；#591 其餘項（`satisfies` projection、雙 legacy phase 對映
  收斂、第二呼叫點儀器化）屬 R2。詳見 `changelog.d/shadow-telemetry-reader.md`。
  新增 `tests/test_coverage_shadow_reader_591.py`（27 測試）。
- **桶C「slice 迴圈家族」workstream 佈線（`#501`／`#497`／`#496`）**——新增三份 todo
  來源（`fix-verification-contract-hash-overwrite`／`fix-superseded-terminal-replay`／
  `fix-dirty-recheck-idempotency`）並在 `.cortex/work-items.yaml` 註冊對應 work item，
  讓 cortex 可自行受理這三張 issue。三張已對 main `48b0205` 逐條複查，缺陷全部仍成立；
  每份 todo 含現況查核段（精確檔案行號）、有界 scope（明列「主體是 X 不是 Y」與禁止
  越界項）與可測驗收條件。三張刻意不合併：`#497` 是 terminal job 重播來源、`#496` 是
  dirty recheck 迴圈、`#501` 是兩者共用的 `_apply_verification_result()` 污染原語。
  純佈線變更，不改動任何執行路徑程式碼。詳見 `changelog.d/bucket-c-workstream-todos.md`。
- **R0.5 D6 / trust-root 隔離 Phase 2a（權限產生器）＋ Phase 2b root 設定 runbook
  （純程式碼＋文件、不需 root）**——新增 `paulsha_cortex/trust_root/permgen.py`：吃
  R1 `ASSET_REGISTRY` ＋參數化的 `UidScheme`（persona→OS 帳號映射），機械產生登記表
  每一項的目標 `owner:group mode` ＋ per-account POSIX ACL，輸出結構化計畫（JSON）與
  runbook 可引用的 `chown`／`chmod`／`setfacl` **命令字串——只產生字串、絕不執行**。
  二分（`TWO_WAY_SCHEME`：`cortex-builder`／`cortex-svc`）為預設；同一資料結構表達
  三分（`THREE_WAY_SCHEME`：把 svc 拆成 `cortex-manager`＋`cortex-reviewer-planner`）
  **不改一行程式碼**——測試證明三分嚴格收緊（builder 於兩方案皆零寫入 Manager-owned；
  三分下全部 headless 帳號零寫入）。on-demand 入口
  `python -m paulsha_cortex.trust_root permissions [two-way|three-way] [--commands]`。
  另新增 `docs/superpowers/runbooks/trust-root-phase2b-setup.md`（Phase 2b root 設定
  runbook 草稿，8 段：前置檢查／建 UID／分樹＋legacy-import／Manager 遷 system-level
  unit＋加固／降權啟動器／升級流程／R9 四族驗收／回滾，逐步標 operator sudo vs 驗證
  命令，權限命令引用產生器為單一真相，標記 7 個未決點與 WSL2 最高風險步驟）。**本票
  只交付程式碼與文件**：不建 UID、不 chown、不動 systemd/pipx/`~/.agents`。詳見
  `changelog.d/p2a-permission-generator.md`。新增 `tests/test_trust_root_permgen_p2a.py`
  （33 測試）。
- **R0.5 D6 / trust-root 隔離 Phase 1（純程式碼、不需 root、含降級安全網）**
  ——新增 `paulsha_cortex/trust_root/` 子套件，依 spec `trust-root-isolation-spec.md`
  Phase 1 與 operator 0816 裁決交付 join gate 未達成期間的契約層地基：**R1 資產登記表**
  （`registry.py`，單一機器可讀真相＋`config/paths.py`／`control/constants.py` 的雙向
  等式測試，涵蓋 builder／reviewer／planner 三 persona）、**R3 啟動自檢**（`selfcheck.py`，
  用登記表對照現行部署把 group/other-writable 的 Manager-owned 路徑標為 job-writable；
  掛在 `manager_daemon` 啟動點，受 `PSC_TRUST_ROOT_SELFCHECK` 閘控，Phase 1 **只 WARN**）、
  **R7 capability 通道＋降級運轉**（`capability.py`，敏感 action 無 capability 時 100%
  被拒；capability action-bound＋single-use＋短效＋不落地 durable state；降級開關
  `PSC_DEGRADED_OPERATION` 預設 `per-case-approval`、可切 `disabled`）。on-demand 入口
  `python -m paulsha_cortex.trust_root {selfcheck,registry,equation}`。**Phase 1 不提供**
  （需 Phase 2 OS 邊界）：真正不可寫強制、持久 nonce ledger、socket OS 隔離、自檢
  fail-closed。詳見 `changelog.d/d6-trust-root-phase1.md`。新增
  `tests/test_trust_root_{registry_r1,selfcheck_r3,capability_r7}.py`（43 測試）。
- **v4 R1（方案 A）：responsibility coverage validator 的 shadow 骨架（零行為變更）**
  ——新增 `paulsha_cortex/coordinator/coverage.py`：新的 coverage validator 與現行
  topology validator（`validate_manager_spine()`，未動）**並行跑、比對、記 telemetry**，
  但 production 決策仍完全由舊 validator 主導。含 `SafetyStage` 列舉、
  `ResponsibilityCoverage` 結構、legacy `phase → responsibility` adapter，deck card
  schema 加 optional `satisfies`（capability declaration，非 self-certification；現有
  deck 不需改）。shadow 掛在 `manager.py` production 派工 gate 旁，永不 raise，受
  `PSC_RESPONSIBILITY_COVERAGE` 閘控（`off` 停用，預設 `on`）；disagreement telemetry
  原子落 `coordinator_root()/coverage-shadow/`。詳見
  `changelog.d/r1-coverage-validator-shadow.md`。新增 `tests/test_coverage_shadow_r1.py`。
- **Issue #506 / D5：headless-only hook 儀器化（claude 先）——D4 spool 的第一個
  producer**——D4 開了本機事件通道卻**沒有任何 producer**：monitor 每輪掃到的永遠
  是空目錄，D1–D3 省下配額的代價（發現延遲）一分錢也沒買回來。新增
  `paulsha_cortex/porcelain/headless_hook.py` 與 `cortex headless-hook
  post-tool-use`：headless claude **builder** job 每跑完一次 `Bash` 工具，由
  launcher 注入的 PostToolUse hook 從命令解析出被動過的 GitHub 物件，依 D4 契約寫一
  則 `github_object` 事件。**使用者硬約束「hook 不得影響正常的互動式 agent 使用」以
  兩道彼此獨立的結構保證落地**（任一道成立，互動 session 即完全 no-op）：(1) **hook
  只經 launcher 注入且從不落地任何檔案**——宣告由 `SubprocessLauncher.launch()` 每次
  現場組出（`_claude_spool_hook_settings()`），經 argv 的 `--settings` 只交給該 job
  的行程，**不寫 `~/.claude/settings.json`、不寫任何 user 層設定、不寫磁碟**；互動
  session 讀 operator 自己的設定，那裡沒有這個 hook，因此**連呼叫寫入端的機會都
  沒有**；打包的使用者全域模板 `scripts/hooks/claude.json`（paulshaclaw thin install
  的切點）刻意不含它，並有測試釘死。(2) **`PSC_JOB_ID` 自守**——`launch()`／
  `executor_environment()` 為派工的 job 注入該標記，`emit_for_tool_use()` 讀不到就直
  接返回，**不建 spool 目錄、不寫檔、不起 subprocess、連命令都不解析**。**只有
  builder 掛 hook**：read-only planner 走 `--tools ""`（沒有 Bash），review-only
  reviewer 是 read-only 契約且其 `--settings` 是那份 deny 掉 `$HOME` 的 sandbox 政策；
  marker 與注入點成對出現，不留「有標記卻沒 hook」的半套狀態。**為何是 `--settings`
  overlay 而非 hermetic `CLAUDE_CONFIG_DIR`**：#404 為 planning 的純 JSON 回聲任務所做
  的 hermetic 選擇若搬到 builder，會一併抽掉 operator 的 `permissions` allowlist，讓
  headless job 卡在無人可核可的授權提示——那是遠超出 D5 範圍的行為變更；overlay 同樣
  是 per-job、走 argv、不落地，且 builder 既有設定原封不動。**hint 不是 authority**：
  事件只帶 repo＋kind＋編號、**不帶新狀態**，`action` 純屬診斷；解析刻意往「寧可漏
  報」失準——只認封閉列舉的 `gh issue`／`gh pr` mutation 動詞與非 GET/HEAD 的
  `gh api` 單物件路徑（`issues/comments/{id}` 改的是留言、不會被誤認成 issue），旗標
  一律當成吃一個值跳過（`--add-label 3` 的 `3` 不會被當編號），一行內 `&&`／`;` 串接
  的多個命令全解析並收斂去重，沒帶 `--repo` 時從 job worktree 的 `origin` 補、補不到
  就丟掉。漏報只是退回 refresh 週期延遲（D3 每日 anti-entropy 的守備範圍），誤報只是
  白花一次條件請求且永遠不污染鏡像。**fire-and-forget**：所有失敗吞成 debug log，CLI
  一律 exit 0 且 **stdout 保持空**（PostToolUse 的 stdout 會被當決策讀、非零 exit 會
  被回報成 hook 失敗甚至回饋給模型），注入的命令再以 `|| true` 兜住 CLI 之外的失敗
  （`cortex` 不在 PATH／套件損壞）並設 `timeout` 上限確保不阻塞 job。**#536／#488 心
  跳本次只預留信封**：每則事件都帶 `job_id`，D4 信封的 `job_id` 欄位與
  `RESERVED_EVENT_TYPES` 的 `job` 型別因此已備妥，心跳 consumer 落地時不需改寫入端契
  約；本次不發 `job` 型別事件。**範圍**：codex 免 hook（`codex exec --json` 的 JSONL
  已被 parse），copilot／agy 留後續，D4 消費端一行未動。詳見
  `changelog.d/d5-headless-claude-hook.md`。新增
  `tests/test_headless_claude_hook_506.py`（73 個測試）。
- **Issue #506 / D4：monitor 的本機事件入口（spool）＋targeted refresh——事件是
  **hint 不是 authority**——新增 `paulsha_cortex/monitor/event_spool.py` 作為本機
  事件契約與唯一入口。D1–D3 把常態讀取壓到每 repo 每日 26 次計費請求，代價是**發現
  延遲**：fleet 自己剛動過的物件也只能等下一次輪詢把整個清單再問一遍。D5（headless
  agent hook，**不在本次**）將依本契約把「我剛動了 GitHub 物件」寫進 spool，monitor
  每輪消費它。**spool 契約**：目錄 `monitor_event_spool_root()`（預設
  `<agents>/monitor/event-spool/`，壞檔隔離到同層 `quarantine/`），**每事件一檔**
  （`<emitted_at 壓平>-<event_id 前綴>.json`，因此消費就是 per-file `unlink`，不需
  鎖或 offset 檔），**原子寫入**（temp 檔 `.` 前綴 → fsync → `os.replace`，消費端
  不可能讀到半寫入的檔案），信封欄位 `schema_version`／`event_id`／`event_type`／
  `emitted_at`／`source`＋選配 `job_id`／`payload`。**fire-and-forget 寫入端語意**：
  `EventSpool.emit()` 不等回應、不與 monitor 交握、**永不 raise**——hook 掛在別人
  （agent job）的工作路徑上，spool 寫不進去絕不能影響工作本體，掉一則 hint 的後果
  只是退回原本的 refresh 週期延遲，而那正是 D3 每日 anti-entropy 的守備範圍。事件
  **契約層就不給 producer 塞新狀態的欄位**，`action` 純屬診斷——對應 `correlation`
  既有的 inferred→confirmed 語彙：spool hint 是 inferred 訊號，只有 targeted 驗證
  回來的物件才是 confirmed、才進鏡像。**消費端**：D3 清單同步跑完後才消費 spool，
  對被點名物件發單物件 `repos/{repo}/issues/{number}` 的**targeted 條件請求**，
  per-object ETag 存進 `IssueSyncState.targeted_etags` 並與清單端點的 `etag` **分開
  存**（兩者 request path 不同，混用會讓條件請求永遠落空；304 一路不取回應的 ETag，
  與 D3 同一顆地雷）；targeted 讀回來的新狀態**不得推進 `since` 游標**（游標只能由
  清單回應推進，否則會跳過那之間被更新的其他物件）。**去重**：同物件多事件收斂成
  一次驗證，所有貢獻事件檔一起消費。**過期安全跳過**：事件早於本輪請求、且該物件已
  被本輪讀取涵蓋（在增量 delta 裡，或本輪是全量）就直接消費、不花請求；清單回 304
  **不算**一次讀取，不得算進涵蓋範圍。**處理成功才消費**：事件檔一路留到鏡像真的
  落地為止。**fail safe**：targeted 請求失敗／壞 JSON／回錯物件一律不寫鏡像也不消費
  事件；回 404 不從鏡像刪任何東西（刪除／transfer 只有每日全量對帳看得到），留給
  anti-entropy。**壞檔隔離不阻塞**：壞 JSON／缺欄位／payload 形狀不合／超過 TTL 的
  孤兒事件移進 `quarantine/`，同輪其餘事件照常處理。**per-cycle 上限 20**：hook 是
  per-tool-call 觸發的，沒有上限等於把 D1–D3 省下的配額交還給事件量決定。**#498
  擴充點**：`event_type` 為封閉列舉的擴充位，本次只消費 `github_object`；
  `steering`／`job` 已在 `RESERVED_EVENT_TYPES` 佔位，掃到時**原地保留、只記 log 與
  計數、絕不刪除**（那些事件屬於未來的另一個 consumer），未知型別與未知
  `schema_version` 同樣保留不動。**D5 的 hook 注入不在本次**，launcher 未動；沒有
  spool 目錄時 provider 行為與 D3 逐位元組相同。詳見
  `changelog.d/d4-event-spool.md`。新增 `tests/test_monitor_event_spool_506.py`
  （51 個測試）。

### Changed
- **v4 重構計畫 R0.5 D6：trust root 隔離 spec 定案（spec-only，本次不含實作）**——
  新增 `docs/superpowers/specs/trust-root-isolation-spec.md`（R1–R12），定案 0.2.0
  穩定版**不可豁免 join gate** 的契約。逐檔核對確認：路徑解析鏈本身即信任根
  （`config/runtime.py:89`；bootstrap env 由 installer 裸寫、無 mode 檢查，
  `deploy/installer.py:162`），全 repo **零 HMAC／零簽章**、所有 evidence 皆為
  自我雜湊（只證明位元組未壞、不證明產生者），`chmod 0400`／`0444` 一線對 owner
  全部無效。本 spec 另揭露一條比 `#484` 更根本的最短攻擊路徑：review verdict 是
  reviewer 模型寫在 worktree 內的 `.psc-review-verdict.json`
  （`coordinator/review.py:22-23,176-185`），同 UID 下 **builder 可直接代寫**，
  即使 reviewer 被正確限制成 read-only 仍然成立。**路線裁決**：完整比較
  (a) OS/MAC 邊界與 (b) 簽章＋強制驗簽後，建議以 (a) 為 0.2.0 的必要且充分基礎、
  (b) 降為 Phase 3 的 defense-in-depth——因為 (b) 的三個前提（金鑰保密、verifier
  完整性、單調計數器）**全部必須由 (a) 提供**，「只做 (b)」不是成本較高的方案而是
  不成立的方案；(b) 的完整規格（canonical encoding／domain separation／
  anti-replay／rotation-revocation／legacy 遷移／fail-closed）仍在本 spec 定案供
  Phase 3 與 Elevated tier 使用。另定案 operator 授權通道（action-bound＋
  single-use＋短效＋本體不落地）、reviewer 身分由 Manager registry 推導、四族 E2E
  測試矩陣（含 negative control 與「實際重啟服務」的 enforcement-plane 十案）、
  三階段落地（Phase 1 不需 root 可先行並帶降級運轉安全網），以及 `#484`／`#480`／
  `#489` 的取代／補強對照與 10 項待 operator 拍板的未決問題。詳見
  `changelog.d/d6-trust-root-spec.md`。本票不新增測試（docs-only）。
- **Issue #506 / D3：GitHub issues 改走 `state=all&since=` ＋ ETag 條件請求的增量
  同步，全量只作每日一次的 anti-entropy 對帳**——`GitHubWorkProvider` 過去每輪對
  **每個** configured repo 全量分頁抓 issues（`--paginate`）；D2 把 `contents`／
  `compare` 歸零之後，這是 monitor 對 REST 配額剩下的主要常態消耗（約 13 個
  configured repo × 每日 288 輪 = **3744 次計費請求／日**），而絕大多數回應與上一
  輪逐位元組相同。新增 `paulsha_cortex/monitor/github_issue_sync.py` 作為增量協定
  與 per-repo durable 狀態（游標／ETag／鏡像投影）的唯一入口。**`state=all` 不可
  退讓**：`state=open&since=` 看不到剛被關閉的 issue，closure reducer 拿不到
  `closed` 證據，manager 可能 auto-claim 已被人類在網頁端關掉的工作；closed issue
  的 `updated_at` 會隨關閉事件更新，`state=all&since=` 的增量天然攜帶關閉事件且
  delta 極小。**`sort=updated&direction=desc` 同樣不可退讓**：預設的 `created`
  desc 排序下，「一個舊 issue 剛被更新」可能落在第 2 頁而**不改變第 1 頁**，第 1
  頁的 ETag 就不再是整個 delta 的變更偵測器、條件請求會漏發。**ETag**：第 1 頁帶
  `If-None-Match`，304 **不計入** rate limit 配額（實測 `x-ratelimit-used` 在條件
  請求前後不變）；ETag 綁定它所屬的 request path，`since` 一前進即作廢，且 304
  一路**不**取回應的 ETag——GitHub 的 304 回強形式 `"<hash>"`、200 回
  `W/"<hash>"`，覆蓋回去會讓條件請求永遠落空而悄悄退化成每輪全額計費。**游標
  紀律**：`since` 取自回應中最大的 `updated_at`（不是本機時鐘），只在整輪完整成功
  後推進、永不倒退；分頁中斷時游標／ETag／鏡像三者原封不動。**每日全量
  anti-entropy**：增量看不到 issue 被刪除／transfer 這類不留 `updated_at` 痕跡的
  事件，因此每 86400s 強制一次不帶 `since`／不帶 `If-None-Match` 的全量重讀對帳，
  drift 一律以全量為準並同時記 log 與 `observations["issue_sync"]["drift"]`。
  **fail closed**：狀態缺失／損壞／游標格式不合／ETag 與 path 失聯一律退回全量
  重建，單一 repo 的紀錄壞掉不拖垮其他 repo。分頁改為本地依 Link header 逐頁重建，
  **不跟隨**伺服器給的絕對 URL（跟隨等於讓對方指定 `gh` 把 token 送去哪），連帶讓
  每一頁都經過 `GitHubPressureGate.throttle`——改動前 `--paginate` 是 gh 在行程內
  自己連發，閘門完全管不到。D1 的 `observations["auto_label_issues"]` 改由 durable
  鏡像導出，網頁端關閉事件因此在**同一個** refresh 週期內就讓該 issue 退出 auto
  派工名單。穩態計費請求降為每日 **26 次**（每 repo 1 次 anti-entropy 全量 ＋ 1 次
  無法沿用 ETag 的增量）。只動 issues 讀取路徑；D2 的 `monitor/git_mirror.py` 未動，
  寫入路徑、label API、events API 均不在本次。詳見
  `changelog.d/d3-incremental-issue-sync.md`。新增
  `tests/test_monitor_incremental_issue_sync_506.py`（34 個測試）。
- **Issue #534 / #509 / #490 / #475：模型引擎解析改為三層解析鏈，packaged roster 降級為
  候選池**——落實使用者裁決「人工指定清單優先 → agent 從 patchmud 評估合格清單挑 →
  未評估模型須先經 eval、合格後人工複核加入清單」。新增
  `paulsha_cortex/coordinator/model_resolution.py` 作為解析鏈單一真值：第 1 層
  `operator-overlay`（host overlay 人工指定，**列序即優先序**）、第 2 層
  `evaluated-roster`（新契約 `model-eval-roster.yaml`，須 `verdict: pass` **且**
  `review_status: approved` 且角色相符）、第 3 層 `packaged-fallback`（候選池，受
  `resolution_policy.packaged_fallback` 的 allow／warn／deny 管制）。解析層是排序主鍵且
  為 stable sort，`#452` 的 measured 側寫優先與 `#262` 的 `primary_domain` 偏好降級為
  同層內次要偏好——packaged roster 的內建列序（「agy 維持首位」）不再壓過人工指定，
  planner 也不會再跑到 operator 未核可的引擎上。`resolved_model_chain` 的 `source` 改記
  解析層（`run-override`／`operator-overlay`／`evaluated-roster`／`packaged-fallback`），
  封套來源移至新的選配欄位 `envelope_source`；#534 之前的紀錄維持可載入。兩處寫死的
  優先序一併移除：`select_secondary_planner` 不再迭代 `PLANNER_PRIORITY`（該常數曾把
  agy 釘在首位、且只認三組 `(executor, domain)`，operator 宣告的 cg／新 executor planner
  永遠不可達），`work_bridge` 的 primary planner 不再寫死 `("codex","claude","agy")`。
  **#509 殘項**：overlay 與 packaged 同鍵不再 `raise ValueError` 打掛 periodic tick——
  改為以 overlay 為準並留下診斷（明示 `override_packaged: true` 記 info、未明示記 warn），
  另新增 `packaged_overrides` 讓 overlay 明示 `park`／`demote` packaged 身分；新增
  `cortex doctor` 的 `model-resolution` probe，走與 tick 相同的載入器與排序函式、明示
  config root，並以不變式守衛「overlay 宣告某角色 → 生效解析必須在第 1 層」。**#490**：
  `review.load_model_identity_registry` 改用合併 registry，packaged 身分不必複製進 overlay
  才能被 retry-review 解析。**#475** 現場收編為測試 fixture（自訂 Claude-compatible 身分
  不得被 packaged 同 executor 身分靜默取代；executable 綁定仍為未竟部分）。所有新能力皆為
  選配欄位／檔案，既有 overlay 不改一行也照載照解析。詳見
  `changelog.d/model-resolution-chain.md`。新增 `tests/test_model_resolution_chain_534.py`
  （29 個測試）。
- **Issue #506 / D2：git 的資料走 git——monitor 對 GitHub REST 的兩類高量讀取改為本機
  git 操作，一輪掃描的 REST `contents`／`compare` 呼叫數固定為 0**——
  `GitHubTerminalProvider` 過去每個 remote `todo.md`／archived `tasks.md` 各打一次
  `repos/{repo}/contents/...`（實測生產 workspace 一輪 **91 次**），每個
  workflow-linked merged PR 各打一次 `repos/{repo}/compare/{merge}...{default}`；讀的
  全是本機 git checkout 本來就有的東西，而 git 協定（fetch）不受 REST rate limit
  管轄。新增 `paulsha_cortex/monitor/git_mirror.py`（`LocalGitMirror`）作為唯一入口：
  blob 一律以 REST tree 給的 blob sha 定址、整批一次 `git cat-file --batch` 讀完
  （sha 定址本身就是內容識別，取代舊 `contents` 路徑的 type／path／sha／encoding
  四項比對）；ancestry 改用 `git merge-base --is-ancestor`，判準與 `compare` 的
  `status in {ahead, identical}` 等價。一輪先做一次 `cat-file --batch-check` 批次
  查缺，**有缺才** fetch（因此 fetch 頻率沿用既有 refresh 週期），refspec 帶
  `--refmap=` 並寫進私有 namespace `refs/cortex/mirror/<hash>/*`，不動
  `refs/remotes/origin/*`、工作區與任何本地分支；merge commit 不在本機的 PR 會把
  `refs/pull/<n>/head` 一併掛進同一次 fetch（該 refspec 屬選配，remote 沒有它時退回
  只 fetch default branch）。身分先驗讀 raw `remote.origin.url`（不套
  `url.*.insteadOf` 改寫）確認 checkout 真的追著宣稱的 repo。**fail closed**：ref
  不存在、fetch 失敗、blob 讀不到、沒有本機 checkout、origin 指向別的 repo、shallow
  checkout 無法判 ancestry——一律 degraded 並由 `_retain_last_good` 保留上一份鏡像，
  絕不把讀取失敗靜默降級成「檔案不存在」或「不是 ancestor」；provenance 落在
  `observations["remote_reads"]`。`work_api` 把該 repo 在 workspace 的 canonical
  checkout（與 `RepoWorkProvider` 同一個 root）傳給 provider。只動讀取路徑，寫入與
  D3 的 `state=all&since=`＋ETag 增量不在本次；`coordinator/github_delivery.py` 的
  `fetch_remote_closure`（每次 PR closure 1 次 compare ＋ N 次 contents，不在掃描
  迴圈內）僅盤點未遷移。詳見 `changelog.d/d2-git-native-reads.md`。新增
  `tests/test_monitor_git_native_reads_506.py`（16 個測試，含量化驗收樁）。

### Fixed
- **#610：測試套件有測試打真實 github.com，builder sandbox 全套中止於 71%；兩處修為
  hermetic ＋ 加上 conftest 層網路守衛**——run `workflow-7812abefede9d9b5d601`（job 494）
  的 builder 在 codex sandbox（network allowlist）跑全套被 egress 攔截、整個 process 被殺
  （`exit -1`），誠實 builder 因此無法完成驗證。以「socket monkeypatch ＋ `PATH` 上的
  `git`／`gh` 記錄 shim ＋ `unshare -rn` 斷網對照」定位出兩處真兇：
  `tests/test_pre_candidate_recovery.py::test_candidate_worktree_dirty_reevaluation_on_tick`
  （collection 順序 69.0%，緊接 `test_porcelain_*` 批次之後）用相對 `spec_path` 讓
  `autonomy._infer_repo_root` 解析到「當下 cwd ＝ 真實 cortex checkout」，於是
  `manager.complete_tick` 對真實 repo 跑 `git fetch --no-tags origin main` 直打
  github.com；`tests/test_work_gc.py::test_cli_main_dry_run_text_and_json` 走 `gc.main`
  時把 `default_pr_status_provider` 接上真的 `gh pr list`。前者改用 conftest 既有的
  `git_origin` fixture（`insteadOf` 改寫到本機 bare origin）＋ fixture repo 內的絕對
  spec 路徑，後者注入本機假 provider 並反過來斷言 CLI 佈線。新增
  `tests/network_guard.py`：session-scope、預設啟用的兩層守衛（socket 層白名單
  AF_UNIX／loopback；subprocess 層檢查 `git` 的 transport subcommand——以
  `git ls-remote --get-url` 解出 `insteadOf` 改寫後的實際 URL 再判本機性——與 `gh`／
  `curl`／`wget`／`pip` 等純網路 client），違規當場失敗並指名測試 nodeid，另有 per-test
  帳本防止 `except Exception:` 吞掉守衛例外；逃生口 `PSC_TEST_ALLOW_NETWORK=1` 與
  `@pytest.mark.network`（預設排除，`--run-network` 才跑）。新增
  `tests/test_network_guard_610.py`（33 測試）自證守衛會抓也會放行。詳見
  `changelog.d/test-network-hermetic.md`。
- **#565：`/tmp` 的空 `.git` 目錄使 `_infer_repo_root` 全域劫持到 `/tmp`，production
  推斷與測試皆不 hermetic**——agent sandbox 基礎設施會在 sandbox 存活期間於 `/tmp`
  暫態 `mkdir` 一個**空的** `.git`（teardown 後消失，`rm` 被防護 hook 擋下只能 `mv`
  隔離），舊判準 `(parent / ".git").exists()` 把它認作 repo 根，於是任何 `/tmp` 底下
  （含 pytest `tmp_path`）的 spec 路徑都被推斷成 `/tmp`。這使
  `tests/test_fix_dispatch_spec_path.py` 的兩個推斷測試在「當下剛好有 sandbox 存活」
  時必紅，Manager gate ledger 對 builder candidate 重跑全套 pytest 因而拒掉**合格**
  candidate（`GateContradictionError`，0816 run `workflow-7812abefede9d9b5d601` 實測），
  且與 builder 真實缺陷混在一起干擾判讀。**production 補兩道判準**：`.git` 必須是**有效**
  repo 標記——目錄需含 `HEAD`、檔案需以 `gitdir:` 開頭（linked worktree／submodule）——
  由新增的 `_is_git_repo_root()` 判定，**不 fork `git rev-parse`**（`_infer_repo_root`
  在派工熱路徑上，對每個 parent 開 subprocess 的代價與 flakiness 都不划算，而檔案級
  判準已足以排除唯一實測到的偽陽性）；同時新增 `_repo_search_boundaries()`
  （`TMPDIR`／`/tmp`／`/var/tmp`）作為向上搜尋的**上界**，共享暫存根本身永遠不是任何
  spec 的 repo 根，其**之下**的真 repo 照常命中。**測試 hermetic 化**：新增
  `tests/git_fixtures.py`（`make_fake_repo()` 建含 `.git/HEAD` 的完整假 repo，
  `make_empty_git_dir()` 建污染形狀），六個測試檔不再以空 `.git` 目錄冒充 repo 根；
  `tests/test_fix_dispatch_spec_path.py` 補 8 個回歸（鏈上空 `.git` 必須穿過、只有空
  `.git` 時落回既有 fallback、worktree `gitdir:` 檔案仍算 repo 根、共享根即使是有效
  repo 也不落錨、上界之下的 repo 照常命中……），污染由測試自備，host `/tmp` 的當下
  狀態不再影響任何判定。
- **Issue #518：instance config isolation**——`cortex install service` 會遷移 legacy
  instance env，原子產生可驗證的 exact-project monitor config 並保留 rollback；monitor
  不再因父目錄 workspace 掃到 sibling repos，`cortex doctor` 也會從本機 env 檔告警 shared
  project config root 與重複掃描影響。
- **#586（缺陷 A）：builder sandbox 無法 bind AF_UNIX socket，全套 pytest 假失敗**
  ——builder（codex，`codex exec --sandbox workspace-write`）的沙箱實測**允許**
  `socket(AF_UNIX)` 建立與 `socketpair()`，但用 seccomp 把 **`bind()`** 擋成 EPERM
  （網路隔離），即使 socket 路徑在可寫根內。凡是 bind 本地 unix-domain socket（起
  `MonitorServer`／直接綁）的測試在 builder 沙箱內必失敗，令 builder 自跑整套
  `python3 -m pytest -q` 永遠有失敗、與 manager 獨立 ledger（正常環境 passed）系統性
  分歧。codex 的 seccomp 無法從本 repo 細粒度只放行 AF_UNIX bind（唯一開關是整片打開
  網路的 `network_access`／`danger-full-access`，正是 #586 安全邊界所禁），故採環境修復：
  新增 `tests/sandbox_support.py` 偵測當前 runtime 能否 bind AF_UNIX，凡需 bind 的測試
  在無法 bind 的沙箱下**明確 skip（帶原因）**而非假失敗，使 builder 自跑 pytest 由「有
  失敗」變 exit 0，與權威 ledger 源頭一致。**安全邊界**：只改「無法 bind 時 run vs skip」
  的判定，不放寬任何 syscall／不打開網路／不允許 builder 連上 manager socket（與 #584
  trust-root 隔離不衝突）。防護 `test_stage9_project_monitor_service.py`、
  `test_monitor_work_api.py`、`test_doctor.py`；新增 `tests/test_sandbox_afunix_skip_586.py`
  以模擬沙箱子行程證明防護測試 skip 而非 fail。
- **Issue #569：reviewer 卡的 `retry-verify` 只重置不重派——`retry-card` 放寬到
  verify／review 的 reviewer 卡**——實測 run `workflow-084f75e2178cf7547476` 的
  verification job（agy，#568 權限剖面缺陷）exit 0 但 log 無 JSON envelope，
  `retry-verify` 受理後只重置卡片與 facet（回應 `job: None`），未在同一個 action 內
  派新 job 也未 supersede 舊 job；之後每個 tick 的 resume 都重讀同一顆壞 job，run 對
  tick 隱形四小時後 `needs_human` 原地回鍋——與 #545 builder 卡同型的 catch-22。修法
  沿用 PR #552 的 `retry-card`：新增
  `registry.RETRY_CARD_PHASE_PERSONA`（`build→builder`、`verify`／`review→reviewer`）
  作為 work action 層與 registry 層共用的單一判準，`retry-card` 因此同時受理中段
  builder 卡與 `verification`／`code-review`／`adversarial-review`。硬約束逐條沿用：
  exact WorkflowRun CAS ＋卡名定錨、已對**現在這個 candidate** 綁定
  `workflow_evidence` 的卡拒絕重派（上一代 candidate 的歷史 evidence 不參與判斷）、
  舊 job 一個位元組都不動（不像 `retry-verify`／`retry-review` 會把舊 exited job 改
  標 `failed`）、新 job 的身分由 identity registry 在 dispatch 當下**重新解析**而非
  複製舊 job 的 executor／model（#568 的 reviewer fail-over 依賴這點）、dispatch 失敗
  時把 `needs_human` 連同 #527 的結構化理由補回去。`manager._dispatch_workflow_card`
  的 `force_new_build` 一般化為 `force_new_card`，並在重派 reviewer 卡前原子回收被取
  代 job 的 sandbox（目錄名 `sha256(run_id:card:candidate)` 必然撞名；回收時
  candidate checkout 已被改動則 fail closed）。`retry-verify`／`retry-review`／
  `retry-build` 的 CAS 與 admission 一字未改（`fix-repair-commit-recovery-spec.md`
  R4），並補上回歸樁。曝光面（#546 的一部分）：`_build_phase_recovery_actions` 更名
  為 `_phase_recovery_actions` 並涵蓋 build／verify／review，`resume` 的
  `next_actions` 與 #527 的 `cortex status` attention 條目因此對卡住的 reviewer 卡
  說得出 `retry-card` 而不再只有 `abandon`。詳見
  `changelog.d/reviewer-card-retry.md`；新增 `tests/test_reviewer_card_retry_569.py`
  （23 項，含 facet 原子性總樁）。
- **Issue #554：taxonomy marker 無詞界，與 #543 的 `<unavailable>` 佔位符相撞；worktree
  drift 的 `content` 誤分類死鎖一併解除**——兩個缺陷共用同一組現場（planning worktree
  drift 的失敗訊息）。**缺陷一**：`_operator_drift_message` 尾端是
  `evidence={location}`，退化值原為 `"<unavailable>"`，而 PR #542 落地的
  `outcome_taxonomy.TRANSIENT_SERVICE_MARKERS` 含**裸** `"unavailable"`（#533 為 agy
  的 `UNAVAILABLE (code 503)` 而收），比對是無界子字串——於是「drift 且備份／報告雙雙
  寫入失敗」這個純環境事件，會靠子字串巧合被判成 transient-service（`evidence=/tmp/
  psc-report.json` → False，`evidence=<unavailable>` → True）。這是 #500（`\btimeout\b`
  命中 nested tool result）、#487（`oauth` 命中 `doc-coauthoring`）的同族無界 token 缺陷
  第三次命中。**兩邊都修**：(a) marker 比對改詞界（新增
  `TRANSIENT_SERVICE_MARKER_RE`），擋住 marker 被埋在更長 word token 裡的誤中——全表
  掃描顯示裸短字串 `"503"`／`"429"` 誤中面最大（`workflow-1a503f0429ab` 這種 run id 修
  法前就會被判 transient），`"unavailable"` 次之（`envelope_unavailable` 等內部欄位
  值）；(b) 佔位符改為
  `planning_runtime.PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER = "<not-written>"`
  並附不變式測試——詞界擋不住「整個 token 就是 marker」，`<unavailable>` 與
  `<evidence-unavailable>` 的 `<`／`>`／`-` 都不是 word char，詞界照樣成立。詞界化會讓
  原本靠子字串巧合命中的真陽性落空，因此 `rate limited`／`rate limiting`／`timeouts`／
  `timeouterror`／`timeoutexpired`／`serviceunavailable` 改為顯式列舉入表（CamelCase
  例外類名尤其關鍵：`TimeoutExpired` 的訊息常在 reason 的 160 字截斷處被切掉
  `timed out`，只剩型別名帶得動訊號）。**缺陷二**：「planning launcher modified
  operator worktree」家族從 `content` 改判 `environment`——#543 之後 drift 不再銷毀任何
  資料（只備份與報告），語意上就是環境事件，維持 `content` 只會讓唯一出口是
  `abandon`（#507 comment 2 記錄、#543 明文留待後續的死鎖）。新增
  `manager._is_planning_worktree_drift_failure`，判準只認 `planning_runtime` 新匯出的
  穩定前綴 `PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX`，不依賴訊息尾段（尾段已在 #543
  由 `changes rolled back` 改為 `operator content preserved`）；`disposable read-only
  sandbox` 家族刻意不在此列，維持 `content`。並把 `_run_define_stage` 中段的三元表達式
  抽成具名的 `_classify_planning_failure`，讓 reason → classification 有單一可測入口。
  `recover-planning` 自身行為一字未改，本次只是讓 drift 案例走得到它。詳見
  `changelog.d/marker-word-boundary.md`。新增
  `tests/test_planning_drift_classification_554.py`（47 個回歸測試）。

- **診斷 invariant 家族（#527／#514／#515／#511／#482）：把「理由」從慣例升格成型別**
  ——0813–0814 **五次**獨立命中同一條缺口：狀態被推向「人要接手」的那一刻，理由沒有
  跟著落地。#527 的 build 階段無聲掛 `needs_human`（無 evidence、無 slice、
  `cortex status` 不呈現、`next_actions` 空）、#514 的 brainstorm 重驗例外不含路徑與
  原因、#515 的 `_post_integration_artifact_evidence()` **14 個裸 `return None`** 塌
  縮成不透明的 `primary-artifact-invalid`、#511 的 planning artifact 拒收原因到不了
  run、#482 的 absent evidence key 不含原因或 identity——五個形態不同、根卻同一條，
  逐案補洞（#397／#408／#513 各補過一次）已證明無效。**invariant**：任何把 run 轉入
  `needs_human`、或把 evidence 標為 absent 的狀態變更，必須同時落一份結構化理由（機
  器可讀 reason ＋ 人可讀 detail ＋ 來源位置）並可由 `cortex status`／`work show` 曝
  光。修法是把驗證層擋在**唯一**的進入點上：新增 `coordinator/diagnostics.py` 的
  `DiagnosticReason`，並在 registry 的兩個狀態轉移 API（全庫唯一能把 `needs_human`
  寫進 run row 的入口）強制「加 facet 必須帶理由／移除 facet 一併清理由／facet 已在
  則沿用既有理由」三條規則，`WorkflowRun` 新增 `needs_human_reason` 欄位持有它；既有
  部署那種「facet 有、理由沒有」的 legacy run 刻意放行，載入時 fail-closed 會把
  manager 打掛。配套的**掃描式 invariant 測試**以 AST 枚舉全庫所有設置點斷言每一個都
  帶理由（另有反證測試防止掃描器壞掉偽裝成通過）。呈現面：`manager.workflow_status_
  entry()` 把 ongoing 且 needs_human 的 run 投影進 `cortex status` 的 `attention`
  ——過去狀態快照 provider 只走 `list_slices()`，而 workflow lane 從不建立 slice
  row，run 在五份清單裡一份都不出現；理由另經 monitor observations 曝光到
  `cortex work show` 的 `blocking_reason`。#482 另把 pre-launch absent evaluation 的
  落點納入原因與請求身分的指紋，`missing → unknown → registered` 這條合法的設定推進
  不再需要刪 evidence 才能前進。**範圍紀律**：只修診斷與理由，後續處置（retry／
  needs_human／fail-closed 邏輯）一律不變；呈現面沿用既有欄位機制，未另立平行欄位體
  系。詳見 `changelog.d/diagnostic-invariant-family.md`。新增
  `tests/test_diagnostic_invariant_family_527.py`（32 個測試）。

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

- **Issue #545：`retry-build` 只受理最後一張 builder 卡——中段 builder 卡採信失敗後
  沒有契約內的重派路徑**——run `workflow-084f75e2178cf7547476`（#540 的殘留項）卡在
  build 階段的中段卡 `tdd-red`：builder 交付的 RED commit 合格、ledger 已由 #540 的
  `regenerate-gates` 重生成正確（`pytest: failed` = 合格 RED），但**舊 job 的
  terminal envelope 是模型輸出**——自報 gate 名 `'focused pytest RED expectation'`，
  envelope 屬契約內不可竄改的證據，`resume` 重新採信仍必敗於 `TerminalContractError:
  terminal 宣稱跑了 gate '...'，但 manager 的 ledger 沒有這一項`（0815 實證）。#540／
  PR #541 已把 canonical gate 名機械注入 dispatch prompt 的 `allowed_names`，因此
  **新的** `tdd-red` job 會產出可採信的 envelope；缺的只是「重派中段 builder 卡」這
  條路——`retry-build` 只受理最後一張 builder 卡、`recover-pre-candidate` 要求 null
  candidate（`worktree-isolation` 早已錨定 candidate）、`abandon` 會連合格的 RED
  commit 與一個世代一起燒掉（該 run 已耗 2/3）。新增 `retry-card` work action：以
  exact WorkflowRun CAS 加卡名定錨，原子清掉 `needs_human` facet 並讓 manager 以
  **原卡片契約**重派一個新 job；舊 job 與舊 envelope 一個位元組都不動、原樣保留供
  稽核，重派只允許產生新 job 與新 envelope，prompt 走既有的 `_workflow_job_prompt`
  （含 #540 的 `allowed_names` 注入）不另開組裝路徑，dispatch 失敗時 `needs_human`
  會被補回去、不留下「facet 清了但沒派出去」的中間態。**取捨**：選「新增 action」
  而非放寬 `retry-build`——後者是 candidate 修復語意，會把目標卡的 `step.action`
  覆寫成 repair 文案，中段卡走那條路等於把「寫一個 RED regression test」這個指示
  抹掉；`retry-build` 的 CAS 與 admission 一字未改，另補回歸樁鎖定它仍拒絕中段卡
  （與 #260 對 `recover-repair-commit` 的取捨同型）。順帶收口 **#546 的一部分**：
  `claim._resume_decision` 拿不到 job 層事實，build 卡卡住時宣告的唯一出口是
  `abandon`；work action 層新增 `_build_phase_recovery_actions`，以與
  `regenerate-gates`／`retry-card` 完全相同的前置驗判定兩者是否真的會被受理，是才
  補進 `resume` 回傳的 `next_actions`（只宣告會成功的動作，#382 的教訓）。詳見
  `changelog.d/midchain-builder-retry.md`；新增
  `tests/test_midchain_builder_retry_545.py`（23 個回歸測試）。

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

### Security
- **新增安全退步 R-6（明講，不是順手接受的）**：`HOME_REDIRECT_TREE` 的目標樹由 job 帳號
  擁有 ⇒ 樹裡的 token 葉檔**可被該 job 刪除或替換**（builder 的 `IN_PLACE_FILE` 擋得住
  「刪／換」，這裡擋不住）。影響面限於該帳號自己的登入態；換到的是 codex／claude
  **能不能起得來**。直接後果是同一棵樹裡**不得**再放任何 root-owned 的 enforcement 檔
  ——`reviewer-planner-codex-hooks` 因此升為 U-9。同時**修掉**一個 #640 刻意接受的代價：
  「暫存檔 ＋ rename 原子替換」形式的 refresh 在這個形狀下走得通。
- **builder 一行未改，且它的同型缺口已記錄**：`builder-executor-credential` 維持 #640 裁決
  (b)（RWP 逐字掛在檔案本身、父目錄 root-owned、runbook 第 4e-2 步的三條反向驗證不變）。
  但 #686 的量測同樣適用它——builder 在模板 unit 下跑 codex 會撞到同一條 `$CODEX_HOME`
  唯讀阻斷。改它會同時賣掉 `codex-hooks` 的 enforcement（一棵 job 擁有的樹裡放不住
  root-owned 檔），因此屬 **U-9** 的同一個裁決，本票不擅自做。

### Changed
- `docs/superpowers/runbooks/trust-root-phase2b-setup.md`：新增第 **4e-2b** 步
  （`cortex-reviewer-planner` 三份登入態的四步部署順序：骨架目標 → 遷移 0818 手動落位的
  舊目錄 → 由權限計畫落 symlink → 放 token，含跨 UID 的「換不掉指向／寫得進樹」反向驗證，
  與三個 executor 在真實加固面下的 rc 驗收）；既有的 per-account 憑證段改為 builder 專用
  並指向新步驟。**加固面一律走既有共用探針 `psc_run_under`**（property 由
  `permgen.unit_replica_properties()` 從落檔的 unit 全量導出），**未新增任何手寫的
  `--property=` 清單、未自帶 `--setenv=PATH=`**（design D13）。

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
