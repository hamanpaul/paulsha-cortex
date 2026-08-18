---
status: accepted
work_item: planner-job-downgrade
---

## Goals

定案「把 planning（define／brainstorm）的模型執行從 Manager 行程內搬到降權 job runner」
這件結構性搬遷的契約面。本票以**設計文件為唯一交付**（比照 #210／#275／#279 的
design-doc 票慣例），**不落地任何 `paulsha_cortex/` 的程式修改**；實作切分成六張後續票，
依賴順序與逐票驗收見 `docs/superpowers/plans/planner-job-downgrade.md`。

## Why

issue #672 的訴求逐點查證：

1. **「planner 從來沒有被降權」——查證屬實。**
   `paulsha_cortex/coordinator/planning_runtime.py:830` `_invoke_json()` 以
   `runner(argv, …)`（預設 `subprocess.run`，同檔 `:1070`）在呼叫端行程內執行；
   `:43` `_planning_argv()` 回傳裸 executor argv（無 `systemd-run`、無 `cortex-job-shim`、
   無模板 unit）；`manager_daemon.py:970`／`:1241` 把該 runtime 接進以
   `User=cortex-manager` 執行的 daemon。reviewer 已藉 `launcher.py:1239 _is_review_persona()`
   接上 job runner，**planner 走的是完全不同的一條 code path**。

2. **「repo 自己已經把這條記成逾期項」——查證屬實，而且有兩處。**
   `job_runner.py:405-408` 的 `JOB_ROLE_REVIEW` rationale 逐字寫「M2（#615）：在此之前
   它們仍在 Manager 行程內以 Manager 帳號執行」；`permgen.deferred_run_dependencies()`
   第四項 `manager-claude-credential` 逐字寫「`planning_runtime` 的 JSON 呼叫在
   **Manager 行程內**直接 exec `claude`（不是派一個降權 job）」，其 `disposition` 給的
   兩條路之一正是「裁決『Manager 不直接跑模型、planning 一律走降權 job』」——本票走那一條。

3. **「搬遷最容易弄丟一次性 sandbox 的防線」——查證屬實，且比 issue 描述的更細。**
   `_invoke_json` 現行有十條防線（含兩次 `_tree_snapshot`、`_copy_planning_sandbox`、
   `_contain_operator_drift` 的備份與三道閘門、`_seed_hermetic_claude_env`、
   codex 的 `-o` 第二輸出候選）。design D2 給出逐條對應表，並把每條標成
   等價／升級／退步，退步兩條（R-1、R-2）附代價與可避免路徑。

4. **「probe 成本不可接受」——查證屬實，而且比預期嚴重。**
   `build_production_planning_runtime()` 對每個 planning-capable identity 各跑一次
   probe，`_probe_identity` 每次做**兩次整棵 repo 的 `copytree`**，`probe_agy_capability`
   是**兩次 CLI 呼叫**；而該函式由 `manager.run_auto_claim_scan()`（periodic tick）
   呼叫。搬到 job 之後每個 probe 就是一個 unit 實例。design D5 定案快取。

5. **「一接上 job unit 就會撞 #673」——查證**不**屬實，該前提已由開票者更正撤回。**
   #673 原 body 主張 `SystemCallFilter=@system-service` 讓 codex／copilot 在全部 job
   unit 下靜默 rc=1。實機複驗（本票獨立確認）：八份 unit（六份 job 模板 ＋ manager ＋
   monitor）**全部帶 `SystemCallErrorNumber=EPERM`**（`cortex-reviewer-job@.service:148`、
   兩份 `-jit` 的 `:162`、`cortex-manager.service:89`），從權限產生器落地的第一個 commit
   起就在；有這一條時被過濾的 syscall 回 `EPERM` 而非 `SECCOMP_RET_KILL_PROCESS`，
   V8 走 fallback ⇒ codex／copilot 照常啟動。真 unit 的**完整** property 集合下實測：
   codex／copilot 在 `jit` 剖面 rc=0，claude／agy 兩種剖面皆 rc=0——**預設派工路徑沒有壞**。
   實際被過濾的是 `pkey_alloc`（`@pkey`），不是原先猜測的 `landlock_*`／`seccomp`
   （`@sandbox`）。誤判來源是 repro 手抄十條 property、漏抄 `SystemCallErrorNumber=EPERM`，
   **比 production 更嚴格**。#673 已由 **PR #677** 以「不放寬任何 syscall」收尾，並
   順帶把「加固面複本全量機械導出」做成程式（`permgen.unit_replica_properties()`／
   CLI `trust_root unit-replica`／runbook 共用探針 `psc_run_under`）。因此 planner 上
   job **不需要任何剖面面的前置修正**，且本設計的驗收面**消費 #677 的機制、不重造**。

6. **本票另外查到一條沒有票的部署缺口。**
   `/opt/cortex/etc/cortex-manager.env` **沒有 `PSC_REVIEWER_PATH`**，Manager unit 也沒有
   `Environment=PATH=` ⇒ 降權 job 的 PATH 是 PID 1 預設
   （`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`）。實測：`codex` 解到
   `/usr/bin/codex`＝`codex-cli 0.42.0`（toolchain 那份是 `0.147.0`），`claude`／`agy`
   **不存在** ⇒ rc=127。reviewer 模板 unit 的註解已預先寫過這條該怎麼修，但那一行從未
   落到 env 檔裡。**這條同時影響今天的 reviewer job**，應獨立開票（見 plan 第 0 節）。

7. **「錯誤語意要能三分」——查證屬實，且 #670 已經是一次實例（上游那一層 PR #674 沒碰）。**
   `select_secondary_planner()` 回的是一個沒有任何附加資訊的字面值
   `no-heterogeneous-planner`，候選被跳過的真正理由全被 `continue` 吃掉。#670 的
   code fence 偽失敗因此被報成拓撲問題。**PR #674 已把 probe 那一端修好**
   （`strip_code_fence()`／`stdout_excerpt()`／`agy models` 兩欄漂移），但那份診斷仍會在
   `select_secondary_planner` 被吃掉——design D8 把 `no-heterogeneous-planner` 從「結論」
   改成「結論 ＋ 逐候選拒因表」，讓診斷活著抵達 blocking reason，這一類誤報因此結構上
   不可能。兩者剛好接得起來，缺任何一半都還是查不出原因。

## What Changes（設計層級，零 code）

- **定案 R1**：`PSC_JOB_RUNNER` ∈ {`systemd-run`, `systemd-template`} 時，planning 的
  四個 adapter 與**所有 probe** 經 `JOB_ROLE_REVIEW` 的降權 job 執行；`direct` 逐字不變。
  執行後端的選擇只有一個輸入（`job_runner.resolve_runner_mode`），**刻意不新增第二個開關**。
- **定案 R2**：十條防線的逐條對應表（design D2）。operator 樹保護由「事後偵測」升級為
  「mount 層不可寫」；「sandbox dirty 偵測」明確禁止由被驗方自證，兩條可行路徑列為 U-2。
- **定案 R3**：probe 結果進 Manager-owned durable 快取；指紋涵蓋 `PSC_JOB_RUNNER`、
  executor 可執行檔、憑證檔、加固剖面與**模板 unit 檔本身**、roster 內容雜湊；
  一律 **fail-closed**（絕不因「上次是 ready」而在無法重探時沿用）。
- **定案 R4**：剖面由 `prepare_systemd_template(executor=…)` 單一判定點取得，
  planning 側零對應表。**現行 `EXECUTOR_HARDENING_PROFILE` 實測可用**（codex／copilot →
  `jit`，claude／agy → `strict`），planner 上 job 不需要任何剖面面的前置修正。
- **定案 R5**：planner 帳號的憑證面由登記表機械導出（沿用 #671 已建立的
  `IN_PLACE_CONTENT_WRITE_ASSETS` 與 `inapplicable_home_anchored_assets()`，**不重造**）。
- **定案 R6**：失敗三分 ＋ `executor-silent-exit` 子類 ＋ 逐候選拒因表 ＋
  environment 級拒因的分類改判。
- **定案 R7**：逾時由 Manager 側 `wait(timeout)` → `systemctl stop` 強制終止。
- **定案 R8 ＋ design D13**：凡宣稱「某 executor 在某加固剖面下可用／不可用」，驗證環境
  MUST 由**已落檔 unit 機械讀出全部 property** 再複製，MUST NOT 手抄子集。**該機制已由
  PR #677 落地**（`unit_replica_properties()` 契約「全帶，不選」、少任一加固鍵即
  `UnitReplicaDriftError`；CLI `trust_root unit-replica`；runbook 共用探針
  `psc_run_under`），本設計要求**消費它**——實作票的矩陣不得自行組 `--property=` 清單。
  規則的理由記在設計裡，因為 planner 是**下一個**會做這種宣稱的地方：判準**雙向**，
  偏寬得假綠（#638／#657），偏嚴得假紅（#673 原 body 與其 repro）——四個實例、兩個方向，
  因此規則不是「防不夠嚴」，而是「驗證環境 ≠ production 環境在結構上不可能」。
  矩陣每格另記「解析到的絕對路徑 ＋ 版本字串」（「解析到非預期版本」不會表現為失敗）。
- **消費 #677 的第二個維度**：seccomp 過濾語意是**剖面之外**的維度
  （`PROFILE_LOCKED_KEYS` 兩剖面逐字相同、`filtered_syscalls`／`filtered_syscall_surfaces()`
  ／`seccomp_filter_is_fatal()`）。兩個直接影響：R3 的快取指紋 MUST 含**模板 unit 檔本身**
  （只放剖面名涵蓋不到第二維）；R6 的 `executor-silent-exit` 診斷 MUST 帶
  `seccomp_filter_is_fatal()` 的結果——#673 走偏正因為當時沒有任何地方回答得了它。
- **本票唯一落地的產物**：`docs/superpowers/specs/planner-job-downgrade-{spec,design}.md`、
  `docs/superpowers/plans/planner-job-downgrade.md`、本 change 三件套與 delta spec、
  `changelog.d/672-planner-downgrade-design.md`、`CHANGELOG.md` entry。

## Capabilities

### Modified Capabilities

- `persona-workflow-orchestration`：`不完整規格必須經異質雙模型brainstorm` 這條
  requirement 的 contract delta——planner subprocess 的執行身分、一次性 sandbox 的
  等價重建、probe 快取、以及 `no-heterogeneous-planner` 必須攜帶逐候選拒因。
  詳見 `specs/persona-workflow-orchestration/spec.md` 的 MODIFIED／ADDED Requirements，
  與 `docs/superpowers/specs/planner-job-downgrade-spec.md` 的完整 R1–R8、
  `docs/superpowers/specs/planner-job-downgrade-design.md` 的 D1–D12、
  未決問題總覽（U-1 ～ U-8）與安全退步總覽（R-1 ～ R-4）。
