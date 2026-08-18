### Fixed
- **#666：實機為啟用 monitor 手動補的兩項收斂回登記表——`pytest`（系統層 python 套件）
  與 Manager 的 `gh` 憑證**。兩項在實機都已生效，但**產生器的計畫產不出它們**：重跑
  `permissions`／`toolchain` 不會有、換一台機器部署也不會有。

  **漂移項 1：`pytest` 裝在 operator 的 user site-packages，gate 讀不到。**
  `PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 是**相對名**，由 gate 的 `PSC_GATE_PATH`
  解析 ⇒ `/usr/bin/python3`——**系統層那一支**。gate unit 自己的 `ExecStart` 用的是
  `/opt/cortex/venv/bin/python3`，但那只涵蓋 ledger writer 本身，**operator 宣告的命令
  另外解析一次**。`ProtectHome=yes` 之後 `~/.local/lib/python3.12/site-packages` 不可達
  ⇒ 每張 build 卡的 gate ledger 為空 ⇒ 撞 #540 的 acceptance chain，而痕跡只有
  `manager.log` 的一行。修法：新增 `permgen.SYSTEM_PYTHON_DISTRIBUTIONS`（`pytest` ＋
  **`PyYAML`**——後者是**被測樹**的 runtime 相依，不是 pytest 的：gate 的 cwd 是被驗那棵
  樹的副本，pytest 把 rootdir 插進 `sys.path` ⇒ `import paulsha_cortex` 解到被驗的樹
  ⇒ 它 `import yaml`；缺它的症狀是 pytest exit code `2`（collection error），不是「測試
  失敗」）。**版本是明示的部署決定**：約束的唯一真相在 `pyproject.toml`（測試對著它比，
  改一邊沒改另一邊即紅），實機解出來的版本由 runbook 第 4f 步記錄並與 operator 側比對。

  **漂移項 2：Manager 沒有 gh 登入態，兩個 github provider `degraded`。** 新增登記表資產
  `manager-gh-credential`（`<manager HOME>/.config/gh/hosts.yml`，服務帳號 owned `0600`、
  列入 `IN_PLACE_CONTENT_WRITE_ASSETS`）與 `manager-gh-config`（`config.yml`，
  **root-owned `0644`**）。**兩個檔的 owner 刻意不同**：`hosts.yml` 是 `gh` 唯一寫回
  token 的檔（不歸該帳號就 refresh 不回來）；`config.yml` 不承載憑證，但其 `aliases` 可
  宣告 `!` 開頭的 shell alias——讓服務帳號改得了它等於給 Manager 一條「把任意命令掛進
  每一次 `gh` 呼叫」的執行面，與三份 `.gitconfig` 維持 root-owned 是**逐字相同**的理由。
  兩層目錄（`.config`、`.config/gh`）由 `scaffold_directories()` 產出 root-owned `0755`。
  **與 #640 的 job 憑證形狀相同、洩漏面不同級，spec／note／runbook 三處都明寫不得混談**：
  #640 那一份是 job 帳號的模型 provider 憑證（job unit 另有 `Environment=GH_TOKEN=` 清空
  GitHub token，成果走 spool 由 Manager 代理推送）；本份是給 durable state owner 的，
  這個 token 推得動 PR、關得掉 issue、merge 得了分支。job 帳號因此**刻意沒有**
  `~/.config/gh` 這一層目錄。

### Added
- **#666：外部相依的窮舉盤點——判準從「這一類東西有哪些」改成「跑完一個 run 需要碰到
  什麼」**。`#640`（executor toolchain ＋ job 憑證）、`#661`／`#664`（`srt`／`openspec`／
  preflight backend）、本票（`pytest` ＋ gh 憑證）是同一族的第一到第五個成員，每次都是
  「症狀出現才補一項」。前面每張表本身都完整，缺的是**沒有任何一處回答「一個 run 需要
  碰到的東西有哪些」**。新增 `permgen.RUN_EXTERNAL_DEPENDENCIES`（25 項，逐項標明 kind／
  哪些 principal／run 的哪一段／登記在哪），並**雙向封閉**：
  `uncovered_run_dependencies()`（盤點列到但表上查無）與 `unlisted_roster_entries()`
  （表上有但盤點沒列到）皆有測試釘住必須為空。**後者才是本票真正買到的東西**——它讓
  「加一支相依」與「說明它在 run 的哪一段被誰碰到」變成同一件事，而不是兩件可以只做一半
  的事。落位計畫（`trust_root toolchain`）把盤點逐段印出來，runbook 第 4h 步要求每次部署
  複核一次。
- **#666：盤點補上三支一直在用、卻從來沒被寫下來的系統層程式**——`bash`（**每一支 job 的
  `command[0]` 就是它**：wrapper 是 `bash -c <script>`，降權模式下 shim 的 `execvpe` 執行
  的第一支程式即為它；Manager 側的 exit 記帳 shell 亦然）、`python3`（gate 宣告與
  `review-sandbox` probe 都以 `PATH` 解析它 ⇒ **系統層那一支**；#661 曾以「它是部署 venv
  自己的 interpreter」為由排除，查證後該前提不成立）、`systemctl`（B 案定案後 Manager
  派工的第一個動作就是 exec 它，解不到就是「降權派工整條不可用」）。
- **#666：第四種外部相依——python 發行版**。`pytest`／`PyYAML`／`policy-check` 不是落在
  `PATH` 上的可執行檔，`command -v` 對它們無解；塞進 `SYSTEM_PROGRAMS` 會讓「名冊上每一項
  都解析得到執行檔」這條既有性質變成假的（與 #661「不得把 `srt` 併進 `EXECUTOR_TOOLS`」
  是同一條論證：盤點完整性不可以用「往別張表塞東西」來換）。因此另立
  `SYSTEM_PYTHON_DISTRIBUTIONS`／`DEPLOYMENT_PYTHON_DISTRIBUTIONS`，各自標明落在哪一個
  interpreter、版本約束的唯一宣告來源、以及誰需要它；`PREFLIGHT_BACKEND_DISTRIBUTION`
  這個原本散落的常數也收進同一張表。
- **#666：`permgen.GATE_COMMAND_DECLARATIONS` ／ `PathLayout.gate_command_env()`
  ——產生器出建議的 gate 宣告值**（定位同 `job_path_value()`／`preflight_command_value()`：
  產生器出值、operator 落進 root-owned 的 EnvironmentFile，不是第二份執行期真相）。買到
  兩件既有形態買不到的事：(i) **gate 宣告的每一段都可以被機械對照到某張表**——`python3`
  必須在 `SYSTEM_PROGRAMS` 上、`-m <module>` 必須在 `SYSTEM_PYTHON_DISTRIBUTIONS` 上，
  而 #666 的漂移正是這條不成立卻沒人看得見；(ii) **覆蓋率**——本表必須是 doctor
  `gate-declarations` probe 由 packaged deck `test_policy` 導出的那個集合的超集，否則照
  runbook 裝出來的部署一開機 doctor 就是紅的。另有契約測試釘住
  `GATE_COMMAND_ENV_PREFIX == gate_ledger.GATE_ENV_PREFIX`（兩邊刻意不互相 import）。
- **#666：`permgen.deferred_run_dependencies()`——盤點撞到、尚無歸宿的四項變成可列舉**
  （比照 #661 的 `unresolved_node_execution_surfaces()`：**不做裁決、不放寬任何一面**，
  但也不讓它靜默消失）。四項的共同形態是「per-account 的機制已就緒，登記表只登記了其中
  一份」：(1) **reviewer／planner 的 executor 憑證**——#640 說「M2 落地時補第二列」，而 M2
  （#615）已經落地，因此這條現在是**逾期未做**；實測 reviewer 模板 unit 的
  `ReadWritePaths` 不含它 ⇒ `ProtectSystem=strict` 下讀得到、改不了 ⇒ token 過期那天
  refresh 靜默失敗（有測試把這個**事實**釘住，補上第二列時該測試會紅，那正是提醒去刪掉
  它與那筆 deferred）；(2) `cortex-gate` 沒有 `.gitconfig`（預防面，目前的 gate 宣告不碰
  git）；(3) reviewer／planner 的 `codex-hooks`（`asset_paths()` 把它寫死在 builder HOME
  下）；(4) **Manager 的 claude 登入態**——`planning_runtime` 是在 **Manager 行程內**直接
  exec `claude` 的（不是派降權 job），它讀 `<HOME>/.claude/.credentials.json`，而
  `executor_credential_relpath` 是**單一**部署決定、一個帳號只表達得了一份憑證。

### Changed
- **#666：HOME-anchored 資產在不適用的方案下不再進入 `ReadWritePaths`
  （`permgen.inapplicable_home_anchored_assets()`）**。幾個掛在帳號 HOME 下的資產由
  `PathLayout` 的部署決定欄位導出路徑，而那些欄位取的是定案的三分／四分；二分把
  Manager／reviewer／planner 併進 `cortex-svc`，同一條路徑在二分部署裡不存在，而 systemd
  對不存在的 `ReadWritePaths=` 目標會讓 unit **直接起不來**。登記表的 note 早就寫著
  「二分下該資產不適用」、權限那一半也早就以 `[ ! -e ] || …` 守衛表達了它，缺的只有 RWP
  那一半——#640 當時的處置是「乾脆不登記第二份憑證」，#666 要登記 Manager 的 gh 憑證時
  同一個陷阱又出現一次，因此改成一條**可列舉的機械規則**（靜默扣掉一條 RWP 與漏授一條
  在輸出上長得一樣，而後者的症狀是 job 跑到一半 EROFS）。附帶效果：#640 當年那個「不要
  登記第二份」的阻礙已經被拆掉。
- **#666：`trust_root toolchain` 的輸出擴為三段**——系統層 python 發行版的落位與版本比對、
  Manager gh 憑證的落位與**以該身分實測**的驗證、以及窮舉盤點與已知未決項。計畫仍是純
  字串（非註解行只可能是 `install -d`／`chown`／`chmod`，有回歸測試）。驗收方式是
  **「重跑計畫後零漂移」**：實機手動補過的東西若產生器出不出來，換一台機器部署就不會有它。
- **#666：runbook 新增第 4f（系統層 python 套件）／4g（Manager gh 憑證）／4h（窮舉盤點
  複核）三步**，驗證一律**以該身分實測**而不是只驗檔案存在：4f 有「gate 身分 `python3 -m
  pytest --version`」＋完整加固面下的 `systemd-run` 實跑（**CPython 不是 V8，MDWE 對它沒有
  影響**，因此這一條在完整加固面下就該過，失敗即為新發現、不得就地放寬）＋版本比對；
  4g 有「Manager 身分 `gh auth status`」＋不變式四條（改得了內容／建不了新檔／刪不掉
  root-owned 鄰居／改不了 `config.yml`）＋ RWP 只掛檔案不掛父目錄＋ job 側反向驗證。
  附錄 A 的漂移自我檢查同步補三條。
- **#666：spec §R1 新增 (b2)（Manager 傳輸層憑證：同形狀、不同級的洩漏面）與 (c)（外部
  相依的盤點判準從 run 反推、雙向封閉、第四種相依、HOME-anchored 資產的不適用規則）。**

### 測試
- 新測試檔 `tests/test_trust_root_external_deps_exhaustive_666.py`：**53 passed, 1 skipped**。
  涵蓋兩個新資產的 owner／mode／目錄形態、`ReadWritePaths` 只掛檔案不掛父目錄、
  traverse 鏈可達、#622 monitor 嚴格窄於 manager 仍成立、二分不得出現不存在帳號的路徑、
  gate 宣告每一段可對照到某張表、盤點雙向封閉、以及落位計畫能重現實機那兩項。
- **OS 層語意以真的檔案系統驗**（#638／#657 的教訓）：`TestGhCredentialOsSemantics` 對
  真實檔案系統驗四條（就地改內容成功、建新檔 `PermissionError`、`unlink`
  `PermissionError`、`os.replace` 蓋 root-owned 鄰居 `PermissionError`）。手法與 #642 的
  `TestInPlaceCredentialOsSemantics` 相同：裁決守的是「**目錄沒有 `w` 位給這個行程**」，
  以 owner 位 `0555` 重現**同一段 kernel 檢查**（`inode_permission(dir, MAY_WRITE)`），
  不需要第二個 UID。**真正需要第二個 UID 的那一半明確 skip 並寫明理由**——「`config.yml`
  由 root 擁有、服務帳號連內容都改不了」在單 UID 下重現不了（本行程就是 owner，chmod
  回來即可改），那一半改由產生器測試（`entry.owner == deploy_account` 且 writer 不含服務
  帳號）與 runbook 的實機驗證守；在這裡跑一次只會證明一件與待驗命題無關的事。
- 全套 `python3 -m pytest tests/ -q`：**4163 passed, 21 skipped, 49 subtests**，零回歸。
