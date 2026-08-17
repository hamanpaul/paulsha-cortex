# reviewer／planner 啟動面降權（#615，trust-root Phase 2b M2）

M1（#584／#603）之後三分只在**檔案權限層**成立：`cortex-reviewer-planner` 帳號、
HOME、cache、verdict spool 的 `wx` 無 `r` ACL、gitconfig 全部到位，但
`launcher.SubprocessLauncher._degraded_runner()` 只對 builder persona 回 True——
reviewer／planner 的模型 job 仍在 Manager 行程內以 `cortex-manager` 身分執行。
A+B 裁決的核心論述「**injection 可達的進程皆無 spawn 授權**」因此**只對 builder
成立**，而 reviewer 正是寫 verdict 的那一個。

M2 把缺的那一半補上：三個會跑模型的 persona **全部離開 Manager 的 UID**。

## 落地形狀

- **`launcher`**：`_downgraded_mode()` 移除「只有 builder 才降權」那條判斷。persona
  不再決定「降不降權」，只決定**降到哪個角色**（`_job_role()` → `builder` / `review`）。
- **`job_runner`**：新增 `JOB_ROLE_CONFIG` 一張表（角色 → 帳號／group／HOME／PATH／
  模板 unit 的 env 變數名 ＋ 預設值 ＋ 理由），`resolve_job_account()`／
  `resolve_job_group()`／`build_job_env()`／`prepare_systemd_template()`／
  `prepare_systemd_run()` 全部改為查表，**沒有任何 `if role == …` 分支**。未知角色
  fail-closed（落回 builder 是最糟的失敗模式：reviewer 以 builder 身分跑、看起來成功）。
- **`permgen`**：`DOWNGRADED_JOB_PRINCIPALS = (BUILDER, REVIEWER)`。unit 產生器
  **一行都沒有為 M2 改**——`build_job_unit(principal=REVIEWER)` 直接產出
  `cortex-reviewer-job@.service`（`User=cortex-reviewer-planner`），`User=`／HOME／
  cache／`ReadWritePaths=` 全部由 scheme 的帳號映射導出。planner **不另開第三份**：
  它與 reviewer 同帳號，同帳號 ⇒ 同 unit（`JOB_PRINCIPAL_PERSONAS` 把這件事寫成
  機器可讀）。
- **polkit**：沿用 #643 的**單一交替 pattern**擴充字幹段，
  `^(?:cortex-job|cortex-job-jit|cortex-reviewer-job|cortex-reviewer-job-jit)@…$`
  ——**不加第二條 `addRule`**，全檔仍只有一個 `return polkit.Result.YES`。字幹段是
  **兩層列舉**（principal × 加固剖面），前後仍錨定、instance 段字元類一字未改。

## 實作中發現並修掉的一個真缺口

**slice lane 的 foreign reviewer 差一點被以 `cortex-builder` 起跑。** 它走
`manager._spool_writable_launcher()` → `as_verdict_spool_writer()`，而那支工廠產出的
launcher `read_only` 與 `review_only` **都是 False**（verdict spool 放行與 read-only
契約互斥，因為 read-only 的 executor 連 `--add-dir` 都拿不到）。只看那兩個旗標的角色
判定會把它判成 builder——而它正是寫 verdict 的那一個，那等於把 verdict 通道交還給
builder 帳號，抵銷 #638／#639 剛修好的東西。角色判定因此收斂為
`_is_review_persona()` 的**三個**判準，第三條是「**被授予了 verdict spool** 本身就是
reviewer 的標記」。同一個判準也修掉「foreign reviewer 會拿到一格 commit spool 並跑
`git bundle create`」——它從不 commit，那一格永遠是空的，而降權後那一段必定失敗。

## reviewer 的可寫面（由登記表機械導出，非手寫）

恰好兩條：`/var/lib/cortex-reviewer-planner/cache`（HOME 快取，明示 extra）與
`/var/lib/cortex/coordinator/review-verdicts`（登記表資產 `review-verdict-spool`）。
**明確不含** builder 的 per-job clone／worktree pool／commit spool、來源樹（唯讀
ACL）、Manager 的 durable state（coordinator／control／gate ledger／job-spec spool／
monitor state）、部署樹。

新增 `permgen.RETIRED_JOB_WRITE_ASSETS = {"review-verdict"}`：登記表仍完整記錄那項
資產（過渡期 legacy fallback 還要讀它），但降權 job unit **不再為它開寫入面**。它是
spec §3 認定的最短攻擊路徑，Phase 2a 已把權威通道整個換成 spool，今天沒有任何消費者；
而它的路徑是 `<worktree pool>/%i`，reviewer 的工作樹不在 pool 底下 ⇒ 放行反而會讓每個
reviewer job 因「`ReadWritePaths=` 目標不存在」而起不來。除役是**嚴格更緊**的一步。

## 測試

新增 `tests/test_reviewer_planner_downgrade_615.py`（50 測試）：啟動身分不變式、
verdict 通道、reviewer 可寫面的正反面、polkit 新字幹的正向放行與 18 種反向混淆、
四份 unit 的加固表**集合比對**（不硬編）、permgen ⟷ job_runner 的成對契約，以及
#629 邊界的機械守衛。

**#638 的教訓**：兩條在單 UID／寬鬆環境測不出來的語意**明確 skip 並說明理由**
（跨 UID 的 verdict roundtrip、polkit 的實際決策）——不用 `sudo -u`／裸跑當替身，
那只會產生一個永遠綠、什麼都沒驗到的測試。真實驗證在 runbook 第 5-6b／5-7／8b-2 步。

## 刻意不做

**gate 執行身分（#629）不掛在 `cortex-reviewer-planner` 上。** gate 命令在 builder
完全掌控的 worktree 裡執行，`pytest` 會載入該 worktree 的 `conftest.py` ⇒ 執行者取得
任意程式碼執行。掛到 reviewer 帳號會讓被攻陷的 builder 經由 gate 執行影響到寫 verdict
的那個帳號。它需要**第四個帳號**，屬 #629。在那之前降權 build 卡對 `require_ledger`
fail closed。
