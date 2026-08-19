- **#716（選項 F）：sandbox mode 由**卡片契約**機械導出，不由 persona 一刀切；#715 的探針是假綠。**
  `#714`／PR #715 落地之後 builder **仍然**一條命令都跑不了，逐字
  `permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock`。
  病因是 argv 上的 **`--sandbox workspace-write`**：codex 由它導出 `:workspace` 族
  permission profile，該族要求 direct runtime enforcement，而 legacy landlock 路徑不實作它
  （`linux-sandbox/src/linux_run_main.rs:318` fail-closed）。判準是一條**性質**而不是某個具名
  profile——**profile 只要攜帶任何 filesystem 寫入授權就要求 direct runtime enforcement**
  （`-P` 實測：`extends=":read-only"` rc=0／`":none"` rc=0／`":workspace"` panic／
  `":read-only"` 加一條 `filesystem={"<路徑>"="write"}` **panic**）。這是 **session 層級**判定，
  發生在任何命令執行**之前**，所以唯讀 `git rev-parse HEAD` 與寫入命令 panic 得一模一樣。
  而 `build_codex_argv` **完全不看 `commit_policy`**（`read_only` 是 launcher 維度
  `as_read_only()`，builder 一律走 `else`）⇒ 一張 `commit_policy=forbidden` 且
  `declared_outputs` 為空的唯讀 build 卡拿到寫入授權——**那是獨立成立的最小權限缺陷**，
  legacy landlock 只是讓它從靜默變成當場 panic。
  **修法**：新增 `registry.SANDBOX_MODE_DERIVATION`（五種 `JobWriteContract` × mode ×
  `grants_filesystem_write` × 量測 note），mode 由 `derive_job_write_contract()` ＋
  `sandbox_mode_for()` 兩步機械導出，**import 當下全覆蓋斷言**（漏一格模組載不起來、
  導出函式對整個布林定義域窮舉、planner／reviewer 恆為 `read-only`、`grants_filesystem_write`
  與 mode 矛盾即 fail）——形狀比照 #708 `JOB_LOG_SPOOLS`／#710 `JOB_WORKSPACE_REACH`／
  #712 `JOB_GIT_WORKSPACE_TRUST`，「只修一格」結構上做不到，`build_codex_argv` 裡**沒有**
  第二個 `if`。**保守方向**：只有 `commit_policy` 逐字 `forbidden` **且** `declared_outputs`
  為空序列才降；缺欄／型別不對／非空一律維持 `workspace-write`（**不猜**）。
  **planner／reviewer 的 argv 逐字不變**（測試釘住 byte-identical），write-forbidden 的
  build 卡與今天**只差 mode 一個 token**（`--skip-git-repo-check` 刻意不加），
  **job 角色不變**（仍以 `cortex-builder` 起跑，`_is_review_persona()` 三個判準一個沒動）。
  **實測**：`-s read-only` ＋ legacy landlock ＋ `-o <path>` 的真實 `codex exec` → rc=0、
  `turn.completed`、`job.last.json` 有內容 ⇒ harvest 需要的 `-o` 產物在唯讀模式下仍寫得出來。
- **#716 第 4 節：`permgen.build_inner_sandbox_probe` 的假綠已修——它從沒碰過 `workspace-write`。**
  #715 的探針跑 `codex sandbox -- <cmd>` **不帶** `-c sandbox_mode=`，而那時導出的是**唯讀族**
  profile ⇒ 驗到的是 planner／reviewer 的形態。改為驗「**`build_codex_argv` 會發出的每一個
  `--sandbox <mode>`** 在真實加固面下都裝得上內層沙箱」：mode 清單由
  `registry.emitted_sandbox_modes()` **機械導出**（手抄就會再抄成只有 `read-only`，正是原症狀），
  產生器與產出的 shell **各自**先斷言清單含 `workspace-write`、不含就當場停並印出理由；
  每個 mode 都配**負向對照**（不帶旗標必須仍然失敗且逐字
  `bwrap: Can't read /proc/sys/kernel/overflowuid`）。
  ⚠️ **第二種假綠的陷阱寫進註解與 runbook**：`codex sandbox` **忽略 `config.toml` 裡的
  `sandbox_mode`**，只吃 `-c` 覆寫（實測：config 寫 `workspace-write` 不帶 `-c` → rc=0
  什麼都沒驗到；config 空、`-c sandbox_mode='"workspace-write"'` → panic rc=101）
  ⇒ **探針的每一條命令都帶 `-c`**。產生器一行 `--property=` 都不自組、一個 `--setenv=`
  都不帶（D13），沿用既有 `psc_run_under`。
  **⚠️ 落地之後這條探針仍然是紅的，那是誠實狀態**：0819 實跑（jit 剖面、真 worktree
  instance、42 條 property）——`1[read-only]` rc=1 bwrap／`3[read-only]` **rc=0**／
  `1[workspace-write]` rc=1 bwrap／`3[workspace-write]` **rc=101 panic**
  （`linux_run_main.rs:318:9`）／旗標仍在 rc=0。**那條紅逐字代表「#716 的寫入卡那一半未解」**
  （F 只解唯讀卡，票上的 A／B／E 裁決仍要做），**不得**為了讓它綠而放寬判準——#715 就是
  這麼綠的。加固面 diff 為空，八份 unit 逐字不變，零部署動作。
