---
status: accepted
work_item: planner-job-downgrade
---

# planner-job-downgrade Plan

對應 spec：`docs/superpowers/specs/planner-job-downgrade-spec.md`
對應 design：`docs/superpowers/specs/planner-job-downgrade-design.md`
issue #672。

**本 plan 描述的是後續實作票的切分，不是本票的工作項。** 本票（#672 設計票）的交付
是 spec ＋ design ＋ 本 plan ＋ openspec 三件套 ＋ changelog，**不含任何
`paulsha_cortex/` 的程式修改**。

## Tasks

### 0. 依賴與前置（不是本設計的工作項，但票 E 起算的 blocker）

**剖面面沒有前置。** 本 plan 早期版本列了「#673 已 merge」為 blocker，該前提已由
開票者更正撤回並移除：八份 unit 全帶 `SystemCallErrorNumber=EPERM`，被過濾的 syscall
回 `EPERM` 而非 `KILL_PROCESS`，codex／copilot 在 `jit` 剖面實測 rc=0，
claude／agy 兩種剖面皆 rc=0——**現行 `EXECUTOR_HARDENING_PROFILE` 就是對的**
（見 design D6）。planner 上 job 不需要任何剖面面的修正。#673 已由 **PR #677** 以
「不放寬任何 syscall」收尾，且該 PR 順帶把本 plan 原本要另開的「runbook 驗證方法改
機械讀取」那張票一併做掉了（見下）。因此**票 E 的前置只剩一條：PATH 宣告**。

- [ ] **PATH 前置票**（本票新查到，尚無 issue）：`/opt/cortex/etc/cortex-manager.env`
      補 `PSC_REVIEWER_PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin`
      （值由 `permgen.job_path_value()` 產生器出，operator 落進 root-owned
      EnvironmentFile）。
      現況：該變數不存在 ⇒ job 的 PATH 是 PID 1 預設 ⇒ `claude`／`agy` rc=127、
      `codex` 解到系統層 `0.42.0`（toolchain 是 `0.147.0`）。
      **這條同時影響今天的 reviewer job**，應獨立開票，不掛在 planner 票下。
      驗收：以 `cortex-reviewer-job@probe` 的完整 property 集合實跑，逐 executor
      記錄「解析到的絕對路徑 ＋ 版本字串」，與 `/opt/cortex/toolchain/bin` 逐條相符。
- [x] ~~**runbook 驗證方法改機械讀取前置票**~~ ——**已由 PR #677 完成，不需另開票**。
      #677 新增 `permgen.unit_replica_properties()` 與 CLI `trust_root unit-replica`
      （契約「全帶，不選」，`require_hardening=True` 下落檔 unit 少任一加固鍵即
      `UnitReplicaDriftError` 且 stdout 保持空），並把 runbook 4e／5-2b／5-3／5-4 的
      手抄子集全部改走共用探針 `psc_run_under`；5-2b 由「正向四段」改為
      4 executor × 2 剖面 × 2 角色 unit 的全矩陣 ＋ 反向對照。
      **票 E 的驗收矩陣直接消費這些，不得自行組 property 清單**（見 design D13）。
- [ ] **operator 裁決 U-1 ～ U-8**（見 design 的未決問題總覽）。U-2 影響票 E 的實作
      形狀；U-4／U-5 影響票 D 的範圍；其餘可在對應票內收。

### 1. 票 A｜錯誤語意三分 ＋ 逐候選拒因表（對應 R6，**不依賴任何部署面改動**）

最先做，因為它**立刻**降低今天的排查成本。PR #674（#670）已經把 probe 那一端的診斷
補齊（`strip_code_fence()`／`stdout_excerpt()`），票 A 要做的是讓那份診斷**活著抵達**
blocking reason——兩者剛好接得起來，缺任何一半都還是查不出原因。

- [ ] `tests/test_planning_failure_taxonomy_672.py`（TDD RED）：
  - `test_no_heterogeneous_planner_reason_carries_per_candidate_rejections`：
    給一組 probe（agy=not-ready/malformed-output、claude=not-ready/safe-probe-failed、
    codex=same-domain），`run_heterogeneous_brainstorm` 的 reason 必須同時含三個
    executor 名與各自的拒因，且 `malformed-output` 的 diagnostic 帶得出 stdout 前綴。
  - `test_probe_diagnostic_survives_into_blocking_reason`：probe 的 diagnostic 不得
    在 `select_secondary_planner` 被吃掉。
  - `test_environment_grade_rejection_reclassifies_to_environment`：拒因表中有任一
    environment 級拒因時，`_classify_planning_failure` 回 `environment`（而非 `content`），
    使 `_resume_decision` 得以浮現 `recover-planning`。
  - `test_content_grade_rejections_stay_content`：全部拒因都是 `same-domain` 時仍為
    `content`（不得把拓撲問題誤判成環境問題——反向誤報同樣不可接受）。
- [ ] `paulsha_cortex/coordinator/model_identities.py`：新增
      `CandidateRejection` dataclass；`SecondarySelection` 增 `rejections` 欄位；
      `select_secondary_planner()` 的每個 `continue` 改成記錄一筆拒因。
- [ ] `paulsha_cortex/coordinator/planning.py`：`run_heterogeneous_brainstorm` 把拒因表
      渲染進 `BrainstormResult.reason`（格式見 design D8）。
- [ ] `paulsha_cortex/coordinator/manager.py`：`_classify_planning_failure` 增一條
      environment 例外（比照既有的 `_is_planning_authority_residue_failure`／
      `_is_planning_transient_service_failure`／`_is_planning_worktree_drift_failure`
      三條，同一個模式，不新發明）。
- [ ] 三個失敗族的具名常數（`planning-job-start-failed`／`planning-executor-failed`／
      `planning-output-malformed` ＋ `executor-silent-exit` 子類）先定義，票 C 才有東西可落。
- 驗收：既有 planning 測試一行不改全綠；新測試全綠；`no-heterogeneous-planner` 的
  reason 可用正規表示式釘住必含拒因表。

### 2. 票 B｜`PlanningInvoker` 抽象（對應 R1 的結構面，**純重構、行為零改變**）

- [ ] `tests/test_planning_invoker_672.py`（TDD RED）：
  - `test_in_process_invoker_preserves_sandbox_contract`：sandbox 被弄髒仍拋
    `planning launcher modified disposable read-only sandbox`。
  - `test_in_process_invoker_preserves_operator_drift_containment`：operator 樹 drift
    仍走 `_contain_operator_drift`，`PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX` 逐字不變
    （那是下游分類契約）。
  - `test_invoker_selection_follows_resolve_runner_mode`：`PSC_JOB_RUNNER` 的值是**唯一**
    輸入；非法值 fail-closed；不存在第二個開關。
- [ ] `paulsha_cortex/coordinator/planning_runtime.py`：抽出 `PlanningInvocation`／
      `PlanningInvoker`／`PlanningOutcome`；把現行 `_invoke_json` 的執行段搬進
      `InProcessPlanningInvoker`；JSON 抽取（`_extract_json`／`_find_json_object`／
      envelope 處理）留在共用層。
- [ ] `build_production_planning_runtime()` 接受 `invoker` 參數（預設由
      `resolve_runner_mode` 決定），四個 adapter 與 `_probe_identity` 全部改走它。
- [ ] `probe_agy_capability()` 目前直接吃 `runner`（＝`subprocess.run`）且**繞過**
      `_invoke_json` 的全部防線——把它一併改走 invoker，兩次 CLI 呼叫各算一次 invocation。
- 驗收：既有 planning／probe 測試**一行不改**全綠（這就是「行為零改變」的定義）；
  `git grep -n "subprocess.run" paulsha_cortex/coordinator/planning_runtime.py` 只剩
  `InProcessPlanningInvoker` 內部一處。

### 3. 票 C｜probe 結果快取（對應 R3；依賴票 A 的診斷欄位、票 B 的 invoker）

- [ ] `tests/test_planning_probe_cache_672.py`（TDD RED）：
  - `test_cache_key_includes_job_runner_mode`：`PSC_JOB_RUNNER` 由 `direct` 改
    `systemd-template` ⇒ 快取必失效（**這條是本票最重要的不變式**）。
  - `test_cache_invalidated_by_executor_binary_fingerprint`：可執行檔 mtime／inode 改變 ⇒ 失效。
  - `test_cache_invalidated_by_template_unit_fingerprint`：模板 unit 檔改變 ⇒ 失效
    （＝任何讓 operator 重跑產生器的改動都自動全重探）。
  - `test_cache_invalidated_by_credential_fingerprint`：憑證檔 mtime／size 改變 ⇒ 失效。
  - `test_cache_invalidated_by_roster_digest`：overlay 改動 ⇒ 失效。
  - `test_corrupt_cache_is_miss_not_ready`：JSON 壞掉 ⇒ 重探，**絕不**沿用舊 ready。
  - `test_expired_ready_never_served_when_reprobe_fails`：ready 過期且重探失敗 ⇒ not ready。
  - `test_cache_records_failure_diagnostics`：失敗側存 reason／diagnostic／rc／
    stdout 前 200 字／unit／resolved_binary／binary_version。
  - `test_cache_asset_not_in_any_job_unit_rwp`：登記表資產不出現在任一 job 模板 unit
    的 `ReadWritePaths` 產出中。
- [ ] `paulsha_cortex/trust_root/registry.py`：新增資產 `planning-probe-cache`
      （`<coordinator_root>/planning-probe-cache.json`，Manager-owned 0600，
      writers／readers 只有 `Principal.MANAGER`），並補進
      `permgen.RUN_EXTERNAL_DEPENDENCIES` 的雙向封閉（否則
      `unlisted_roster_entries()` 的測試會紅）。
- [ ] `paulsha_cortex/coordinator/planning_probe_cache.py`（新檔）：指紋計算、讀寫、
      TTL、fail-closed 語意。
- [ ] `build_production_planning_runtime()` 改成先查快取、miss 才 probe。
- 驗收：direct 模式下連續兩次建構 runtime，第二次的 executor 呼叫次數為 0；
  改動任一指紋輸入後第二次必須重探；`python3 -m pytest tests/ -q` 全綠。

### 4. 票 D｜permgen 把 planner 憑證面 codify（對應 R5；依賴 U-4／U-5／U-7 裁決）

- [ ] `tests/test_trust_root_planner_credentials_672.py`（TDD RED）：
  - `test_reviewer_planner_credential_is_registered`：登記表有
    `reviewer-planner-executor-credential`，且列在 `IN_PLACE_CONTENT_WRITE_ASSETS`。
  - `test_reviewer_template_rwp_includes_credential_file_not_parent`：reviewer 模板
    unit 的 `ReadWritePaths` 含**檔案本身**，不含父目錄。
  - `test_two_way_scheme_unaffected`：二分方案下該資產經
    `inapplicable_home_anchored_assets()` 機械排除，Manager unit 的 RWP 不多出
    不存在的路徑（＝#640 當年不敢登記的那個理由已被 #671 拆掉）。
  - `test_deferred_dependencies_shrink`：`deferred_run_dependencies()` 不再含
    `reviewer-planner-executor-credential`、`reviewer-planner-codex-hooks`、
    `manager-claude-credential` 三項（第三項因為本票的裁決是「planning 一律走降權
    job」，Manager 不再需要 claude 登入態）。
- [ ] `paulsha_cortex/trust_root/registry.py`：新增憑證資產列；
      `paulsha_cortex/trust_root/permgen.py`：加進 `IN_PLACE_CONTENT_WRITE_ASSETS`、
      移除對應的 `DeferredDependency`、`asset_paths()` 補 `codex-hooks` 的 per-account 版。
- [ ] 依 U-5 的裁決處理 `executor_credential_relpath`（維持單一 vs 擴成
      per-(account, executor) 表）。若裁決是擴表，本票範圍會顯著變大，應再切一張子票。
- [ ] 依 U-7 的裁決處理 agy 狀態樹（symlink 資產 vs env 導向 `cache`）。
- [ ] runbook `docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 4e 步：
      補「逐帳號 × 逐 executor」的反向驗證矩陣（#668 的 C 項），並固定部署順序
      「憑證落位 → 重跑產生器 → daemon-reload → `systemctl status` 確認 unit 起得來」。
- 驗收：`python -m paulsha_cortex.trust_root permissions` 產出的 reviewer 模板 unit
      逐字含憑證檔那一條 RWP；二分方案的產出不含它；deferred 清單縮短且測試釘住。

### 5. 票 E｜`JobPlanningInvoker`（對應 R1／R2／R4／R7／R8；依賴票 B、票 C、PATH 前置票）

本票是整個搬遷的主體，也是唯一會改變執行身分的一張。

- [ ] `tests/test_planning_job_invoker_672.py`（TDD RED）：
  - `test_role_is_review_and_not_derivable_from_spec`：角色恆為 `JOB_ROLE_REVIEW`，
    且 job spec 不含任何身分／剖面欄位（`SPEC_FORBIDDEN_KEYS` 兩端各擋一次）。
  - `test_profile_comes_only_from_executor`：剖面唯一輸入是 `identity.executor`；
    未登記 executor（例如 `cg`）fail-closed。
  - `test_planning_wrapper_has_no_gate_bundle_verdict_sentinel`：planning 的 wrapper
    只有模型 argv 一段（不靠既有旗標碰巧為 None）。
  - `test_instance_name_unique_per_invocation`：questioner／secondary／integrator／probe
    的 instance 名互不相同且過 `JOB_SEGMENT_RE`。
  - `test_timeout_stops_unit_and_classifies_as_timeout`：逾時 ⇒ 發 `systemctl stop`
    ⇒ 落 `planning-job-timeout`（`environment`）⇒ 確認 unit 離開 active。
  - `test_job_start_failure_maps_to_job_start_failed`：`JobRunnerError` 各族 ⇒
    `planning-job-start-failed`。
  - `test_silent_rc1_maps_to_executor_silent_exit`：rc=1、stdout／stderr 皆空 ⇒
    `planning-executor-failed / executor-silent-exit`，reason 指名 unit／剖面／
    resolved_binary（＝這一類「連錯誤訊息都沒有」的失敗不得再被誤報成拓撲問題），
    並帶 `permgen.seccomp_filter_is_fatal()` 的結果——「該不該懷疑 seccomp」現在有
    機械答案（PR #677），不得讓下一個人再猜一次。
  - `test_malformed_output_maps_to_output_malformed`：rc=0、輸出非 JSON ⇒
    `planning-output-malformed`，detail 帶 stdout 前綴。
  - `test_operator_tree_not_in_job_rwp`：`repo-source-tree` 不在 reviewer 模板 unit 的
    RWP 產出中（＝ D-e 的 kernel 層保證有測試釘住，不只靠 unit 註解）。
- [ ] `paulsha_cortex/coordinator/planning_job.py`（新檔）：`JobPlanningInvoker`。
      復用 `job_runner.prepare_systemd_template`／`build_job_spec`／`write_job_spec`／
      `build_systemctl_start_argv`／`build_manager_exit_recorder_argv`／
      `confirm_template_instance_started`；**不複製**任何一份 job_runner 的邏輯。
- [ ] 依 U-1／U-2 的裁決實作 scratch（空 vs 複製；job 可寫 vs 唯讀＋`PrivateTmp`）。
- [ ] 依 U-3 的裁決處理 codex 的第二輸出候選。
- [ ] 實機驗收矩陣（R8）：`{codex, claude, agy} × {實際剖面}`，在**真實 unit 的完整
      property 集合**下實跑，每格記錄 rc、stdout 前 80 字、**解析到的絕對路徑與版本
      字串**。矩陣 MUST 走 runbook 第 4e 步的共用探針 `psc_run_under`（其加固面由
      `permgen.unit_replica_properties()` 全量導出），MUST NOT 自行組 `--property=`
      清單（design D13）。本 repo 已有四個手抄子集的事故，**兩個方向都出現過**
      （#638／#657 假綠，#673 原 body 與其 repro 假紅）。
- 驗收：矩陣全綠；`cortex-manager` 的行程樹在一輪完整 planning 期間不出現任何
      executor 可執行檔（以 `systemd-cgls` 或 `ps --ppid` 佐證）。

### 6. 票 F｜切換、宣稱更正與收尾（依賴票 D、票 E）

- [ ] 生產部署切換：確認 `PSC_JOB_RUNNER=systemd-template`（已是），重啟 daemon，
      跑一輪真實 define 並收集 evidence。
- [ ] 更正既有宣稱（#672 明列，且屬「不得順手宣稱」紀律）：
  - `docs/superpowers/runbooks/trust-root-phase2b-setup.md`：把「reviewer/planner
    啟動面降權完成」改成分述，並在本票落地後才改回「兩者皆已降權」。
  - #615 的 M2 完成宣稱：補一條註記說明它當時只涵蓋 reviewer。
  - `job_runner.py` 的 `JOB_ROLE_REVIEW` rationale：把「在此之前它們仍在 Manager
    行程內」的時態改掉（那句話對 planner 一直是現在式）。
  - D6 的全稱宣稱（「三分已生效」）：在本票落地前對 planner 這一支不成立。
- [ ] `permgen.deferred_run_dependencies()` 的 `manager-claude-credential` 移除，
      並在 CHANGELOG 記錄「裁決＝planning 一律走降權 job」（那正是該項
      `disposition` 給的兩條路之一）。
- [ ] 若 U-2 的裁決是 (1)（接受 R-1 退步），該退步 MUST 逐字寫進 runbook 與
      CHANGELOG，不得只活在程式註解裡。
- 驗收：一輪完整的 define → build → review 在四分部署上跑通；`cortex status` 的
      `attention` 不再含 `no-heterogeneous-planner`；R8 矩陣與 runbook 的反向驗證
      逐條有實測輸出。

### 7. 交付要件（本設計票 #672 自己的）

- [ ] `docs/superpowers/specs/planner-job-downgrade-spec.md`
- [ ] `docs/superpowers/specs/planner-job-downgrade-design.md`
- [ ] `docs/superpowers/plans/planner-job-downgrade.md`（本檔）
- [ ] `openspec/changes/2026-08-18-planner-job-downgrade/` 三件套 ＋
      `specs/persona-workflow-orchestration/spec.md` delta
- [ ] `changelog.d/672-planner-downgrade-design.md` fragment（R-09 硬性 gate，
      須 **commit** 才進 diff）
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry
- [ ] 帶 PR 上下文執行 `policy_check`，確認 `fail: 0`
- [ ] `python3 -m pytest tests/ -q` 全綠（本票不改 code，應與 base 相同）
- [ ] **不含任何 `paulsha_cortex/` 的程式修改**——`git diff --stat origin/main` 只有
      `docs/`、`openspec/`、`changelog.d/`、`CHANGELOG.md`

## 票的依賴順序

```
PATH 前置票（唯一待開的前置）──────────────────┐
                                               ↓
票 A（拒因表／三分）→ 票 B（invoker 抽象）→ 票 C（probe 快取）→ 票 E（job invoker）→ 票 F（切換與更正）
                                                                  ↑
U-4／U-5／U-7 裁決 → 票 D（permgen codify）───────────────────────┘

（加固面驗證機制＝PR #677 已落地；票 E 消費 `psc_run_under`／`unit-replica`，非前置。）
```

- 票 A、票 B、票 C 可在 direct 部署上獨立 land 並各自產生價值（見 design D11）。
- 票 D 與票 E 沒有硬性先後，但票 D 應在切換前完成，否則「這台機器可以、下一台不行」
  會變成一條沒有票追蹤的隱性狀態。
- 票 E land 之後，生產切換是**一次到位**：不能一半 planning 走 job、一半走 in-process
  （probe 快取的指紋含 `PSC_JOB_RUNNER`，混用會讓兩種語意的結論在同一輪並存）。

## 本票不做（範圍切分給後續實作票）

- 不修改 `paulsha_cortex/` 下任何程式。
- 不改剖面表、不改 unit 的加固項（現行 `EXECUTOR_HARDENING_PROFILE` 實測可用）。
- 不重做 #670——其本體已由 PR #674 修好；本票只補「診斷活著抵達上游」那一層。
- 不改 unit 檔、不動部署、不跑任何會改變 `/var/lib/cortex` 狀態的指令。
- 不做非同步 planning 狀態機（design D12）。
- 不做 provider 層的獨立（每個 job 帳號各自的 provider 帳號）。
