### Fixed
- **#698（R9 族 3 的 T3.9 實測攻破）：`cortex-reviewer-planner` 可以植入 codex hooks
  ——operator 裁決採**方案 A（目錄 sticky bit ＋ `hooks.json` 由 root 擁有）**，且**兩個
  帳號的形狀由同一條規則導出**。** 0818 的 R9 抽驗在該帳號上量到
  `T3.9 改 codex hooks :: !! SUCCEEDED (FAIL)`（事後檢查 `cache/codex/hooks.json` 內容
  為 `x`）。成因是 #685 把它的 `~/.codex` 改成指向 job-owned `cache/codex` 的 symlink
  ——那是**必要的**（#686 實測 codex 需要 `$CODEX_HOME` **整棵**可寫才啟動得了），
  代價是那棵樹裡放不住任何 root-owned 的 enforcement 檔。codex hooks **會執行命令**
  ⇒ **跨 job 持久化** ⇒ 四分隔離「每個 job 一次性」的前提在該帳號上不成立，而它正是
  吃 untrusted issue 內容的那一個。
- **新形狀 `CredentialShape.HOME_STICKY_TREE`**：HOME 底下一棵 **root-owned ＋ sticky
  bit（`1755`）** 的真目錄，job 帳號以一條具名 **access** ACL（`u:<帳號>:rwx`）取得整棵
  的寫入權。兩件在 #685 的形狀下互斥的事因此同時成立——(i) 整棵可寫 ⇒ codex 起得來；
  (ii) 樹裡的 `hooks.json`（root:root 0644）**三個動詞全關**：unlink／rename 由 sticky
  擋（非 owner 只動得了自己的檔）、改內容由檔案自己的 mode 擋。
  三個刻意的細節，缺任何一個整個裁決就是空的：
  **(a) 目錄 owner 必須是 root**（POSIX 的 sticky 對**目錄 owner** 免疫）；
  **(b) 只設 access ACL、不設 default ACL**（default 會讓 root 日後補放的 enforcement
  檔自動帶上 job 的 `rwx`）；
  **(c) `hooks.json` 必須先存在**（sticky 不管「建一個還不存在的檔」）——權限計畫因此
  對它出 **create-if-absent**，而不是其他葉檔那條「不存在就跳過」。
- **permgen 的 mode 管線現在表達得了 sticky（#685 記為 U-9 不做的兩個理由之一）**。
  `build_entry()` 尾端的 §R2 安全網從 `mode & 0o700` 改成
  `mode & (STICKY_BIT | 0o700) & ~setuid/setgid`：**§R2 一行未放寬**（group／other 的
  write 位仍無條件清除，job 的寫入權一律走具名 ACL），新增的只有「sticky 通得過」；
  setuid／setgid 則從「被 `& 0o700` 順手吃掉」升級為**明文清除**。另新增
  `OwnerClass.STICKY_SHARED`——它刻意**不是** `DEPLOYMENT`，否則
  「deployment 對全部 headless 唯讀」這句話（多條不變式的依據）會退化成有例外清單。
- **範圍涵蓋兩個帳號，形狀由 `EXECUTOR_ENFORCEMENT_LEAVES` 一條規則導出**：
  「executor 的狀態樹裡必須住著 root-owned 的 enforcement 檔 ⇒ 凡持有它登入態的帳號，
  那一格一律是 `HOME_STICKY_TREE`」。規則由
  `permgen._assert_shape_follows_enforcement_rule()` 在 **import 當下**強制——
  「只修 reviewer-planner 那格、在 builder 上留一個等著爆的差異」因此在結構上做不到。
  `builder` 一併遷移：它當天守得住只是因為 `.codex` 還沒遷成可寫樹，而那個形態的代價是
  **codex 在降權 unit 下根本起不來**（#685 已逐字記錄）。憑證表上 codex 因此只剩**一列**，
  兩個帳號共用它。
- **登記表資產隨之調整**：`builder-executor-credential` → **`builder-codex-state`**
  （形狀從 `IN_PLACE_FILE` 改成 sticky 樹，writer 面加上 `INSTALLER` 才落得到
  `STICKY_SHARED`）；單一的 `codex-hooks` → **`builder-codex-hooks`** ＋
  **`reviewer-planner-codex-hooks`**，由 `enforcement_placements()` 從同一條規則長出來。
  `reviewer-planner-codex-hooks` 因此從 `deferred_run_dependencies()` 的 **U-9 消失**——
  不是刪一列，是那條張力（「codex 起得來」與「樹裡放得住 root-owned 檔」互斥）**真的
  不存在了**。deferred 清單剩一項（`gate-gitconfig`）。
- **mount 層那一道沒有淨退**。`IN_PLACE_FILE` 讓 hooks 同時被 **DAC** 與 **systemd
  mount** 兩層擋住（`IN_PLACE_CONTENT_WRITE_ASSETS` 的整段說明就是這件事）；sticky 樹
  整棵必須進 `ReadWritePaths=`，因此新增機械導出的巢狀
  `ReadOnlyPaths=<HOME>/.codex/hooks.json`（`enforcement_read_only_paths()`）。
  **刻意沒有 `-` 前綴**：目標不存在時 unit 直接起不來——那正是要的行為，因為缺那個檔
  時 job 植得進 hooks，而一個植得進 hooks 的 job 不該起得來。
- **修掉 `planning_probe_cache` 的一份複本（本票的形狀變更才暴露它）**。direct 那一支
  自己拼 `relpath + token_leaf`，而 codex 那一列的樹與葉現在**都**由
  `executor_credential_relpath` 的 head／tail 導出 ⇒ 那份複本會拼出**目錄本身**，指紋
  退化成 `stat ~/.codex`、憑證 refresh 偵測不到。改走
  `PathLayout.credential_token_relpath_of()` 這個唯一來源。
- **`plan_to_commands()` 的兩個部署守衛**（既有部署不是 greenfield）：
  sticky 樹前面加 `[ ! -L <path> ] || { … exit 1; }`——`install -d` 對既有 symlink
  **不報錯也不取代**，它會跟著連結去建目標，把 chown／chmod／setfacl 全部套到
  `cache/codex` 那棵 job-owned 的樹上（**安靜地做錯事**）；enforcement 檔同樣先擋
  symlink，否則 job 預埋一條**懸空** symlink 就能讓 root 的 `cat >` 寫到別處去。
  另外把多行 rationale 逐行加上 `#` 前綴——那份輸出是 runbook 以 `sudo sh -e` 直接執行
  的 script，漏前綴的行會被當成命令解析。

### Changed
- runbook 第 4e-2b 步整段重寫為「兩個 job 帳號的 codex 狀態樹（sticky）」，含**實機遷移
  步驟**（builder 是真目錄、reviewer-planner 是 symlink，兩者順序不同）與逐條驗證；
  agy／claude 的 symlink 形態移到新的 4e-2c。記錄 0818 遷移實際踩到的三個陷阱：
  (1) `install -d` 跟著既有 symlink 走；(2) **`cp -a src/. dst/` 會把來源目錄自己的
  owner／mode／ACL 蓋到目的地**——剛設好的 `root:root 1755 + ACL` 被還原成
  `job 0700`，sticky 與 ACL 一起消失且 `cp` 不會抱怨（因此**先搬內容、後套權限**）；
  (3) `ls -ld` 顯示的 group `rwx` 是 POSIX ACL 的 **mask** 不是 group 寫入權，
  驗證一律用 `getfacl`，且任何一次 `chmod` 都會重設 mask ⇒ chmod 之後必須重跑 setfacl。
- R9 攻擊腳本的 T3.9 從 1 條擴成 3 條（改內容／unlink／rename）——sticky 關的是**三個
  動詞**，只驗「改內容」會漏掉真正靠 sticky 擋的那兩條；並在其前加 `need()` 前置，
  因為那個檔缺席時本項會以「建得出新檔」翻紅（**正確的紅字**，處置是把檔種回去）。
  `need()` 的訊息一併更正為「不存在**或本身分看不到**」——`[ -e ]` 在缺 traverse 權時
  同樣為假，`cortex-reviewer-planner` 對 `<A>/monitor/` 就是這個情形。

### 實測（0818，本機四分部署，完整模板 unit 加固面，`unit-replica` 全量導出）

| 量測 | 修前 | 修後 |
|---|---|---|
| R9 T3.9 `cortex-builder` | `denied (OK) rc=2` | `denied (OK) rc=2`（三個動詞全 denied） |
| R9 T3.9 `cortex-reviewer-planner` | **`!! SUCCEEDED (FAIL)`** | **`denied (OK) rc=2`**（三個動詞全 denied） |
| 族 1–4 `SUCCEEDED` 總數 | builder 0／planner **1** | **兩者皆 0** |
| `codex exec` @ `cortex-job-jit` | **`failed to initialize in-process app-server client: Read-only file system (os error 30)`** | **`turn.completed`（rc=0）** |
| `codex exec` @ `cortex-reviewer-job-jit` | `turn.completed`（rc=0） | `turn.completed`（rc=0） |
