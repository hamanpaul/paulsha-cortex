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
