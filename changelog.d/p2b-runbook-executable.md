# p2b-runbook-executable

**R0.5 D6 / trust-root 隔離 Phase 2b：permgen 產 systemd unit ＋ polkit 規則，runbook
收斂為可執行版**（純程式碼＋文件，仍不需 root、不動系統）。

## permgen 擴充（`paulsha_cortex/trust_root/permgen.py`）

- **`PathLayout`（部署 layout）**：把 operator 0816 第二輪裁決的路徑固化為機器可讀
  config——`agents_root=/var/lib/cortex`、`worktree_root=/var/lib/cortex/worktree`、
  `deploy_root=/opt/cortex`。`asset_paths()` 對 R1 登記表**每一項**給出目標主機的真實
  絕對路徑（等式測試：無遺漏、無多餘），runbook 因此不再有 `<PATH:asset_id>` placeholder。
  `with_job_segment("%i")` 讓 per-job 路徑在 systemd 模板 unit 中直接展開。
  `scaffold_directories()` 另出非登記表資產的父層骨架（部署樹、兩個服務帳號 HOME、
  job spool），原則是**凡保護資產的父目錄一律 root 擁有**——父目錄可寫者能 unlink／
  rename 子物件，root-owned 檔放進服務帳號可寫的目錄等於沒保護。
- **systemd unit 產生**（`build_manager_unit()`／`build_job_unit()`）：
  - Manager 走 system-level unit，`User=<durable_state_owner>`、`ExecStart` 指 `/opt/cortex`
    部署樹、`EnvironmentFile=`（**無 `-` 前綴＝fail-closed**，spec §R3）落在全 root-owned
    的 `/opt/cortex/etc/`。
  - **`ReadWritePaths=` 由 R1 登記表機械導出**（未決 5 的定案）：規則只有一條——某帳號
    可寫的資產，目錄取自身、檔案取父目錄，再做最小覆蓋。等式測試同時釘住**無遺漏**
    （每個 Manager 需寫的資產都被覆蓋）與**無多餘**（移除任一條就有資產失去覆蓋）、
    最小性（沒有任何一條被另一條包含）、與 `ProtectSystem`／`ProtectHome` 的一致性
    （部署樹永不可寫、RWP 不得落在 `ProtectHome` 遮蔽的 `/home`、`/root`）。非登記表
    的額外可寫路徑只能經 `ExtraWritePath` 明示宣告，且**每條必須附理由**（測試強制）。
  - 27 項加固指令逐項附「為何」的註解（測試斷言每一項都有前置註解），含
    `NoNewPrivileges`／`ProtectSystem=strict`／`ProtectHome`／`PrivateTmp`／
    `ProtectProc=invisible`（直接封 R9 族 4 的 `/proc/<pid>/environ`）／空的
    `CapabilityBoundingSet` 與 `AmbientCapabilities`（裁決 6：cortex 任何元件永不具 root）。
  - 降權 job 走 **root-owned 模板 unit** `cortex-job@.service`：`User=<job 帳號>`
    **硬寫死**、`GH_TOKEN`／`GITHUB_TOKEN` 清空、`ExecStart` 讀 Manager-owned job spool
    的 `run.sh`（job 帳號唯讀 ⇒ 改不了自己的命令列）、`CollectMode=inactive-or-failed`。
    job 側 RWP 同樣機械導出，僅涵蓋 builder 需寫者（自己的 worktree ＋ event-spool
    的 append ACL），不含任何 Manager-owned。
- **polkit 規則產生**（`build_polkit_rule(..., plan=PolkitPlan.TRANSIENT|TEMPLATE)`）：
  兩個降權方案共用同一套產生邏輯與同一組骨架（subject 檢查、action 檢查、
  **unit／verb 明細缺席即拒**、verb 白名單 `start`/`stop`、錨定的 unit pattern），
  差別只在 pattern 與說明段：
  - **A（`TRANSIENT`，預設）**——`^cortex-job-<id>\.service$`，對應 #603 的
    `job_runner.UNIT_NAME_PREFIX`（測試釘住這是**成對契約**）。`plan_residual_risk()`
    由 `UidScheme` 機械導出殘餘風險並**逐條寫進規則檔開頭**：polkit 的
    `manage-units` 只暴露 unit 名稱、**不暴露 `User=`／`--uid=`**，故與 `cortex-svc`
    同 UID 的任何行程都能請求任意 `User=`（含 root）；二分方案下 reviewer／planner
    跑模型且與 Manager 併帳，三分方案下該條自動消失（測試釘住這個差異）。
  - **B（`TEMPLATE`）**——`^cortex-job@<id>\.service$`，配合 root-owned 模板 unit
    把 `User=` 硬寫死，`residual_risks` 為空；檔頭寫明「為何 transient unit 一律拒」。
  兩案互不放行對方的 unit 形狀（測試釘住）。`evaluate_polkit()` 是規則決策的 Python
  鏡像（polkit 無法本機執行），與 JS **共用同一組常數**，兩案各跑一份決策矩陣
  （含前綴／後綴名稱混淆、其他 verb、其他 action、其他 subject）。
  `transient_unit_properties()` 另把同一套加固表 ＋ 同一份登記表導出的 RWP 展開成
  A 方案的 `systemd-run --property=` 清單，作為「A 與 B 加固面是否等價」的對照。
- **兩者都只產生內容字串**：新增靜態測試把 `open(`／`write_text`／`mkdir`／`shutil`
  一併納入禁用字串（原本只擋 `subprocess`／`os.system`／`os.chown`／`os.chmod`），
  維持 permgen 的無特權靜態測試不變式。
- **命令輸出可直接執行**：目錄資產先出 `install -d`；尚未建立的葉檔包上
  `[ ! -e <path> ] ||` 守衛（`sh -e` 下不中斷，且服務以 `UMask=0077` 建立即符合目標
  權限）；per-job 資產（帶 `<job-id>` segment）以註解形式輸出，可讀但不會被誤執行。
- **file/dir 推斷修正**：`evidence/*`、`workflow-report-transactions`、
  `engineering-outcomes`、`gate-ledger` 等**目錄**先前被 token heuristic 誤判為單檔
  （會產生 `chmod 0600` 到目錄上）；改為明列。多 persona 容器分支不再無條件視為目錄，
  per-job handoff manifest 因此得到 `0600` 而非 `0701`。
- **CLI**：`python -m paulsha_cortex.trust_root {unit,polkit,scaffold}`，
  `permissions ... --commands --paths` 以真實 layout 路徑輸出。

## runbook 收斂（`docs/superpowers/runbooks/trust-root-phase2b-setup.md`）

`status: draft` → `status: executable`。7 個 `⚠️ 未決` 全數移除，替換為 0816 第二輪
裁決定案表（含每點落在哪一步）。結構：**執行前提 ＋ 9 個步驟 ＋ WSL2 風險段 ＋ 附錄**，
24 個 `🔧 sudo` 點、94 個 `✅ 驗證` 點。

- **第 3b 節**吸收 PR #599 的 review verdict 受控通道（`review-verdict-spool` 已進登記表
  ⇒ 權限由第 2 步的 script 一併套用，本節只做確認與三分方案下 reviewer write-only ACL
  的說明）。
- **執行前提**（新增）：in-flight job 手動收尾（裁決 3）、服務已停、Phase 1 自檢
  baseline 存檔、登記表等式綠、`sudo` 可用、**polkit 在跑**（缺 polkit ⇒ 降權必然
  fail-closed 全停）、磁碟空間。
- 每步命令改為**真實絕對路徑**、可直接複製；權限／unit／polkit 一律「產生器輸出 →
  operator 讀過 → 落檔」，並附 `diff <(產生器) <(系統檔)` 的漂移檢查。
- **第 5 步（降權）兩案都寫完整**：5-0 共用前提（polkit 能／不能強制什麼的對照表、
  A/B 比較表含「與提前三分的關係」、兩案規則並排 `diff`）；5-A 涵蓋 #603 的 argv 形狀、
  env 白名單、unit 名前綴契約驗證、polkit 落檔、`PSC_JOB_RUNNER=systemd-run` 開關、
  正向驗證、反向驗證（含**負控制：移除 polkit 規則後 dispatch 必須 fail-closed 落
  `job-runner-transient-unit-start-failed` 而非退回 direct**）與殘餘風險實測；
  5-B 涵蓋模板 unit ＋ polkit 落檔、job spool 的 ACL 佈置、正向驗證與 7 條反向驗證
  （`systemd-run --uid=0`／`--uid=cortex-builder`／夾帶 `AmbientCapabilities`／起別的
  unit／名稱混淆／其他 verb／直接改模板 unit）**全部必須非 0 退出**。
- **第 8 步 R9 手動抽驗**（裁決 7）：一份逐條攻擊測試 script（族 1 五條／族 2 二十條／
  族 3 十一條／族 4 七條），每條附確切命令與預期輸出（`denied (OK) rc=<非 0>`），
  外加三組 negative control（受信任身分做同一件事必須成功）與族 3 的「重啟後仍綠」複驗。
- **第 9 步回滾**：每階段各自的退路 ＋「全部退回 Phase 1 降級運轉」的總回滾（新樹整棵
  丟棄不遺失資料，因為舊 state 走 legacy-import 物理隔離而非併入）。
- **WSL2 風險段**補上：system unit 開機拉起的驗證流程（`wsl --shutdown` 前後的
  `is-enabled`／`is-active`／`journalctl -b` 逐項）、`ProtectSystem=strict` 誤擋的
  五步診斷（含以 root 的 `systemd-run` 在同一組沙箱條件下重現）與依機率排序的常見
  誤擋清單；鐵律是「被擋路徑只能經回填登記表 → 重跑 permgen 進入 RWP，drop-in 只能
  是當天的臨時措施」。

## 唯一還需要 operator 拍板的一點（已在 runbook 開頭與 PR body 標明）

裁決寫「systemd-run transient unit ＋ polkit 收窄到只能 `User=cortex-builder`」。
#603 實測確認 polkit 的 `manage-units` action **只暴露 unit 名稱與 verb**，
**不暴露 `User=`／`--uid=`**——「只能降到 job 帳號」這一半 polkit 無法強制。本 PR 因此
把第 5 步寫成**兩案並列、各自完整可執行**：

- **A（`systemd-run`，裁決的字面方案）**：程式碼已落地（#603），「降到哪個帳號」由
  Manager 端封閉 argv 產生器在 code level 保證。殘餘風險寫死在 runbook 與規則檔裡，
  且 5-A-5 (4) 是一條**預期會成功**的實測（`sudo -u cortex-svc systemd-run --uid=0`），
  用途是讓 operator 親眼確認自己接受了什麼。
- **B（root-owned 模板 unit）**：`User=` 硬寫死在 root 擁有的 `cortex-job@.service`，
  polkit 只放行該模板實例、transient unit 一律拒，殘餘為零；代價是 Manager 端要從
  `systemd-run` 改成 `systemctl start cortex-job@<id>`（待排的程式碼工項）。

同時需一併裁決「**是否提前三分**」——三分把 A 的殘餘風險從「三個與 Manager 併帳的
persona」縮回「Manager 自己」，permgen 只需把 `two-way` 換成 `three-way`。

## 範圍

**本票只交付程式碼與文件**：不建 UID、不 chown、不動 systemd／polkit／pipx／`~/.agents`。
新增 `tests/test_trust_root_permgen_p2b.py`（61 測試）。
