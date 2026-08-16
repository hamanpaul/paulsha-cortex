# ab-runbook-converged

- **trust-root Phase 2b runbook 由「A／B 兩案並列、待 operator 拍板」收斂為
  **A+B 單一路徑**（docs-only，不改任何一行程式碼、不動部署）**——落實 operator
  0816 第三輪裁決（#584 留言）：polkit 的
  `org.freedesktop.systemd1.manage-units` 只暴露 unit 名與 verb、**不暴露
  `User=`／`--uid=`**（#603 實測），因此「誰持有授權」與「授權能做什麼」兩層必須
  **一起收**，而不是二擇一。
  - **UID 方案：三分為唯一路徑**。第 1 步由建兩個帳號改為建三個——
    `cortex-manager`（Manager＋monitor、durable state owner、持 spawn 授權、
    **不跑任何模型程式碼**）／`cortex-reviewer-planner`（reviewer＋planner 模型
    job）／`cortex-builder`（builder 模型 job）。全文 `two-way` 改 `three-way`
    （`permgen.THREE_WAY_SCHEME` 由備選轉定案）；二分縮為一行歷史註記與附錄 C 的
    差異表，不再是可選路徑。新增三帳號的互斥驗證（互不可讀對方 HOME cache、
    三者皆無 sudo、group 互不交集）。
  - **第 5 步：A+B 合一，原 5-A／5-B 兩節（共 10 小節）收斂為一條九節路徑**——
    (a) polkit 只授權 `cortex-manager` 對 `cortex-job@*.service` 的 `start`／`stop`，
    **不授權 `manage-units` 的 transient 建立**（`polkit three-way --template`）；
    (b) root-owned template unit `cortex-job@.service`（`User=cortex-builder` 寫死、
    加固段寫死）；(c) **root-owned shim** `/opt/cortex/bin/cortex-job-shim` 讀
    Manager-owned job-spec spool 導出 argv——把裁決保留的第三層 C（code-level argv
    保證）從 Manager 端搬進 root 擁有的檔案；(d) 切換點
    `PSC_JOB_RUNNER=systemd-template`。(c)(d) 依賴 #603 follow-up PR
    （template-instance 模式），runbook 以「將由該 PR 提供」標註並附
    「PR 未落地時做到哪裡」的分岔指引；每個落檔步驟後都跟一條對產生器的 `diff`，
    介面一落地即自動對齊，runbook 不需改寫。
  - **反向測試由「4 條 ＋ 7 條、其中 1 條期望成功」收斂為 5-7 的 11 條、全部期望被拒**
    ——移除了舊版 5-A-5 (4) 那條「已知不會被拒（以 `cortex-svc` 起 `--uid=0`）」。
  - **殘餘風險段重寫**：三分＋template 之下，「polkit 看不到 `User=`」與
    「reviewer／planner 與 Manager 併帳」兩條殘餘**不再成立**（各附實測條目）；
    polkit 的粗粒度仍在但已不構成提權（被授權的 unit 名 pattern 只對應一個
    root-owned template，安全屬性全不由呼叫端提供）。剩下的殘餘收斂為
    **`cortex-manager` 帳號的 supply-chain 類**（部署樹供應鏈、Manager 自身邏輯、
    operator 帳號、polkit 不可用），逐條列出現有緩解與缺口。
  - **R9 攻擊矩陣：四族 → 五族，且族 1–4 各跑兩個 subject**（`cortex-builder`
    與 `cortex-reviewer-planner`，`cortex-manager` 僅作 negative control）。
    新增**族 5 privilege-boundary**：(5.1) 以 `cortex-manager` 身分請求 transient
    unit `--uid=root` 的五種形式（直接、借 job unit 名包裝、改用 `--property`
    夾帶、`--user` bus 旁路、`busctl` 直打 `StartTransientUnit`）必須全部被 polkit
    拒；(5.2) 三個 headless 帳號 × 九種手法（append／truncate／replace／drop-in／
    改 shim／symlink 換 shim／rename `bin/`／改 polkit／刪 polkit）改寫
    `/etc/systemd/system/cortex-job@.service` 或 `/opt/cortex/bin/cortex-job-shim`
    必須全部被 root-owned 檔案權限拒（27 條）。兩族各附 negative control
    （operator 的 sudo 做同樣的事必須成功；`cortex-manager` 起**合法** instance
    必須成功），避免「polkit 把該帳號全擋掉」造成假綠。
  - **執行前提補兩項**：三個帳號名稱未被占用＋產生器對 `three-way` 的 persona→帳號
    映射自述；以及 `systemd-template` 是否已在 `job_runner.RUNNER_MODES`
    （決定第 5 步 (d) 能不能開）。
  - **回滾段補齊 A+B 的每個物件**：第 5-2（template unit，含 drop-in 目錄）、
    5-3（shim）、5-4（polkit）、5-5（切換點）各自獨立一列；「全面回退」新增移除
    shim、template drop-in 與**三個帳號**。
  - **重新統計並列表**：9 步 ＋ 3 附錄、**32 個 `🔧 sudo` 點**、
    **122 個 `✅ 驗證` 點**（逐段落明細表）。
  - **新增附錄**：附錄 B「降級備援——transient unit」保留原方案 A 的完整操作，
    但明示**不是主路徑**、必須在 #584 記錄啟用原因與預計關閉時間，並以對照表列出
    降級期間多出來的殘餘風險（含產生器 `plan_residual_risk()` 的自述輸出）；
    附錄 C 列出與第二輪裁決的逐項差異，供讀過舊版的人對照。
  - **誠實邊界（M1／M2）**：`launcher.SubprocessLauncher._degraded_runner()` 目前
    只對 builder persona 降權，因此 M1（本 runbook 全程）之後 reviewer／planner 仍
    在 Manager 行程內以 `cortex-manager` 身分執行——「injection 可達的進程皆無
    spawn 授權」在 M1 **只對 builder 成立**（builder 是唯一會跑 untrusted repo code
    的 persona）。runbook 於開頭、5-8 殘餘風險表與第 8 步各標註一次，並要求把
    「M2 是否完成」寫進 #584 的 D6 判定紀錄。
  - 另標註 permgen 尚未跟上三分定案的兩個已知缺口（Manager HOME 仍沿用
    `/var/lib/cortex-svc`；`cortex-reviewer-planner` 未進 `scaffold_directories`），
    第 1 步以「一律以產生器輸出為準」＋手動補三行的方式處理，並在第 2 步稽核 3
    給出對應的期望值。
