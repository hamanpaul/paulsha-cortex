# Dispatch Discipline Improve Implementation Plan

> **執行要求：** 依 task 順序在獨立 `wt/dispatch-discipline/<task>` worktree 執行；每個 task 都先取得指定 RED，再做最小實作、跑局部測試並 commit。不得在同一 task 順手實作第 10 節 deferred workstreams。

**Goal:** 沿用 cortex 現有 `JobRegistry → manager → handoff → DAG` 骨架，將 process exit 與可信 completion 分離，只有通過 deterministic verification、必要的異質 exact-HEAD review，且 Candidate 已進入 target branch 時才釋放 downstream。

**Architecture:** `JobRegistry` 仍是 manager 單一 writer 的 atomic JSON state；在同一 state 新增 `slices`，Job 只表示 execution，Slice 表示交付生命週期。verification/review evidence 與 CompletionRecord 使用帶 `schema_version` 的 immutable JSON。manager tick 依序 poll Job、固定 Candidate、驗證、派 reviewer、驗 verdict、確認 target ancestry、先寫 CompletionRecord、最後標記 Slice completed。

**Tech stack:** Python 3.10+、stdlib `subprocess/json/pathlib/tempfile`、既有 PyYAML wrapper、pytest/unittest、git CLI、Bash reaper fixture。

**Source spec:** `docs/superpowers/specs/2026-07-12-dispatch-discipline-improve.md`

**OpenSpec change:** `openspec/changes/dispatch-discipline-improve/`；`tasks.md`只追蹤apply進度，本文件是exact files、RED/PASS commands與commit boundaries的canonical execution plan。

## Scope guardrails

- v1 只支援 `tier: shareable`、preserving-commit merge、單一 configured remote、同一 dependency chain 共用 target branch。
- v1 的 verification runner 只有 typed argv；不經 shell。sanitized env 不等於 network/filesystem sandbox，錯誤訊息與文件不得宣稱已隔離 untrusted code。
- `informational`、`trivial` 可由 `review_policy=not-required` 直接從 verification 進 `verified`；`normative`、`code` 必須 ForeignReview。
- 不做自動 retry/fix-loop、resume、batch integration、automatic rollback、remote override、attention digest、cost routing、journal platform或 cgroup ownership。
- 每個 code task 都要更新 `CHANGELOG.md [Unreleased]`；同一系列可先在 Task 1 建一筆 umbrella entry，後續 task 只在行為實質改變時補充，不重複灌水。
- 每個Task在focused/full gate通過後，必須勾選`openspec/changes/dispatch-discipline-improve/tasks.md`對應編號，並把該檔納入同一commit；未更新apply progress不得宣稱該Task完成。

## Cross-task invariants

1. `Job.exited` 只代表 exit code 0；永遠不能直接滿足 `depends_on`。
2. `GateEvaluation` 每個 reviewer Job 一筆、terminal 後 immutable；stale evaluation 只留 audit。
3. 任何 missing/invalid evidence、exception、unknown schema、identity absence、fetch failure 都 fail-closed 到 `needs_human` 或維持 `verified`，不得轉成 passed。
4. Completion ordering 固定為：atomic CompletionRecord → atomic Slice `completed`。任一步失敗都不能 release downstream。
5. `default_is_satisfied` 必須同時驗 Slice state、CompletionRecord、spec/plan hash 與 remote-tracking target ancestry。
6. 所有 subprocess seam 都能注入；unit tests 不得啟真 agent、fetch 真 remote 或 signal 真 process。

---

### Task 1: 停止 periodic automatic reaper，建立 scoped operator command

**Files:**

- Modify: `paulsha_cortex/coordinator/broker_reaper.py`
- Modify: `paulsha_cortex/coordinator/cli.py`
- Modify: `paulsha_cortex/coordinator/manager_daemon.py`
- Modify: `paulsha_cortex/scripts/reap-codex-brokers.sh`
- Modify: `tests/test_coordinator_broker_reaper.py`
- Modify: `tests/test_coordinator_cli_tick.py`
- Modify: `tests/test_coordinator_manager_daemon.py`
- Create: `tests/test_reap_codex_brokers_script.py`
- Modify: `CHANGELOG.md`

**Step 1: Write the failing Python wiring tests**

新增測試鎖定：

- `tick` 與 manager daemon 預設都不呼叫 reaper，移除 `--no-reap` 反向旗標。
- 新增 `cortex reap-brokers`（top-level `cortex` 會直接透傳 coordinator CLI）且預設 `apply=False`。
- `--apply` 未帶 `--cwd-root` 回 exit 2；帶入時 wrapper argv 同時包含 `--apply --cwd-root <canonical-realpath>`。

Run:

```bash
python3 -m pytest -q \
  tests/test_coordinator_broker_reaper.py \
  tests/test_coordinator_cli_tick.py \
  tests/test_coordinator_manager_daemon.py
```

Expected: FAIL，因 periodic tick 仍預設 apply，且 operator subcommand/cwd-root 尚不存在。

**Step 2: Write the failing shell safety fixtures**

以 `REAP_PROC_ROOT` fake `/proc` 與腳本既有 snapshot seam 建立 cases：同 project candidate、另一 project candidate、cwd 消失、cmdline/parent/start-time 在 apply 前改變。fake killer 只能收到同 root 且 recheck 完全一致的 PID；任何未知值都 skip。

Run:

```bash
python3 -m pytest -q tests/test_reap_codex_brokers_script.py
```

Expected: FAIL，因目前腳本只用 cmdline + parent snapshot，且不要求 cwd root。

**Step 3: Implement the minimum safe reaper path**

- `reap_orphan_brokers()` 預設 `apply=False`；apply 時強制要求 resolved `cwd_root`。
- 腳本解析 `--cwd-root`，以 path-component containment 判斷 cwd，不用字串 prefix。
- signal 前即時重讀 PID/start-time/cmdline/parent/cwd；與候選 snapshot 不一致或讀取失敗就 skip。
- 僅送 `SIGTERM`，不加入 wait/kill escalation。
- 從 `cli.py`、`manager_daemon.py` 移除 periodic reaper wiring；`run_tick` 可暫留注入 seam 供相容測試，但 production path 不再傳入。

**Step 4: Run focused tests and policy-sensitive help tests**

```bash
python3 -m pytest -q \
  tests/test_coordinator_broker_reaper.py \
  tests/test_reap_codex_brokers_script.py \
  tests/test_coordinator_cli_tick.py \
  tests/test_coordinator_manager_daemon.py \
  tests/test_coordinator_cli_flags.py
```

Expected: PASS；negative fixture 沒有任何 signal。

**Step 5: Commit**

```bash
git add paulsha_cortex/coordinator paulsha_cortex/scripts/reap-codex-brokers.sh tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "fix(coordinator): scope broker cleanup to operator command"
```

---

### Task 2: 建立 versioned Job/Slice state foundation 與 clean-start gate

**Files:**

- Modify: `paulsha_cortex/coordinator/registry.py`
- Modify: `paulsha_cortex/coordinator/completion.py`
- Modify: `paulsha_cortex/coordinator/dispatcher.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/coordinator/manager_daemon.py`
- Modify: `paulsha_cortex/coordinator/cli.py`
- Modify: `paulsha_cortex/control/contract.py`
- Modify: `paulsha_cortex/control/client.py`
- Modify: `tests/test_persona_phase2_coordinator_cli.py`
- Modify: `tests/test_coordinator_registry_headless.py`
- Modify: `tests/test_coordinator_headless_completion.py`
- Modify: `tests/test_coordinator_cli_complete.py`
- Modify: `tests/test_coordinator_manager.py`
- Modify: `tests/test_coordinator_manager_daemon.py`
- Modify: `tests/test_control_contract.py`
- Modify: `tests/test_control_client.py`

**Step 1: Write RED state-schema tests**

新增/修改測試要求：

- state root 必須是 `{"schema_version": COORDINATOR_STATE_SCHEMA_VERSION, "seq": ..., "jobs": [...], "slices": [...]}`；測試引用production constant，不另寫magic number。
- 無 state file 為合法 clean-start；缺/未知 `schema_version` 或含 legacy `done` 的 state 明確拒載，錯誤訊息包含檔案路徑與 archive/remove 指引，且不改寫原檔。
- Job statuses 只接受 `dispatched/running/exited/failed`；headless exit code 0 產生 `exited`。
- `SliceRecord` 至少保存 spec/plan path+hash、target branch、dispatch base、builder/reviewer IDs、candidate、state、gate state與 evidence refs。
- state transition validator 拒絕表外轉移；Slice current evidence/evaluation refs與history containers必須可reload。GateEvaluation row/schema在Task 5才建立。
- 移除無spec metadata的低階`cortex dispatch --task ...`；測試它明確拒絕且不寫state。
- `fanout/tick/complete`等既有mutation CLI改成只提交control request，daemon未運行時明確失敗；read-only`jobs/stat/ready/status`可讀atomic snapshot但不得寫。

Run:

```bash
python3 -m pytest -q \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_coordinator_registry_headless.py \
  tests/test_coordinator_headless_completion.py \
  tests/test_coordinator_manager.py
```

Expected: FAIL，因 state 尚無 schema/slices 且仍使用 `done`。

**Step 2: Implement one atomic state owner**

- 將 registry persistence 擴充為 versioned root；沿用 temp file + `os.replace`。
- 提供最小 `create_slice/get_slice/update_slice/record_action` API，不另建 TaskRun aggregate。此 task 先以完整 metadata fixture驗 API；production dispatch 到 Slice 的接線留在 Task 3，避免 parser尚未能提供 target/hash時寫出半套 SliceRecord。
- 在 registry 內集中驗 Job/Slice/Gate 狀態與合法轉移；callers 不直接改 `_jobs/_slices`。
- `classify_completion()` 回 `exited|failed`，dispatcher/poller/manager constants 同步改名。
- 擴充既有control contract讓daemon消費`complete`；CLI永不自行建立第二個mutable JobRegistry writer。

**Step 3: Pin crash/restart behavior**

測試reload後`seq`、Slice current refs與history containers都保持；launch handle缺失只允許`failed`，不得猜測running。確認同一slice不會同時存在兩個active builder；legacy direct dispatch拒絕後state不變。

**Step 4: Run focused state suite**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_control_contract.py \
  tests/test_control_client.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 5: Commit**

```bash
git add paulsha_cortex/coordinator paulsha_cortex/control tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): separate execution and slice state"
```

---

### Task 3: Parse and pin the v1 verification contract

**Files:**

- Modify: `paulsha_cortex/coordinator/autonomy.py`
- Modify: `paulsha_cortex/coordinator/registry.py`
- Modify: `paulsha_cortex/coordinator/dispatcher.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/coordinator/manager_daemon.py`
- Modify: `paulsha_cortex/coordinator/cli.py`
- Modify: `paulsha_cortex/control/contract.py`
- Modify: `paulsha_cortex/control/client.py`
- Create: `paulsha_cortex/coordinator/verification.py`
- Create: `tests/test_coordinator_verification.py`
- Modify: `tests/test_persona_phase4_fanout_autonomy.py`
- Modify: `tests/test_coordinator_manager.py`
- Modify: `tests/test_coordinator_manager_daemon.py`
- Modify: `tests/test_control_contract.py`
- Modify: `tests/test_control_client.py`

**Step 1: Write RED frontmatter contract tests**

覆蓋`target_branch`與`verification.docs_class|required_artifacts|checks|tests|full_suite`。`checks`只允許`persona-scope`與typed-argv `command`，且auto-dispatch必須恰有一個persona-scope與至少一個具名policy command。錯誤/未知型別一律讓該spec `dispatch=hold`並帶structured parse error，不可吞成空verification。文件class決定review policy：`normative/code=required`、`informational/trivial=not-required`。

Run:

```bash
python3 -m pytest -q \
  tests/test_persona_phase4_fanout_autonomy.py \
  tests/test_coordinator_verification.py
```

Expected: FAIL，因 parser 尚不回 target/verification contract。

**Step 2: Implement immutable dispatch inputs**

- parser 只接受 spec 定義的 v1 keys；typed argv 必須是非空字串 list、timeout 為正數、cwd/path 經 normalize 後不可逃出 repo。
- dispatch 前讀取 spec/plan bytes，保存 SHA-256；同時固定 target branch與 verification contract hash。
- builder 後續修改 spec/plan不影響本輪；manager 比對 hash mismatch 時轉 `needs_human`。
- v1 target remote 由 `PSC_TARGET_REMOTE` 取得，未設時用 `origin`；remote name只作為 git argv element，不進 shell。
- production spec-driven `dispatch/fanout/tick`全部經control request交給同一manager writer，依序建立完整`pending` Slice、builder Job，再更新`building`；任一步失敗保留可診斷state且不得出現半套必填metadata。legacy低階direct dispatch維持Task 2的拒絕語意。

**Step 3: Implement evidence schema and atomic writer**

在 `verification.py` 定義 `VERIFICATION_SCHEMA_VERSION`、evidence validator與 atomic JSON write；evidence path 放在 coordinator root 下的 `evidence/verification/`，檔名包含 safe slice ID 與 Candidate SHA。已存在且內容 hash 完全一致可冪等重讀；不一致則隔離並 fail-closed。

**Step 4: Run focused parser/schema tests**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 5: Commit**

```bash
git add paulsha_cortex/coordinator paulsha_cortex/control tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): pin verification contracts at dispatch"
```

---

### Task 4: 固定 Candidate 並執行 deterministic ResultVerification

**Files:**

- Modify: `paulsha_cortex/coordinator/verification.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/coordinator/dispatcher.py`
- Create: `tests/test_coordinator_candidate_verification.py`
- Modify: `tests/test_coordinator_manager.py`

**Step 1: Write RED Candidate fencing tests**

builder Job`exited`後，manager讀branch HEAD並要求`dispatch_base`是Candidate ancestor且Candidate不等於base；v1沒有no-op/already-satisfied proof。無新commit、force-push到非descendant、branch ref在snapshot後偏離Candidate都轉`needs_human`，不得寫CompletionRecord。

**Step 2: Write RED verification runner tests**

以 injected subprocess/git runner 覆蓋：

- required artifact missing、`must_change=true` 但未在 base..Candidate diff、scope violation。
- command missing、non-zero、timeout、runner exception、cwd escape、evidence partial。
- `persona-scope`使用dispatch base persona catalog bytes/hash、builder role與`base...Candidate`changed paths；Candidate修改catalog不影響本輪。
- 每個typed-argv command check、task test與Candidate full suite都須exit 0。
- full-suite在dispatch base與Candidate以相同argv/cwd/sanitized env各跑一次；Candidate non-zero、任一command missing/timeout/signal/runner error/evidence不完整都`needs_human`。base non-zero + Candidate zero可視為改善；兩者都non-zero不可比較且fail-closed。
- informational/trivial 通過後直接 `verified`；normative/code 通過後進 `reviewing`。

Run:

```bash
python3 -m pytest -q \
  tests/test_coordinator_candidate_verification.py \
  tests/test_coordinator_manager.py
```

Expected: FAIL，因 exit 0 現在仍走 completion shadow path。

**Step 3: Implement deterministic checks**

- 使用`subprocess.run(argv, shell=False, cwd=resolved_cwd, timeout=...)`；env只保留原環境中存在的`PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR`、`VIRTUAL_ENV`，contract不得自訂env，不帶token/credential env。
- base full-suite 使用 manager 建立、finally 必清理的 detached temporary worktree；Candidate用已 pin 的 builder worktree/HEAD。
- 先做 hash/Candidate/artifact/scope checks，再跑 task tests，最後 full suite；第一個拒絕仍寫完整 fail evidence。
- manager 只依 validated evidence更新 Slice，不信任 agent log 的 done claim。

**Step 4: Replace false-green regression tests**

把現有「gate false/exception仍 passed」測試改成「verification reject/exception → needs_human、default_is_satisfied=false」。保留一個明確測試證明 Job `exited` 對 DAG 無效果。

**Step 5: Run focused completion suite**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 6: Commit**

```bash
git add paulsha_cortex/coordinator tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): verify candidate results before completion"
```

---

### Task 5: 派送 exact-HEAD ForeignReview 並驗證 immutable verdict

**Files:**

- Create: `paulsha_cortex/coordinator/review.py`
- Modify: `paulsha_cortex/coordinator/registry.py`
- Modify: `paulsha_cortex/coordinator/launcher.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/coordinator/manager_daemon.py`
- Modify: `paulsha_cortex/coordinator/cli.py`
- Create: `tests/test_coordinator_foreign_review.py`
- Modify: `tests/test_coordinator_launcher.py`
- Modify: `tests/test_coordinator_manager.py`

**Step 1: Write RED identity and selection tests**

定義 registry fixture：`(executor, model_id) -> independence_domain`。要求 builder與reviewer都具 explicit model ID；不同 CLI 但同 domain 仍為 absent，unknown identity亦 absent，只有不同 domain 才能建立 reviewer Job。v1 遇非 shareable tier直接 `needs_human`。

**Step 2: Write RED exact checkout/verdict tests**

要求manager建立detached reviewer worktree at exact Candidate、Job row保存kind/executor/model/domain/subject HEAD，並驗證verdict：schema version、reviewer/builder Job IDs、Candidate SHA、launch identity與findings schema。category enum固定為`correctness|acceptance|security|data-loss|race|scope-bypass|verification-bypass|style|pre-existing-out-of-scope`；severity為`critical|important|minor`，每筆必含summary/evidence list/recommendation。manager由canonical category+evidence location+summary算SHA-256 finding ID。verdict自稱的identity不能覆蓋launch metadata。

覆蓋 stale HEAD、malformed JSON、missing provenance、reviewer process failure、blocking finding與只有 non-blocking style finding。

Run:

```bash
python3 -m pytest -q \
  tests/test_coordinator_foreign_review.py \
  tests/test_coordinator_launcher.py \
  tests/test_coordinator_manager.py
```

Expected: FAIL，因目前只有同步 shadow `GateRunner`。

**Step 3: Implement the minimum model registry and reviewer Job**

- 從 `PSC_PROJECT_CONFIG_ROOT/model-identities.yaml` 讀靜態映射；缺檔/重複 key/unknown fields fail-closed。
- CLI/daemon增加 explicit builder `--model` 與 reviewer `--review-executor/--review-model` 接線；review-required slice缺任一值時 Gate=`absent`。
- reviewer也是 registry Job，`persona=reviewer`、job kind明確；prompt以結構化區塊標示 repo/spec/diff/log皆為 untrusted data，要求只輸出 verdict schema。
- manager啟動前移除detached reviewer worktree內固定的`.psc-review-verdict.json`，prompt要求reviewer只把結果寫到該路徑；process exited後manager讀取、驗schema/provenance，再atomic複製到coordinator root的`evidence/review/`。Candidate中預置檔、缺檔或多餘非JSON輸出都不能成為有效verdict。
- policy在cortex端只使用正式category enum；`correctness/acceptance/security/data-loss/race/scope-bypass/verification-bypass`預設blocking，`style/pre-existing-out-of-scope`預設non-blocking。evidence item固定為`{path,line,detail}`，finding ID以category、summary與排序後evidence的sorted-key JSON取SHA-256；severity/recommendation不參與ID。

**Step 4: Preserve immutable evaluations**

GateEvaluation持久為Slice evaluation history所引用的versioned immutable evidence；每個reviewer Job建立新row，`passed/rejected/absent`後不可改。Candidate/spec/plan/verification hash變動時只清除Slice current ref並記`stale-input` reason，舊evaluation state/content完全不變，operator必須建立fresh reviewer Job。

**Step 5: Run focused foreign-review suite**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 6: Commit**

```bash
git add paulsha_cortex/coordinator tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): require foreign exact-head review"
```

---

### Task 6: 以 target ancestry 與 CompletionRecord 關閉 dependency release

**Files:**

- Modify: `paulsha_cortex/coordinator/completion.py`
- Modify: `paulsha_cortex/coordinator/autonomy.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/coordinator/seams.py`
- Modify: `paulsha_cortex/coordinator/dispatcher.py`
- Create: `tests/test_coordinator_completion_record.py`
- Create: `tests/test_coordinator_dependency_ancestry.py`
- Modify: `tests/test_persona_phase4_fanout_autonomy.py`
- Modify: `tests/test_coordinator_manager.py`
- Modify: `tests/test_coordinator_manager_daemon.py`
- Modify: `tests/test_coordinator_cli_tick.py`
- Modify: `tests/test_persona_phase2_coordinator_cli.py`

**Step 1: Write RED CompletionRecord tests**

CompletionRecord必含schema、slice/spec/plan hashes、builder ID、dispatch base、Candidate、target branch、verification ref、review policy與completed_at。`required`shape必須有non-null reviewer ID/gate ref；`not-required`shape必須兩者為null並有docs class+contract hash導出的proof。混合shape、invalid schema/hash/evidence ref/symlink path均fail-closed。

以injected atomic writer模擬：record write成功後、Slice update前crash。此時`default_is_satisfied=false`；restart須先重新fetch/recheck target ancestry，只有record、verified Slice與current ancestry都匹配才補第二步且不重跑reviewer。mismatch orphan移到quarantine並保持blocked；若remote已移除Candidate則不得補completed。

**Step 2: Write RED git ancestry tests**

以 temporary local bare remote + repo fixtures覆蓋：

- fetch failure時 Slice維持 verified。
- Candidate尚未進 `refs/remotes/<remote>/<target>` 時 blocked。
- preserving merge後 completed；target新增無關 commit仍 satisfied。
- squash/cherry-pick造成 Candidate非 ancestor時 needs_human。
- dependency chain target branch不一致時 scan/dispatch fail-closed。
- readiness後 target移動的 TOCTOU：downstream dispatch前必以 actual base SHA重驗所有 upstream Candidate。

Run:

```bash
python3 -m pytest -q \
  tests/test_coordinator_completion_record.py \
  tests/test_coordinator_dependency_ancestry.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: FAIL，因現行 readiness只看 `gate_status=passed`，worktree base固定字串 `main`。

**Step 3: Implement completion ordering and readiness**

- `verified` tick先 fetch configured remote target，再以 `git merge-base --is-ancestor Candidate remote_tracking_ref`判定。
- atomic寫 CompletionRecord後才 atomic更新 Slice completed。
- 新增`DependencyResolver.resolve(meta) -> ResolvedDependencySet`，回satisfied、target branch、target ref SHA、upstream Candidate list與blocked reasons；`ready_units`回傳meta+同一proof，`dispatch_ready`不得以第二套global lookup重算。
- `default_is_satisfied`若保留為compatibility wrapper，也只能委派resolver並回其`satisfied`；更新CLI、daemon、status provider callsites，不保留只看`gate_status`的旁路。

**Step 4: Pin downstream base SHA**

- `WorktreeCreator.create()`改為接受explicit `base_sha`；從`ResolvedDependencySet.target_ref_sha`取得，不另讀字串`main`。
- 建 worktree/launch前重驗每個 upstream Candidate都是 `base_sha` ancestor；失敗則不建 worktree。
- builder Job與Slice都保存 actual downstream dispatch base SHA，後續 verification沿用同一值。
- 同步更新所有`WorktreeCreator` fake、CLI/daemon wiring與signature tests；Task 6 focused gate必須包含全部`tests/test_coordinator_*.py`，不得把callsite紅燈延後到Task 8。

**Step 5: Run dependency suite**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 6: Commit**

```bash
git add paulsha_cortex/coordinator tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): gate dependencies on merged candidates"
```

---

### Task 7: 提供 persisted local operator actions 與完整狀態說明

**Files:**

- Modify: `paulsha_cortex/coordinator/cli.py`
- Modify: `paulsha_cortex/coordinator/manager_daemon.py`
- Modify: `paulsha_cortex/coordinator/registry.py`
- Modify: `paulsha_cortex/coordinator/manager.py`
- Modify: `paulsha_cortex/control/contract.py`
- Modify: `paulsha_cortex/control/client.py`
- Create: `tests/test_coordinator_operator_actions.py`
- Modify: `tests/test_coordinator_manager_daemon.py`
- Modify: `tests/test_coordinator_cli_flags.py`
- Modify: `tests/test_control_contract.py`
- Modify: `tests/test_control_client.py`

**Step 1: Write RED command tests**

新增 local-only command contract：

```text
cortex slice-action <slice-id> retry-build  --actor <text>
cortex slice-action <slice-id> retry-verify --actor <text>
cortex slice-action <slice-id> retry-review --actor <text>
cortex slice-action <slice-id> abandon      --actor <text>
```

CLI只能透過既有atomic control request queue送`slice-action`，不得直接開`JobRegistry`寫`jobs.json`。測試action/state不合法、缺actor、unknown slice皆由daemon/manager單一writer拒絕；合法action先持久化request，再由manager執行並寫done response。不得直接修改terminal evaluation或復活failed/completed Slice。

**Step 2: Write RED status explanation tests**

status output對每個 Slice顯示：目前 Slice/Job/Gate state、blocked/needs_human reason、Candidate/target ancestry摘要、current evidence refs、允許的下一個 operator actions。多筆 attention事項一次以清單回傳，不逐筆互動提問。

Run:

```bash
python3 -m pytest -q \
  tests/test_coordinator_operator_actions.py \
  tests/test_coordinator_manager_daemon.py \
  tests/test_coordinator_cli_flags.py
```

Expected: FAIL，因尚無 persisted action history/status detail。

**Step 3: Implement explicit retry semantics**

- `retry-build`建立新 builder Job並清除 current candidate/evidence refs，但保留舊 history。
- `retry-verify`只允許可信 Candidate，建立新 verification evidence ref。
- `retry-review`只允許 verification passed，建立新 reviewer Job/GateEvaluation。
- `abandon`只將非 terminal Slice標 failed。
- action record保存 action、actor、requested_at、consumed_at/result；v1只依本機檔案權限，不新增 remote API或 signed override。

**Step 4: Run focused operator suite**

```bash
python3 -m pytest -q \
  tests/test_coordinator_*.py \
  tests/test_control_contract.py \
  tests/test_control_client.py \
  tests/test_persona_phase2_coordinator_cli.py \
  tests/test_persona_phase4_fanout_autonomy.py
```

Expected: PASS。

**Step 5: Commit**

```bash
git add paulsha_cortex/coordinator paulsha_cortex/control tests CHANGELOG.md openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "feat(coordinator): add explicit slice recovery actions"
```

---

### Task 8: Disposable canary、文件與完整 gates

**Files:**

- Create: `tests/test_coordinator_dispatch_discipline_e2e.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-12-dispatch-discipline-improve.md` only if implementation reveals a factual mismatch
- Modify: `CHANGELOG.md`

**Step 1: Add a disposable end-to-end fixture**

在 temporary repo/bare remote、fake builder/reviewer launcher與injected command runner上跑完整 v1 flow，至少覆蓋：

1. exit 0 + missing artifact → needs_human。
2. verification passed + same-domain reviewer → absent。
3. stale verdict HEAD → audit-only，不能 verified。
4. review passed但 Candidate未 merge → verified/blocked。
5. preserving merge + valid record → completed。
6. downstream worktree actual base含 upstream Candidate才 dispatch。
7. CompletionRecord/state crash window restart補完。
8. reaper對另一 cwd root與changed process identity永不 signal。

Run:

```bash
python3 -m pytest -q tests/test_coordinator_dispatch_discipline_e2e.py
```

Expected: PASS；本 task只整合既有行為，不新增 lifecycle state。

**Step 2: Update operator documentation**

README需說明：

- Job/Slice/Gate三層狀態及 `exited != completed`。
- verification frontmatter完整範例與 shareable-only信任邊界。
- model identity config與 foreign判定。
- preserving-commit/同 target branch限制。
- CompletionRecord crash ordering、restart與 clean-start舊 state處理。
- operator actions與「一次列出全部 needs_human事項」的 status用法。
- broker reaper預設 dry-run、`--apply --cwd-root`及 best-effort PID race聲明。

**Step 3: Run full verification**

```bash
python3 -m pytest -q
python3 -m policy_check --repo .
git diff --check
```

Expected: 全部 PASS、policy 0 failure。若 full suite有與本 feature 無關的既有 failure，保存 base/Candidate雙跑證據並停在 `needs_human`，不得自行豁免。

**Step 4: Independent review gates**

- 依 `superpowers:requesting-code-review` 對全 diff review。
- 每次修 review finding後重新 review。
- 最後由獨立 Codex adversarial review 嘗試推翻：false completion、stale evidence、identity spoof、TOCTOU、crash ordering、cross-project reaper safety。
- Critical/Important 未清零前不得 archive/claim done。

**Step 5: Commit**

```bash
git add tests/test_coordinator_dispatch_discipline_e2e.py README.md CHANGELOG.md docs/superpowers/specs openspec/changes/dispatch-discipline-improve/tasks.md
git commit -m "test(coordinator): cover dispatch discipline canary"
```

## Implementation handoff

1. 開始實作前先把本spec、plan與完整`openspec/changes/dispatch-discipline-improve/**`納入feature integration branch的commit；目前未commit的文件不會自動出現在新worktree，也無法於最後archive。再從該branch建`wt/dispatch-discipline/reaper`，只執行Task 1。
2. 每個 task merge/串接到 feature integration branch後才開下一個 worktree，避免多個 agent同時改 `manager.py/registry.py`。
3. Task 2–7 都是 code change，必須各自保有 RED output與 focused PASS output；Task 8才跑 full suite/policy。
4. 完成後依 feature-delivery-pipeline 進 requesting-code-review → verification → archive `dispatch-discipline-improve` → conventional commit/finish branch → Codex adversarial review。
5. 未經使用者明確要求，不 push、不開 PR、不 merge。
