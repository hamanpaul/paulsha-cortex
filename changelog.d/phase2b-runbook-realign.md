# phase2b-runbook-realign

- **trust-root Phase 2b runbook 與 M1 實機對齊：十三處逐條修正（docs-only，
  不改任何一行程式碼、不動部署）**——來源是 #584 M1（2026-08-17 實機執行）
  逐步對照 `docs/superpowers/runbooks/trust-root-phase2b-setup.md` 累積的落差。
  **沒有一條是安全破口**，但每一條都會讓下一個執行者卡住或誤判。

  - **A. 舊佈局殘留（#616 已修 permgen，runbook 文字沒跟上）**
    - 第 1 步 `useradd --home-dir` 由 `/var/lib/cortex-svc` 改為
      `/var/lib/cortex-manager`；「⚠️ 目前已知缺口 (a)(b)」整段刪除，改列
      `scaffold_directories()` 的實際八行期望值（HOME 由 `PathLayout.home_of()`
      機械導出，不再有二分時代的字面量）。
    - 第 1 步的四行手動 `install -d`（替 `cortex-reviewer-planner` 補
      HOME／`.codex`／`cache`）**整段刪除**——#616 後 scaffold 已自動涵蓋第三個
      帳號；改為一段「不得再手動建目錄」的說明。
    - 第 2 步稽核 3 的 `grep -c "cortex-svc" p2b-scaffold.sh` 期望由 **2** 改為
      **0**，並新增一條「三個 HOME 路徑與帳號名逐字對應」的正向稽核。
    - 第 2 步驗證的 `ls -ld /var/lib/cortex-svc` 改為 `/var/lib/cortex-manager`；
      第 2／9 步回滾命令中的 `cortex-svc` 一併改名。

  - **B. job-spec spool 路徑改版（runbook 仍停在 `run.sh` 時代）**
    - 5-3 表格／5-6 正向 smoke／8a 攻擊腳本／8c NC5 的
      `/var/lib/cortex/jobs/<id>/run.sh` 全部改為登記表資產 `job-spec-spool` 的
      實際契約 `<coordinator_root>/job-specs/<instance>.json`，且**一律用
      `job_runner.build_job_spec()`／`write_job_spec()` 產生**（手捏欄位會被 shim
      的白名單 schema 擋掉）。連帶修正三件原本會誤導執行者的事：job 的輸出在
      **spec 的 `log_path`** 而非 journal（shim 在已降權之後接管 stdout/stderr）；
      job 的 env **完全等於** spec 的 `env`、不繼承 unit 的 `Environment=`
      （`PATH`／`HOME` 必須在 spec 內給）；token 的保證是「名字根本進不了 spec」
      （寫入端 `CREDENTIAL_ENV_RE`／`DENIED_ENV_NAMES` 直接 raise）而非執行時 scrub。
    - 第 2 步的 `ls -ld /var/lib/cortex/jobs`（期望 `0711`）刪除——該資產不在
      登記表、目錄不會被建立；改為驗 spool 的 `0700 cortex-manager` ＋ builder
      唯讀 ACL，並把「builder 讀得到／寫不進去」拆成兩條分別驗。
    - 「產生器＝單一真相」段的兩項 `⏳ 待 #603 follow-up` 改為已落地
      （`trust_root shim three-way`、`build_job_spec()` 契約），並補上 spec 必填／
      禁用欄位的自檢；第 6 步升級流程補回 shim 的 `diff` 對齊。

  - **C. R9 的三條期望與實際設計矛盾（會讓執行者把設計誤判為破口）**
    - **T1.1 由「讀 EnvironmentFile 期望 denied」改為測寫入面**（追加／截斷／
      symlink 換掉／刪除四式）。登記表 `runtime-bootstrap-env` 的 rationale 明寫
      「對全部 headless **唯讀**」、mode 就是 `0644`——**可讀是設計**；該守的是
      「改不了 `PSC_*` 就重導不了整棵 durable state」。原本的讀取測項改由新增的
      `d()` 助手記為 `readable (BY DESIGN)`，讓設計契約也有實測背書。
    - **T1.5 由「枚舉 job-spec spool 期望 denied」改為測寫入四式**（建立／追加／
      symlink 換掉／刪除）。template unit 的 `User=cortex-builder` 由 systemd 在
      `ExecStart` **之前**套用，shim 本身即以 builder 身分讀 spec，**讀不到就起不了
      job**。讀取面的期望改為**依 subject 而異**（builder 讀得到；
      reviewer-planner 被拒——三分在檔案層生效的直接證據）。5-3 表格同步改寫：
      刪掉不精確的「job 只讀自己那格」，改為「守的是寫入面」，並明確註記
      **per-job 讀隔離需 per-job UID，不在本方案範圍**。
    - **T2 delete jobs.json 的 `rm -f` 改為 `rm`**（`-f` 對不存在的檔回 0，在乾淨
      新樹上是必然的假陽性），並新增 `need()` 前置守衛與一段 operator 預建目標檔的
      步驟——「刪不掉」必須是「權限被拒」，不能是「檔不存在」。

  - **D. 族 4 的假綠**
    - `MPID=$(pgrep -u cortex-manager -n -f paulsha_cortex)` **刪除**。job unit 帶
      `ProtectProc=invisible` ＋ `ProcSubset=pid`，job 眼中的 `/proc` 只有自己，
      `pgrep` 必回空 → 後續每一條變成對 `/proc//…` 操作，因「路徑不存在」失敗而被
      記成 denied。**測到的是 pid 不存在，不是權限被拒。** 改由 operator 從外部取
      `systemctl show cortex-manager.service -p MainPID --value`，經 spec 的 `env`
      （pass 1）或 `--setenv`（pass 2）注入；另新增 **T4.0 `test -d /proc/<pid>`**
      直接把「pid 不可見」本身測出來，不讓它偽裝成其他測項的 denial。
      8c 族 4 的 negative control 同樣改用 `MainPID`（原本的 `pgrep -f
      paulsha_cortex` 連 operator 都匹配不到 `cortex service run`，是假紅）；
      pass 2 補上 `ProtectProc=invisible`／`ProcSubset=pid`，兩趟的 `/proc` 語意
      才可比。WSL2 風險段第 3 條同步改寫。

  - **E. 第 4a／第 6 步：pipx venv 遷 `/opt/cortex` 後不可執行**
    - `cp -a` ＋ `chown` ＋ `chmod` 之後 venv 仍有**兩處指回 operator 樹**，M1 實測
      `sudo -u cortex-manager /opt/cortex/venv/bin/cortex --version` 直接
      `Permission denied`：`bin/*` 的 shebang 仍是 pipx 樹的 python、
      `site-packages/pipx_shared.pth` 指向 operator 的 pipx shared site-packages。
      補上**重寫 shebang 前綴**與**移除 `pipx_shared.pth`**兩步（必須在
      `chmod a-w` 硬化**之前**），並說明這同時是**安全條件**——留著等於部署樹的
      第一支被執行的程式與 import path 仍受 operator 可寫目錄影響。另加一條
      涵蓋所有形式的總驗收（`grep -rIl` ＋ symlink 檢查）。第 6 步升級流程的
      `cp -a` 有同樣問題，逐字補上同三步；升級的 hash diff 另附「哪兩類差異是
      這兩步造成的、不是供應鏈訊號」的過濾。

  - **F. 執行前提補兩個硬性 gate**
    - **G1 `acl` 套件**：原本只在第 2 步「稽核 4」提示且失敗不中止；缺 acl 時
      `setfacl` 全數失效、跨帳號授權整段成為**無聲 no-op**，而權限 script 仍以
      exit 0 收場。上移為執行前提的硬性 gate，第 2 步保留一次複驗。
    - **G2 `/etc/sudoers.d/` 萬用 NOPASSWD 規則**：M1 本機原有
      `ALL ALL=NOPASSWD: ALL`，第 1 步新建的三個服務帳號**一建立就自動取得無密碼
      root**，整個降權設計歸零。新增 gate（`grep` sudoers ＋ `sudo -l -U`）與
      「先收斂到具名帳號再建帳號」的順序要求；第 1 步的「三帳號皆無 sudo 授權」
      驗證升級為 `sudo -l -U` 複驗並與 G2 成對。

  - **另補（issue 未列、對照時發現）**
    - **R9 攻擊腳本加身分鎖**：腳本會真的執行破壞性動作
      （truncate、`rm`、`mv "$HOME/.codex"`、覆寫 hooks.json），在沙箱外跑會弄壞
      operator 自己的環境，而且那些「成功」會被誤讀成邊界失守。新增
      `id -un` 白名單（只允許 `cortex-builder`／`cortex-reviewer-planner`），
      並加一條「以 operator 身分跑必須被拒」的驗證。腳本位置一併改為
      root-owned 的 `/var/lib/cortex/r9-attack.sh`（`PrivateTmp=yes` 讓 `/tmp`
      不可用；builder-owned worktree 則 reviewer-planner 讀不到）。
    - **WSL2 段「重啟後仍可起 job」補上重建 spec 的步驟**——8e 已清掉 negctl5 的
      spec 與 worktree，原文直接 `systemctl start` 會因「spec 缺席」而失敗，
      診斷會指向錯誤的層。
    - **#620 的手動繞過收進第 2 步**：permgen 尚未產生父目錄 traverse ACL，
      `coordinator/`／`monitor/` 是 `0700`，POSIX 要求路徑每層都要 `x`，兩條
      append-only 正向路徑與 spool 唯讀路徑因此全斷。補上 M1 實測的三條
      `setfacl -m u:<acct>:--x`（只 traverse、不可列目錄）與驗證，並標明
      **#620 落地後整段刪除**。
    - **M2 的追蹤 issue 由 #603 follow-up 更正為 #615**。
    - **第 4d：monitor unit「裝好但不得啟動」的 gate（#623）**——4d 的安裝與驗證
      全部照做，但 `enable --now` 必須等 #623 關閉。`PSC_DEGRADED_OPERATION=
      per-case-approval` **不會**阻止派工（它只 gate `headless-acceptance`／
      `outbox-mutation`／`ship`／`merge` 四個動作，見 `trust_root/capability.py`），
      因此 monitor 一起來就會真的派 builder job，而那些 job 現在必然失敗
      （`ProtectHome=yes` 讓 repo 不可達、EnvironmentFile 缺八個操作變數），
      後果是燒模型額度、needs_human 噪音、半死的 run 狀態——且**沒有任何一條結構性
      驗收會因此變紅**。4d 的驗證因此拆成「安裝面（不需服務在跑）」與
      「執行面（⛔ #623 關閉後才適用）」兩段，正確終態是 `disabled`／`inactive`。
    - **第 3 步分成兩類處理（3-0／3a-2）**——裁決「legacy-imported 不得滿足任何
      ship gate」針對的是**模型產出的 state／evidence**（`coordinator/**`／
      `monitor/**`／`registry/**`／`runtime/**`）；`config/**`／`specs/**` 是
      **operator 撰寫**的、不是任何 gate 的受檢對象。原本兩類一起關進 quarantine，
      而第 2 步只建 `config/paulsha` **目錄**、沒有任何一步搬**內容**，實測導致
      `cortex monitor --once` 直接 `錯誤: 無 project 設定：…皆不存在`。新增 3-0 的
      分類表與 3a-2 的「明示逐檔複製 ＋ 逐份審閱 ＋ 功能面驗證」段（刻意用白名單而
      非 `cp -r`：要搬什麼是 operator 的決定）。
    - **第 7b 功能面檢查**——M1 的每一條驗收都是結構性的（誰擁有什麼、誰被拒、
      攻擊有沒有失敗），**沒有一條是功能性的**，這正是部署能通過 M1 卻做不了任何
      實際工作的原因。新增三級：F1 `cortex monitor --once` 載得到設定（兩秒鐘，
      是上一條缺口的最短偵測路徑）、F2 `cortex status`／`jobs` 在新樹上答得出話、
      F3 真實 intake 一案跑到 terminal（⛔ #623 關閉前不得執行）。通過條件改為
      「結構面 ＋ F1／F2」，並明訂 F3 通過前不得宣稱「Phase 2b 可用」。
    - **第 2a 稽核 6：script 裡的每個帳號名都要 `getent passwd` 得到（#626）**
      ——permgen 會為本機不存在的 principal（`operator`／`cortex-outbox`）產出
      `setfacl` 條目，而 2b 是 `sh -e`：一條錯就中止整份 script，留下**半套權限的
      樹**（含尾端整個 traverse 節沒套）。稽核 6 在套用前就攔下來，並給出兩條合法
      處置（建帳號／等 #626 修產生器），明確禁止「拿掉 `-e` 硬跑」——那會把中止
      換成靜默略過。2b 另補一段：exit 非 0 時樹是半套的、不要往下驗證，
      且整份重跑是安全的（每條命令皆冪等）。
    - **#620 的手動繞過段落刪除**——PR #624 已把父目錄 traverse ACL 進產生器，
      第 2 步改由 main 的稽核 5 與正／負向驗證守門；「M1 實機基準值」下的註記
      同步改寫為「已收編」，並新增「M1 之後才發現、目前仍未關的兩條」（#626／#623）
      指向各自的步驟標注。
    - **新增「M1 實機基準值」對照表**（自檢 `job_writable_count` 5→0、登記表等式、
      legacy-import manifest 檔數、5-7 十一條全非 0、8a 兩個 subject 的 denied 條數、
      8c 五組 negative control、8d 重啟複驗），讓下一個執行者有數字基準；並在第 8 步
      補一段「判讀紀律」——`SUCCEEDED` 要先確認測項測的是不是該守的那一面，
      `denied` 也可能是假綠。
    - 規模統計同步更新：**32 → 38 個 `🔧 sudo` 點、122 → 134 個 `✅ 驗證`點**。

  相關 issue：#621（本票）、#584（M1）、#616（三分＋template 落地）、
  #620／PR #624（父層 traverse ACL，已落地，本 PR 移除對應的手動繞過段）、
  #622／PR #625（monitor unit，本 PR 加上「裝好但不得啟動」的 gate）、
  #623（monitor 啟動前提＋operator 設定搬遷＋功能面驗收，本 PR 只在 runbook 標注，
  程式與部署面的修正屬該票）、#626（phantom principal 的 `setfacl` 條目，本 PR 只加
  第 2a 稽核 6 這道 operator 側的攔截，產生器修正屬該票）、#615（M2）。
