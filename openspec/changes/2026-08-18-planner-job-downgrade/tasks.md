---
status: draft
work_item: planner-job-downgrade
---

# Tasks

design-doc 票（結構性搬遷，直接開工很可能產出一個大而錯的 PR；比照 #210／#275／#279
既有慣例以設計文件交付），非 code TDD RED/GREEN 主體；實作切分成六張後續票，
依賴順序見 `docs/superpowers/plans/planner-job-downgrade.md`。

- [ ] 1.1 `proposal.md`／`design.md`／`specs/persona-workflow-orchestration/spec.md`
      三件套完整，且與 `docs/superpowers/specs/planner-job-downgrade-{spec,design}.md`
      內容一致（openspec 三件套為摘要、docs/superpowers 為完整論證，兩者不得互相矛盾）。
- [ ] 1.2 `docs/superpowers/specs/planner-job-downgrade-spec.md` 的 R1–R8 逐條對應
      issue #672 原文的五件事（一次性 sandbox 的等價重建、probe 成本與快取、剖面路由、
      憑證、錯誤語意），且每條有 `origin/main` 上的具體改動錨點。
- [ ] 1.3 design 的 D2 給出十條防線的**逐條**對應表，每條標成等價／升級／退步；
      退步項（R-1、R-2、R-3、R-4）逐條附代價與可避免路徑。
- [ ] 1.4 design 的未決問題總覽（U-1 ～ U-8）逐條列出選項與取捨，**不由本票擅自決定**。
- [ ] 1.5 `docs/superpowers/plans/planner-job-downgrade.md` 的實作切分含六張票、
      依賴順序圖、每張票的 TDD RED 測試名與驗收條件。
- [ ] 1.6 本設計文件經至少一輪 review（人工或 reviewer persona）才可勾完此清單；
      不可自我勾完就 claim done。
- [ ] 1.7 `changelog.d/672-planner-downgrade-design.md` fragment 與
      `CHANGELOG.md [Unreleased]` entry（#672）。
- [ ] 1.8 `python3 -m pytest -q` 全綠（本票不改 code，應與 base 相同）；
      帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。
- [ ] 1.9 `git diff --stat origin/main` 只含 `docs/`、`openspec/`、`changelog.d/`、
      `CHANGELOG.md`——**零 `paulsha_cortex/` 變動**（這是本票的硬性限制）。

## 驗收

「做完」＝ 一個沒有本票上下文的人，讀完三份 docs 之後可以：(i) 說出 planner 今天在哪個
帳號上跑、為什麼那是問題；(ii) 逐條說出搬到 job 之後現行十條防線各自由誰保證，以及
哪兩條會退步、代價多大；(iii) 知道哪些事需要 operator 拍板才能開工（U-1 ～ U-8）；
(iv) 照 plan 的依賴順序開出第一張實作票並知道它的 RED 測試該長什麼樣。

## 本票不做（範圍切分給後續實作票）

- 不修改 `paulsha_cortex/` 下任何程式（含 `planning_runtime.py`、`job_runner.py`、
  `launcher.py`、`model_identities.py`、`planning.py`、`manager.py`、`trust_root/**`）。
- 不改剖面表、不改 unit 的加固項。現行 `EXECUTOR_HARDENING_PROFILE` 實測可用
  （八份 unit 全帶 `SystemCallErrorNumber=EPERM`，codex／copilot 在 `jit` 剖面 rc=0），
  planner 上 job **不依賴 #673**——該 issue 原 body 的前提已由開票者更正撤回，
  且 #673 已由 PR #677 以「不放寬任何 syscall」收尾。
- 不重做 #670——其本體已由 PR #674 修好（`strip_code_fence()`／`stdout_excerpt()`／
  `agy models` 兩欄漂移）；本票只補「診斷活著抵達 blocking reason」那一層。
- 不改 systemd unit、不動部署、不跑任何會改變 `/var/lib/cortex` 狀態的指令。
- 不裁決 U-1 ～ U-8（那是 operator 的）。

## 後續應拆分的 code 票（本 plan 已定案，此處只列摘要）

1. **票 A｜錯誤語意三分 ＋ 逐候選拒因表**（不依賴任何部署面改動；與 PR #674 接得起來）。
   驗收：`no-heterogeneous-planner` 的 reason 可用正規表示式釘住必含拒因表；
   既有 planning 測試一行不改全綠。
2. **票 B｜`PlanningInvoker` 抽象**（純重構、行為零改變）。
   驗收：既有 planning／probe 測試一行不改全綠；`planning_runtime.py` 內
   `subprocess.run` 只剩 `InProcessPlanningInvoker` 一處。
3. **票 C｜probe 結果快取**（依賴 A、B）。
   驗收：連續兩次建構 runtime 的第二次 executor 呼叫次數為 0；改動任一指紋輸入必重探；
   快取損毀一律視為 miss（不得沿用 ready）。
4. **票 D｜permgen 把 planner 憑證面 codify**（依賴 U-4／U-5／U-7 裁決）。
   驗收：reviewer 模板 unit 的 RWP 逐字含憑證**檔案**（非父目錄）；二分方案產出不含它；
   `deferred_run_dependencies()` 縮短且測試釘住。
5. **票 E｜`JobPlanningInvoker`**（依賴 B、C、PATH 前置票）。
   驗收：`{codex, claude, agy} × {實際剖面}` 矩陣在**真實 unit 的完整 property 集合**下
   全綠——加固面走 PR #677 的 `psc_run_under`／`unit_replica_properties()`，不得自行組
   `--property=` 清單（D13），每格記錄解析到的絕對路徑與版本字串；一輪完整 planning
   期間 `cortex-manager` 的行程樹不出現任何 executor 可執行檔。
6. **票 F｜切換、宣稱更正與收尾**（依賴 D、E）。
   驗收：四分部署跑通一輪 define → build → review；runbook 與 #615／D6 的既有宣稱
   逐條更正；`deferred_run_dependencies()` 的 `manager-claude-credential` 移除。

另需獨立開票一張（本票查證時發現，不屬 planner 範圍、但比 planner 更廣）：

- **`PSC_REVIEWER_PATH` 未在部署 EnvironmentFile 宣告**，導致今天的 reviewer job 與未來
  的 planner job 都拿到 PID 1 的預設 PATH（`claude`／`agy` rc=127、`codex` **安靜地**解到
  系統層 `0.42.0` 而非 toolchain 的 `0.147.0`——不會失敗，只是產出來自舊 CLI）。

（原本另列的「runbook 加固面驗證改機械讀取」已由 **PR #677** 完成，不需開票。）
