---
status: draft
work_item: trust-root-isolation
phase: 2b
audience: operator
supersedes: none
tracking: "#584 — trust-root D6 母議題（本票 reference-only，不 auto-close；R-17 走 policy-exempt:issue-link 豁免）"
refs:
  - docs/superpowers/specs/trust-root-isolation-spec.md
  - paulsha_cortex/trust_root/registry.py
  - paulsha_cortex/trust_root/permgen.py
---

# trust-root Phase 2b：root 設定 runbook（草稿，供 operator review）

> **本文件是草稿**，供 operator 逐步 review 後**手動** `sudo` 執行。文件本身
> **不執行任何 root 操作**；所有特權步驟都由 operator 親自輸入。凡標記
> **`⚠️ 未決`** 的段落，其最終形態待 operator 拍板後才可執行。

實作 `trust-root-isolation-spec.md` 的 **Phase 2**（spec §R10 Phase 2 第 1–8 步）。
Phase 2a 的權限產生器（`paulsha_cortex/trust_root/permgen.py`）已把 R1 登記表機械
轉成目標 `owner:group mode`；本 runbook 是把那份計畫**落到 OS**的手動流程。

## operator 0816 裁決（本 runbook 的前提）

- **路線 A**（OS／MAC 邊界），非簽章路線。
- **Manager 專屬 UID**：Manager 不再跑在 operator 帳號，改跑在服務帳號。
- **現階段先二分**：`cortex-builder`（builder）／`cortex-svc`（Manager＋reviewer＋
  planner＋monitor 共用，且為 durable state owner）。**保留二往三分彈性**——未來把
  Manager 拆成第三個 UID 時，只需換 `permgen.THREE_WAY_SCHEME`，runbook 結構不變。
- **Manager 落 system-level unit**（非 `--user`），以 `cortex-svc` 執行。
- **降權機制＝`systemd-run` transient unit**（未決 1 已裁決）。程式碼面 Phase 2a 已落地
  （`paulsha_cortex/coordinator/job_runner.py`，`PSC_JOB_RUNNER` 預設 `direct`）；
  本 runbook 第 5 步是把開關打開的部署動作。polkit 授權面收窄到 `cortex-job-` 前綴的
  transient unit。
- **舊 state 走 legacy-import 重新入帳**，**不**直接 `chown` 沿用（spec §R6(e)）。
- **降級運轉逐案核可**（`PSC_DEGRADED_OPERATION=per-case-approval`，Phase 1 已上線）。

## 當前環境事實（WSL2）

| 項目 | 現況 |
|---|---|
| OS | WSL2、system-level systemd 可用（`systemctl is-system-running` = `running`） |
| sudo | **需密碼**——每個 `sudo` 步驟都是互動式，**不可假設自動化** |
| 帳號 | 單一 `operator` 登入帳號（本文以 `operator` 代稱，勿寫死使用者名） |
| 部署 | pipx tree 在 operator HOME（`$HOME/.local/share/pipx/venvs/paulsha-cortex/`），實測 `drwxrwxr-x` |
| headless job | 現以 operator 帳號跑 |
| `~/.agents/{control,config/paulsha}` | 現為 `775`（`g+w`） |

## 標記約定

- **`🔧 operator sudo`**：operator 親自 `sudo` 執行的特權變更。
- **`✅ 驗證`**：唯讀驗證命令（可由 operator 或 CI 執行，無特權）。
- **`⚠️ 未決`**：最終形態待 operator 拍板；**未拍板前不得執行**。
- 命令中的路徑一律以 shell 變數表示（如 `"$AGENTS_ROOT"`），**勿寫死絕對路徑**。

## 權限命令的單一真相

所有 `chown`／`chmod`／`setfacl` **不手寫**——由權限產生器輸出，operator 逐項對照
登記表 review：

```bash
# ✅ 驗證：印出二分方案的完整權限計畫（JSON）
python3 -m paulsha_cortex.trust_root permissions two-way

# ✅ 驗證：印出可直接對照的命令序列（含 <PATH:asset_id> placeholder）
python3 -m paulsha_cortex.trust_root permissions two-way --commands

# ✅ 驗證：印出登記表摘要（含每項 path_resolver，供解析真實路徑）
python3 -m paulsha_cortex.trust_root registry
```

> **路徑代入**：命令輸出以 `<PATH:asset_id>` placeholder 呈現。operator 依
> `registry` 的 `path_resolver` 在目標主機解析真實路徑（見第 3 步的 `AGENTS_ROOT`
> 等變數），再逐項替換。`path_resolver=None` 的葉資產（如 `jobs-registry` →
> `<coordinator_root>/jobs.json`）依 spec 背景段的定義位置表解析。

---

## 第 0 步：前置檢查（不需特權，全部 `✅ 驗證`）

先把 baseline 記下來，切換後才能對照。

```bash
# ✅ 系統 systemd 可用
systemctl is-system-running || true          # 期望 running（degraded 亦可，記錄之）

# ✅ sudo 可用（會要密碼；只驗證能取得，不做任何變更）
sudo -v

# ✅ Phase 1 自檢 baseline——記錄現況哪些 Manager-owned 路徑仍 job-writable
python3 -m paulsha_cortex.trust_root selfcheck | tee "/tmp/trust-root-baseline-$(date +%Y%m%d).json"

# ✅ 登記表等式必須綠（否則盤點已漂移，先修 Phase 1 再繼續）
python3 -m paulsha_cortex.trust_root equation

# ✅ 記錄現況權限（切換後對照）
ls -ld "$HOME/.agents" "$HOME/.agents"/* 2>/dev/null
ls -ld "$HOME/.local/share/pipx/venvs/paulsha-cortex" 2>/dev/null
```

**通過條件**：`equation` 回傳 `ok: true`；baseline JSON 已存檔。
Phase 1 自檢此時**預期為紅**（有 `job-writable` finding）——那正是本 runbook 要收斂
的清單，切換完成後（第 7 步）應轉綠。

---

## 第 1 步：建 UID（二分）

建立兩個 **system service 帳號**，皆 **no-login**（`nologin` shell）、**最小 home**。
慣例：每帳號一個同名 primary group（權限產生器的 ACL 以此為前提）。

```bash
# 🔧 operator sudo：建 cortex-svc（durable state owner + Manager/monitor/reviewer/planner）
sudo groupadd --system cortex-svc
sudo useradd  --system --gid cortex-svc \
     --home-dir /var/lib/cortex-svc --create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex trusted service (Manager/monitor/reviewer/planner)" cortex-svc

# 🔧 operator sudo：建 cortex-builder（唯一完全隔離的 headless job 帳號）
sudo groupadd --system cortex-builder
sudo useradd  --system --gid cortex-builder \
     --home-dir /var/lib/cortex-builder --create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex headless builder job" cortex-builder
```

**群組設計理由**：跨帳號存取一律走 **per-account POSIX ACL**（見權限產生器輸出的
`setfacl -m u:<acct>:rX`），**不**用共用 group 開放，避免「一個 group 開了就全開」的
過寬授權。因此兩帳號各自獨立 group、彼此無交集。

**為何 no-login／最小 home**：這兩個帳號只被 systemd 與降權啟動器使用，永不互動登入；
最小 home 只放 per-user runtime（如 XDG dirs），不放 durable state（durable state 在
第 3 步的分樹）。

```bash
# ✅ 驗證：帳號存在、shell 為 nologin、互不同 group
getent passwd cortex-svc cortex-builder
id cortex-svc; id cortex-builder
# 期望：兩者 primary group 不同、彼此不在對方 group
```

> **✅ 未決 1 已裁決**：降權經 **`systemd-run` transient unit** ＋ narrow polkit rule
> （見第 5 步）。unprivileged 的 `cortex-svc` 無法直接 setuid，改由 systemd（PID 1）
> 代為切換身分。本步因此**不需要**額外的輔助群組——只要兩個帳號各自有同名 primary
> group（`job_runner` 的 `--gid` 預設即帳號同名 group，與 `permgen.UidScheme.group_of()`
> 同一慣例）。

---

## 第 2 步：解析目標路徑變數（不需特權，`✅ 驗證`）

分樹前先把登記表的容器路徑解析成 shell 變數，供第 3 步引用。這些是**目標**部署下
`cortex-svc` 的路徑（不是 operator HOME）。

```bash
# ⚠️ 未決 2：目標 AGENTS_ROOT 的最終位置待 operator 定。
#   候選：/var/lib/cortex（system service 慣例）或保留 ~/.agents 但改 owner。
#   本 runbook 以 /var/lib/cortex 為示例；operator 定案後替換下列變數。
export AGENTS_ROOT="/var/lib/cortex/agents"          # ⚠️ 待定
export CONTROL_ROOT="$AGENTS_ROOT/control"
export COORDINATOR_ROOT="$AGENTS_ROOT/coordinator"
export SPECS_ROOT="$AGENTS_ROOT/specs"
export REGISTRY_ROOT="$AGENTS_ROOT/registry"
export MONITOR_STATE_ROOT="$AGENTS_ROOT/monitor"
export RUN_ROOT="$AGENTS_ROOT/run"

# ✅ 驗證：確認變數與登記表 path_resolver 對得上
python3 -m paulsha_cortex.trust_root registry | \
  python3 -c "import sys,json;print('\n'.join(sorted(a['path_resolver'] or a['asset_id'] for a in json.load(sys.stdin)['assets'])))"
```

> **背景 §7 instance-scope 漏洞**：遷移前務必確認 bootstrap env 含
> `PSC_CONTROL_ROOT`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT` 且已 instance-scope，
> 否則兩個 instance 仍共用 `jobs.json`、分樹會失效（spec §R2 最後一條）。

---

## 第 3 步：路徑分樹 + legacy-import（**不 chown 舊 state**）

裁決：舊 state **不**沿用（不 `chown` 現存 `~/.agents`），而是**建新樹→驗證→把舊
state 標記為 `legacy-imported` 重新入帳**（spec §R6(e)）。`legacy-imported` 標記
**不可**滿足任何 ship gate。

### 3a. 建新樹骨架（Manager-owned 與 job-visible 兩棵）

```bash
# 🔧 operator sudo：建樹根（root 擁有樹根——headless/svc 皆不可 relink 整棵信任根，spec §1）
sudo install -d -o root -g root -m 0755 "$AGENTS_ROOT"

# 🔧 operator sudo：Manager-owned 子樹（cortex-svc 擁有、0700）
for d in "$CONTROL_ROOT" "$COORDINATOR_ROOT" "$SPECS_ROOT" "$REGISTRY_ROOT" \
         "$MONITOR_STATE_ROOT" "$RUN_ROOT"; do
  sudo install -d -o cortex-svc -g cortex-svc -m 0700 "$d"
done
```

> 上列 owner/mode **來自權限產生器**，勿手寫。逐項對照：
> `python3 -m paulsha_cortex.trust_root permissions two-way --commands`
> 的 `manager-state` 區塊（如 `control-root-tree`／`coordinator-root-tree`）。
> 產生器對有跨帳號 reader 的容器（如 `control-root-tree` 有 operator 讀 done/status）
> 會多出 `setfacl -m u:operator:rX`——照抄。

### 3b. job-visible 樹（worktree pool：per-job 逐案 chown，容器 0701）

```bash
# ⚠️ 未決 2（同上）：worktree pool 目標位置
export WORKTREE_ROOT="/var/lib/cortex/worktrees"     # ⚠️ 待定

# 🔧 operator sudo：容器由 cortex-svc 擁有、0701；per-job 子目錄由第 5 步啟動器逐案 chown
sudo install -d -o cortex-svc -g cortex-svc -m 0701 "$WORKTREE_ROOT"
```

> `dispatch-worktree-pool` 是 `runtime_managed` 資產——容器層只給 owner 寫，
> **reviewer 與 builder 的 worktree 是各自被 chown 的子目錄**（R2 在子目錄粒度強制）。
> `monitor-event-spool` 亦特殊：cortex-svc 擁有、以 `setfacl -m u:cortex-builder:wx`
> 讓 builder 只能 append，consumer(svc) 讀＋消費。照抄產生器輸出。

### 3b-2. review verdict spool（Phase 2a 已就位的受控通道）

Phase 2a 已把 review verdict 的落點從 reviewer worktree 搬到
`<coordinator_root>/review-verdicts/<reviewer_job_id>/verdict.json`（登記表
`review-verdict-spool`，spec §R2 ＋未決 10-6 的 per-job spool 案）。**程式碼側
已完成**：per-job 目錄由 Manager 在 dispatch 當下以 `0700` 建立、帶 pre-seed
守衛，落地後轉 `0444`；reviewer 身分由 Manager job registry 推導，verdict payload
的自述綁定欄位一律忽略。

```bash
# 🔧 operator sudo：spool 根由 cortex-svc（Manager）擁有；builder 完全無權限
sudo install -d -o cortex-svc -g cortex-svc -m 0700 "$COORDINATOR_ROOT/review-verdicts"

# 三分方案才需要：reviewer 帳號以 **write-only** ACL 進入（寫得進自己那格、讀不到他人）
# sudo setfacl -m u:cortex-reviewer-planner:wx "$COORDINATOR_ROOT/review-verdicts"
# sudo setfacl -d -m u:cortex-reviewer-planner:wx "$COORDINATOR_ROOT/review-verdicts"
```

> 二分方案下 reviewer 與 Manager 併帳（`cortex-svc`），owner 位已涵蓋寫入權，
> 產生器不會輸出多餘 ACL。**builder（`cortex-builder`）在兩個方案下都拿不到任何
> 權限**——這一步完成後，spec 背景 §3 的「builder 代寫 verdict」最短攻擊路徑才
> 從「結構上不可能被採信」升級為「OS 層寫不進去」。
>
> 另：Phase 2a 的降權啟動器需把該 job 的 spool 目錄放進 executor sandbox 的放行
> 清單（`--add-dir`，見 `SubprocessLauncher.as_verdict_spool_writer()`）——只放行
> **那一格**，不是整棵 coordinator 樹。

### 3c. legacy-import 重新入帳（**不滿足 ship gate**）

```bash
# ✅ 驗證：把舊 state 的內容 hash 記下（import attestation 的內容，spec §R6(e)）
OLD="$HOME/.agents"
find "$OLD" -type f -print0 2>/dev/null | \
  xargs -0 sha256sum 2>/dev/null | tee "/tmp/legacy-import-manifest-$(date +%Y%m%d).txt"

# 🔧 operator sudo：把舊樹整包搬到 quarantine（唯讀、cortex-svc 擁有），不併入新樹
sudo install -d -o cortex-svc -g cortex-svc -m 0700 "$AGENTS_ROOT/legacy-imported"
sudo cp -a "$OLD/." "$AGENTS_ROOT/legacy-imported/" 2>/dev/null || true
sudo chown -R cortex-svc:cortex-svc "$AGENTS_ROOT/legacy-imported"
sudo chmod -R a-w "$AGENTS_ROOT/legacy-imported"     # 唯讀，僅供查詢
```

> **關鍵**：新樹（3a/3b）是**空的、乾淨的**；舊 state 只進 `legacy-imported/`
> quarantine，標記為不可信、唯讀、僅供歷史查詢。**不**把舊 `jobs.json`／evidence
> 併入新 `COORDINATOR_ROOT`。任何切換後產生、無正常來源的 record 走正常 gate；
> 帶 `legacy-imported` 來源者 **MUST NOT** 被 ship gate 採計。

> **⚠️ 未決 3**：`legacy-imported` attestation 的實體格式（Phase 3 R6(e) 的
> `trust: legacy-imported` 標記由簽章方案落地）。Phase 2b 先做「物理隔離＋內容
> hash manifest」；正式 attestation 待 Phase 3。operator 需確認：切換期間是否需要
> 把任何 in-flight 的 job 手動收尾（見第 8 步回滾對照）。

### 3d. 移除舊 775 的 g+w

```bash
# 🔧 operator sudo：收斂現有 g+w（~/.agents 與 config/paulsha 實測 775）
#   注意：新樹已在 3a 以正確 mode 建立；此步是清理**舊路徑殘留**，避免仍被引用。
chmod -R g-w,o-w "$HOME/.agents" 2>/dev/null || true

# ✅ 驗證：新樹 mode 與產生器一致、舊路徑不再 g+w
sudo ls -ld "$CONTROL_ROOT" "$COORDINATOR_ROOT" "$WORKTREE_ROOT"
```

**執行後驗證（整步）**：以 `cortex-builder` 身分試寫 Manager-owned 樹須 `EACCES`
（見第 7 步 R9 族 2）。

---

## 第 4 步：Manager 遷 root-owned 部署 + system-level unit

### 4a. pipx tree 遷出 operator HOME

```bash
# ⚠️ 未決 4：Manager 部署路徑的確切位置待 operator 定。
#   候選：/opt/cortex/venv（root 擁有、唯讀 for svc）。以下為示例。
export DEPLOY_ROOT="/opt/cortex"                     # ⚠️ 待定

# 🔧 operator sudo：把現有 pipx venv 複製到 root-owned 部署路徑（不是原地 chown）
sudo install -d -o root -g root -m 0755 "$DEPLOY_ROOT"
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" "$DEPLOY_ROOT/venv"
sudo chown -R root:root "$DEPLOY_ROOT/venv"
sudo find "$DEPLOY_ROOT/venv" -type d -exec chmod 0755 {} +
sudo find "$DEPLOY_ROOT/venv" -type f -exec chmod a-w {} +   # site-packages 對 svc/headless 唯讀
```

> spec §R3：executable／deps／launcher／venv 對 headless **不可寫**，owner=root。
> 這封掉背景 §5 的「改寫 verifier／注入 `sitecustomize.py`／`.pth`」攻擊面。

### 4b. bootstrap env 遷入受保護樹（fail-closed）

```bash
# 🔧 operator sudo：env 檔 root 擁有、0644（全部行程唯讀，spec §R3「全部行程唯讀」）
export ENV_DIR="$AGENTS_ROOT/core/runtime"
sudo install -d -o root -g root -m 0755 "$ENV_DIR"
# 依 permgen 的 deployment 區塊（runtime-bootstrap-env）產生 owner/mode：
#   chown root:root <PATH:runtime-bootstrap-env>; chmod 0644 ...
```

> env 檔內容須含 instance-scoped 的 `PSC_AGENTS_ROOT`／`PSC_CONTROL_ROOT`／
> `PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT`／`PSC_MONITOR_STATE_ROOT`，全部指向第 2 步
> 的目標路徑（修掉背景 §7 漏洞）。

### 4c. system-level unit（以 cortex-svc 跑、加固指令切實列出）

```bash
# ⚠️ 未決 5：unit 最終形態（含 ReadWritePaths 的精確清單）待 operator 定稿後寫入。
# 🔧 operator sudo：寫 /etc/systemd/system/cortex-manager.service（示例骨架）
sudo tee /etc/systemd/system/cortex-manager.service >/dev/null <<'UNIT'
[Unit]
Description=cortex Manager (trust-root Phase 2, system-level)
After=network.target

[Service]
Type=simple
User=cortex-svc
Group=cortex-svc

# --- fail-closed env（移除 EnvironmentFile 的 '-'，缺檔即拒絕啟動，spec §R3）---
EnvironmentFile=/var/lib/cortex/agents/core/runtime/cortex-manager.env

ExecStart=/opt/cortex/venv/bin/cortex service run
WorkingDirectory=/var/lib/cortex

# --- systemd 加固（spec §R3；現況三個 unit 一項都沒有）---
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectClock=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=
# durable state 樹是唯一可寫路徑白名單（由 R1 登記表產生，勿手擴）：
ReadWritePaths=/var/lib/cortex/agents /var/lib/cortex/worktrees

[Install]
WantedBy=multi-user.target
UNIT

# 🔧 operator sudo：載入與啟用（system-level，非 --user）
sudo systemctl daemon-reload
sudo systemctl enable --now cortex-manager.service
```

> **`EnvironmentFile` 無 `-` 前綴**＝缺檔 fail-closed（spec §R3 Scenario「刪除
> EnvironmentFile」）。`CapabilityBoundingSet=`（空）＝丟掉所有 capability，且因為
> 未決 1 已裁決走 `systemd-run`（由 PID 1 代為切換身分，Manager 自己不需要任何
> capability），**這裡維持空集合，不加 `AmbientCapabilities=`**——落選的 CAP_SETUID
> 方案才需要放寬，那正是它被否決的理由。

```bash
# ✅ 驗證：unit 以 cortex-svc 跑、加固生效
systemctl show cortex-manager.service -p User -p NoNewPrivileges -p ProtectSystem -p ReadWritePaths
systemctl status cortex-manager.service
sudo journalctl -u cortex-manager.service -n 50 --no-pager
```

> **WSL2 注意**：system-level unit 在 WSL2 需 `systemd=true`（`/etc/wsl.conf`）且已
> `is-system-running: running`（第 0 步已驗）。system unit **不需** lingering
> （lingering 只對 `--user` unit 有意義）。若 WSL 重啟後 systemd 未起，見第 8 步回滾。

---

## 第 5 步：降權啟動器啟用（Manager → cortex-builder，關 FD、不傳 token）

Manager（cortex-svc）spawn headless job 時降到 `cortex-builder`，且：
- **關閉 FD 傳遞**（`close_fds`，不繼承任何指向受保護資產的 fd——spec §R9 T4.1）；
- **不傳 gh token**（scrub `GH_TOKEN`／`GITHUB_TOKEN`；GitHub 寫入改由 Manager 代理，
  沿用 D1 outbox）；
- 只暴露該 job 自己被 chown 的 worktree 子目錄。

> **✅ 未決 1 已裁決（operator 0816 第二輪）**：降權機制＝**`systemd-run` transient
> unit**（方案 A）。落選案留檔備查：B（最小 setuid 助手）引入 setuid 二進位＝新攻擊面；
> C（Manager 授 `CAP_SETUID`）可切到**任意** uid，等於放大 Manager 權限。
>
> 程式碼面已落地（Phase 2a，`paulsha_cortex/coordinator/job_runner.py`）：
> `PSC_JOB_RUNNER` 預設 `direct`（現行行為不變），設為 `systemd-run` 才降權。
> **本步驟即「把那個開關打開」的部署動作**，前提是本 runbook 第 1 步的帳號已建立、
> 以及下面的 polkit 規則已就位。

### 5a. Manager 端會實際發出的 argv（polkit 規則要收窄的那個面）

`job_runner.build_systemd_run_argv()` 產出的形狀是**封閉**的（新增旗標要改程式碼並過
測試），因此 polkit 規則可以據此收窄：

```text
systemd-run --quiet --collect --pipe --wait \
  --unit=cortex-job-<job_id 片段>-<sha256 前 8 碼>.service \
  --uid=cortex-builder --gid=cortex-builder \
  --service-type=exec \
  --working-directory=<該 job 的 worktree> \
  --property=NoNewPrivileges=yes \
  --setenv=<白名單 env 逐項> \
  -- bash -c '<job wrapper script>'
```

- unit 名前綴固定為 `cortex-job-`（`job_runner.UNIT_NAME_PREFIX`，polkit 以
  `action.lookup("unit")` 比對的就是它）；接上 job_id 的 sha256 前 8 碼確保消毒後仍唯一。
- `--wait` 讓 client 與 unit 同壽命，Manager 既有的 `pid_alive()` 判活不必改。
- `--quiet` 是必要的：`systemd-run` 的狀態訊息會被 `--pipe` 導進 job 的 JSONL log，
  那份 log 是 terminal evidence 的來源。
- FD：只有 stdin/stdout/stderr 三個（Python `close_fds=True`），且 **stdin 顯式接
  `/dev/null`**；stdout/stderr 是 Manager 開的 job log。
- **env 不是 scrub 黑名單，而是白名單**：transient unit 不繼承呼叫端的 environ，
  job 只看得到 `--setenv` 列出的那幾項（`PATH`／`LANG`／`LC_ALL`／`LC_CTYPE`／
  `SSL_CERT_FILE`／`SSL_CERT_DIR`／`NODE_EXTRA_CA_CERTS` ＋ `PSC_JOB_ID`／
  `PSC_SLICE_ID`／`PSC_REPO_ROOT`／選配的 `PSC_RELAY_TARGET`／`HOME`）。
  gh token、daemon 的 `CLAUDE_CONFIG_DIR`／`GH_CONFIG_DIR` 都**不在**白名單上。

```bash
# ✅ 驗證（不需 root）：印出本機會發出的白名單與旗標，與上表逐項對照
python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
for item in job_runner.BUILDER_FORWARDED_ENV:
    print(f"{item.name:22} {item.rationale}")
print("synthesized:", job_runner.BUILDER_SYNTHESIZED_ENV)
print("unit prefix:", job_runner.UNIT_NAME_PREFIX)
print("properties :", job_runner.TRANSIENT_UNIT_PROPERTIES)
PY
```

### 5b. polkit 規則（收窄到「只能以 cortex-builder 身分、限定 unit 名」）

```bash
# 🔧 operator sudo：/etc/polkit-1/rules.d/49-cortex-spawn.rules
sudo tee /etc/polkit-1/rules.d/49-cortex-spawn.rules >/dev/null <<'RULES'
// 僅允許 cortex-svc 起「cortex-job-」前綴的 transient unit（最小授權）。
// 注意：unit 名是 Manager 端產生的（job_runner.UNIT_NAME_PREFIX），這條規則與
// 那個常數是成對契約——改任一邊都必須同步改另一邊。
polkit.addRule(function(action, subject) {
  if (subject.user !== "cortex-svc") {
    return polkit.Result.NOT_HANDLED;
  }
  if (action.id !== "org.freedesktop.systemd1.manage-units") {
    return polkit.Result.NOT_HANDLED;
  }
  var unit = action.lookup("unit");
  if (unit && unit.indexOf("cortex-job-") === 0 && unit.indexOf(".service", unit.length - 8) !== -1) {
    return polkit.Result.YES;
  }
  return polkit.Result.NOT_HANDLED;
});
RULES
```

> **⚠️ 殘餘限制（誠實標註）**：polkit 的 `manage-units` action 只暴露 unit **名稱**，
> **不暴露 `User=`／`--uid=`**。因此「只能降到 cortex-builder」這一半在 polkit 層
> 無法強制，只能由 Manager 端的封閉 argv 產生器保證（`--uid` 來自
> `PSC_BUILDER_ACCOUNT`，值受 POSIX 帳號名 pattern 檢查、拒絕注入）。要在 OS 層
> 也硬化這一半，需另設一支 root 擁有的 wrapper unit template 並只授權該 template
> ——屬 Phase 3 的加固，不是 0.2.0 的 join gate。

### 5c. 打開開關（Manager env）

```bash
# 🔧 operator sudo：把降權模式寫進 Manager 的 EnvironmentFile（見第 4b 步）
#   PSC_JOB_RUNNER=systemd-run        # 必要；預設 direct＝不降權
#   PSC_BUILDER_ACCOUNT=cortex-builder # 選配（預設值即此）
#   PSC_BUILDER_HOME=/var/lib/cortex-builder  # 選配；未設時 systemd 依 passwd 填
#   PSC_BUILDER_PATH=...              # 選配；模型 CLI 不在 Manager PATH 上時用
sudo systemctl restart cortex-manager
```

builder 帳號必須自己具備模型 CLI 的登入態（`sudo -u cortex-builder claude /login` 之類），
**Manager 不會把自己的憑證傳過去**——那正是本步驟的重點。

```bash
# ✅ 驗證 1：以 cortex-svc 起一個降到 cortex-builder 的 transient job，檢查身分/FD/token
sudo -u cortex-svc systemd-run --quiet --collect --pipe --wait \
  --unit=cortex-job-smoke-00000000.service \
  --uid=cortex-builder --gid=cortex-builder --service-type=exec \
  --property=NoNewPrivileges=yes \
  /bin/sh -c 'id; echo "GH_TOKEN=[$GH_TOKEN]"; ls -l /proc/self/fd'
# 期望：uid=cortex-builder；GH_TOKEN 為空；/proc/self/fd 只有 0/1/2
```

```bash
# ✅ 驗證 2（負控制）：暫時移除 polkit 規則後，dispatch 必須 fail-closed 而非退回 direct
#   期望：job 落 needs_human，理由碼 job-runner-transient-unit-start-failed，
#   detail 帶 systemd-run 的實際拒絕訊息。**不得**出現以 cortex-svc 身分跑起來的 job。
```

---

## 第 6 步：Manager 升級流程（設定後升級需 root）

切換後，Manager 部署在 root-owned 樹（`/opt/cortex/venv`），升級**需 root**。設計一個
**受控升級步驟**——驗證來源後**替換整棵 svc-owned/root-owned tree**，而非裸 `chown`。

```bash
# ✅ 驗證：先在 operator HOME 的 pipx 環境 build/驗證新版（不碰部署樹）
pipx run --spec paulsha-cortex==<新版> cortex --version   # 或既有 build 流程
# 產出 wheel/venv 後，先算內容 hash 與現行部署對照差異

# 🔧 operator sudo：受控替換（atomic：新樹旁建→驗→切 symlink→保留舊樹回滾）
export NEW="/opt/cortex/venv.next"
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" "$NEW"
sudo chown -R root:root "$NEW"
sudo find "$NEW" -type f -exec chmod a-w {} +
# 驗證新樹自檢通過後才切換：
sudo -u cortex-svc "$NEW/bin/python" -m paulsha_cortex.trust_root selfcheck
sudo ln -sfn "$NEW" /opt/cortex/venv.active   # ExecStart 指向 venv.active
sudo systemctl restart cortex-manager.service
```

> **不裸 chown**：升級不是「把 headless 產出的檔 chown 給 svc」，而是「operator 驗證
> 來源後，以 root 身分整棵替換部署樹」。任何 headless 都碰不到 `/opt/cortex`。
> 舊樹（`venv.active` 前一目標）保留一個 release 供回滾（第 8 步）。

> **⚠️ 未決 6**：是否提供 `cortex install trust-root --system` 子命令把此流程
> codify（spec 未決問題 #3）。codify 較可重複，但等於把特權操作寫進 codebase，本身
> 需審查。Phase 2b 先走手動 runbook；子命令化留待 operator 決定。

---

## 第 7 步：切換驗收（Phase 1 自檢轉綠 + R9 四族實測）

### 7a. Phase 1 自檢切 fail-closed 應綠

```bash
# ✅ 驗證：以 cortex-svc（Manager 身分）跑自檢——Manager-owned 樹應無 job-writable
sudo -u cortex-svc /opt/cortex/venv.active/bin/python \
  -m paulsha_cortex.trust_root selfcheck
# 期望：ok=true、job_writable_count=0（對照第 0 步 baseline 的紅）
```

### 7b. R9 四族攻擊測試（以 `cortex-builder` shell 實測攻不進）

每族都要有 **negative control**（以受信任身分執行相同動作**必須成功**，否則是環境
壞掉的假綠，spec §R9）。

```bash
# 取一個 cortex-builder shell（經第 5 步降權機制）
BUILDER='sudo -u cortex-svc systemd-run --uid=cortex-builder --pipe --wait --collect /bin/sh -c'

# 族 1 capability-theft
$BUILDER 'cat /var/lib/cortex/agents/core/runtime/*.env'      # 期望 EACCES（或無此權限）
$BUILDER 'printf x > /var/lib/cortex/agents/control/requests/evil.json'  # 期望 EACCES

# 族 2 durable-state-tampering（對每個 Tier-0/1 資產：modify/truncate/delete/replace/symlink/rollback）
$BUILDER 'printf "{}" > /var/lib/cortex/agents/coordinator/jobs.json'    # 期望 EACCES、jobs.json 位元不變
$BUILDER 'rm -f /var/lib/cortex/agents/coordinator/evidence/*'          # 期望 EACCES
# review verdict 代寫（背景 §3 最短攻擊路徑）——builder 對 reviewer 產出零寫入
$BUILDER 'printf "{\"verdict\":\"pass\"}" > <reviewer_worktree>/.psc-review-verdict.json'  # 期望 EACCES

# 族 3 enforcement-plane-tampering（每案改完 MUST 實際重啟服務再驗證）
$BUILDER 'echo x >> /opt/cortex/venv.active/lib/python*/site-packages/sitecustomize.py'  # 期望 EACCES
$BUILDER 'echo x > /etc/systemd/system/cortex-manager.service'          # 期望 EACCES

# 族 4 行程間路徑
$BUILDER 'cat /proc/$(pgrep -u cortex-svc -n cortex)/environ'           # 期望 EACCES
$BUILDER 'ls -l /proc/self/fd'                                          # 期望：無可寫的受保護 fd

# negative control（以 cortex-svc 執行族 2 的寫入，必須成功）
sudo -u cortex-svc /bin/sh -c 'touch /var/lib/cortex/agents/coordinator/.wtest && rm -f /var/lib/cortex/agents/coordinator/.wtest && echo NEG-OK'
```

**通過條件**：族 1–4 全部被 OS 拒絕（`EACCES`／`EPERM`／`ECONNREFUSED`）；每族的
negative control 成功；族 3 每案**重啟服務後**仍綠。逐項對照 spec §R9 的 T1–T4 表。

> **⚠️ 未決 7**：R9 的完整自動化矩陣（對登記表**每一項** Tier-0/1 資產機械展開族 2）
> 是 Phase 2 的 R9 測試工項，**不在本 runbook**——本步是 operator 手動抽驗；完整
> 矩陣另由測試碼交付並掛 CI。

---

## 第 8 步：回滾（每階段的退路 → 退回 Phase 1 降級運轉）

Phase 1 完全不需 root 且含降級運轉安全網（`PSC_DEGRADED_OPERATION=per-case-approval`）。
任一階段出問題即退回「operator 帳號跑 + 降級運轉」。

| 出問題的階段 | 症狀 | 回滾動作（`🔧 operator sudo`） |
|---|---|---|
| 第 4c（system unit） | WSL2 重啟後 systemd 未起、Manager 起不來 | `sudo systemctl disable --now cortex-manager.service`；改回 operator 的 `systemctl --user` 舊 unit；确認 `PSC_DEGRADED_OPERATION` 仍在 |
| 第 4c 加固誤擋 | 服務起來但功能靜默失效（`ProtectSystem`/`ReadWritePaths` 擋到寫入路徑） | 先看 `journalctl -u cortex-manager` 的 EPERM；把被擋路徑加進 `ReadWritePaths`（且回填 R1 登記表）→ `daemon-reload` + `restart` |
| 第 5（降權機制） | polkit/systemd-run 在 WSL2 不如預期、job spawn 不了 | 暫停 headless 派工；Manager 以 `per-case-approval` 降級運轉（不 spawn），等機制修好 |
| 第 3（分樹） | 新樹路徑契約破壞既有 instance | env 檔改指回 `~/.agents`（保留舊路徑一個 release 為唯讀相容層）；`legacy-imported/` 仍在，未污染新樹 |
| 全面回退 | 任一不可解 | 停 system unit、回 operator HOME 部署、`PSC_DEGRADED_OPERATION=per-case-approval`（或 `disabled`），fleet 回到 Phase 1 狀態；**join gate 維持未通過**（不得宣告 0.2.0 stable，spec §R12） |

```bash
# ✅ 驗證（回滾後）：回到 Phase 1，自檢回 WARN-only、降級運轉生效
python3 -m paulsha_cortex.trust_root selfcheck            # 預期回到有 WARN 的 Phase 1 狀態
echo "PSC_DEGRADED_OPERATION=$PSC_DEGRADED_OPERATION"     # 期望 per-case-approval 或 disabled
```

> **回滾原則**：舊 state 走 legacy-import（第 3c）而非併入，所以回滾時新樹可整棵丟棄
> 而不遺失舊資料；`legacy-imported/` quarantine 是唯讀副本，兩邊都不互相污染。

---

## 未決點彙整（需 operator 拍板）

1. **降權機制最終形態**（最高風險）：`systemd-run`+polkit／setuid 助手／CAP_SETUID
   三擇一（第 5 步）。**未定案前第 5 步不可執行。**
2. **目標路徑位置**：`AGENTS_ROOT`／`WORKTREE_ROOT` 落 `/var/lib/cortex` 或保留
   `~/.agents` 改 owner（第 2、3 步）。
3. **legacy-import attestation 格式**：Phase 2b 只做物理隔離＋hash manifest；
   `trust: legacy-imported` 正式標記待 Phase 3 簽章（第 3c 步）。
4. **Manager 部署路徑**：`/opt/cortex/venv` 或其他 root-owned 位置（第 4a 步）。
5. **unit 加固最終清單**：`ReadWritePaths` 的精確白名單（由 R1 登記表產生，第 4c 步）。
6. **是否 codify 成 `cortex install trust-root --system`**（第 6 步；spec 未決問題 #3）。
7. **R9 完整自動化矩陣**：本 runbook 只手動抽驗；完整矩陣由測試碼另交付（第 7b 步）。

## 對 WSL2 環境，本 runbook 最不確定／最高風險的步驟

1. **第 5 步降權機制（最高風險）**：機制已裁決為 `systemd-run` transient unit 且程式碼
   已落地（`PSC_JOB_RUNNER=systemd-run`），但 WSL2 上 polkit daemon 是否常駐、
   `systemd-run --uid` 的實際行為仍需**先在拋棄式環境跑通 R9 族 4** 再上正式機。
   啟動器對「systemd 不可用／帳號不存在／unit 起不來（含 polkit 拒絕）」一律
   fail-closed，不會靜默退回同 UID 執行——負控制驗證見第 5c 步驗證 2。
2. **第 4c system-level unit 在 WSL2 的持久性**：WSL 關閉/重啟後 systemd system session
   是否可靠拉起 `cortex-manager.service`；lingering 對 system unit 無效，需驗 boot 行為。
3. **第 4c 加固誤擋**：`ProtectSystem=strict` + `ReadWritePaths` 白名單若漏列某條實際
   寫入路徑，會**靜默**功能失效（服務起得來但寫不進）；務必先跑既有 E2E 再收窄。
