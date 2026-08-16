---
type: feat
scope: trust-root
---
**R0.5 D6 / trust-root Phase 2a：權限產生器 + Phase 2b root 設定 runbook（純程式碼＋
文件，不需 root、不執行任何 root 操作）**

依 spec `trust-root-isolation-spec.md` §R10 Phase 2 第 2 步「目錄 owner／mode 由 R1
登記表產生（不手寫）」與 operator 0816 裁決（路線 A、Manager 專屬 UID、現階段先二分
但保留二往三分彈性、Manager 落 system-level unit、舊 state legacy-import、降級逐案
核可）交付：

- **`paulsha_cortex/trust_root/permgen.py`（權限產生器）**：吃 `ASSET_REGISTRY` ＋一個
  參數化的 `UidScheme`（persona→OS 帳號映射），機械算出登記表每一項的目標
  `owner:group mode` ＋ per-account POSIX ACL。輸出 (a) 結構化計畫（可轉 JSON）、
  (b) 可供 runbook 引用的 `chown`／`chmod`／`setfacl` **命令字串**——**只產生字串，
  絕不執行**（靜態測試釘住無 `subprocess`／`os.system`／`os.chown`／`os.chmod`）。
  - **二分／三分參數化保留彈性**：`TWO_WAY_SCHEME`（`cortex-builder` ／
    `cortex-svc`，後者即 durable state owner，Manager＋reviewer＋planner＋monitor 共用）
    為預設；同一資料結構表達 `THREE_WAY_SCHEME`（把 `cortex-svc` 拆成
    `cortex-manager` 與 `cortex-reviewer-planner`）**不改一行程式碼**。測試證明三分
    嚴格收緊——二分下 reviewer/planner（＝svc）仍可寫 durable state（既知殘餘），
    三分下**任何 headless 帳號皆零寫入** Manager-owned/deployment。
  - policy：Manager-owned durable state → owner＝durable_state_owner、base owner-only
    （dir 0700／file 0600）、跨帳號讀取走精確唯讀 ACL；enforcement plane／樹根 →
    root 擁有、全部行程唯讀；job-visible 單 writer → 對應 job 帳號、Manager 唯讀
    ACL；多 persona 容器（worktree pool）→ 容器 Manager-owned、per-job 子目錄由降權
    啟動器逐案 chown（R2 在子目錄粒度強制）；spool → trusted consumer 擁有、producer
    僅 ACL 授 write；control queue → 依 R4 收斂為 Manager-owned（提交改走 socket）。
    group/other 一律無 write 位（收斂現存 g+w）。
  - `python -m paulsha_cortex.trust_root permissions [two-way|three-way] [--commands]`
    on-demand 入口。
- **`docs/superpowers/runbooks/trust-root-phase2b-setup.md`（Phase 2b runbook 草稿）**：
  8 段——前置檢查/baseline、建 UID（二分）、路徑分樹＋legacy-import（不 chown 舊
  state）、Manager 遷 root-owned＋system-level unit（含 `NoNewPrivileges`/
  `ProtectSystem=strict`/`ReadWritePaths`/`ProtectHome`/`CapabilityBoundingSet` 等切實
  列出的加固指令、`EnvironmentFile` 去 `-` 改 fail-closed）、降權啟動器（關 FD、不傳
  gh token）、Manager 升級（受控 sudo 替換而非裸 chown）、切換驗收（Phase 1 自檢轉綠
  ＋R9 四族含 negative control）、回滾。每步標明 operator 手動 sudo vs 驗證命令；權限
  命令一律引用產生器輸出（單一真相），標記 7 個待 operator 拍板的未決點（降權機制、
  目標路徑、legacy-import 格式、部署路徑、加固白名單、是否 codify 子命令、R9 完整
  矩陣）與 WSL2 三個最高風險步驟。

**本票只交付程式碼與文件**：不建 UID、不 chown、不動 systemd/pipx/`~/.agents`。
新增 `tests/test_trust_root_permgen_p2a.py`（33 測試）。
