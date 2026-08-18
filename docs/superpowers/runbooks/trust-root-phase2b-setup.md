---
status: executable
work_item: trust-root-isolation
phase: 2b
audience: operator
supersedes: none
tracking: "#584 — trust-root D6 母議題（本票 reference-only，不 auto-close；R-17 走 policy-exempt:issue-link 豁免）"
decision: "operator 0816 第三輪裁決（#584 留言）——降權機制定案 **A+B**：UID 三分 ＋ root-owned template unit，單一路徑，無 A／B 分歧"
refs:
  - docs/superpowers/specs/trust-root-isolation-spec.md
  - paulsha_cortex/trust_root/registry.py
  - paulsha_cortex/trust_root/permgen.py
  - paulsha_cortex/coordinator/job_runner.py
---

# trust-root Phase 2b：root 設定 runbook（可執行版．A+B 單一路徑）

> **本文件是可執行版**：每一步的命令可直接複製、每一步有驗證、每一步可回滾。
> 文件本身**不執行任何 root 操作**；所有 `sudo` 都由 operator 親自輸入。
> **cortex 任何元件永不具 root**——cortex 只**產生**命令字串與驗證結果，
> root 操作一律由 operator 手動執行（0816 裁決，未決 6）。

實作 `trust-root-isolation-spec.md` 的 **Phase 2**（spec §R10 Phase 2 第 1–8 步）。
Phase 2a 的權限產生器（`paulsha_cortex/trust_root/permgen.py`）把 R1 登記表機械轉成
目標 `owner:group mode`、systemd unit 與 polkit 規則；本 runbook 是把那份計畫**落到
OS** 的手動流程。

---

## 0816 第三輪裁決定案表（本 runbook 的前提）

| 未決點 | 定案（0816 第三輪，#584 留言） | 落在本 runbook |
|---|---|---|
| **降權機制** | **A+B 並行，單一路徑**。A＝UID **三分提前**；B＝**root-owned template unit**（#643 起每個角色兩份加固剖面、#615 起兩個角色 ⇒ 共**四份**：`cortex-job@` / `cortex-job-jit@` / `cortex-reviewer-job@` / `cortex-reviewer-job-jit@`，`User=` 四份都寫死）。C（code-level argv 保證）自動保留為第三層，由 **root-owned shim** 承接 | 第 1、5 步 |
| **UID 方案** | **三分為唯一路徑**：`cortex-manager`（Manager＋monitor，durable state owner，持 spawn 授權，**不跑任何模型程式碼**）／`cortex-reviewer-planner`（reviewer＋planner 模型 job）／`cortex-builder`（builder 模型 job）。`permgen.THREE_WAY_SCHEME` 由備選轉為**定案方案** | 第 1 步 |
| durable state 路徑 | **`/var/lib/cortex`**；worktree pool＝**`/var/lib/cortex/worktree`** | 第 2 步 |
| legacy-import | **物理隔離 ＋ hash manifest**（無簽章；簽章屬 Phase 3）。切換前 in-flight job **手動收尾** | 執行前提、第 3 步 |
| Manager 部署 | **`/opt/cortex`**（root 擁有，對服務唯讀）；system-level unit，`User=cortex-manager` | 第 4 步 |
| `ReadWritePaths` | **由 R1 登記表經 permgen 機械產生**，不手寫；monitor 再多一層 persona 過濾，嚴格窄於 Manager | 第 4c／4d 步 |
| root 命令 codify | **不 codify**——不提供 `cortex install trust-root --system`；cortex 只產生命令字串 | 第 6 步 |
| R9 | **手動抽驗**（#629 起共**七族**：runbook 族 1–4 ＋ 族 5 privilege-boundary ＋ 族 6 verdict 通道 ＋ 族 7 gate 執行身分。完整自動化矩陣屬另一工項） | 第 8 步 |

> **歷史註記（二分）**：0816 第二輪曾以二分（`cortex-svc` / `cortex-builder`）先行、
> 三分為保留彈性；第三輪裁決後二分（`permgen.TWO_WAY_SCHEME`）**不再是可選路徑**，
> 僅保留在程式碼中作為方案參數化的對照組，本 runbook 全文一律 `four-way`。

### 為什麼是 A+B（而不是二選一）

#603 實測確認一個**硬限制**：polkit 的 `org.freedesktop.systemd1.manage-units` action
**只暴露 unit 名稱與 verb**，**不暴露 `User=`／`--uid=`**。授權之後，systemd 會照請求的
**任意** `User=` 起 unit。

| polkit **能**強制 | polkit **不能**強制 |
|---|---|
| 呼叫者是哪個 UID（`subject.user`） | job 降到哪個帳號（`User=`／`--uid=`） |
| unit 名前綴／pattern（`action.lookup("unit")`） | 任何 unit 屬性（`AmbientCapabilities=`、`ExecStart=`…） |
| verb（只放行 `start`／`stop`） | — |

單靠 polkit 收窄，「只能降到 `cortex-builder`」這一半守不住。裁決因此把邊界拆成兩層，
**兩層一起裝**：

- **A（三分）縮小「誰持有授權」**：polkit 的 subject 只有 `cortex-manager`，而
  `cortex-manager` **不跑任何模型程式碼**。prompt injection 可達的進程
  （builder／reviewer／planner job）因此**完全不在授權面上**。
- **B（template unit）縮小「授權能做什麼」**：被授權的唯一動作是 start／stop 一個
  **root-owned** template 的 instance；`User=cortex-builder`、加固段、`ExecStart=` 全部
  寫死在 root 擁有的檔案裡。**即使 `cortex-manager` 整個被攻陷，也改不了 `User=`、
  傳不了屬性、換不掉 argv 的入口。**
- **C（argv 保證）由 root-owned shim 承接**：`ExecStart=` 指向 `/opt/cortex/bin/` 底下
  root 擁有的 shim，shim 讀 Manager-owned 的 job-spec spool 導出 argv。argv 的**形狀**
  因此由 root-owned 程式決定，Manager 只能給參數。

**transient unit（原方案 A 的主路徑）在本 runbook 中不再是主路徑**——polkit 規則
**不授權** `manage-units` 的 transient 建立。它只保留在 **附錄 B（降級備援）** 供
「template 路徑在本機不可用」時臨時使用，並在那裡逐條標明殘餘風險。

### 分段落地（M1／M2）——請先讀完再開工

| 里程碑 | 內容 | 本 runbook |
|---|---|---|
| **M1** | 三帳號建立、**檔案權限面完整三分**、builder job 經 `cortex-job@.service`／`cortex-job-jit@.service` 降權、polkit 只授 `cortex-manager` 對這**兩個具名模板** | ✅ 已於 2026-08-17 實機完成 |
| **M2** | reviewer／planner job 也改經 template instance（`User=cortex-reviewer-planner`）落到自己的帳號 | ✅ 程式碼已落地（**#615**）。落檔與驗證步驟已收進本 runbook：第 5-2 步落**四份** unit、5-4 polkit 涵蓋四個字幹、5-5 補 reviewer 的 env、5-6b 正向、5-7 反向、8b-2 verdict 端到端 |

**M2 之後可以宣稱的**：三個會跑模型的 persona（builder／reviewer／planner）**啟動面
全部離開 Manager 的 UID**，且每一個的 `User=` 都寫死在 root-owned 的 template unit 裡
——「**injection 可達的進程皆無 spawn 授權**」這條的**全稱**因此成立，D6 的「三分已
生效」不再被 #615 blocking。

**M2 仍未涵蓋的（不得順手宣稱）**：

- **gate 執行身分**（#629）：gate 命令在 builder 完全掌控的 worktree 裡跑，`pytest`
  會載入該 worktree 的 `conftest.py` ⇒ 執行者取得任意程式碼執行。**刻意不掛在
  `cortex-reviewer-planner` 上**——那會讓被攻陷的 builder 經由 gate 執行影響到寫
  verdict 的那個帳號，把 #638／#639 剛修好的東西整個抵銷。它需要**第四個帳號**，
  屬 #629。在那之前降權 build 卡對 `require_ledger` fail closed。
- **reviewer 的 executor 憑證就地 refresh**：`cortex-reviewer-planner` 的 `~/.codex`
  骨架目錄已由 `scaffold_directories()` 建出並保護（root-owned），憑證檔由 root 於
  第 4e 步複製並 chown 給它；但**該檔不在 reviewer 模板 unit 的 `ReadWritePaths=`
  內**（登記表目前只登記 `builder-executor-credential` 一份，理由見該資產的 note：
  在二分部署上登記第二份會讓 Manager unit 的 RWP 指向一條不存在的路徑而起不來）。
  淨效果：reviewer 的 token 過期時**無法自行 refresh**，需 operator 重跑第 4e 步。
- **reviewer 的工作樹位置**：仍是 Manager provision 的 review worktree
  （`<來源樹>/.psc-review-worktrees/…`），不是 per-job clone。reviewer 是 read-only
  契約，對它只需**唯讀**可達；per-job clone 化屬 #623／#648 的範圍。

---

## 執行前提（開工前逐項確認，全部 `✅ 驗證`）

### 兩個硬性 gate——不通過就**不要開工**

這兩條是 M1 實機（2026-08-17）踩到的**部署環境**前提，不是 cortex 的缺陷；
但任一條沒守住，底下九項全部驗綠也沒有意義——整個降權設計會靜默歸零。
**它們是 gate，不是提示**：不符合就停下來處理，處理完再回到這裡。

```bash
# ✅ G1（硬性 gate）：`acl` 套件必須已安裝
#    缺 acl 時 setfacl 全數失效，跨帳號授權整段變成**無聲 no-op**——append-only
#    出口與 job-spec spool 的唯讀 ACL 全部不存在，而權限 script 仍會以 exit 0
#    收場（第 2 步看起來全綠）。這就是它從第 2 步的「稽核 4」上移成 gate 的理由。
command -v setfacl && command -v getfacl && setfacl --version | head -1
#   期望：三行都印得出來。
#   若缺：`sudo apt-get install -y acl`，裝完重跑本條。**未通過不得往下做。**

# ✅ G2（硬性 gate）：`/etc/sudoers.d/` 不得有萬用 NOPASSWD 規則
#    M1 本機原有 `ALL ALL=NOPASSWD: ALL`，第 1 步新建的三個服務帳號
#    **一建立就自動取得無密碼 root**（`sudo -l -U cortex-builder`
#    → `(root) NOPASSWD: ALL`）。整個降權設計因此歸零——builder 不必攻擊任何
#    邊界，直接 `sudo` 就好；第 1 步「三帳號皆無 sudo 授權」那條驗證會是紅的。
sudo grep -rnE '^[[:space:]]*ALL[[:space:]]+ALL[[:space:]]*=' /etc/sudoers /etc/sudoers.d/
#   期望：**空輸出**（grep 找不到時 exit 1，屬正常）。
sudo -l -U nobody 2>&1 | tail -3
#   期望：`not allowed to run sudo` 之類。**若印出 `(root) NOPASSWD: ALL`**，
#   代表規則對「任何」使用者成立，新帳號一建立就吃得到。
#
#   若命中：**先把規則收斂到具名帳號，再建帳號**——把
#   `ALL ALL=(ALL) NOPASSWD: ALL` 改成 `<operator 帳號> ALL=(ALL) NOPASSWD: ALL`
#   （M1 即如此收斂，三帳號複驗為 `is not allowed to run sudo`）。
#   **順序不可調換**：先建帳號再收斂規則，中間那段時間三個服務帳號就是 root。
```

**G1／G2 判定**：任一不通過 ⇒ 停止並處理，不得「先做下去之後再補」。
第 1 步末尾有一條複驗（`sudo -l -U` 對三帳號），與 G2 成對——G2 過了但複驗紅，
代表建帳號的過程中又引入了新規則。

### 其餘九項

```bash
# ✅ 1. in-flight job 手動收尾（裁決：切換期間不得有半途 job）
cortex jobs
cortex status
#   期望：`cortex jobs` 無 running／dispatched 狀態的列；`cortex status` 無 pending request。
#   若有：(a) 等它自然結束；或 (b) `cortex slice-action` / `cortex work` 以 operator
#   身分明示收尾後再繼續。
#   **不可**在有 in-flight job 時切換——舊樹被隔離後那些 job 的 evidence 會變孤兒。

# ✅ 2. Manager／Monitor 服務已停（切換期間不得有行程持續寫舊樹）
systemctl --user stop cortex-manager.service cortex-monitor.service 2>/dev/null || true
systemctl --user is-active cortex-manager.service || echo "manager stopped"
pgrep -a -f "paulsha_cortex" || echo "no cortex process"

# ✅ 3. Phase 1 自檢 baseline（切換後要對照）
python3 -m paulsha_cortex.trust_root selfcheck \
  | tee "/tmp/trust-root-baseline-$(date +%Y%m%d-%H%M).json" >/dev/null
echo "baseline saved"
#   M1 實測參考值：baseline `job_writable_count` = **5**、`ok: false`。
#   這是預期的紅——第 7 步應收斂為 0（見「M1 實機基準值」表）。

# ✅ 4. 登記表雙向等式必須綠（紅＝盤點已漂移，先修 Phase 1 再繼續）
python3 -m paulsha_cortex.trust_root equation

# ✅ 5. sudo 可用（會要密碼；只取得授權，不做任何變更）
sudo -v

# ✅ 6. system systemd 與 polkit 皆在跑（缺 polkit＝第 5 步降權必失敗）
systemctl is-system-running || true          # 期望 running；degraded 亦可，記錄之
systemctl is-active polkit.service || systemctl is-active polkitd.service
#   期望 active。若 inactive：`sudo systemctl enable --now polkit.service`
#   若本機根本沒有 polkit 套件，**第 5 步不可執行**（見第 9 步回滾）。

# ✅ 7. 磁碟空間足夠複製整棵 venv 與舊 state
df -h /var /opt
du -sh "$HOME/.agents" "$HOME/.local/share/pipx/venvs/paulsha-cortex" 2>/dev/null

# ✅ 8.（A+B 新增）三分方案的三個帳號名稱皆未被占用，且產生器認得三分
getent passwd cortex-manager cortex-reviewer-planner cortex-builder \
  && echo "!! 已有同名帳號，先確認來源再繼續" || echo "three accounts free: OK"
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.registry import Principal
s = permgen.SCHEMES["four-way"]
print("scheme_id          :", s.scheme_id)
print("manager            :", s.resolve(Principal.MANAGER))
print("monitor            :", s.resolve(Principal.MONITOR))
print("reviewer           :", s.resolve(Principal.REVIEWER))
print("planner            :", s.resolve(Principal.PLANNER))
print("builder            :", s.resolve(Principal.BUILDER))
print("durable_state_owner:", s.durable_state_owner)
print("headless_accounts  :", sorted(s.headless_accounts()))
PY
#   期望：manager/monitor → cortex-manager（＝durable_state_owner）；
#         reviewer/planner → cortex-reviewer-planner；builder → cortex-builder。

# ✅ 9.（A+B 新增）template-instance 模式是否已在程式碼落地（決定第 5 步 (d) 能不能開）
python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
modes = getattr(job_runner, "RUNNER_MODES", ())
print("RUNNER_MODES =", modes)
print("systemd-template available:", "systemd-template" in modes)
PY
#   期望：`RUNNER_MODES = ('direct', 'systemd-run', 'systemd-template')`、True。
#   （#616 已 merge，template 模式與 root-owned shim 皆已在主線落地。）
#   若 False（部署的 wheel 比 #616 舊）：第 5 步的 (a)(b)(c) 仍可全部安裝並用 5-7
#   的反向測試證明邊界，只有 (d) 切換點暫不打開（Manager 維持 per-case-approval
#   不 spawn job）——但正確處置是先把部署樹升到含 #616 的版本。
```

**通過條件**：**G1／G2 皆通過**（缺一即停）；1–8 全部符合期望；`equation` 回傳
`ok: true`；baseline JSON 已存檔；第 9 項無論真假都**記錄**在 #584
（決定本次是否走到 (d)）。
Phase 1 自檢此時**預期為紅**（有 `job-writable` finding）——那正是本 runbook 要收斂的
清單，切換完成後（第 7 步）應轉綠。

---

## 當前環境事實（WSL2）

| 項目 | 現況 |
|---|---|
| OS | WSL2、system-level systemd 可用（`systemctl is-system-running` = `running`） |
| sudo | **需密碼**——每個 `sudo` 步驟都是互動式，**不可假設自動化** |
| 帳號 | 單一 `operator` 登入帳號（本文以 `operator` 代稱，勿寫死使用者名） |
| 部署 | pipx tree 在 `$HOME/.local/share/pipx/venvs/paulsha-cortex/`，實測 `drwxrwxr-x` |
| headless job | 現以 operator 帳號跑 |
| `$HOME/.agents/{control,config/paulsha}` | 現為 `775`（`g+w`） |

## 標記約定

- **`🔧 sudo`**：operator 親自 `sudo` 執行的特權變更。
- **`✅ 驗證`**：唯讀驗證命令（無特權或只讀，可重複執行）。
- 命令中的路徑一律為裁決定案的**真實絕對路徑**，可直接複製。
- 個人路徑一律以 `$HOME` 表示，**不寫死使用者名**。

## 本 runbook 的規模（收斂後重新統計）

A/B 並列時同一件事要寫兩遍（5-A／5-B、第 8 步兩種起法、附錄兩組 diff）；
收斂為單一路徑後統計如下：

| 段落 | 🔧 sudo 點 | ✅ 驗證點 |
|---|---:|---:|
| 執行前提（含 G1／G2 兩個硬性 gate） | 0 | 11 |
| 產生器＝單一真相 | 0 | 10 |
| 第 1 步：建三帳號 | 3 | 5 |
| 第 2 步：目標樹與權限（含 2c 來源樹） | 4 | 21 |
| 第 3 步：legacy-import（含 operator 設定搬遷） | 3 | 11 |
| 第 4 步：Manager／monitor 部署與 unit（含 4e toolchain／憑證） | 16 | 33 |
| **第 5 步：降權（A+B ＋ #643 per-executor 剖面）** | **8** | **38** |
| 第 6 步：升級流程 | 5 | 6 |
| 第 7 步：切換驗收（含功能面檢查） | 0 | 5 |
| 第 8 步：R9 抽驗（五族） | 5 | 19 |
| 第 9 步：回滾 | 2 | 1 |
| WSL2 風險與診斷 | 2 | 12 |
| 附錄 A：自我檢查 | 0 | 9 |
| 附錄 B：降級備援 | 2 | 3 |
| **合計** | **50** | **184** |

（統計方式：全文 `🔧`／`✅` 標記出現次數，扣除說明性用法——「標記約定」的定義行、
段落標題內的標記、以及表格裡當狀態記號用的那幾個。）

- **步驟數**：9 步（未變）＋ 3 個附錄；第 5 步由「兩個並列方案（5-A 六節／5-B 四節，
  共 10 節）」收斂為**一條九節路徑**（5-1…5-9），#643 於其中插入 5-2b（真實加固面下
  的雙剖面驗證）。
- **反向測試**：原本分散在 5-A-5（4 條，其中 1 條「已知不會被拒」）與 5-B-4（7 條），
  收斂後集中在 5-7（**11 條**），且**全部期望為「被拒」**——不再有任何一條期望成功。
  #643 再補第 12 條（三小條，期望為「拒絕確實發生」），並讓 (5)(7)(8)(9)(10) 對
  **兩個**模板字幹各跑一次。
- **R9 族數**：4 → **5**（新增族 5 privilege-boundary），且族 1–4 各跑**兩個 subject**
  （builder ／ reviewer-planner），實測條數約為舊版的兩倍。
- **#621（M1 對照後的修正）新增**：執行前提兩個硬性 gate（`acl`／sudoers 萬用規則）、
  第 2a 稽核 6（#626 的帳號存在性）、第 3-0／3a-2（operator 設定的分類與搬遷）、
  第 4a 與第 6 步各補三個 pipx 遷移必要步驟（shebang／`pipx_shared.pth`／總驗收）、
  第 4d 的「裝好但不得啟動」gate（#623）、第 7b 功能面檢查、族 4 的 pid 改由 operator
  注入並新增 T4.0 可見性測項、族 1 的 T1.1／T1.5 由「讀」改測「寫」並新增
  `d()`／`need()`／身分鎖三個判讀守衛。合計（#624／#625 落地後的 35／133 基礎上）
  → **43** 個 sudo 點、**156** 個驗證點。
- **#623（per-job clone）新增**：第 2c 步「建立來源樹 ＋ 落**三份** root-owned
  `.gitconfig`」——2 個 sudo 點、5 個驗證點（來源樹 Manager 可寫／job 唯讀、job 真的
  clone 得動、三份 `.gitconfig` 對應帳號不可寫、commit spool 的 `wx` 無 `r`）
  → **45** 個 sudo 點、**161** 個驗證點。
- **#640（executor 執行面）新增**：第 4e 步「executor toolchain 落位 ＋ per-account
  憑證」——4 個 sudo 點、9 個驗證點（toolchain 對 job 唯讀、以 job 帳號實跑
  `--help` 期望 rc=0、**版本與 operator 側逐字相同**、在真實加固面下複跑一次、
  憑證「檔 job-owned／目錄 root-owned」的三條反向不變式），另在「產生器＝單一真相」
  與 5-5 各補一條 ✅、附錄 A 補兩條漂移自我檢查（版本分岔／憑證 owner）
  → **49** 個 sudo 點、**174** 個驗證點。
- **#643（per-executor 加固剖面）新增**：第 5-2 步改為落**兩份** template unit
  （strict／jit，差異必須只有 `MemoryDenyWriteExecute` 一項）、新增第 5-2b 步「在
  真實加固面下驗證兩種剖面」（**含負向對照**：node 型 executor 在 strict 剖面下必須
  失敗——只驗 jit 成功等於什麼都沒驗）、5-7 補第 12 條（Manager 選不了剖面）與
  兩字幹複跑（含 `systemd-analyze verify` 的未知鍵落檔後檢查——#645 修的是產生器，
  已落檔的 unit 不會自己更新），4e 的 executor 形態表回填「`copilot` 也需要 node」
  → **50** 個 sudo 點、**184** 個驗證點。
- **#673（加固面複本必須全量導出）新增**：4e 新增「共用探針 `psc_run_under`」一節，
  加固面改由 `permgen.unit_replica_properties()`（CLI：`trust_root unit-replica`）從
  **已落檔的 unit** 全量導出——4e 的三條、5-3 的 pytest、5-4 的 `gh` 原本各自手抄
  四條 property，5-2b 則是一份手維護的 27 鍵 grep 白名單且不含
  `ReadWritePaths=`／`WorkingDirectory=`／per-account `Environment=`；5-2b 的驗收
  由「正向四段」改為 **4 executor × 2 剖面 × 2 角色 unit 的全矩陣**（新增**反向對照**：
  `claude`／`agy` 在 jit 剖面下仍須 rc=0），並補「失敗原因要對得上」與 seccomp 過濾
  語意不變式兩條。**起因**：一份漏抄 `SystemCallErrorNumber=EPERM` 的手抄複本比
  production 更嚴格，量出一個不存在的 P0——手抄子集的假紅與假綠一樣會發生。
- **#615（M2：reviewer／planner 啟動面降權）新增**：第 5-2 步再擴為落**四份**
  template unit（2 角色 × 2 剖面，另含「四份加固表集合比對」與「reviewer 的 RWP
  恰好兩條且不含任何 `%i` 路徑」兩條 gate）、5-5 補 reviewer 那一組 env ＋ 角色解析
  複驗、新增 **5-6b**（reviewer 模板的正向 smoke：`id` 必須是
  `cortex-reviewer-planner`，並逐條驗它對 builder 工作區／commit spool／來源樹／
  gate ledger 皆不可寫）、5-7 的字幹改由產生器導出成**四個**並補 10 條 reviewer 字幹
  混淆、(10) 擴為四份 unit 逐一試、8a pass 2 由「operator sudo 模擬」升級為**真實
  template instance**、新增 **8b-2 族 6「verdict 通道端到端」**（#638／#639 的修法
  第一次被真正驗到：檔案 owner 是 reviewer／Manager 讀得到／builder 零權限／
  seal 後 reviewer 改不動，含 negative control）
  → **54** 個 sudo 點、**205** 個驗證點。
- **#633（env 展開慣用法 ＋ ACL 結構變更）新增**：第 4b 的 EnvironmentFile 模板補上
  操作變數（`PSC_REPO_ROOT`／`PSC_REPO_IDENTITY`／`PSC_MANAGER_EXECUTOR`／
  `PSC_GATE_CMD_PYTEST`／`PSC_GATE_TIMEOUT`／兩個 interval）**且全部加引號**，並補一條
  「這份 env 檔 shell 也 source 得動」的驗證；全文 **10 處** `env $(… | xargs)` 改為
  `sh -c 'set -a; . <envfile>; set +a; …'`（前者依空白切詞，會把
  `PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 拆成多個參數 → `env: ‘-m’: …` rc=127；
  systemd 自己沒事，壞的只有驗證指令）；第 7b 補 **F2b**（`paths.repo_root()` 真的指向
  來源樹——#633 之後 repo 解析是 lazy 的，`is-active` 全綠**不代表**派得了工）；
  第 2b 補「升級既有部署重跑也要守 scaffold→permissions 順序」的警語；5-3a 補「改 ACL
  結構時舊的 default ACL 會靜默繼承」的警語 ＋ `setfacl -k <容器>` 步驟 ＋ 一條機械
  判準（每格具名條目 `foreign=0`、`#effective:` 註記為 0）
  → **55** 個 sudo 點、**208** 個驗證點。

---

## M1 實機基準值（2026-08-17，#584 M1 留言）

下表是 **M1 在 9900X／WSL2 上實跑出來的值**，供下一個執行者當**對照基準**：
數字對不上不一定是錯，但**必須查清楚差在哪**才能繼續。
（檔數這類與本機資料量相關的值只作量級參考；`ok` / 條數這類是硬期望。）

| 關卡 | 本 runbook 位置 | M1 實測值 |
|---|---|---|
| Phase 1 自檢 baseline | 執行前提 3 | `ok: false`、`job_writable_count` = **5** |
| Phase 1 自檢（切換後） | 第 7 步 | `ok: true`、`job_writable_count` = **0**、`remaining` 為空 |
| 登記表雙向等式 | 執行前提 4／第 7 步 | `ok: true`（切換前後皆是） |
| legacy-import manifest | 第 3 步 | 78,674 檔；全量複驗 78,674/78,674 `OK` |
| 5-6 正向 smoke | 第 5 步 | 成功；`uid=…(cortex-builder)`、token 已 scrub、fd 僅 0/1/2、`HOME=/var/lib/cortex-builder`（root-owned）、部署樹不可寫 |
| 5-2b 雙剖面（#643／#673） | 第 5 步 | **在 `unit-replica` 全量導出的加固面下**跑滿 4 executor × 2 剖面 × 2 角色 unit：正向四格 rc=0 且版本相符；**負向對照** `codex`／`copilot` @strict 失敗且 stderr 為 V8 的 `Runtime_CompileLazy`（**不是**空 stderr）；**反向對照** `claude`／`agy` @jit 仍 rc=0 |
| 5-7 反向 11 條 | 第 5 步 | **全數非 0**（(5)(7)(8)(9)(10) 對兩個字幹各一次） |
| 5-7 (12) 剖面不可選（#643） | 第 5 步 | 三小條**全部 0**（＝拒絕確實發生） |
| 5-7 (11) fail-closed | 第 5 步 | 移除 polkit 規則後起 job **失敗**、還原後**成功**（證明是規則在守，不是全紅假綠） |
| 8a 族 1–4（builder） | 第 8 步 | 46 `denied` ／ 3 條 `SUCCEEDED` 標記 → 三條**全部判定為非破口**，且**三條的成因就是本 runbook 這次修掉的 #621 第 7／8／9 條**（期望寫反、測錯面、`rm -f` 假陽性）。按修正後的腳本重跑應為**全數 denied** |
| 8a 族 1–4（reviewer-planner） | 第 8 步 | 47 `denied` ／ 2 條標記；**T1.5 對它是拒絕的** ← 三分在檔案層生效的直接證據 |
| 8c negative control ×5 | 第 8 步 | 全部 `*-OK`（含 NC5：manager 起合法 instance 成功且 `uid=…(cortex-builder)`） |
| 8d 重啟複驗 | 第 8 步 | 服務 `active`、自檢仍 `ok=true`、`NRestarts=0` |

> **M1 期間手動繞過、之後已收編的一條**：permgen 當時沒產生**父目錄 traverse ACL**
> ——葉節點 ACL 正確，但 `coordinator/`／`monitor/` 是 `0700 cortex-manager`，
> POSIX 要求路徑**每一層**都要 `x`，兩條 append-only 正向路徑因此全斷。
> M1 以三條手動 `setfacl --x` 解封；**#620 已由 PR #624 進產生器**，第 2 步的
> 稽核 5 與正／負向驗證即是它的守門條款，**不需要再手補**。
>
> **M1 之後才發現、目前仍未關的兩條**（下一個執行者會撞到，見各步的標注）：
> **#626**——permgen 為本機不存在的 principal（`operator`／`cortex-outbox`）產出
> `setfacl` 條目，`sh -e` 下會中止整份權限 script、留下半套的樹（第 2a 稽核 6）；
> **#623**——monitor unit 起得來但 job 跑不完（`ProtectHome=yes` 讓 repo 不可達、
> EnvironmentFile 缺八個操作變數），因此 **4d 裝好但不得啟動**（見第 4d 步）。
> **（#633 已補：第 4b 的模板現在含那八個操作變數且值一律加引號；缺 `PSC_REPO_ROOT`
> 也不再讓 Manager 啟動即崩——repo 解析改 lazy，只是派不了工。）**

---

## 產生器＝權限／unit／polkit／shim 的單一真相

`chown`／`chmod`／`setfacl`／unit 內容／polkit 規則**一律不手寫**，全部由 permgen 由
R1 登記表機械產生。**三分（`four-way`）是唯一 scheme**：

```bash
# ✅ 完整權限計畫（JSON，含每項 rationale）
python3 -m paulsha_cortex.trust_root permissions four-way

# ✅ 可直接執行的權限命令（帶真實絕對路徑，無 placeholder）
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths

# ✅ 骨架目錄（非登記表資產的父層）
python3 -m paulsha_cortex.trust_root scaffold four-way

# ✅ Manager system unit 內容（User=cortex-manager，ReadWritePaths 由登記表導出）
python3 -m paulsha_cortex.trust_root unit four-way --manager

# ✅ monitor system unit 內容（同帳號、同加固段，ReadWritePaths 嚴格窄於 Manager）
python3 -m paulsha_cortex.trust_root unit four-way --monitor

# ✅ job template unit 內容（`User=` 硬寫死；B 的核心）——**四份**
#    #643（加固剖面）：strict（預設）與 jit（node 型 executor），差異只有
#      MemoryDenyWriteExecute 一項；對應表由 permgen.EXECUTOR_TOOLS 機械導出。
#    #615（job 角色）：--job＝builder；--review-job＝reviewer＋planner
#      （同帳號同模板）。兩個角色的差異全部由帳號帶出來。
python3 -m paulsha_cortex.trust_root unit four-way --job
python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit
python3 -m paulsha_cortex.trust_root unit four-way --review-job
python3 -m paulsha_cortex.trust_root unit four-way --review-job --profile jit
# ✅ #629：gate 執行身分的兩份模板（User=cortex-gate；只有 four-way 有這個帳號）
python3 -m paulsha_cortex.trust_root unit four-way --gate-job
python3 -m paulsha_cortex.trust_root unit four-way --gate-job --profile jit

# ✅ 降權 polkit 規則內容（**單一檔、單一 addRule、單一 return YES**）
#    放行的是四個具名模板的 start/stop：cortex-job@ / cortex-job-jit@ /
#    cortex-reviewer-job@ / cortex-reviewer-job-jit@（皆 *.service）
python3 -m paulsha_cortex.trust_root polkit four-way --template

# ✅ root-owned shim 內容（/opt/cortex/bin/cortex-job-shim）—— #616 已 merge
python3 -m paulsha_cortex.trust_root shim four-way

# ✅ 帳號 HOME 下 root-owned 的 .gitconfig 內容（來源樹的 safe.directory）
#    <slug> ＝ 來源樹底下那一格的目錄名（見第 2c 步）；未給即 fail-closed。
#    三份同構：兩個 job 帳號 ＋ Manager（Manager 也對來源樹跑 git，同樣會撞 dubious ownership）。
python3 -m paulsha_cortex.trust_root gitconfig four-way --builder --source-repo <slug>
python3 -m paulsha_cortex.trust_root gitconfig four-way --reviewer-planner --source-repo <slug>
python3 -m paulsha_cortex.trust_root gitconfig four-way --manager --source-repo <slug>

# ✅ executor toolchain 的落位步驟（#640；四個模型 CLI 進 /opt/cortex/toolchain、
#    node 走系統層、job 的 PSC_BUILDER_PATH 值）——見第 4e 步
#    #661 起同一份輸出另含：srt／openspec 的搬移、preflight backend 的 venv 落點；
#    #666 起再含：系統層 python 套件（第 4f 步）、Manager 的 gh 憑證（第 4g 步），
#    以及**窮舉盤點**與已知未決項（第 4h 步）。
python3 -m paulsha_cortex.trust_root toolchain four-way
```

> **「重跑計畫後零漂移」是本步的驗收方式**（#666）：實機手動補過的東西，如果產生器出
> 不出來，換一台機器部署就不會有它。上面那一行 ＋ `permissions`／`scaffold` 三份輸出
> 合起來必須涵蓋 `/opt/cortex/toolchain`、系統層 python 套件、以及每一個帳號 HOME 下的
> 憑證／設定；**逐項對完仍為空差異**才算這一步做完。

**job-spec 的欄位契約也已隨 #616 落地**——`coordinator/job_runner.py` 的
`build_job_spec()` 是**唯一**的產生入口，`coordinator/job_shim.py` 是唯一的讀取端：

```bash
# ✅ spec 契約（路徑推導 ＋ 必填／禁用欄位）——本 runbook 一律用它產 spec，
#    **不自行捏造欄位**（手捏的 spec 會被 shim 的白名單 schema 拒絕）
python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
for role in job_runner.JOB_ROLES:
    s = job_runner.resolve_job_spec_spool({}, role=role)
    print(f"spool[{role:<8}]:", s, "→", job_runner.job_spec_path(s, "<instance>"))
print("spec_version    :", job_runner.JOB_SPEC_VERSION)
print("必填欄位        :", job_runner.SPEC_REQUIRED_KEYS)
print("禁用（身分）欄位:", sorted(job_runner.SPEC_FORBIDDEN_KEYS))
PY
#   期望：spool = /var/lib/cortex/coordinator/job-specs/builder（#657：**per-principal**，
#         `role=` 決定哪一格；**不是** /var/lib/cortex/jobs，也不是無尾段的 job-specs）；
#         spec 路徑 = <該角色的 spool>/<instance>.json（**一個檔，不是一個目錄**）；
#         禁用欄位含 user／uid／group／gid／properties／exec_start
#         ——身分只有一個來源＝root-owned unit 檔的 `User=`，spec 連提都不准提。
```

> **一律以產生器輸出為準**：本 runbook 的每個落檔步驟後面都跟一條 `diff` 驗證，
> 上述任一項內容改變，`diff` 會立刻抓到漂移，runbook 不需改寫。

---

## 第 1 步：建 UID（**四分**）

建立四個 **system service 帳號**，皆 **no-login**、**home 由 root 擁有**。
慣例：每帳號一個同名 primary group（權限產生器的 ACL 以此為前提）。

| 帳號 | 跑什麼 | durable state owner | 持 spawn 授權（polkit subject） |
|---|---|:--:|:--:|
| `cortex-manager` | Manager ＋ monitor。**不跑任何模型程式碼** | ✔ | ✔ |
| `cortex-reviewer-planner` | reviewer ＋ planner 模型 job | ✘ | ✘ |
| `cortex-builder` | builder 模型 job（會跑 untrusted repo code） | ✘ | ✘ |
| **`cortex-gate`**（#629） | operator 宣告的 gate 命令（`PSC_GATE_CMD_*`）。**不跑模型**，但跑 builder 工作樹裡的 `conftest.py`／plugin ⇒ 同樣是 untrusted 執行 | ✘ | ✘ |

> **為什麼一定要第四個帳號**（#629；完整論證見 spec §R3「誠實的取捨：gate 執行身分」）：
> gate 命令必然在 builder 完全掌控內容的樹上執行，`pytest` 會載入該樹的 `conftest.py`
> ⇒ **執行者取得任意程式碼執行**。既有三個逐一皆不可承接——`cortex-builder`＝模型自證
> （違反 #540 的 acceptance chain）、`cortex-manager`＝把那個執行引到持 spawn 授權、
> 擁有全部 durable state 的帳號、`cortex-reviewer-planner`＝它是寫 verdict 的那一個
> （等於把 #638／#639 剛關掉的通道重新打開）。
>
> **不能用 `DynamicUser=yes` 省掉這個帳號**：本方案兩端都靠**具名 POSIX ACL**
> 授權（gate 對 `gate-ledger-spool` 是 `wx` 無 `r`、對 builder 工作樹是 `rX`），
> 而 `setfacl -m u:<名>:…` 在**產生權限的當下**就要解析得到那個帳號；DynamicUser
> 的 UID 在 unit 起動前不存在、每次還不同。`User=nobody` 更糟——那是與系統上其他
> 服務共用的帳號，授它 Tier-0 spool 的寫入權等於開給所有 `nobody` 行程。

```bash
# ✅ 先取產生器對三分方案的 HOME／cache 計畫——**不手寫路徑**
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
s = permgen.SCHEMES["four-way"]
for path, owner, group, mode in permgen.DEFAULT_LAYOUT.scaffold_directories(s):
    if path.startswith("/var/lib/cortex-"):
        print(f"{path}\t{owner}:{group}\t{mode:04o}")
PY
#   期望（#616 已 merge）：**三組 HOME＋cache，目錄名與帳號名逐字一致**——
#     /var/lib/cortex-manager            root:root  0755
#     /var/lib/cortex-manager/cache      cortex-manager:cortex-manager 0700
#     /var/lib/cortex-builder            root:root  0755
#     /var/lib/cortex-builder/.codex     root:root  0755
#     /var/lib/cortex-builder/cache      cortex-builder:cortex-builder 0700
#     /var/lib/cortex-reviewer-planner   root:root  0755
#     /var/lib/cortex-reviewer-planner/.codex  root:root 0755
#     /var/lib/cortex-reviewer-planner/cache   cortex-reviewer-planner:… 0700
#   **不得**再出現 `/var/lib/cortex-svc`（二分時代的字面量；#616 已改為由帳號名
#   機械導出 `PathLayout.home_of()`／`cache_of()`）。若仍出現 ⇒ 部署樹比 #616 舊，
#   先升級再繼續，**不要**照舊值建帳號。
#   註：只有跑模型的兩個 job 帳號有 root-owned `~/.codex`；Manager 不跑模型故無此條。

# 🔧 sudo：cortex-manager（Manager＋monitor；durable state owner；持 spawn 授權；不跑模型）
sudo groupadd --system cortex-manager
sudo useradd  --system --gid cortex-manager \
     --home-dir /var/lib/cortex-manager --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex manager+monitor (durable state owner, spawn grant, no model code)" \
     cortex-manager
#   ↑ --home-dir 取自上面產生器輸出的那一行。

# 🔧 sudo：cortex-reviewer-planner（reviewer＋planner 模型 job）
sudo groupadd --system cortex-reviewer-planner
sudo useradd  --system --gid cortex-reviewer-planner \
     --home-dir /var/lib/cortex-reviewer-planner --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex reviewer/planner model job" cortex-reviewer-planner

# 🔧 sudo：cortex-builder（builder 模型 job；跑 untrusted repo code）
sudo groupadd --system cortex-builder
sudo useradd  --system --gid cortex-builder \
     --home-dir /var/lib/cortex-builder --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex headless builder job" cortex-builder

# 🔧 sudo：cortex-gate（#629；operator 宣告的 gate 命令，不跑模型）
sudo groupadd --system cortex-gate
sudo useradd  --system --gid cortex-gate \
     --home-dir /var/lib/cortex-gate --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex gate execution identity (declared gate commands, no model)" \
     cortex-gate
```

> **gate 帳號沒有 `~/.codex`、也沒有 executor 憑證**——它不跑模型 CLI。這件事由
> `scaffold_directories()` 機械保證（來源是 `scheme.model_job_accounts()`，不是
> `headless_accounts()`）。若第 2 步的 scaffold script 裡出現
> `/var/lib/cortex-gate/.codex` ⇒ 停下來，部署樹比 #629 舊。

> **不需要手動補目錄**：舊版此處有四行 `install -d` 替 `cortex-reviewer-planner`
> 補 HOME／`.codex`／`cache`——#616 讓 `scaffold_directories()` 改由
> `scheme.headless_accounts()` 導出帳號清單後，第三個帳號**自動入列**，
> 由第 2 步的 scaffold script 一併產生。**此處不得再手動建任何目錄**——
> 手建會繞過產生器這個單一真相，也讓第 2 步的稽核失去意義（稽核比對的是
> script 內容，不是磁碟現況）。若上一輪已手建過，第 2 步的 scaffold 冪等重跑
> 會把 owner／mode 拉回計畫值，不必先刪。

**為何 `--no-create-home`**：四個帳號的 HOME 由第 2 步以 **root 擁有**的方式建立
（`useradd --create-home` 會把 HOME 建成帳號自己擁有）。HOME 若由帳號自己擁有，
它就能 rename 掉 `~/.codex`／`~/.gitconfig` 這類 root-owned 設定的**父目錄**——
父目錄可寫者能 unlink／rename 子物件，等於保護失效。只有 `cache/` 子目錄開放給帳號寫。

**群組設計理由**：跨帳號存取一律走 **per-account POSIX ACL**（見 permgen 輸出的
`setfacl -m u:<acct>:rX`），**不**用共用 group 開放，避免「一個 group 開了就全開」。
三分之後這條更關鍵：`cortex-reviewer-planner` 對 verdict spool 的授權是
**write-only（`wx` 無 `r`）**，只有 per-account ACL 表達得出來；#629 的 `cortex-gate`
再加兩條同樣只有 ACL 表達得出來的授權——對 `gate-ledger-spool` 的 `wx` 無 `r`，
以及對 builder per-job 工作樹的 `rX`（**讀得到、寫不進**）。

```bash
# ✅ 驗證：四帳號存在、shell 為 nologin、四個 group 互不交集
getent passwd cortex-manager cortex-reviewer-planner cortex-builder cortex-gate
id cortex-manager; id cortex-reviewer-planner; id cortex-builder; id cortex-gate
#   期望：uid/gid 各自成對；四者的 groups 互不包含對方
getent group cortex-manager cortex-reviewer-planner cortex-builder cortex-gate

# ✅ 驗證：四個帳號都登不進去（nologin），且都不在 sudo／wheel 群組
for U in cortex-manager cortex-reviewer-planner cortex-builder cortex-gate; do
  printf '%s shell=%s groups=%s\n' "$U" "$(getent passwd "$U" | cut -d: -f7)" "$(id -nG "$U")"
done
#   期望：shell 全為 /usr/sbin/nologin；groups 只含自己的同名 group

# ✅ 驗證：job 帳號兩兩互不可讀對方 HOME（分帳的第一條不變式）
sudo -u cortex-builder ls /var/lib/cortex-reviewer-planner/cache 2>&1 | tail -1
sudo -u cortex-reviewer-planner ls /var/lib/cortex-builder/cache 2>&1 | tail -1
sudo -u cortex-gate ls /var/lib/cortex-builder/cache 2>&1 | tail -1
sudo -u cortex-builder ls /var/lib/cortex-gate/cache 2>&1 | tail -1
#   期望：四者皆 Permission denied（cache 為 0700 且 owner 不同）

# ✅ 驗證：四帳號皆無 sudo 授權（**與執行前提 G2 成對**——這是 G2 的事後複驗）
for U in cortex-manager cortex-reviewer-planner cortex-builder cortex-gate; do
  printf '=== %s ===\n' "$U"
  sudo -l -U "$U" 2>&1 | tail -2
  sudo -u "$U" sudo -n true 2>&1 | tail -1
done
#   期望：`sudo -l -U` 三個都印 `is not allowed to run sudo`（M1 收斂萬用規則後的
#   實測字串）；`sudo -n true` 全部失敗（不可有任何一個成功）。
#   ⚠️ 若出現 `(root) NOPASSWD: ALL` ⇒ `/etc/sudoers.d/` 有萬用規則（G2 沒守住或
#   被重新引入）。**立即停止**：這三個帳號現在就是無密碼 root，後面的降權全部無效。
#   處置：先收斂規則到具名 operator 帳號，再重跑本條複驗，綠了才往下做。
```

**回滾**：
```bash
sudo userdel cortex-manager; sudo userdel cortex-reviewer-planner
sudo userdel cortex-builder; sudo userdel cortex-gate
sudo groupdel cortex-manager; sudo groupdel cortex-reviewer-planner
sudo groupdel cortex-builder; sudo groupdel cortex-gate
```
（此時尚無任何檔案或目錄屬於它們——HOME／cache 由第 2 步的 scaffold 建立。）

---

## 第 2 步：建目標樹並套用權限（全部機械產生）

裁決：`AGENTS_ROOT=/var/lib/cortex`、`WORKTREE_ROOT=/var/lib/cortex/worktree`、
`DEPLOY_ROOT=/opt/cortex`。這些值已固化在 `permgen.DEFAULT_LAYOUT`，下列命令直接引用。

### 2a. 產生兩份 script 並**先讀過**

```bash
# ✅ 產生骨架目錄 script（非登記表資產的父層：/opt/cortex、HOME、job spool…）
python3 -m paulsha_cortex.trust_root scaffold four-way > /tmp/p2b-scaffold.sh

# ✅ operator 與外部 outbox reader 是**抽象角色名**，不是帳號——對應到誰是**部署決定**，
#    必須在產生當下指定（#626）。未指定時產生器 fail-closed：stdout 一行都不輸出、
#    回傳碼 2，stderr 指出是哪個 principal 與怎麼指定。
OPERATOR_ACCOUNT="$(id -un)"   # 單人機器＝現在這個登入帳號；多人／CI 部署請改成專用帳號
getent passwd "$OPERATOR_ACCOUNT" >/dev/null \
  && echo "operator account OK: $OPERATOR_ACCOUNT" \
  || echo "!! $OPERATOR_ACCOUNT 不存在，先建帳號或改指定"

# ✅ 產生權限 script（登記表每一項的 install -d／chown／chmod／setfacl）
#    `--external-reader-account none`＝**明示**本部署沒有外送管線 reader 的實體，
#    該角色的 ACL 整組略去（是一個被記錄下來的決定，不是漏掉）。之後真的有了
#    再改成它的帳號名重跑即可。旗標也可改用 env：PSC_OPERATOR_ACCOUNT／
#    PSC_EXTERNAL_READER_ACCOUNT（旗標優先）。
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  --operator-account "$OPERATOR_ACCOUNT" \
  --external-reader-account none \
  > /tmp/p2b-permissions.sh
echo "exit=$?"   # 期望 0；2＝有 principal 沒對應到真實帳號（stderr 已指出是哪個）

# ✅ 逐行讀過再執行——這是 operator 核可的實體動作
less /tmp/p2b-scaffold.sh
less /tmp/p2b-permissions.sh

# ✅ 稽核 1：所有 mode 都不得有 group／other 寫入位（spec §R2）
grep -oE "chmod [0-7]{4}" /tmp/p2b-permissions.sh | sort -u \
 | awk '{m=$2; if (substr(m,3,1) ~ /[2367]/ || substr(m,4,1) ~ /[2367]/) {print "!! group/other writable: " m; bad=1}}
        END{ if (!bad) print "no group/other write: OK" }'

# ✅ 稽核 2：ACL 授「寫」只准出現在兩個 append-only 出口（三分下**恰好四行**）
grep -E "^setfacl" /tmp/p2b-permissions.sh | grep -E "u:[^:]+:[^ ]*w"
#   注意 pattern 必須錨在 `u:<帳號>:` 之後才找 `w`——裸的 `:[^ ]*w` 會把
#   `u:cortex-reviewer-planner:--x` 也撈進來（`reviewer` 自己帶一個 w），
#   #620 的 traverse 節上線後這條稽核就一直多出一行假陽性。
#   期望（三分）：**恰好四行**
#     setfacl -m u:cortex-builder:wx           /var/lib/cortex/monitor/event-spool
#     setfacl -d -m u:cortex-builder:wx        /var/lib/cortex/monitor/event-spool
#     setfacl -m u:cortex-reviewer-planner:wx  /var/lib/cortex/coordinator/review-verdicts
#     setfacl -d -m u:cortex-reviewer-planner:wx /var/lib/cortex/coordinator/review-verdicts
#   **wx 無 r**——寫得進自己那格、讀不到他人的。多出任何一行都要停下來查。

# ✅ 稽核 3：三分的帳號名確實出現在計畫裡（兩份 script 都不得殘留 cortex-svc）
grep -c "cortex-svc" /tmp/p2b-permissions.sh
#   期望：**0**（若非 0，代表 scheme 傳錯或 permgen 仍有二分殘留，停下來查）
grep -oE "cortex-(manager|reviewer-planner|builder)" /tmp/p2b-permissions.sh | sort | uniq -c
#   期望：三個帳號名皆出現，且**不含** cortex-svc。
grep -c "cortex-svc" /tmp/p2b-scaffold.sh
#   期望：**0**。#616 之前 scaffold 會殘留 2 筆（`/var/lib/cortex-svc` 與其
#   `cache/`）——那是二分時代寫死的字面量，現已改由帳號名機械導出。
#   **非 0 ⇒ 部署樹比 #616 舊，先升級再繼續**，不要照舊值建目錄。
grep -oE "/var/lib/cortex-(manager|reviewer-planner|builder)" /tmp/p2b-scaffold.sh | sort -u
#   期望：三行，與三個帳號名逐字對應（HOME 由帳號名導出，不會再漂移）。

# ✅ 稽核 4：setfacl 可用 —— 已上移為**執行前提 G1（硬性 gate）**
#   此處僅複驗一次（script 產生後、套用前的最後一道），失敗即停止：
command -v setfacl >/dev/null || echo "!! acl 缺席——回到執行前提 G1，不得套用權限"

# ✅ 稽核 5：父目錄 traverse ACL 已由產生器導出（#620）
grep -E "^setfacl -m u:[^ ]+:--x " /tmp/p2b-permissions.sh
#   期望（三分）：**至少**含下列三行——它們是兩條正向路徑成立的**必要條件**
#     setfacl -m u:cortex-builder:--x           /var/lib/cortex/monitor
#     setfacl -m u:cortex-builder:--x           /var/lib/cortex/coordinator
#     setfacl -m u:cortex-reviewer-planner:--x  /var/lib/cortex/coordinator
#   （另有 `--operator-account` 指定的那個帳號的對應條目，同樣由葉節點 ACL 機械
#     導出；外部 reader 明示 `none` 時，它那一組條目整組不出現）
grep -E "^setfacl .*:r-x " /tmp/p2b-permissions.sh
#   期望：空輸出。traverse 一律 `--x` 而非 `r-x`：走得到自己那格，但**列不出**
#   coordinator/ 底下還有哪些 Manager 資產。
grep -E "^setfacl -d -m u:[^ ]+:--x " /tmp/p2b-permissions.sh
#   期望：空輸出。traverse 只設 access ACL、不設 default——default 會讓該目錄底下
#   新建的每個物件都繼承這條授權，等於把一條 traverse 放大成整棵子樹的授權。
tail -n 20 /tmp/p2b-permissions.sh | grep -c ":--x "
#   期望：> 0。traverse 節**必須留在 script 尾端**：`chmod` 在帶 ACL 的物件上會重寫
#   ACL **mask**，先 setfacl 再 chmod 會讓具名條目的有效權限被 mask 成空（靜默失效，
#   不會報錯）。因此也**不要**在執行完 permissions 之後再重跑 scaffold。

# ✅ 稽核 5b：**job 工作樹底下不得有任何 setfacl**（#641）
#   註解掉的 per-job 行也算數——那些是降權啟動器逐案要套的，因此連 `#` 開頭的行
#   一起看：
grep -E "setfacl" /tmp/p2b-permissions.sh | grep -E "/var/lib/cortex/worktree/"
#   期望：**空輸出**。
#   #637 把成果交付整條換成 bundle spool（builder 產 bundle → commit-spool →
#   Manager 從**檔案** fetch），reviewer 的 verdict 走 review-verdict-spool。
#   Manager 因此沒有任何理由讀 job 的樹；#641 把登記表裡殘留的三條讀取授權
#   （`repo-worktree` 的 `rX`、`review-verdict` 與 `work-items-yaml` 的 `r`）
#   一起收掉。
#   **非空輸出 ⇒ 部署樹比 #641 舊，先升級再繼續**——照舊值套用會讓
#   #637 的不變式（Manager 全程不碰 builder 的 clone）在實機上不成立，而測試
#   仍是綠的。
#   注意 `/var/lib/cortex/worktree`（pool 容器本身，無 `/` 結尾）**不在**此列：
#   它是 `0701 cortex-manager`，Manager 是 owner、別的帳號只能 traverse，
#   產生器對它只出 `install -d`／`chown`／`chmod`，本來就沒有 setfacl。

# ✅ 稽核 5c：**即時回收沒有偷偷把那條 ACL 加回來**（#658）
#   #658 讓 build 卡被採信之後即時回收它的工作區。回收身分是 **Manager 自己**，
#   而且**刻意不新增任何授權**——推導是：抵達回收必須先走完
#   `manager._verify_exact_candidate()`，那裡已經以 Manager 身分對**同一棵樹**跑過
#   `git -C <worktree> rev-parse HEAD`。因此「Manager 進得去那棵樹」是**採信本身
#   既有的前提**，不是 #658 新增的假設；稽核 5b 的「零 setfacl」因此仍然成立，
#   上面那條 grep 就是本項的稽核（不需要另一條命令）。
#   **降權部署下今天的實況**：#641 收掉 ACL 之後 `_verify_exact_candidate()` 會先
#   fail-closed（訊息含 `blocked on #629`），所以那裡根本不會出現「被採信卻沒回收
#   的工作區」。等 #629 把 candidate 驗證搬到 gate 執行身分之後，「誰讀得到那棵樹」
#   會跟著改變——**屆時回收身分必須與 candidate 驗證身分一起重新裁決**，不得單獨
#   為回收開一條授權。
#   **運轉期稽核**（run 跑過之後看得到）：
#     journalctl -u cortex-manager | grep workflow-build-workspace-reclaim
#   期望：每張被採信的 build 卡各一行 `-reclaimed`。
#     `-skipped reason=<code>` ⇒ 前置條件不成立（刻意不收），reason 逐字說明是哪一條；
#     `-failed` ⇒ 試了但後置條件不成立，**採信不受影響**，殘留由 `cortex work gc` 收。
#   pool 佔用的對照：一個進行中的 run 在 build phase 應只有**一個** build 工作區
#   （正在跑的那一張卡）：
#     sudo ls -1 /var/lib/cortex/worktree | grep -v -- '-review-' | wc -l

# ✅ 稽核 6：script 裡出現的每個帳號名都**真的存在**（#626）
#   `setfacl` 對解析不到的使用者名直接失敗（`Invalid argument near character 3`），
#   而 2b 是 `sh -e`——一條錯就**中止整份 script**，留下**半套權限的樹**（前半已套、
#   後半沒套，包括尾端的 traverse 節），而錯誤訊息完全看不出是「帳號不存在」。
#   實測命中的是 permgen 為 `operator`／`cortex-outbox` 這兩個「登記表上有 principal、
#   本機卻沒有對應帳號」的名字產出的條目。#626 已在**產生器側**擋掉——未對應的
#   principal 一律 fail-closed、一行都不輸出（見上方產生命令的 `exit=`）；本稽核是
#   **實機側的第二道**：對應到的帳號**這台機器上真的有嗎**。
for U in $(grep -oE "u:[A-Za-z0-9._-]+:" /tmp/p2b-permissions.sh | cut -d: -f2 | sort -u); do
  getent passwd "$U" >/dev/null && echo "OK      $U" || echo "!! 不存在 $U"
done
#   期望：全部 OK。
#   出現 `!! 不存在` 時的處置（**兩者擇一，不可硬跑**）：
#     (a) 該 principal 在本機確實該有對應帳號 ⇒ 先建帳號（比照第 1 步的形態），
#         或以 `--operator-account <既有帳號>` 重新產生 script；
#     (b) 它是本部署形態下不存在的 principal ⇒ 以 `--<principal>-account none`
#         **明示**它不存在後重新產生——該角色的 ACL 會整組略去，不必再手改 script。
#   **不要**改用 `sh`（去掉 `-e`）硬跑：那會把「中止」換成「靜默略過」，
#   結果是一棵你以為套好、其實少了幾條授權的樹。

# ✅ 稽核 6b：骨架 script 的 owner 欄位同樣逐一 getent
grep -oE "install -d -o [a-z_][a-z0-9_-]*" /tmp/p2b-scaffold.sh | awk '{print $4}' \
 | sort -u | while read -r acct; do
     getent passwd "$acct" >/dev/null \
       && echo "scaffold owner OK: $acct" \
       || echo "!! scaffold owner 不存在: $acct"
   done
```

> **中止後直接重跑是安全的**：兩份 script 都只由 `install -d`／`chown`／`chmod`／
> `setfacl -m` 組成，全部是**冪等**操作。因此上面任何一條稽核紅了，修好成因（改
> `--operator-account`／建帳號／明示 `none`）後**重新產生、整份重跑**即可，不需要
> 先回滾、也不必只挑沒跑到的那幾行。順序要求仍在：先 scaffold、後 permissions
> （`chmod` 會重寫 ACL mask，見稽核 5）。

### 2b. 執行（順序固定：先骨架、後權限）

```bash
# 🔧 sudo：骨架目錄（root-owned 父層先就位，權限 script 才不會建出錯誤的中間層）
sudo sh -e /tmp/p2b-scaffold.sh && echo "scaffold applied"

# 🔧 sudo：登記表每一項的目標權限（冪等，可重複執行）
sudo sh -e /tmp/p2b-permissions.sh 2>&1 | tee /tmp/p2b-permissions.log
echo "exit=${PIPESTATUS[0]}"     # 期望 0
#   **非 0 ⇒ 樹是半套的**（`sh -e` 在第一個錯誤就停，後面的條目——包括尾端的
#   traverse 節——完全沒套）。此時**不要**繼續往下做驗證：先看 log 最後一行是哪一條
#   命令失敗、修掉成因（最常見是稽核 6 的帳號不存在），再**整份重跑**。
#   重跑是安全的：每條命令都是冪等的（`install -d`／`chown`／`chmod`／`setfacl -m`
#   對已是目標狀態的物件皆為 no-op），沒有「已套過的部分會被套壞」這回事。
```

> **葉檔守衛**：尚未建立的葉檔（`jobs.json`、`status.json`、ledger…）由產生器包上
> `[ ! -e <path> ] ||` 前綴——不存在就跳過，`sh -e` 不會中斷。它們由服務首次寫入時
> 建立，而 unit 的 `UMask=0077` 讓新檔**出生即 0600**，容器目錄已是 owner-only，
> 因此跳過不留缺口。`/tmp/p2b-permissions.log` 應**沒有**任何
> `No such file or directory`——若有，代表某個**目錄**沒建起來，需查。

> **為何要先 scaffold**：`install -d a/b/c` 會把**新建的每一層**都套上同一組
> `-o/-g/-m`。先讓 `/opt/cortex`、`/var/lib/cortex/config`、`/var/lib/cortex/run`、
> `/var/lib/cortex/runtime`、三個 HOME 以 root 身分就位，權限 script 之後只會補
> 葉節點，不會把 root-owned 父層蓋成服務帳號所有。

> ### ⚠️ 升級既有部署時**重跑也要守同一個順序**（0817 實機，#633）
>
> 上面那條理由是針對「全新安裝」寫的，但它在**重跑**時同樣成立，而且後果更難查——
> 樹已經存在，兩份 script 都不會報錯，只是把權限靜默地弄成錯的：
>
> * **scaffold 在後**：`install -d -m <mode>` 對**既存**目錄不是 no-op，它會把 mode
>   重設回骨架宣告的那一組，蓋掉權限 script 剛套好的值。
> * **`chmod` 重寫 ACL mask**：而 scaffold 的 mode 重設就是一次 `chmod`。於是權限
>   script 剛放上去的具名條目全部變成 `#effective:---`——**ACL 還在**、`getfacl`
>   逐條看得到、實際權限是零。這正是 5-3a「mask 陷阱」的同一個成因，只是觸發點從
>   「手動補 ACL」換成「兩份 script 跑反了」。
>
> **因此**：任何一次重跑（升級、改登記表、改 ACL 結構）都是
> `p2b-scaffold.sh` → `p2b-permissions.sh`，**不可只跑其中一份、也不可調換順序**。
> 只跑 permissions 會漏掉新增的目錄；只跑 scaffold 會把既有 ACL 的 mask 打掉。
>
> **驗收判準與 5-3a 一致**：看 `mask::` 與 `#effective:`，**不是**「有沒有那條 ACL」。
> 重跑後至少複驗一次下面「兩個 append-only 出口的 ACL 已就位」與「父目錄 traverse」
> 兩組，以及 5-3a 的 per-principal 那一組。

```bash
# ✅ 驗證：樹根 root 擁有、Manager-owned 子樹 cortex-manager 0700、無 g+w／o+w
ls -ld /var/lib/cortex /opt/cortex
ls -ld /var/lib/cortex/control /var/lib/cortex/coordinator /var/lib/cortex/specs \
       /var/lib/cortex/monitor /var/lib/cortex/registry /var/lib/cortex/worktree
#   期望：/var/lib/cortex → root:root 0755
#         control/coordinator/specs/monitor/registry → cortex-manager:cortex-manager 0700
#         worktree → cortex-manager:cortex-manager 0701（others 只 traverse，不可列目錄）

# ✅ 驗證：全樹沒有任何 group/other 可寫的路徑（spec §R2 硬性要求）
sudo find /var/lib/cortex /opt/cortex -perm /022 -print | tee /tmp/p2b-world-writable.txt
#   期望：空輸出

# ✅ 驗證：兩個 append-only 出口的 ACL 已就位（跨帳號一律 write-only 或唯讀）
sudo getfacl -p /var/lib/cortex/monitor/event-spool 2>/dev/null | grep -E "^user:"
#   期望：user:cortex-builder:-wx（producer 只能 append，不可讀他人）
sudo getfacl -p /var/lib/cortex/coordinator/review-verdicts 2>/dev/null | grep -E "^user:"
#   期望：user:cortex-reviewer-planner:-wx（**無 r**）

# ✅ 驗證：父目錄 traverse 已就位——**正向路徑真的走得通**（#620）
sudo getfacl -p /var/lib/cortex/monitor 2>/dev/null | grep -E "^user:"
#   期望：user:cortex-builder:--x（**無 r**）
sudo getfacl -p /var/lib/cortex/coordinator 2>/dev/null | grep -E "^user:"
#   期望：user:cortex-builder:--x、user:cortex-reviewer-planner:--x（皆**無 r**）
sudo -u cortex-builder sh -c 'echo x > /var/lib/cortex/monitor/event-spool/probe.json' \
  && sudo rm -f /var/lib/cortex/monitor/event-spool/probe.json \
  && echo "builder → event-spool: OK"
sudo -u cortex-reviewer-planner mkdir /var/lib/cortex/coordinator/review-verdicts/probe \
  && sudo rmdir /var/lib/cortex/coordinator/review-verdicts/probe \
  && echo "reviewer-planner → review-verdicts: OK"
#   期望：兩行 OK。任一 `Permission denied` 就停下來查——先看它抱怨的是**哪一層**，
#   父目錄的錯與葉節點的錯訊息長得很像但缺的授權不同層。

# ✅ 驗證（負向）：traverse 沒有連帶開放列目錄
sudo -u cortex-builder ls /var/lib/cortex/coordinator 2>&1 | tail -1
#   期望：Permission denied（--x 只給 search，不給 read）
sudo -u cortex-builder ls /var/lib/cortex/coordinator/evidence 2>&1 | tail -1
#   期望：Permission denied（走得到 job-specs，仍看不到別的 Manager 資產）

# ✅ 驗證：三個 HOME 與 ~/.codex 由 root 擁有（帳號不得替換自己的設定）
ls -ld /var/lib/cortex-manager /var/lib/cortex-manager/cache
ls -ld /var/lib/cortex-reviewer-planner /var/lib/cortex-reviewer-planner/.codex \
       /var/lib/cortex-reviewer-planner/cache
ls -ld /var/lib/cortex-builder /var/lib/cortex-builder/.codex /var/lib/cortex-builder/cache
#   期望：HOME 與 .codex 皆 root:root 0755；三個 cache 各自 <帳號>:<帳號> 0700。
#   （Manager 不跑模型，故無 `~/.codex` 這一條。）

# ✅ 驗證：job-spec spool 是 **per-principal** 的（#657）——容器 ＋ 三格
#   ⚠️ 路徑是 **<coordinator_root>/job-specs/<principal>**。#657 之前三份模板 unit
#      共用一個 `<coordinator_root>/job-specs`，而 ACL 只授 builder ⇒ reviewer／gate
#      的**每一個** job 都在 shim 讀 spec 時 EACCES → 78/CONFIG。詳見 5-3a。
ls -ld /var/lib/cortex/coordinator/job-specs
#   期望：cortex-manager:cortex-manager 0700（容器本身**不授**任何 job 帳號讀權）
sudo getfacl -p /var/lib/cortex/coordinator/job-specs | grep -E "^(user|mask):"
#   期望：三條 `user:cortex-{builder,reviewer-planner,gate}:--x`（只給 traverse）
#         ＋ `mask::--x`。**看 mask**：mask 收窄成 `---` 會讓上面三條全部
#         `#effective:---`，ACL 還在但沒有作用（見 5-3a 的 mask 陷阱）。
for P in builder reviewer gate; do
  ls -ld "/var/lib/cortex/coordinator/job-specs/$P"
  sudo getfacl -p "/var/lib/cortex/coordinator/job-specs/$P" \
    | grep -E "^(user|default:user|mask|default:mask):"
done
#   期望（逐格）：cortex-manager:cortex-manager 0700，
#         builder  格 → user:cortex-builder:r-x           ＋ default 同一條
#         reviewer 格 → user:cortex-reviewer-planner:r-x  ＋ default 同一條
#         gate     格 → user:cortex-gate:r-x              ＋ default 同一條
#         且**沒有任何 `#effective:` 註記**（有就是被 mask 吃掉了）。
#   **job 帳號讀得到自己那格是設計**（見 5-3 表格下的說明）：template unit 的
#   `User=` 由 systemd 在 `ExecStart` **之前**套用，shim 本身就是以 job 身分去讀
#   spec——讀不到就起不了 job。守的是**寫入面**（下面第二條）。
sudo -u cortex-builder ls /var/lib/cortex/coordinator/job-specs/builder >/dev/null \
  && echo "builder 可列自己那格（BY DESIGN）"
sudo -u cortex-builder sh -c 'printf "{}" > /var/lib/cortex/coordinator/job-specs/builder/probe.json' 2>&1 | tail -1
#   期望：Permission denied ← **這條才是邊界**（builder 改不了自己的命令列）
sudo -u cortex-builder ls /var/lib/cortex/coordinator/job-specs 2>&1 | tail -1
#   期望：Permission denied（容器只給 traverse，不給列目錄——builder 因此連
#         「這台機器上還有誰的 job」都看不到）
sudo -u cortex-gate ls /var/lib/cortex/coordinator/job-specs/builder 2>&1 | tail -1
sudo -u cortex-reviewer-planner ls /var/lib/cortex/coordinator/job-specs/gate 2>&1 | tail -1
#   期望：兩行 Permission denied（跨 principal 讀不到彼此的 spec——#657 選
#         per-principal 而非「擴大共用 spool 的 reader 面」買到的正是這一條）
```

### 2c. 建立來源樹 ＋ 落三份 root-owned `.gitconfig`（#623）

第 2b 步已把 `/var/lib/cortex/repos`（登記表資產 `repo-source-tree`）建成
**`cortex-manager` 擁有、0700**、兩個 job 帳號各帶一條唯讀 ACL（`rX`）的空容器。
本步把受治理的 repo 放進去，並讓 Manager 與兩個 job 帳號都有辦法對它跑 git。

**為什麼是 per-job 完整 clone 而不是 `git worktree`**：#623 實測——worktree 的
`.git` 是指向共用 object store 的指標，builder 要 `git add`／`git commit` 就必須能寫
那個 store，而 store 在 `0700 cortex-manager` 底下。**「builder 能 commit」與「三分
隔離」互斥**，不是權限沒調好。per-job clone 實測 0.5 秒／35MB，來源對 job 唯讀。

**為什麼是 working checkout 而不是 bare mirror**：monitor 掃的是工作樹裡的檔案
（`workstreams/*/todo.md` …），bare 沒有工作樹。同一份 checkout 因此兼作 monitor 的
掃描目標與 job 的 clone 來源。

```bash
# 🔧 sudo：把受治理的 repo 放進來源樹，並交給 Manager 擁有
SLUG=paulsha-cortex          # 目錄名；下面每個命令都用它
UPSTREAM=https://github.com/<owner>/"$SLUG"   # ← 真正的上游，不是本機路徑
sudo git clone <來源 remote 或 operator 的 checkout> /var/lib/cortex/repos/"$SLUG"

# 🔧 sudo：**把 origin 指回真正的上游**（#661；從 operator 的 checkout clone 時必做）
sudo git -C /var/lib/cortex/repos/"$SLUG" remote set-url origin "$UPSTREAM"
sudo git -C /var/lib/cortex/repos/"$SLUG" fetch --prune origin

sudo chown -R cortex-manager:cortex-manager /var/lib/cortex/repos/"$SLUG"
# 兩個 job 帳號要讀得到整棵樹（容器的 default ACL 只涵蓋**之後**新建的物件，
# 這一步是把 clone 當下已存在的那幾萬個檔一次補齊）。
sudo setfacl -R -m u:cortex-builder:rX,u:cortex-reviewer-planner:rX \
  /var/lib/cortex/repos/"$SLUG"
sudo setfacl -R -d -m u:cortex-builder:rX,u:cortex-reviewer-planner:rX \
  /var/lib/cortex/repos/"$SLUG"
```

> **⚠️ 為什麼一定要 `remote set-url origin`（#661 實機踩到，兩個獨立後果）**：
> 從 operator 的 checkout clone 時，git 會把 `origin` 設成**那條本機路徑**。
>
> 1. **doctor 立刻紅**：`PSC_REPO_IDENTITY` 宣告的是 `git:github.com/<owner>/<repo>`，
>    而 `repo-identity` probe 是拿 `PSC_REPO_ROOT` 解出來的**現況** identity 去比對，
>    本機路徑解不出那個值 ⇒ `PSC_REPO_ROOT identity drift`（required fail）。
> 2. **更要緊的是 ship 段會推錯地方**：#656 的 ship 走 `push origin`——`origin` 指向
>    operator 的 checkout 時，Manager 會把交付推進**本機那棵樹**，而不是 GitHub。
>    那不會報錯，只會安靜地什麼都沒送上去。
>
> 先 `set-url` 再 `fetch`，再 `chown`：順序不可調換（chown 之後 root 對 `.git` 的寫入
> 會踩到 dubious ownership，而此時 Manager 的 `.gitconfig` 還沒落位）。
>
> ✅ 驗證：`sudo git -C /var/lib/cortex/repos/"$SLUG" remote get-url origin`
> 期望印出 `$UPSTREAM`，**不得**是任何 `/home/...` 或本機路徑。

> **為什麼 `cortex-manager` 擁有而不是 root 擁有**（0817 裁決，推翻本 runbook 前一版）：
> `git fetch` 必須把 `FETCH_HEAD` 寫進**目標 repo**，而成果回收正是「fetch 進來源樹」；
> provisioning 那半邊的 `git branch -f <branch> <base>` 同樣是對來源樹的寫入。
> root-owned 下實測：
>
> ```
> error: cannot open '.git/FETCH_HEAD': Permission denied
> ```
>
> **「Manager 唯讀」與「Manager 回收成果」互斥**，取後者。隔離不因此變弱：不受信任的
> 是 **job 帳號**，它們對這棵樹只有 `rX`；Manager 本來就擁有 gate ledger／evidence／
> `jobs.json`——多這一棵樹不改變攻擊面。monitor 雖與 Manager 同帳號，但它的 unit 少了
> 那條 `ReadWritePaths`，因此仍寫不進去（#622 的 persona 過濾）。

```bash
# 🔧 sudo：三份 root-owned .gitconfig（內容由 permgen 產生，勿手寫）
# 旗標名與帳號後綴刻意同名：--<who> 的產物落在 /var/lib/cortex-<who>/.gitconfig。
for who in builder reviewer-planner manager; do
  python3 -m paulsha_cortex.trust_root gitconfig four-way --"$who" --source-repo "$SLUG" \
    | sudo tee "/var/lib/cortex-$who/.gitconfig" >/dev/null
done
sudo chown root:root /var/lib/cortex-{builder,reviewer-planner,manager}/.gitconfig
sudo chmod 0644 /var/lib/cortex-{builder,reviewer-planner,manager}/.gitconfig
```

> **為什麼需要這三個檔**：來源樹與讀它的帳號不同 owner 時，git 的 dubious-ownership
> 保護會擋下操作；解法只有 `safe.directory`，而它**必須由 root 放進該帳號的 HOME**
> ——那些 HOME 都是 root-owned，帳號自己放不了這個檔。與既有的 `~/.codex/hooks.json`
> 同一個模式（登記表資產 `builder-gitconfig`／`reviewer-planner-gitconfig`／
> `manager-gitconfig`）。**Manager 那份不是冗餘**：來源樹是 root 建立後才 chown 過去的，
> chown 前或任何 owner 不相符的中途狀態，Manager 的每一個 git 操作都會失敗
> （`fatal: detected dubious ownership in repository at '<來源樹>/<slug>'`）。
>
> **為什麼每個 repo 是兩條 `safe.directory`**：實測從**非 bare** 來源 clone 時 git 檢查
> 的是 `<repo>/.git`，而 `git -C <repo> …` 報的是工作樹根——兩個位置就是兩條逐字的值。
> 產生器已自動出兩條，不必手加。
>
> **為什麼逐個列 repo 而不是萬用字元**：git 的 `safe.directory` 只認**逐字相等**的
> 路徑或字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒），而字面 `*` 等於對該帳號整個
> 關掉這個保護。多一個受治理 repo 就多帶一次 `--source-repo`。

```bash
# ✅ 驗證：來源樹由 Manager 擁有、Manager 寫得進去
ls -ld /var/lib/cortex/repos /var/lib/cortex/repos/"$SLUG"
#   期望：容器 cortex-manager 0700；<slug> 亦 cortex-manager 擁有
sudo -u cortex-manager git -C /var/lib/cortex/repos/"$SLUG" fetch --prune origin
#   期望：rc=0。若出現 `cannot open '.git/FETCH_HEAD': Permission denied` ⇒ chown 沒做完；
#         若出現 `fatal: detected dubious ownership` ⇒ manager 那份 .gitconfig 沒生效。

# ✅ 驗證：兩個 job 帳號讀得到、但一個位元都寫不進去
sudo -u cortex-builder git -C /var/lib/cortex/repos/"$SLUG" rev-parse --short HEAD
#   期望：印出 commit（讀得到）
sudo -u cortex-builder sh -c "touch /var/lib/cortex/repos/$SLUG/evil" 2>&1 | tail -1
#   期望：Permission denied ← **這條才是邊界**
sudo -u cortex-reviewer-planner sh -c "touch /var/lib/cortex/repos/$SLUG/evil" 2>&1 | tail -1
#   期望：Permission denied

# ✅ 驗證：兩個 job 帳號真的 clone 得動（.gitconfig 生效）
sudo -u cortex-builder git clone --no-hardlinks \
  /var/lib/cortex/repos/"$SLUG" /tmp/clone-probe-builder
#   期望：成功。若出現 `fatal: detected dubious ownership` ⇒ .gitconfig 沒生效：
#         先確認檔案 root:root 0644、且該帳號的 HOME 與 unit 的 Environment=HOME 一致，
#         再確認 `[safe]` 段裡**同時**有工作樹根與 `<root>/.git` 兩條。
sudo rm -rf /tmp/clone-probe-builder

# ✅ 驗證：三份 .gitconfig 對應帳號皆不可寫
for who in builder reviewer-planner manager; do
  sudo -u "cortex-$who" sh -c "printf x >> /var/lib/cortex-$who/.gitconfig" 2>&1 | tail -1
done
#   期望：三行皆 Permission denied

# ✅ 驗證：commit spool 是 `wx` 無 `r`（builder 寫得進、讀不到他人）
getfacl -p /var/lib/cortex/coordinator/commit-spool 2>/dev/null | grep '^user:cortex-builder'
#   期望：user:cortex-builder:-wx（**沒有 r**）
sudo -u cortex-builder ls /var/lib/cortex/coordinator/commit-spool 2>&1 | tail -1
#   期望：Permission denied（列不出別人的 bundle）
sudo -u cortex-reviewer-planner sh -c \
  "touch /var/lib/cortex/coordinator/commit-spool/probe" 2>&1 | tail -1
#   期望：Permission denied（producer 只有 builder）

# ✅ 驗證（#638）：dispatch 之後那**一格**的具名條目沒有被 mask 遮掉
getfacl -p /var/lib/cortex/coordinator/commit-spool/<job-id> 2>/dev/null \
  | grep -E '^(user:cortex-builder|mask)'
#   期望：user:cortex-builder:-wx（**不得**帶 `#effective:---`）、mask::-wx 或更寬
#   出現 `mask::---` ⇒ 那一格是以明確 mode 建立的（#638 缺陷 1 復發），
#         builder 會在 `commits.bundle.part.lock` 這一步就 Permission denied。
```

**更新來源樹**（日常操作）：Manager 現在是 owner，因此**服務自己就能** `fetch`；以
operator 身分手動更新時記得別把 owner 換掉：

```bash
sudo -u cortex-manager git -C /var/lib/cortex/repos/"$SLUG" fetch --prune
sudo -u cortex-manager git -C /var/lib/cortex/repos/"$SLUG" merge --ff-only origin/main
# 服務以 UMask=0077 建檔，新物件對 job 帳號預設不可讀；補一次遞迴 ACL 讓 clone 仍成立。
sudo setfacl -R -m u:cortex-builder:rX,u:cortex-reviewer-planner:rX \
  /var/lib/cortex/repos/"$SLUG"
```

**回滾**：`sudo rm -rf /var/lib/cortex/repos
/var/lib/cortex-{builder,reviewer-planner,manager}/.gitconfig`。

---

**回滾（第 2 步整體）**：`sudo rm -rf /var/lib/cortex /var/lib/cortex-manager /var/lib/cortex-reviewer-planner
/var/lib/cortex-builder /opt/cortex`（此時新樹仍空，舊樹完全未動）。

---

## 第 3 步：legacy-import（物理隔離 ＋ hash manifest；**不 chown 沿用**）

裁決：舊 state **不**併入新樹、**不** `chown` 沿用，而是整包搬到 quarantine，
留下內容 hash manifest。`legacy-imported` 來源 **MUST NOT** 滿足任何 ship gate。

### 3-0. 先分清楚兩類東西——裁決只針對其中一類

`$HOME/.agents` 底下**不是同質的**。裁決「legacy-imported 不得滿足任何 ship gate」
針對的是**模型產出的 state／evidence**；把 operator 親手寫的設定一起關進 quarantine，
等於要求 operator 憑記憶把自己的設定重打一次——而且**沒有任何一步負責搬**。

| 類別 | 內容 | 為什麼 | 本步怎麼處理 |
|---|---|---|---|
| **模型產出的 state／evidence** | `coordinator/**`、`monitor/**`、`registry/**`、`runtime/**` | 是 gate 的**受檢對象**；來源不可信正是裁決要隔離的東西 | **整包 quarantine，不併入新樹**（3a） |
| **operator 撰寫的設定** | `config/**`（`project-cortex.yaml`、`projects.yaml`、`model-identities.yaml`…）、`specs/**` | **不是模型輸出**，也不是任何 gate 的受檢對象；它是 operator 的意圖宣告 | **明示複製 ＋ 逐份審閱後放進新樹**（3a-2） |

**漏掉 3a-2 的症狀**（M1 實測）：第 2 步只建了 `config/paulsha` 這個**目錄**並套權限，
沒有任何一步搬**內容**；於是新樹拿不到設定，`cortex monitor --once` 直接：

```text
錯誤: 無 project 設定：manual（project-cortex.yaml / legacy）與 project-hippo.yaml 皆不存在
```

而這條**不會**被第 7 步的結構性自檢抓到——它結構全綠、功能全死。
第 7 步因此另加了功能面檢查（見該步）。

### 3a. 模型產出的 state／evidence → quarantine

```bash
# ✅ 1. 先確認 in-flight 已收尾（執行前提第 1 項；此處再確認一次，直接讀舊 jobs.json）
python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.path.expanduser("~/.agents/coordinator/jobs.json"))
if not p.exists():
    print("no jobs.json — nothing in flight")
else:
    blob = json.loads(p.read_text(encoding="utf-8") or "{}")
    rows = blob.get("jobs") or blob.get("records") or []
    live = [r for r in rows if str(r.get("status", "")).lower() in {"running", "dispatched", "launched"}]
    print(f"in-flight = {len(live)}")
    for r in live:
        print("  ", r.get("job_id"), r.get("status"))
PY
#   期望：in-flight = 0

# ✅ 2. 內容 hash manifest（import attestation 的實體，spec §R6(e)）
STAMP="$(date +%Y%m%d-%H%M)"
( cd "$HOME/.agents" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) \
  > "/tmp/legacy-import-manifest-$STAMP.txt"
wc -l "/tmp/legacy-import-manifest-$STAMP.txt"
sha256sum "/tmp/legacy-import-manifest-$STAMP.txt"
#   ↑ 把這個 manifest 自身的 hash 記在 #584，作為 import 的錨點

# 🔧 sudo：建 quarantine 並整包搬入（唯讀、cortex-manager 擁有，不併入新樹）
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/legacy-imported
sudo cp -a "$HOME/.agents/." /var/lib/cortex/legacy-imported/
sudo cp "/tmp/legacy-import-manifest-$STAMP.txt" \
        /var/lib/cortex/legacy-imported/.legacy-import-manifest.txt
sudo chown -R cortex-manager:cortex-manager /var/lib/cortex/legacy-imported
sudo chmod -R a-w /var/lib/cortex/legacy-imported
```

```bash
# ✅ 驗證：quarantine 唯讀、內容與 manifest 相符、新樹仍是空的
sudo find /var/lib/cortex/legacy-imported -perm /222 -print | head
#   期望：空輸出（整棵唯讀）
( cd /var/lib/cortex/legacy-imported && sudo sha256sum -c .legacy-import-manifest.txt ) \
  2>&1 | grep -c ": OK$"
#   期望：與 manifest 行數相同
ls -A /var/lib/cortex/coordinator /var/lib/cortex/control
#   期望：空（或只有第 2 步建的空子目錄）——舊 jobs.json／evidence **沒有**被併進來

# ✅ 驗證：兩個模型 job 帳號都讀不到 quarantine
sudo -u cortex-builder ls /var/lib/cortex/legacy-imported 2>&1 | tail -1
sudo -u cortex-reviewer-planner ls /var/lib/cortex/legacy-imported 2>&1 | tail -1
#   期望：兩者皆 Permission denied
```

> **重要**：新樹的 **state／evidence 面**是乾淨的。切換後產生的 record 一律走正常
> gate；`legacy-imported/` 只是唯讀歷史副本，任何從中還原的 **state／evidence**
> **不得**被 ship gate 採計。正式的 `trust: legacy-imported` 簽章標記屬 **Phase 3**，
> 本階段以「物理隔離＋hash manifest」達成同等的不可竄改性主張。
> 下一節搬的 `config/**`／`specs/**` **不在這個限制內**——它們不是 gate 的受檢對象。

### 3a-2. operator 撰寫的設定 → 明示複製進新樹（**不可略過**）

quarantine 是唯讀副本，因此以它為來源複製是安全的（內容已被 3a 的 manifest 錨定）。
**逐份看過再放**——這是 operator 對「新樹該照什麼設定跑」的重新確認，不是機械搬運。

```bash
# ✅ 1. 先看 quarantine 裡有哪些 operator 設定（不是全部都要搬）
sudo find /var/lib/cortex/legacy-imported/config /var/lib/cortex/legacy-imported/specs \
     -type f 2>/dev/null | sort
#   M1 實機出現的三份：config/paulsha/project-cortex.yaml、projects.yaml、
#   model-identities.yaml。**逐份 `sudo less` 看過**——舊路徑（`~/.agents/...`）
#   若被寫死在設定裡，要改成新樹路徑，否則搬過去也是指回舊樹。

# 🔧 sudo：以 cortex-manager 身分複製（owner 直接就對，不必事後 chown）
sudo -u cortex-manager sh -c '
set -e
SRC=/var/lib/cortex/legacy-imported/config/paulsha
DST=/var/lib/cortex/config/paulsha
for f in project-cortex.yaml projects.yaml model-identities.yaml; do
  [ -f "$SRC/$f" ] || { echo "skip（來源沒有）: $f"; continue; }
  [ -e "$DST/$f" ] && { echo "skip（新樹已有，不覆蓋）: $f"; continue; }
  cp "$SRC/$f" "$DST/$f" && echo "copied: $f"
done'
sudo chmod 0600 /var/lib/cortex/config/paulsha/*.yaml
#   ↑ 清單刻意寫死而不是 `cp -r`：**要搬什麼是 operator 的決定**。
#     `specs/**` 同理——有需要就以同樣形態補一段，沒有就不動。

# ✅ 驗證：新樹讀得到設定，且內容與 quarantine 一致
ls -l /var/lib/cortex/config/paulsha/
sudo -u cortex-manager cat /var/lib/cortex/config/paulsha/project-cortex.yaml | head -20
for f in /var/lib/cortex/config/paulsha/*.yaml; do
  b=$(basename "$f")
  sudo cmp -s "$f" "/var/lib/cortex/legacy-imported/config/paulsha/$b" \
    && echo "same as quarantine: $b" || echo "!! 與 quarantine 不同（若是刻意改路徑則正常）: $b"
done

# ✅ 驗證（功能面）：設定真的被載入——這條是 3a-2 的**存在理由**
#   展開 env 用 source 而非 `env $(… | xargs)`（#633：後者會拆壞含空格的值）。
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/cortex monitor --once
' 2>&1 | head -20
#   期望：**不得**出現
#     `錯誤: 無 project 設定：manual（project-cortex.yaml / legacy）與 project-hippo.yaml 皆不存在`
#   出現這行 ⇒ 設定沒搬到／檔名不符／路徑被寫死指回舊樹，回上面重做。
#   註：本步在第 4b（EnvironmentFile）之後才跑得起來；若此時尚未到第 4b，
#       把這條記在待辦，第 7 步的功能面檢查會再驗一次。
```

> **為什麼不是 `cp -a` 整棵 `config/`**：`config/` 底下未來可能混進非 operator 撰寫的
> 快取或衍生檔；整棵搬會把「哪些是 operator 的意圖」這條界線再度模糊掉。逐檔白名單
> 讓每一份設定進新樹都是一個**明示決定**，也讓 diff 在下次升級時看得懂。

### 3b. review verdict spool（Phase 2a 已就位的受控通道；三分下才完整）

Phase 2a（PR #599）已把 review verdict 的落點從 reviewer worktree 搬到
`/var/lib/cortex/coordinator/review-verdicts/<reviewer_job_id>/verdict.json`
（登記表 `review-verdict-spool`，spec §R2）。**程式碼側已完成**：per-job 目錄由
Manager 在 dispatch 當下建立、帶 pre-seed 守衛，落地後把**那一格目錄**轉唯讀
（`0500`）；reviewer 身分由 Manager job registry 推導，verdict payload 的自述綁定
欄位一律忽略。

> **#638 修正的三件事**（三分下才看得見，單 UID 環境全部無感）：
>
> 1. per-job 目錄**不再以明確 mode 建立**——明確 mode 會把 default ACL 繼承來的
>    具名條目連同 **mask** 一起重設成 `#effective:---`，reviewer 因此連 verdict
>    都寫不出來（`commit-spool` 的同一個 bug 讓 builder 連 `.part.lock` 都建不了）。
> 2. verdict 由 **reviewer 自己在寫完後 `chmod 0644`**（wrapper script 的發表段）
>    ——否則檔由 reviewer 擁有、又常帶 `UMask=0077`，Manager 讀不到。
> 3. seal 從「`chmod 0444` verdict 檔」改成「封**目錄**」——只有 owner 或 root 能
>    `chmod`，Manager 不是 verdict 的 owner，舊做法必定 `PermissionError` 且刻意
>    不 raise ⇒ **無聲失敗**，reviewer 可以在 Manager 判讀之後回頭覆寫自己的 verdict。
>
> 驗證（三分主機上，spool 那一格建立之後）：
>
> ```bash
> # 期望：具名條目沒有 #effective:--- ，mask 不是 ---
> sudo getfacl -p /var/lib/cortex/coordinator/review-verdicts/<reviewer_job_id>
> ```

spool 根的權限**已包含在第 2 步的權限 script 內**（它是登記表資產），這裡只做確認：

```bash
# ✅ 驗證：spool 根由 cortex-manager 擁有、0700；reviewer 只有 write-only ACL
ls -ld /var/lib/cortex/coordinator/review-verdicts
sudo getfacl -p /var/lib/cortex/coordinator/review-verdicts | grep -E "^user:"
#   期望：user:cortex-reviewer-planner:-wx —— **wx 無 r**：
#         寫得進自己那格、讀不到他人 verdict。

# ✅ 驗證：builder 對 verdict spool 零權限（三分下這是硬邊界，不是慣例）
sudo -u cortex-builder ls /var/lib/cortex/coordinator/review-verdicts 2>&1 | tail -1
sudo -u cortex-builder sh -c 'printf x > /var/lib/cortex/coordinator/review-verdicts/evil' 2>&1 | tail -1
#   期望：兩者皆 Permission denied

# ✅ 驗證：reviewer 寫得進、讀不到（write-only 的正反面）
sudo -u cortex-reviewer-planner sh -c \
  'printf x > /var/lib/cortex/coordinator/review-verdicts/.negctl && echo WRITE-OK' 2>&1 | tail -1
sudo -u cortex-reviewer-planner ls /var/lib/cortex/coordinator/review-verdicts 2>&1 | tail -1
sudo rm -f /var/lib/cortex/coordinator/review-verdicts/.negctl
#   期望：第一條 WRITE-OK；第二條 Permission denied
```

> 這一步完成後，spec 背景 §3 的「builder 代寫 verdict」最短攻擊路徑才從「結構上
> 不可能被採信」升級為「**OS 層寫不進去**」；而三分讓 reviewer 自己也**讀不到**
> 其他 reviewer 的 verdict。
>
> 另：降權啟動器需把該 job 的 spool 目錄放進 executor sandbox 的放行清單
> （`--add-dir`，見 `SubprocessLauncher.as_verdict_spool_writer()`）——只放行**那一格**，
> 不是整棵 coordinator 樹。

### 3c. 收斂舊路徑殘留的 `g+w`

```bash
# 🔧 sudo：舊樹的 775 殘留（避免仍被任何殘留設定引用時再度成為寫入面）
chmod -R g-w,o-w "$HOME/.agents" 2>/dev/null || true

# ✅ 驗證
find "$HOME/.agents" -perm /022 -print | head
#   期望：空輸出
```

**回滾**：`sudo rm -rf /var/lib/cortex/legacy-imported`；舊 `$HOME/.agents` 未被刪除，
原地仍可用（第 9 步全面回退即用它）。

---

## 第 4 步：Manager／monitor 遷 root-owned 部署 ＋ system-level unit

### 4a. venv 遷入 `/opt/cortex`

> **⚠️ `cp -a` 之後 venv 還沒好**：pipx 的 venv 有**兩處硬編碼**指回 operator 樹，
> 光靠 `cp -a` ＋ `chown` ＋ `chmod` 搬不掉。M1 實測直接
> `sudo -u cortex-manager /opt/cortex/venv/bin/cortex --version` → `Permission denied`：
>
> | 殘留 | 內容 | 為什麼是**安全條件**而不只是「跑不起來」 |
> |---|---|---|
> | `bin/*` 的 shebang | `#!$HOME/.local/share/pipx/venvs/paulsha-cortex/bin/python` | 部署樹的**第一支被執行的程式**其實住在 operator 可寫的目錄裡 |
> | `lib/python3.*/site-packages/pipx_shared.pth` | 指向 operator 的 pipx shared site-packages | 部署樹的 **import path** 仍受 operator 可寫目錄影響——`.pth` 是背景 §5 那條「注入 `.pth`」攻擊面的同一個機制 |
>
> 兩者都必須在硬化（`chmod a-w`）**之前**清掉，否則 `/opt/cortex` 全 root-owned
> 這件事只是表面的：真正決定執行什麼的兩個指標仍在信任邊界外。

```bash
# 🔧 sudo：複製（不是原地 chown）到 root-owned 部署路徑
sudo rm -rf /opt/cortex/venv.new
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" /opt/cortex/venv.new

# 🔧 sudo：(1) 重寫 bin/* 的 shebang 前綴 —— 指回部署樹自己的 python
#   只改「第一行確實是舊前綴」的檔案，二進位檔與 symlink 一律不碰。
#   注意寫的是最終路徑 /opt/cortex/venv（不是 venv.new）——下面就會 mv 過去。
sudo env OLD_PREFIX="$HOME/.local/share/pipx/venvs/paulsha-cortex" sh -s <<'SH'
set -eu
for f in /opt/cortex/venv.new/bin/*; do
  [ -f "$f" ] || continue            # 跳過 symlink／目錄
  IFS= read -r first < "$f" || continue
  case "$first" in
    "#!$OLD_PREFIX/bin/"*) ;;
    *) continue ;;
  esac
  interp=${first#"#!$OLD_PREFIX/bin/"}
  sed -i "1s|.*|#!/opt/cortex/venv/bin/$interp|" "$f"
  echo "shebang rewritten: $f -> /opt/cortex/venv/bin/$interp"
done
SH

# 🔧 sudo：(2) 移除 pipx_shared.pth —— 部署樹不得再 import operator 的 shared 樹
sudo find /opt/cortex/venv.new -name "pipx_shared.pth" -print -delete
#   註：pipx shared 樹裡是 pip／setuptools／wheel，runtime 不需要；部署樹本來就
#   不該在裡面裝東西（升級走第 6 步整棵替換）。

# 🔧 sudo：硬化（順序不可調換——上面兩步要在 a-w 之前做完）
sudo chown -R root:root /opt/cortex/venv.new
sudo find /opt/cortex/venv.new -type d -exec chmod 0755 {} +
sudo find /opt/cortex/venv.new -type f -exec chmod a-w {} +
sudo find /opt/cortex/venv.new/bin -type f -exec chmod 0755 {} +

# ✅ 驗證（切換前）：部署樹裡不得殘留任何指回 operator 樹的路徑
#   這是上面兩步的**總驗收**——它同時涵蓋 shebang、.pth、pyvenv.cfg、
#   console-script 內嵌路徑等一切形式，比逐項檢查更難漏。
sudo grep -rIl -- "$HOME/.local/share/pipx" /opt/cortex/venv.new | head
#   期望：空輸出
sudo find /opt/cortex/venv.new -type l -lname "*/.local/share/pipx/*" | head
#   期望：空輸出（symlink 也不得指回 operator 樹）

# 🔧 sudo：切成 active（venv 目錄本身即 ExecStart 的目標；保留舊的供回滾）
sudo rm -rf /opt/cortex/venv.prev
[ -d /opt/cortex/venv ] && sudo mv /opt/cortex/venv /opt/cortex/venv.prev
sudo mv /opt/cortex/venv.new /opt/cortex/venv
```

```bash
# ✅ 驗證：對服務帳號唯讀、可執行、版本正確
sudo -u cortex-manager /opt/cortex/venv/bin/cortex --version
#   期望：印出版本。**若是 `Permission denied`** ⇒ shebang 仍指回 operator 樹
#   （`head -1 /opt/cortex/venv/bin/cortex` 即可確認），回上面的 (1) 重做。
sudo -u cortex-manager /opt/cortex/venv/bin/python -c "import paulsha_cortex, sys; print(sys.prefix)"
#   期望：/opt/cortex/venv —— 確認 import path 沒有經由 .pth 逃回 operator 樹
sudo -u cortex-manager test -w /opt/cortex/venv/bin/cortex && echo "!! 可寫，停止" || echo "read-only: OK"
sudo find /opt/cortex/venv -perm /022 -print | head
#   期望：空輸出

# ✅ 驗證：兩個模型 job 帳號也都不可寫部署樹
for U in cortex-reviewer-planner cortex-builder; do
  sudo -u "$U" sh -c 'printf x >> /opt/cortex/venv/bin/cortex' 2>&1 | tail -1
done
#   期望：兩者皆 Permission denied

# ✅ 沒有可注入點
sudo find /opt/cortex/venv -name "sitecustomize.py" -o -name "*.pth" | xargs -r ls -l
#   期望：若有，全部 root:root 且不可寫
```

> spec §R3：executable／deps／launcher／venv 對 headless **不可寫**、owner=root。
> 這封掉背景 §5 的「改寫 verifier／注入 `sitecustomize.py`／`.pth`」攻擊面。

**delivery preflight 的 backend 也裝進這個 venv（#661）**。它不是可執行檔而是一個
python 套件，因此落點不是 toolchain 而是這棵已經 root-owned 的樹——`PSC_PREFLIGHT_CMD`
指向的 `paulsha_cortex.preflight_ci` 只是個 typed-argv 轉接器，真正跑 CI-parity 檢查的
是治理引擎自己的 `policy_check.preflight`。

```bash
# 🔧 sudo：在**硬化之前**（chmod a-w 之前）裝進 venv.new；版本必須逐字等於
#         .project-policy.yml 的 policy_version
POLICY_VERSION="$(grep -oE '^policy_version:[[:space:]]*[0-9.]+' \
  /var/lib/cortex/repos/paulsha-cortex/.project-policy.yml | grep -oE '[0-9.]+$')"
sudo /opt/cortex/venv.new/bin/pip install --no-input \
  "git+https://github.com/hamanpaul/paulsha-conventions@v${POLICY_VERSION}#egg=policy-check"
#   ⚠️ 唯一相依是 PyYAML（venv 裡已經有），不會拉進其他東西。
#   ⚠️ 已經硬化過（a-w）才想補裝時：pip 會 Permission denied——回到 4a 開頭重做整棵，
#      不要 `chmod +w` 之後補裝（那會在部署樹留下一個可寫窗口）。
```

```bash
# ✅ 驗證：版本逐字相符（引擎自己也會驗，對不上就 fail-closed）
/opt/cortex/venv/bin/python3 -c \
  "from importlib.metadata import version; print(version('policy-check'))"
#   期望：與 .project-policy.yml 的 policy_version 逐字相同。
#   這與 R-23（workflow pin ⟷ policy_version）是同一個部署決定的兩半：兩條都成立時，
#   本機 preflight 跑的引擎版本 == CI 跑的引擎版本。

# ✅ 驗證：轉接器與 backend 都 import 得到、且 doctor 收得下這個值
sudo -u cortex-manager /opt/cortex/venv/bin/python3 -c \
  "import paulsha_cortex.preflight_ci, policy_check.preflight; print('ok')"
#   期望：ok

# ✅ 驗證：doctor 的 preflight probe 由紅轉綠
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/cortex inspect doctor --json' | \
  python3 -c "import json,sys;[print(p['name'],p['status'],p['detail']) for p in json.load(sys.stdin)['probes'] if p['name']=='preflight']"
#   期望：preflight pass。
#   ⛔ `executable-unavailable` ⇒ EnvironmentFile 的路徑打錯或 venv 尚未切成 active。
```

### 4b. bootstrap env 遷入部署樹（root 擁有、fail-closed）

env 檔放在 **`/opt/cortex/etc/`**（全 root-owned 樹）而**不是** `/var/lib/cortex` 底下：
`/var/lib/cortex` 的子樹由 `cortex-manager` 擁有，**目錄可寫者能 unlink／replace 其中的
root-owned 檔案**——把 env 檔放進去等於沒保護。

```bash
# 🔧 sudo：寫 EnvironmentFile（instance-scoped，修掉背景 §7 的多 instance 共用漏洞）
sudo tee /opt/cortex/etc/cortex-manager.env >/dev/null <<'ENVFILE'
PSC_INSTANCE="cortex"
PSC_AGENTS_ROOT="/var/lib/cortex"
# ⚠️ 五個 root **刻意不列**（#661）：PSC_CONTROL_ROOT／PSC_COORDINATOR_ROOT／
#    PSC_SPECS_ROOT／PSC_MONITOR_STATE_ROOT／PSC_RUN_ROOT 由 PSC_AGENTS_ROOT 導出。
#    顯式列出反而製造漂移——理由見本段下方的說明方塊。
PSC_PROJECT_CONFIG_ROOT="/var/lib/cortex/config/paulsha"
PSC_WORKTREE_ROOT="/var/lib/cortex/worktree"
PSC_DEGRADED_OPERATION="per-case-approval"
# --- 操作變數（#623 缺口 2／#633）：少了這一段，服務起得來但**做不了事** ---
# 目標 repo。第 2c 步建立的來源樹，**不是** operator 的 checkout。
PSC_REPO_ROOT="/var/lib/cortex/repos/paulsha-cortex"
PSC_REPO_IDENTITY="hamanpaul/paulsha-cortex"
PSC_MANAGER_EXECUTOR="codex"
PSC_MANAGER_INTERVAL_SECONDS="60"
PSC_MANAGER_GITHUB_INTERVAL_MS="600000"
# gate 命令是**命令列**，天生含空格 ⇒ 這一族就是「值含空格」的常態，不是邊角案例。
PSC_GATE_CMD_PYTEST="python3 -m pytest -q"
PSC_GATE_TIMEOUT="900"
# delivery preflight（#661）。值由產生器出，不要手打：
#   python3 -m paulsha_cortex.trust_root toolchain four-way | grep PSC_PREFLIGHT_CMD
PSC_PREFLIGHT_CMD="/opt/cortex/venv/bin/python3 -m paulsha_cortex.preflight_ci"
ENVFILE
sudo chown root:root /opt/cortex/etc/cortex-manager.env
sudo chmod 0644 /opt/cortex/etc/cortex-manager.env
```

> **⚠️ 只設 `PSC_AGENTS_ROOT`，其餘 root 一律讓它 derive**（#661 實機踩到）。
>
> 本模板前一版顯式設了 `PSC_CONTROL_ROOT`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT`／
> `PSC_MONITOR_STATE_ROOT`／`PSC_RUN_ROOT`。它看起來只是「把導出值寫明」，實際上是
> **在製造漂移**：doctor 的 `managed-path-drift` probe 比對的是 installer 的 managed_env
> 推導公式（`deploy/installer.py`），而 `PSC_CONTROL_ROOT` 在那條公式裡是
> **instance-scoped** 的 `control/<instance>`——模板寫的 `control` 與它不相等：
>
> ```
> FAIL managed-path-drift: managed path drift detected
>      PSC_CONTROL_ROOT: effective=/var/lib/cortex/control derived=/var/lib/cortex/control/cortex
> ```
>
> 拿掉之後 `config/runtime.py` 的 `resolve_runtime_root()` 會由 `PSC_AGENTS_ROOT` 導出
> `<agents_root>/control`，**逐字等於 `permgen.PathLayout.control_root`**——也就是登記表
> 與 unit `ReadWritePaths` 實際保護的那條路徑。換句話說：不列出來不只是「drift 消失」，
> 是解析結果與保護面對齊；列出來反而讓兩者分岔。
>
> 保留 `PSC_PROJECT_CONFIG_ROOT`（它的顯式值與兩邊的推導都相同，且 probe 對「鍵缺席」
> 只給 warn）與 `PSC_WORKTREE_ROOT`（不在 `RUNTIME_ROOT_DEFAULTS` 裡，沒有導出值）。
>
> ✅ 驗證：`cortex inspect doctor --json` 的 `managed-path-drift` 不得為 `fail`。

> **⚠️ 值一律加引號，`PSC_PREFLIGHT_CMD` 自 #661 起有值**（#633／#661）。
>
> **加引號**：systemd 的 `EnvironmentFile` 把 `=` 之後整段當值，加不加引號它都對
> （加了會剝掉）。加引號是為了**讀這個檔的另外兩種方式**——`sh` 的 `.`（source）
> 與下面各節的驗證指令。`PSC_GATE_CMD_PYTEST=python3 -m pytest -q` 未加引號時
> `. <envfile>` 會把 `-m` 當成命令執行（`sh: -m: not found`）。一份三邊都讀得對的
> 檔案比「只有 systemd 讀得對」便宜太多，所以整份統一加。
>
> **`PSC_PREFLIGHT_CMD` 的落定（#661，取代 #633 當時的「刻意留白」）**：舊值
> `~/.local/bin/cortex-preflight-ci` 是 shell wrapper，它指向的 backend
> （另一個 repo 的 `preflight.sh`）也在 `/home` 底下——`ProtectHome=yes` 之後**兩層
> 都不可達**（doctor 報的是 `executable-unavailable`）。新值是 typed argv、絕對路徑、
> 落在既有的 root-owned 部署 venv 裡，因此天生在保護面內。**它需要第 4a 步一併把
> backend 裝進同一個 venv**（見 4a 末段），否則 doctor 會過、ship 當下才失敗。
>
> **`PSC_REPO_ROOT` 未宣告時的行為（#633）**：Manager **起得來**——`ScriptWorktreeCreator`
> 的 repo 解析自 #633 起是 lazy 的，因此缺這個變數只讓「派不了工」，不再讓 daemon
> 啟動即崩、被 `Restart=on-failure` 推進 crash-loop（#630 落地當下的實機症狀：
> `NRestarts` 連跳 7 次）。fail-closed 沒有放寬，只是挪到派工當下：第一次 dispatch
> 會拿到 `PSC_REPO_ROOT 未宣告，拒絕退回 cwd（#612）` 這條可操作的訊息。

```bash
# ✅ 驗證：owner/mode 與 permgen 的 deployment 區塊一致
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  | grep -A3 "runtime-bootstrap-env"
ls -l /opt/cortex/etc/cortex-manager.env
#   期望：-rw-r--r-- root root

# ✅ 驗證：這份 env 檔 shell 也 source 得動（含空格的值不會炸）
sh -c 'set -a; . /opt/cortex/etc/cortex-manager.env; set +a; echo "[$PSC_GATE_CMD_PYTEST]"'
#   期望：`[python3 -m pytest -q]`（**一個**值，含空格）。
#   ⛔ `sh: -m: not found` ⇒ 某一行的值沒加引號，回上面補。

# ✅ 驗證：env 生效後路徑解析全部落在受保護樹內
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -c "from paulsha_cortex.config import paths; print(paths.agents_root(), paths.control_root(), paths.coordinator_root())"
'
#   期望：三者都在 /var/lib/cortex 底下
```

> **⚠️ 展開 env 檔一律用 `sh -c 'set -a; . <envfile>; set +a; …'`，不要用
> `env $(grep -v '^#' <envfile> | xargs)`**（#633，本 runbook 原有 10 處已全部改掉）。
> `$(… | xargs)` 依**空白**切詞，於是任何值含空格的變數都會被拆成多個參數：補上
> `PSC_GATE_CMD_PYTEST` 之後，實機那 10 條驗證指令全數變成
>
> ```
> env: ‘-m’: No such file or directory        ← rc=127
> ```
>
> 而**systemd 自己完全沒事**——壞的只有驗證指令。這種錯最貴的地方在於它讓
> operator 以為是部署壞了，回頭去改一份其實正確的 env 檔。

> **為何 unit 的 `EnvironmentFile` 一定生效**：`config/runtime.py` 的
> `resolve_runtime_root()` **先看行程 env**，其次才看 `$HOME/.agents/core/runtime/`
> 的 bootstrap 檔。unit 注入的 `PSC_*` 因此優先，且 `ProtectHome=yes` 讓 HOME fallback
> 根本不可見——雙保險。

### 4c. Manager system-level unit（內容由 permgen 產生）

```bash
# ✅ 先看內容（ReadWritePaths 逐條附「涵蓋哪些登記表資產」註解）
python3 -m paulsha_cortex.trust_root unit four-way --manager | less

# 🔧 sudo：寫入 unit（內容一字不改，直接由產生器落檔）
python3 -m paulsha_cortex.trust_root unit four-way --manager \
  | sudo tee /etc/systemd/system/cortex-manager.service >/dev/null
sudo chown root:root /etc/systemd/system/cortex-manager.service
sudo chmod 0644 /etc/systemd/system/cortex-manager.service

# 🔧 sudo：載入並啟用（system-level，非 --user）
sudo systemctl daemon-reload
sudo systemctl enable cortex-manager.service
sudo systemctl start cortex-manager.service
```

```bash
# ✅ 驗證：身分、加固、ReadWritePaths 都如產生器所述
systemctl show cortex-manager.service \
  -p User -p NoNewPrivileges -p ProtectSystem -p ProtectHome -p PrivateTmp \
  -p CapabilityBoundingSet -p ReadWritePaths
#   期望：**User=cortex-manager**（三分：不再是 cortex-svc）、NoNewPrivileges=yes、
#         ProtectSystem=strict、ProtectHome=yes、PrivateTmp=yes、
#         CapabilityBoundingSet=（空）

# ✅ 驗證：unit 檔內容與產生器輸出逐位元相同（防手改漂移）
diff <(python3 -m paulsha_cortex.trust_root unit four-way --manager) \
     /etc/systemd/system/cortex-manager.service && echo "unit in sync: OK"

# ✅ 驗證：加固評分（systemd 自己的評估，僅供對照）
systemd-analyze security cortex-manager.service | tail -5

# ✅ 驗證：服務起得來、無 EPERM/EROFS
systemctl status cortex-manager.service --no-pager
sudo journalctl -u cortex-manager.service -n 100 --no-pager | grep -Ei "eperm|erofs|eacces|read-only" || echo "no denial in log: OK"

# ✅ 驗證：Manager 行程確實以 cortex-manager 身分跑（三分的行程面證據）
ps -o user=,pid=,cmd= -p "$(systemctl show cortex-manager.service -p MainPID --value)"
#   期望：user 欄為 cortex-manager

# ✅ 驗證：fail-closed——刪掉 env 檔必須拒絕啟動（測完立刻還原）
sudo mv /opt/cortex/etc/cortex-manager.env /opt/cortex/etc/cortex-manager.env.bak
sudo systemctl restart cortex-manager.service; echo "exit=$?"
#   期望：restart 失敗（非 0），status 顯示 EnvironmentFile 缺檔
sudo mv /opt/cortex/etc/cortex-manager.env.bak /opt/cortex/etc/cortex-manager.env
sudo systemctl restart cortex-manager.service && echo "restored: OK"
```

**回滾**：`sudo systemctl disable --now cortex-manager.service;
sudo rm /etc/systemd/system/cortex-manager.service; sudo systemctl daemon-reload`，
再以 `systemctl --user start cortex-manager.service` 回到舊部署（見第 9 步）。

### 4d. Monitor system-level unit（內容由 permgen 產生）

> **不可省略**（issue #622）。舊的 `cortex-monitor.service` 是 `--user` unit、以
> operator 身分跑、`PSC_MONITOR_STATE_ROOT` 指著舊的 `~/.agents/monitor`。第 4 步之後
> 把它起回來只會**雙寫**——`monitor-state-tree`／`monitor-work-items-snapshot`／
> `monitor-github-sync-cursor` 出現兩個來源，正是 Phase 2b 要收斂掉的狀態；而且它
> 寫不進 `0700 cortex-manager` 的 `/var/lib/cortex/monitor`，只會靜默地繼續寫舊樹。
> **跳過本步＝instance 沒有 monitor**：GitHub issue sync／work-items 快照停擺，
> `monitor-event-spool` 只有 builder 的 `wx` 生產端、沒有消費端，spool 只增不減。

> **⛔ 但**：**現在只安裝，不要 `enable --now`**（issue #623，實機驗證）。
> 「裝好」與「啟動」是兩件事——本步的**安裝與驗證全部照做**，
> **唯獨最後的 `enable`／`start` 要等 #623 的三個缺口全解**。
>
> 為什麼不能先起來擋著：`PSC_DEGRADED_OPERATION=per-case-approval` **不會**阻止
> 派工。它只 gate 四個敏感動作（`headless-acceptance`／`outbox-mutation`／`ship`／
> `merge`，見 `trust_root/capability.py`），**job spawn 不在其中**。
> 因此 monitor 一起來，intake 就會開始運作並真的派 builder job 出去，而那些 job
> **現在不可能成功**——#623 記錄的兩個成因：unit 的 `ProtectHome=yes` 讓 repo
> 路徑不可達；EnvironmentFile 缺八個操作變數（含 `PSC_GATE_CMD_PYTEST`）。
> 後果是**燒模型額度、產生 needs_human 噪音、留下半死的 run 狀態**——
> 全部發生在「看起來裝好了」之後，而且沒有任何一條 M1 的結構性驗收會變紅。
>
> **#633 補充**：第 4b 的模板已含那八個操作變數（值一律加引號）。**但這個 gate 仍然
> 成立**，而且理由更需要寫清楚：#633 之後 repo 解析是 lazy 的，缺 `PSC_REPO_ROOT`
> 只讓「派不了工」而不是「Manager 起不來」——因此 `systemctl is-active` 全綠、自檢
> `ok=true`，**看起來完全正常**。判準是第 7b 的 **F2b**（`paths.repo_root()` 印得出
> 來源樹），不是服務狀態。
>
> **本步的正確終態**：unit 已落檔、與產生器逐位元相同、`systemctl show` 的身分與
> 加固段全部驗過，而服務保持 **`disabled` ／ `inactive`**。
> #623 關閉後再回來執行「啟動」那一小段，並補跑第 7 步的功能面檢查。

monitor 與 Manager **同帳號**（UID 方案表：`cortex-manager`＝Manager ＋ monitor——
唯有同帳號才寫得進自己的 `0700` state 樹），加固段與 EnvironmentFile 也是同一份；
但 `ReadWritePaths` **嚴格更窄**：產生器只從 monitor persona 在 R1 登記表上的
writer／spool-consumer 面導出。

| `ReadWritePaths` | 涵蓋的登記表資產 |
|---|---|
| `/var/lib/cortex/monitor` | `monitor-state-tree`、`monitor-work-items-snapshot`、`monitor-github-sync-cursor`、`monitor-event-spool`（消費＝讀完 unlink，需要容器目錄的寫入權） |
| `/var/lib/cortex/run/cortex` | `runtime-run-tree`（monitor 的 unix socket） |
| `/var/lib/cortex-manager/cache` | 明示 extra：服務帳號 HOME 快取（git／gh／uv），與 Manager 是同一條、不是第二條 |

`coordinator/`、`specs/`、`control/`、`worktree/`、`registry/`、`config/` 一律**不在**
其中：monitor 在登記表上既不是它們的 writer、也不是它們的 spool consumer。要讓某條
回來，唯一的辦法是改登記表再重跑產生器——unit 沒有手擴的入口。

```bash
# ✅ 先看內容（ReadWritePaths 逐條附「涵蓋哪些登記表資產」註解）
python3 -m paulsha_cortex.trust_root unit four-way --monitor | less

# ✅ 先驗「窄」這件事本身：monitor 的 RWP 必須是 Manager 的真子集
diff <(python3 -m paulsha_cortex.trust_root unit four-way --monitor | grep ^ReadWritePaths=) \
     <(python3 -m paulsha_cortex.trust_root unit four-way --manager | grep ^ReadWritePaths=)
#   期望：只有 `<` 那側缺行（monitor 少），**不得**出現 `>` 獨有的 monitor 條目

# 🔧 sudo：寫入 unit（內容一字不改，直接由產生器落檔）
python3 -m paulsha_cortex.trust_root unit four-way --monitor \
  | sudo tee /etc/systemd/system/cortex-monitor.service >/dev/null
sudo chown root:root /etc/systemd/system/cortex-monitor.service
sudo chmod 0644 /etc/systemd/system/cortex-monitor.service

# 🔧 sudo：確認舊的 --user monitor 已停用且不會被 lingering 拉回來
systemctl --user disable --now cortex-monitor.service 2>/dev/null || true

# 🔧 sudo：只 daemon-reload，**先不要 enable／start**（見上方 #623 的 ⛔）
sudo systemctl daemon-reload

# ✅ 驗證：本步的正確終態就是「裝好但沒跑」
systemctl is-enabled cortex-monitor.service; systemctl is-active cortex-monitor.service
#   期望：`disabled` ／ `inactive`（`is-enabled` 對未 enable 的 unit 回非 0，屬正常）
```

**#623 關閉後**才執行下面這一小段，並在 #584 記錄啟動時間：

```bash
# 🔧 sudo：啟用（system-level，非 --user）—— **#623 三個缺口全解之後才做**
sudo systemctl enable cortex-monitor.service
sudo systemctl start cortex-monitor.service
```

**安裝面驗證（不需要服務在跑，#623 未關也照做）**：

```bash
# ✅ 驗證：unit 檔內容與產生器輸出逐位元相同（防手改漂移）
diff <(python3 -m paulsha_cortex.trust_root unit four-way --monitor) \
     /etc/systemd/system/cortex-monitor.service && echo "monitor unit in sync: OK"

# ✅ 驗證：身分與加固段（應與 cortex-manager.service 逐項相同）
#   `systemctl show` 讀的是 unit 定義，服務沒在跑也答得出來。
systemctl show cortex-monitor.service \
  -p User -p NoNewPrivileges -p ProtectSystem -p ProtectHome -p ProtectProc \
  -p CapabilityBoundingSet -p MemoryDenyWriteExecute -p ReadWritePaths
#   期望：**User=cortex-manager**、NoNewPrivileges=yes、ProtectSystem=strict、
#         ProtectHome=yes、ProtectProc=invisible、CapabilityBoundingSet=（空）、
#         MemoryDenyWriteExecute=yes；ReadWritePaths 僅上表三條
```

**執行面驗證（⛔ 只在 #623 關閉、服務已啟動後才適用）**：

```bash
# ✅ 驗證：monitor 行程確實以 cortex-manager 身分跑
ps -o user=,pid=,cmd= -p "$(systemctl show cortex-monitor.service -p MainPID --value)"
#   期望：user 欄為 cortex-manager，cmd 為 /opt/cortex/venv/bin/cortex monitor

# ✅ 驗證：起得來、無 EPERM/EROFS（加固誤擋的第一現場）
systemctl status cortex-monitor.service --no-pager
sudo journalctl -u cortex-monitor.service -n 100 --no-pager \
  | grep -Ei "eperm|erofs|eacces|read-only" || echo "no denial in log: OK"

# ✅ 驗證：新樹確實有寫入（不是還在寫舊的 ~/.agents/monitor）
sudo ls -la /var/lib/cortex/monitor
sudo find /var/lib/cortex/monitor -maxdepth 1 -newermt '-5 minutes' -print
#   期望：work-items.snapshot.json／github-issue-sync.json 於近幾分鐘內被更新
ls -la "$HOME/.agents/monitor" 2>/dev/null && echo "⚠️ 舊樹仍在——確認其 mtime 未再前進"

# ✅ 驗證：event-spool 真的被消費（#622 的契約面：有生產端也要有消費端）
sudo ls /var/lib/cortex/monitor/event-spool | head
#   期望：空的、或項目數會隨時間下降；持續單調增加代表消費端沒在跑

# ✅ 驗證：monitor socket 落在 run root、由 cortex-manager 擁有
sudo ls -la /var/lib/cortex/run/cortex/project-monitor.sock
#   期望：owner cortex-manager、mode 0600

# ✅ 驗證：fail-closed——刪掉 env 檔必須拒絕啟動（測完立刻還原）
sudo mv /opt/cortex/etc/cortex-manager.env /opt/cortex/etc/cortex-manager.env.bak
sudo systemctl restart cortex-monitor.service; echo "exit=$?"
#   期望：restart 失敗（非 0），status 顯示 EnvironmentFile 缺檔
sudo mv /opt/cortex/etc/cortex-manager.env.bak /opt/cortex/etc/cortex-manager.env
sudo systemctl restart cortex-monitor.service && echo "restored: OK"
```

**回滾**：`sudo systemctl disable --now cortex-monitor.service;
sudo rm /etc/systemd/system/cortex-monitor.service; sudo systemctl daemon-reload`，
再以 `systemctl --user start cortex-monitor.service` 回到舊部署（見第 9 步）。
**注意**：回滾後 monitor 會重新寫舊的 `~/.agents/monitor` 樹，與 system-level Manager
的 `/var/lib/cortex/monitor` 形成雙寫——僅可作為短時間的救急手段。

### 4e. toolchain 落位 ＋ per-account 憑證（#640；#661 擴到非 executor）

**沒有這一步，前面全部做完 dispatch 仍會在「呼叫模型」那一步失敗。** job unit 帶
`ProtectHome=yes`，而四個 executor 原本全在 operator 的 HOME 底下：

```
$ sudo -u cortex-builder env HOME=/var/lib/cortex-builder codex exec --help
/usr/bin/env: ‘node’: No such file or directory        rc=127
```

**0817 裁決 (a)**：`node` 走**系統層**（通用 runtime，換版本幾乎不影響產出）；四個
模型 CLI 落進 `/opt/cortex/toolchain`（登記表資產 `executor-toolchain`，root-owned、
job／服務帳號唯讀＋可執行）。理由是「job 跑的是哪個版本的模型 CLI」**會**影響產出，
那必須是一個可稽核的部署決定。

> **為什麼不是「系統層有什麼就用什麼」**：這台機器上實測有**兩份** `codex`——系統層
> `/usr/lib/node_modules/@openai/codex` 是 **0.42.0**，operator 實際在用的 nvm 那份是
> **0.147.0**，差 100 個以上小版本。裁決要防的漂移在這裡**已經是現況**，不是假設。
> 所以來源一律取 operator 實際在用的那一份（`command -v` 解出來的），**不要**另外
> `npm install -g` 裝一份。

> **⚠️ #661：這一步要搬的**不只**四個 executor。** #640 的盤點只涵蓋 dispatch 直接
> 執行的四支模型 CLI，而實機 doctor 在四分部署完成後仍紅在
> `review-sandbox`——原因是 `srt`（Claude sandbox runtime）同樣住在 operator 的 nvm 樹
> 底下、同樣被 `ProtectHome=yes` 擋掉。`openspec`（ship 段的 archive／validate）是同一族
> 的第三個。判準不是「是不是 executor」，而是「**這支程式的版本會不會影響治理產出**」。
> 完整名冊在 `permgen.EXECUTOR_TOOLS` ＋ `permgen.SERVICE_TOOLS`，系統層那一半在
> `permgen.SYSTEM_PROGRAMS`；三張表都由 `trust_root toolchain` 印出來。

**各支的實體形態不同，搬移方式不能一概而論**（表在 `permgen.EXECUTOR_TOOLS` ／
`permgen.SERVICE_TOOLS`）：

| 程式 | 名冊 | 形態 | 需要 node | 執行面／加固剖面 | 搬移方式 |
|---|---|---|:--:|---|---|
| `codex` | executor | Node.js script（`.js` ＋ `#!/usr/bin/env node`） | ✅ | `jit` | **整包** npm 套件樹，`bin/` 放進入點 symlink |
| `claude` | executor | 原生 ELF 執行檔 | — | `strict` | 單檔複製 |
| `copilot` | executor | bash script → **內部 exec node** | ✅ | `jit` | 單檔複製；**先 `head -n 20` 查它內部再叫什麼** |
| `agy` | executor | 原生 ELF 執行檔 | — | `strict` | 單檔複製 |
| `srt` | 非 executor | Node.js script（ESM） | ✅ | 由 `claude` exec ⇒ `strict` ⚠️ | **整包** npm 套件樹，`bin/srt` 必須是 symlink |
| `openspec` | 非 executor | Node.js script | ✅ | 由 Manager 的 system unit exec ⚠️ | **整包** npm 套件樹，`bin/openspec` 必須是 symlink |

> **`srt` 為什麼一定要整包搬、而且 `bin/` 那一支必須是 symlink**（#661 實機量到的
> 兩個後果，第二個是**無聲**的）：
>
> 1. `dist/cli.js` 是 ESM，第一行就 `import { quote } from './utils/shell-quote.js'`。
>    單檔複製到 `<toolchain>/bin/srt` 之後相對 import 解到 `<toolchain>/bin/utils/…`：
>
>    ```
>    Error [ERR_MODULE_NOT_FOUND]: Cannot find module
>      '/opt/cortex/toolchain/bin/utils/shell-quote.js'     rc=1
>    ```
>
>    而 doctor 的 `review-sandbox` probe 會實跑 `srt --version`，rc≠0 就報
>    `Claude sandbox dependency execution failed`——**症狀離原因很遠**：兩支相依都
>    `command -v` 得到，看起來「都找得到了」。
> 2. `coordinator/launcher.py` 的 `_srt_runtime_root()` 是從 `which("srt")` 往上找
>    `package.json` 且 `name == "@anthropic-ai/sandbox-runtime"` 來解出套件根，再把它
>    加進 reviewer sandbox 政策的 `allowRead`。單檔形態下它解出 `None`——沙箱政策少一
>    條放行，**而且不報錯**。symlink 形態下 `readlink -f` 落在套件樹內，才解得到。

> ⚠️ **`srt` 與 `openspec` 的加固面是一個已知的未決點（#661 盤點結果）**：兩者都是
> node（V8 的 JIT 需要 W→X），但執行它們的 unit 目前都是 `MemoryDenyWriteExecute=yes`
> ——`srt` 由 `claude`（`strict` 剖面）exec、`openspec` 由 Manager 的 system unit exec。
> #643 的剖面推導看不到這一格：它的唯一輸入是 **executor 名**，涵蓋不了「executor
> 在執行途中再 exec 出來的 node 程式」。下方有專門的量測步驟；**量到才改，不得就地
> 放寬**——回報 issue 由 operator 裁決（#643 的先例）。可列舉的形式：
> `python3 -c "from paulsha_cortex.trust_root import permgen; print(permgen.unresolved_node_execution_surfaces())"`

> `claude`／`agy` 自帶原生執行檔，**不會因為 node 版本而行為改變**——因此系統層 node
> 的版本風險只涵蓋 `codex`／`copilot` 兩個。
>
> **`copilot` 的「需要 node」是 #643 回填的**：#640 落表時只知道它是 shell script、
> 還沒查它內部 exec 什麼（上表當時就寫著「先 `head -n 20` 查」）。#643 在真實加固面
> 下量到它與 `codex` 的症狀逐字相同（`MemoryDenyWriteExecute=yes` 下空輸出、拿掉即
> 正常）——那就是 node。**這一欄同時決定加固剖面**（`permgen.EXECUTOR_TOOLS` 的
> `needs_node` 是唯一真相來源，剖面由它機械導出，見第 5-2 步）。

```bash
# ✅ 先讀落位步驟（含每支 CLI 的形態與搬移方式；產生器＝單一真相）
python3 -m paulsha_cortex.trust_root toolchain four-way | less

# 🔧 sudo：系統層 node（apt；nodesource repo 已設定時候選為 20.x）
sudo apt-get install -y nodejs
node --version
#   期望：v20 以上。codex 0.147.0 宣告 `node >=16`，故 20 可用。
#   ⚠️ node 版本是**部署決定**：某個 CLI 哪天提高下限時要一併升，
#      否則它會變成下一個無聲漂移點。

# 🔧 sudo：系統層那一半（#661 補完盤點：不只 node）
sudo apt-get install -y bubblewrap socat git
#   bwrap／socat 是 `srt` 在 Linux 上的相依（doctor 的 review-sandbox probe 會逐一
#   實跑 `bwrap --version`／`socat -V`）；gh 若尚未安裝，依 GitHub CLI 的 apt repo 裝。
for p in node git gh bwrap socat; do printf '%-8s %s\n' "$p" "$(command -v "$p" || echo MISSING)"; done
#   期望：五支都解得到，且**都不在 /home 底下**（在 /home ⇒ ProtectHome 之後不可達）。

# 🔧 sudo：建骨架並把整包搬的那幾支從 operator 實際在用的那一份複製進來
#   （逐支的來源與方式照 `trust_root toolchain` 的輸出）
sudo install -d -o root -g root -m 0755 /opt/cortex/toolchain{,/bin,/lib}
for pkgcli in codex srt openspec; do
  SRC="$(readlink -f "$(command -v "$pkgcli")")"
  PKG="$(cd "$(dirname "$SRC")/.." && pwd)"   # npm 套件根（單搬進入點會缺 node_modules）
  # ⚠️ 進入點若在 `dist/` 或 `bin/` 底下，套件根是**再上一層**：以「哪一層有
  #    package.json」為準，不要照抄 `..`。
  while [ ! -f "$PKG/package.json" ] && [ "$PKG" != / ]; do PKG="$(dirname "$PKG")"; done
  sudo cp -a "$PKG" "/opt/cortex/toolchain/lib/$pkgcli"
  REL="${SRC#"$PKG"/}"                        # 進入點在套件內的相對路徑
  sudo ln -sfn "/opt/cortex/toolchain/lib/$pkgcli/$REL" "/opt/cortex/toolchain/bin/$pkgcli"
done
for cli in claude copilot agy; do
  sudo cp -a "$(readlink -f "$(command -v "$cli")")" "/opt/cortex/toolchain/bin/$cli"
done

# 🔧 sudo：統一收權（root 擁有、全部 job／服務帳號唯讀＋可執行）
sudo chown -R root:root /opt/cortex/toolchain
sudo chmod -R u=rwX,go=rX /opt/cortex/toolchain
```

```bash
# ✅ 驗證（#661 的核心驗收）：整包搬的三支，`bin/` 那一支必須是 symlink 且指進 lib/
ls -l /opt/cortex/toolchain/bin/{codex,srt,openspec}
#   期望：三行都是 `->` 指向 /opt/cortex/toolchain/lib/<name>/… 的 symlink。
#   ⛔ 若是 regular file（-rwxr-xr-x），就是單檔複製——**回上面重做**。
#      單檔形態下 `srt --version` 會 ERR_MODULE_NOT_FOUND，而 `_srt_runtime_root()`
#      會安靜地解出 None（沙箱政策少一條 allowRead 且不報錯）。

# ✅ 驗證：srt 的套件根解得到（這條就是 `_srt_runtime_root()` 走的那條路）
PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin \
  /opt/cortex/venv/bin/python3 -c \
  "from paulsha_cortex.coordinator.launcher import _srt_runtime_root; print(_srt_runtime_root())"
#   期望：/opt/cortex/toolchain/lib/srt（**不得是 None**）

# ✅ 驗證：doctor 的 review-sandbox 相依逐條實跑（與 probe 同一組命令）
for c in "bwrap --version" "socat -V" "srt --version" "claude --version"; do
  PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin sh -c "$c" >/dev/null 2>&1
  echo "$c rc=$?"
done
#   期望：四行皆 rc=0。`srt --version` rc≠0 ⇒ doctor 會報
#   `Claude sandbox dependency execution failed`（#661 的原症狀）。
```

```bash
# ✅ 驗證：權限與登記表產生的計畫逐位元一致
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  --operator-account "$USER" --external-reader-account none \
  | grep -A3 "executor-toolchain"
ls -ld /opt/cortex/toolchain
#   期望：drwxr-xr-x root root（**一個 group／other 的 w 都不得有**）

# ✅ 驗證：job 帳號寫不進去（這條才是邊界）
sudo -u cortex-builder sh -c "touch /opt/cortex/toolchain/bin/evil" 2>&1 | tail -1
#   期望：Permission denied

# ✅ 功能驗證（本票的核心驗收）：以 job 帳號實跑一次 `--help`，期望 rc=0
for cli in codex claude copilot agy; do
  sudo -u cortex-builder env HOME=/var/lib/cortex-builder \
    PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin \
    "$cli" --help >/dev/null 2>&1; echo "$cli rc=$?"
done
#   期望：四行皆 rc=0。**rc=127 ＝ #640 的原症狀**（CLI 或它的 runtime 不可達）：
#     先 `sudo -u cortex-builder ... command -v <cli>` 看解到哪裡，
#     再 `head -n 1 /opt/cortex/toolchain/bin/codex` 確認 shebang 解得開（node 在系統層）。

# ✅ 驗證（**裁決 (a) 真正要守的那條**）：job 帳號跑出來的版本 == operator 側的版本
sudo -u cortex-builder env HOME=/var/lib/cortex-builder \
  PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin codex --version
codex --version
#   期望：**兩行逐字相同**。只驗 rc=0 是不夠的——系統層那份 0.42.0 一樣會 rc=0，
#   而 job 跑的就變成 operator 從未判讀過的版本（症狀是「結果對不上」，不是報錯）。
#   不同 ⇒ PATH 順序錯（toolchain 沒排最前面），或複製到的是系統那份。

```

#### 共用探針：`psc_run_under`（在**真實加固面**下跑一條命令）

> **本 runbook 之後每一條「在真實加固面下」的驗證都用它，不再手打 `--property=`。**
> 這是 #638（單 UID 讓 ACL 斷言真空）／#657（同型）／#673（同型）三次同一族事故
> 之後定下的規矩：**加固面的定義只有一份，在 `permgen.unit_replica_properties()`**。
> runbook 與 `tests/test_trust_root_syscall_profile_673.py` 共用它。
>
> #673 值得逐字記住：那份手抄複本抄了 `SystemCallFilter=@system-service`、**漏抄**
> 同一份 unit 上的 `SystemCallErrorNumber=EPERM`，於是複本落回 systemd 預設的
> `SECCOMP_RET_KILL_PROCESS`——**比 production 更嚴格**，量出一個 production 沒有的
> `rc=1`。**手抄子集的假紅與假綠一樣會發生，方向不由人選。**

```bash
# 一次貼進 shell，之後各步驟直接呼叫。
psc_probe_path() {
  # PATH 也不手打：job 的 PATH 來自 Manager 端 root-owned 的 EnvironmentFile
  # （模板 unit 刻意不寫 Environment=PATH=，因為 shim 會整份換掉環境）。
  sudo grep -hoE '^(PSC_[A-Z]+_PATH|PATH)=.*' /opt/cortex/etc/cortex-manager.env \
    | head -1 | cut -d= -f2-
}

psc_run_under() {   # psc_run_under <unit 字幹> <命令> [參數…]
  local stem="$1"; shift
  local -a props
  # `systemctl cat` 帶上 drop-in，因此驗到的是**實際生效**的那一份，不是主檔而已。
  mapfile -t props < <(
    sudo systemctl cat "${stem}@.service" \
      | python3 -m paulsha_cortex.trust_root unit-replica - --instance probe
  )
  # unit-replica 在落檔的 unit 少任一加固鍵時 fail-closed（stdout 空、rc=2）——
  # 這一關不能省：空清單會讓下面那條變成「完全沒有加固的探針」，然後全綠。
  if [ "${#props[@]}" -lt 30 ]; then
    echo "⛔ 加固面複本只有 ${#props[@]} 條——unit 落檔不完整或產生器已漂移，停下來" >&2
    return 90
  fi
  sudo systemd-run --pipe --wait --collect --quiet --service-type=exec \
    "${props[@]}" --setenv=PATH="$(psc_probe_path)" "$@"
}
```

**兩個必須先做的前置**（做不到就是探針本身壞了，不是被驗的東西壞了）：

```bash
# 1) 模板 unit 的 ReadWritePaths 含 %i（builder 的 /var/lib/cortex/worktree/%i）。
#    systemd 對不存在的 ReadWritePaths 目標**在 exec 前就失敗**（rc=226 EXIT_NAMESPACE，
#    輸出全空——症狀與「CLI 起不來」一模一樣）。--instance probe 因此需要一格真目錄。
sudo install -d -o cortex-builder -g cortex-builder -m 0700 /var/lib/cortex/worktree/probe
#    收尾：sudo rm -rf /var/lib/cortex/worktree/probe
#    ⚠️ **這一格正是手抄四 property 複本永遠看不到的東西**——那份複本連
#       ReadWritePaths 都沒有，所以它從來不會因為這個原因失敗，也就從來沒驗到它。

# 2) 確認複本條數與 unit 相符（漂移偵測；數字會隨加固表成長，不要寫死在別處）
sudo systemctl cat cortex-reviewer-job@.service \
  | python3 -m paulsha_cortex.trust_root unit-replica - | wc -l
#    期望：與 `grep -cE '^[A-Za-z]+=' <unit 的 [Service] 段>` 扣掉執行面指令後相同，
#    且**至少**含 27 條加固鍵。少於 30 ⇒ 停下來，不要繼續往下驗。
```

回到 4e 那條「在真實加固面下驗 toolchain 可達性」的驗證——現在它走共用探針：

```bash
# ✅ 驗證（**在真實加固面下**跑一次）：`sudo -u` 沒有 unit 的加固，兩者可能不同結果
#    這一步**不再手打 --property=**（#673：手抄的子集抄了 SystemCallFilter 卻漏抄
#    SystemCallErrorNumber，量出一個 production 根本沒有的失敗）。
psc_run_under cortex-job /opt/cortex/toolchain/bin/codex --version
#   期望（`codex` 在 **strict** 剖面下）：**stdout 空**、rc≠0。
#   ✅ 這不是失敗，是 #643 已經定案的事實：`MemoryDenyWriteExecute=yes` 與 V8 的 JIT
#      天生互斥。stderr 會出現 V8 的 `Check failed: 12 == (*__errno_location ())`
#      ——那就是它（V8 期待 mmap 回 ENOMEM(12)，被 MDWE 擋成 EPERM(1)）。
#   ✅ 改跑 `psc_run_under cortex-job-jit /opt/cortex/toolchain/bin/codex --version`，
#      應印出與 operator 側逐字相同的版本——那正是 jit 剖面的加固面。
#   ⚠️ 若**兩種**都失敗，那就不是 MDWE，回到上一條查 PATH／toolchain 可達性。
#   ⚠️ `claude`／`agy` 在**兩種**下都應該 rc=0；若它們在完整加固面下也失敗，代表
#      這台機器上還有第三個阻斷點——**停下來查清楚**，不要順手再放寬一項。
#   完整的雙剖面驗證（含負向對照與反向矩陣）在第 5-2b 步；這裡只是提早看見那條分岔。
```

```bash
# ✅ 量測（#661 的未決點，**只量不改**）：非 executor 的 node 程式跑在誰的加固面上
#    先列出待量的組合（由登記表機械導出，不要手抄）
python3 -c "from paulsha_cortex.trust_root import permgen
for s in permgen.unresolved_node_execution_surfaces(): print(s.program, '←', s.detail)"
#   目前預期兩列：srt ← executor claude ⇒ strict；openspec ← manager-unit。

# (1) srt 在 reviewer job 的 strict 剖面下（＝ claude 實際 exec 它的那個加固面）
psc_run_under cortex-reviewer-job /opt/cortex/toolchain/bin/srt --version

# (2) openspec 在 Manager system unit 的加固面下
#     Manager 不是模板 unit，字幹沒有 `@`——直接餵檔案（--instance 用不到）。
mapfile -t mprops < <(sudo systemctl cat cortex-manager.service \
  | python3 -m paulsha_cortex.trust_root unit-replica -)
sudo systemd-run --pipe --wait --collect --quiet --service-type=exec \
  "${mprops[@]}" --setenv=PATH="$(psc_probe_path)" \
  /opt/cortex/toolchain/bin/openspec --version

#   ⚠️ 兩條**預期都失敗（stdout 空、rc=1）**，症狀與 #643 的 codex／copilot 逐字
#      相同——#673 已在完整加固面下複驗，兩條的 stderr 都是同一段 V8 stack trace：
#          # Check failed: 12 == (*__errno_location ()).
#          … v8::internal::Runtime_CompileLazy …
#      `MemoryDenyWriteExecute=yes` 與 V8 的 JIT 天生互斥（V8 期待 mmap 回
#      ENOMEM(12)，被 MDWE 擋成 EPERM(1)，於是那個 CHECK 炸掉）。
#   ✅ 確認阻斷點是 MDWE 而非別的：改跑 `psc_run_under cortex-reviewer-job-jit
#      /opt/cortex/toolchain/bin/srt --version`（jit 剖面，與 strict 只差 MDWE 一項）
#      ——#673 實機為 `1.0.0`、rc=0。openspec 沒有對應的 jit Manager unit，
#      因此 (2) 目前**無解**，維持 #661 的未決狀態。
#   ⚠️ **注意 stderr 不是空的**。#673 的原始回報寫「stdout 與 stderr 皆空」，那是
#      因為它的手抄複本漏掉 `SystemCallErrorNumber=EPERM`，行程被 SIGSYS 當場殺掉、
#      連 V8 的錯誤都來不及印。在**真實**加固面下這一族失敗是有 stack trace 的
#      ——看到空 stderr 就代表你的複本不等於 production，回去查複本。
#   ⛔ **不要就地把 unit 的 MDWE 改掉**。#643 的先例是「量到才改，而且改的是一份具名
#      剖面，不是全域放寬」——reviewer 要不要走 jit 剖面、Manager 這一面要不要動，
#      是 operator 的裁決。把兩條的實際輸出貼回 #661 的 follow-up issue。
#   （這也是本 repo 測試裡**明確 skip** 的那一族：單 UID／無 systemd 加固面的環境
#     重現不了這個語意，不得靜默宣稱已驗證——見 #638／#657 的教訓。）
```

**per-account 憑證（0817 裁決 (b)）**：憑證**檔**由 job 帳號擁有（才 refresh 得了
過期 token），**放它的目錄維持 root-owned**。

> **這個組合的安全性質**：job **能**就地改寫自己那份憑證的內容；但**建不了新檔、
> 刪不掉、也換不掉**同目錄下的其他 root-owned 檔（例如 `codex-hooks` 的
> `hooks.json`）——建立／unlink／rename 需要的是**目錄**的寫入權，而目錄對 job 只有
> `r-x`。
>
> **已知限制**：因此「暫存檔 ＋ rename 原子替換」形式的 refresh 會失敗（它要在同目錄
> 建檔），只有就地覆寫的 refresh 走得通。這是裁決刻意接受的代價。
>
> **這買到的是什麼**：把 operator 的憑證複製給 job 帳號，代表 job 用的是**同一個
> provider 帳號**。三分買到的是**檔案系統層**的隔離（job 偷不到 Manager 的 token、
> 改不了 Manager 的 state、也讀不到另一個 job 帳號的憑證），**不是** provider 層的
> 獨立——與 `independence_domain` 不是同一件事（見 spec §R1）。

```bash
# 🔧 sudo：把 operator 那份憑證複製給兩個 job 帳號，owner 給該帳號、目錄維持 root 的
for who in builder reviewer-planner; do
  sudo install -o "cortex-$who" -g "cortex-$who" -m 0600 \
    "$HOME/.codex/auth.json" "/var/lib/cortex-$who/.codex/auth.json"
done
#   ↑ `.codex/` 目錄本身由第 2b 步的骨架建成 root:root 0755，這裡**不要**動它。
#   其他 executor 的憑證檔位置不同（`claude` 等各有自己的落點，本票未實測）：
#   落位規則完全一樣——先確認該 CLI 實際寫哪個檔，再以同一條 install 命令落位；
#   若該檔的父目錄還不存在，用 `sudo install -d -o root -g root -m 0755 <dir>` 補。
```

```bash
# ✅ 驗證：檔案 owner 是 job 帳號、**目錄** owner 是 root
ls -ld /var/lib/cortex-builder/.codex
ls -l  /var/lib/cortex-builder/.codex/auth.json
#   期望：目錄 drwxr-xr-x root root；檔案 -rw------- cortex-builder cortex-builder

# ✅ 驗證：與登記表產生的計畫一致
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  --operator-account "$USER" --external-reader-account none \
  | grep "executor-credential"
#   期望：chown cortex-builder:cortex-builder … ＋ chmod 0600 …（帶 `[ ! -e ] ||` 守衛）

# ✅ 驗證（**不變式：能改內容、不能增刪換**）
sudo -u cortex-builder sh -c \
  "printf '{}' > /var/lib/cortex-builder/.codex/auth.json" && echo "改內容: OK"
#   期望：OK（refresh 走得通）——測完記得把真的憑證放回去
sudo -u cortex-builder sh -c \
  "touch /var/lib/cortex-builder/.codex/newfile" 2>&1 | tail -1
#   期望：Permission denied ← **建不了新檔**
sudo -u cortex-builder sh -c \
  "rm -f /var/lib/cortex-builder/.codex/auth.json" 2>&1 | tail -1
#   期望：Permission denied ← **刪不掉**
sudo -u cortex-builder sh -c \
  "mv /var/lib/cortex-builder/.codex/auth.json /var/lib/cortex-builder/.codex/hooks.json" \
  2>&1 | tail -1
#   期望：Permission denied ← **換不掉同目錄下的 root-owned 檔**
```

> **job unit 會因為憑證缺席而起不來**：模板 unit 的 `ReadWritePaths` 直接掛在憑證
> **檔**上（不是它的父目錄），而 systemd 對不存在的 `ReadWritePaths` 目標會讓 unit
> 起不來。那是刻意的 fail-closed——沒有登入態的 job 本來就做不了事，在 exec 前失敗
> 比走到呼叫模型那一步才 rc=127 好查得多。journal 若出現
> `Failed to set up mount namespacing … No such file or directory`，先回頭看這一步。

**回滾**：`sudo rm -rf /opt/cortex/toolchain
/var/lib/cortex-{builder,reviewer-planner}/.codex/auth.json`。

---

### 4f. 系統層 python 套件（#666 漂移項 1：`pytest`）

**沒有這一步，前面全部做完 dispatch 也走得完——但每張 build 卡都會在採信階段被拒。**
症狀離原因最遠的一步就是這一個，因此它獨立成一節。

`PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 是**相對名**，由 gate 的 `PSC_GATE_PATH`
（`/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin`）解析 ⇒ `/usr/bin/python3`，
**系統層那一支**。gate unit 自己的 `ExecStart` 用的是 `/opt/cortex/venv/bin/python3`，
但那只涵蓋 ledger writer 本身——**operator 宣告的命令另外解析一次**。兩者是不同的
interpreter，這是本節存在的唯一理由。

```
$ sudo -u cortex-gate env HOME=/var/lib/cortex-gate python3 -m pytest --version
/usr/bin/python3: No module named pytest
```

> **後果不是「gate 失敗」**：wrapper 仍會寫出 ledger，只是每一項都記 `failed`／或
> 整份為空 ⇒ 每張帶 `test_policy` 的 build 卡在 harvest 撞
> `gate-ledger-missing-expected-gate`（#540 的 acceptance chain）⇒ **交付全部卡住**，
> 而唯一的痕跡是 `manager.log` 裡的一行。`cortex doctor` 的 `gate-declarations` probe
> 只驗宣告的 argv 形狀，**驗不到「那個模組 import 得到嗎」**——這一節補的正是那個缺口。

**要裝哪些、為什麼是兩個**（清單由產生器出，不要手抄）：

```bash
# ✅ 清單與版本約束（含每一項的理由）
python3 -m paulsha_cortex.trust_root toolchain four-way | sed -n '/系統層 python 發行版/,/完整加固面下/p'
#   期望兩項：
#     pytest —— gate 實際跑的 test runner；約束宣告在 pyproject.toml 的 test extra
#     PyYAML —— **被測樹**的 runtime 相依（不是 pytest 的）：gate 的 cwd 是被驗那棵樹
#                的副本，pytest 會把 rootdir 插進 sys.path ⇒ `import paulsha_cortex`
#                解到被驗的樹 ⇒ 它 import yaml。缺它的症狀是 pytest exit code 2
#                （collection error），不是「測試失敗」。
```

```bash
# 🔧 sudo：裝到系統層（**不是** `pip install --user`——ProtectHome 之後讀不到）
sudo pip install --break-system-packages 'pytest>=7' 'PyYAML>=6'
```

```bash
# ✅ 驗證（1）：**以 gate 身分實測**，不是只驗檔案存在
sudo -u cortex-gate env HOME=/var/lib/cortex-gate \
  PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin \
  python3 -m pytest --version
#   期望：印出版本，rc=0（不是 `No module named pytest`）
sudo -u cortex-gate env HOME=/var/lib/cortex-gate python3 -c 'import yaml; print(yaml.__version__)'
#   期望：印出版本

# ✅ 驗證（2）：**在完整加固面下**複跑（`sudo -u` 沒有 unit 的加固面）
#    探測目錄放在 gate **自己登記表上的可寫面**底下（`/var/lib/cortex/gate-worktree`），
#    不放 /tmp——理由見下方 ⚠️。並給 gate 帳號擁有：pytest 會在 rootdir 建
#    `.pytest_cache`，root-owned 的目錄會讓它印一段與待驗命題無關的警告。
PROBE=/var/lib/cortex/gate-worktree/psc-probe
sudo install -d -o cortex-gate -g cortex-gate -m 0700 "$PROBE"
printf 'def test_ok():\n    assert True\n' | sudo tee "$PROBE/test_probe.py" >/dev/null
sudo chown cortex-gate:cortex-gate "$PROBE/test_probe.py"
#    加固面從已落檔的 gate 模板 unit 機械導出（見上方「共用探針」），只覆寫
#    WorkingDirectory 指向探測目錄——**其餘一條都不減**。
mapfile -t gprops < <(sudo systemctl cat cortex-gate-job@.service \
  | python3 -m paulsha_cortex.trust_root unit-replica - --instance probe)
sudo systemd-run --pipe --wait --collect --quiet --service-type=exec \
  "${gprops[@]}" \
  --property=WorkingDirectory="$PROBE" \
  --setenv=PATH="$(psc_probe_path)" \
  /usr/bin/python3 -m pytest -q
#   期望：`1 passed`，rc=0。
#   ⚠️ `--property=WorkingDirectory=` 必須排在複本**之後**才覆寫得掉（systemd 取
#      最後一次指定的值）。
#   ⚠️ **不要把探測目錄放在 /tmp**：完整加固面含 `PrivateTmp=yes`，host 上的
#      `/tmp/psc-gate-probe` 在 unit 的私有 /tmp 裡根本不存在，`WorkingDirectory=`
#      指過去會在 exec **之前**就失敗——rc=226（EXIT_NAMESPACE）、輸出全空，
#      症狀與「pytest 起不來」一模一樣。#673 實機踩到並複驗過。
#      **這正是舊版四 property 複本看不到的東西**：那份複本沒有 PrivateTmp，
#      所以 /tmp 一直能用，於是這條驗證多年來驗的都不是 production 的條件。
#   ✅ **這一條在完整加固面下就應該過，不需要放寬任何一項**：CPython 不是 V8，
#      `MemoryDenyWriteExecute=yes` 對它沒有影響（與 #643 的 node 型 executor 相反）。
#      #673 實機複驗：完整 38 條複本下 `1 passed`、rc=0。
#   ⚠️ 若這一條失敗而拿掉 MDWE 才過，那是新發現——**停下來記回 issue**，不要就地放寬。
sudo rm -rf "$PROBE"

# ✅ 驗證（3）：**版本是明示的部署決定** —— 記下來並與 operator 側比對
python3 -m pytest --version                                  # operator 側
sudo -u cortex-gate env HOME=/var/lib/cortex-gate python3 -m pytest --version   # gate 側
#   ⚠️ 兩者**可以**不同（兩個 interpreter 各有一份是常態），但**必須被記下來**：
#      版本分岔的症狀是「gate 判定與本機跑不一樣」，不是報錯。把 gate 側那一版寫進
#      本 runbook 的部署紀錄；哪天要升，升的是 gate 側那一份。

# ✅ 驗證（4）：宣告本身
sh -c 'set -a; . /opt/cortex/etc/cortex-manager.env; set +a; echo "[$PSC_GATE_CMD_PYTEST]"'
#   期望：`[python3 -m pytest -q]`（產生器出的建議值；見 4b 步）
python3 -m paulsha_cortex.trust_root toolchain four-way | grep 'PSC_GATE_CMD_'
```

> **為什麼不乾脆把宣告改成 `/opt/cortex/venv/bin/python3 -m pytest`？** 那是一個
> **operator 可以做、但產生器不會替你做**的裁決：改了之後系統層這兩個套件的需求整個
> 消失，但 gate 跑的 pytest 版本就與 cortex 自己的部署版本綁在一起（升 cortex 會連帶
> 換掉 gate 的判準）。兩邊各有取捨。**若你改了宣告，這一節就不再適用**——同時要把
> `permgen.SYSTEM_PYTHON_DISTRIBUTIONS` 一起改掉，測試會提醒你。

**回滾**：`sudo pip uninstall --break-system-packages pytest`（`PyYAML` 通常是系統套件
的相依，**不要**動它）。

---

### 4g. Manager 的 gh 憑證（#666 漂移項 2）

**沒有這一步，monitor 起得來但兩個 github provider 一起 `degraded`**，且 doctor 的
`gh-auth`／`gh-permissions`／`auto-label` 三個 required probe 全紅。

```
$ sudo -u cortex-manager env HOME=/var/lib/cortex-manager gh auth status
You are not logged into any GitHub hosts.
```

operator 側的登入態在 `~/.config/gh/` 底下，`ProtectHome=yes` 之後 `/home` 整個不可見
——與 #640／#661 的成因逐字相同，這是那一族的**第五個**成員。

> **⚠️ 這一份與 4e 的 job 憑證形狀相同、洩漏面不同級，不要混為一談。**
> 4e 那一份是給 **job 帳號**的模型 provider 憑證：被竊只換得到模型呼叫額度，而且 job
> 模板 unit 另有 `Environment=GH_TOKEN=`／`GITHUB_TOKEN=` 把 GitHub token 清空，成果
> 一律走 `commit-spool` 由 Manager 代理推送。
> 本節這一份是給 **Manager** 的，而 Manager 是 durable state owner——這個 token
> **推得動 PR、關得掉 issue、改得了 label、merge 得了分支**，洩漏的是治理平面對上游
> repo 的寫入權。因此它只落在 Manager 的 HOME 下；**job 帳號刻意沒有 `~/.config/gh`
> 這一層目錄**（`trust_root scaffold` 不會為它們建，建了就是多開一條被明確關掉的通道）。

> **⚠️ 兩個檔的 owner 刻意不同，這不是疏漏**（下一個人最可能做錯的就是把兩個設成同一種）：
>
> | 檔 | owner / mode | 為什麼 |
> |---|---|---|
> | `.config/`、`.config/gh/` | `root:root 0755` | 增／刪／換需要**目錄**的寫入權；性質全部落在這一層 |
> | `hosts.yml` | `cortex-manager:cortex-manager 0600` | `gh` **唯一寫回 token 的檔**（`auth login`／`refresh` 就地覆寫），不歸它就 refresh 不回來 |
> | `config.yml` | `root:root 0644` | 非憑證；但 `aliases` 可宣告 `!` 開頭的 **shell alias** ⇒ 讓服務帳號改得了它等於給 Manager 一條「把任意命令掛進每一次 `gh` 呼叫」的執行面。與三份 `.gitconfig` 維持 root-owned 是**同一條理由**（`core.fsmonitor`／`alias.*`） |

> **落點為什麼是 `~/.config/gh` 而不是別條**：`gh` 依序看 `$GH_CONFIG_DIR` →
> `$XDG_CONFIG_HOME/gh` → `$HOME/.config/gh`，而產生出來的 unit 設 `HOME=` 與
> `XDG_CACHE_HOME=`、**刻意不設 `XDG_CONFIG_HOME=`**。哪天有人在 unit 或
> EnvironmentFile 補上 `XDG_CONFIG_HOME`，憑證的實際落點會跟著搬走而登記表不會知道
> ——那是**無聲**的漂移：`gh auth status` 變成「未登入」，而檔案還好端端躺在原處。

```bash
# ✅ 先確認兩層目錄已由骨架建出（第 2b 步；這裡只驗，不建）
python3 -m paulsha_cortex.trust_root scaffold four-way | grep '\.config'
#   期望兩行：
#     install -d -o root -g root -m 0755 /var/lib/cortex-manager/.config
#     install -d -o root -g root -m 0755 /var/lib/cortex-manager/.config/gh
ls -ld /var/lib/cortex-manager/.config /var/lib/cortex-manager/.config/gh
#   期望：兩層皆 drwxr-xr-x root root
```

```bash
# 🔧 sudo：把 operator 那份登入態複製過去（**兩個檔、兩種 owner**）
sudo install -o cortex-manager -g cortex-manager -m 0600 \
  "$HOME/.config/gh/hosts.yml" /var/lib/cortex-manager/.config/gh/hosts.yml
sudo install -o root -g root -m 0644 \
  "$HOME/.config/gh/config.yml" /var/lib/cortex-manager/.config/gh/config.yml
#   ↑ `.config/gh/` 目錄本身**不要**動。
#   落位步驟也由產生器出（含理由與驗證）：
#     python3 -m paulsha_cortex.trust_root toolchain four-way | sed -n '/Manager 的 gh 憑證/,/Permission denied/p'
```

```bash
# ✅ 驗證（1）：與登記表產生的計畫逐字一致
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  --operator-account "$USER" --external-reader-account none | grep 'config/gh'
#   期望四行（皆帶 `[ ! -e ] ||` 守衛）：
#     chown cortex-manager:cortex-manager …/hosts.yml
#     chmod 0600                            …/hosts.yml
#     chown root:root                       …/config.yml
#     chmod 0644                            …/config.yml
ls -l /var/lib/cortex-manager/.config/gh/
#   期望：-rw------- cortex-manager cortex-manager hosts.yml
#         -rw-r--r-- root           root           config.yml

# ✅ 驗證（2）：**以 Manager 身分實測**（不是只驗檔案存在）
sudo -u cortex-manager env HOME=/var/lib/cortex-manager gh auth status
#   期望：以 fleet 正式身分登入成功、`Token scopes` 列得出來。
#   ⚠️ 若仍是 `You are not logged into any GitHub hosts.`：先看有沒有人設了
#      `XDG_CONFIG_HOME`／`GH_CONFIG_DIR`（見上方那條無聲漂移的警告）。

# ✅ 驗證（3）：**在完整加固面下**複跑（`sudo -u` 沒有 unit 的加固面）
#    加固面從已落檔的 Manager unit 機械導出——手打子集抄漏一條就驗不到 production
#    的條件（#673）。Manager 不是模板 unit，因此不需要 --instance。
mapfile -t mprops < <(sudo systemctl cat cortex-manager.service \
  | python3 -m paulsha_cortex.trust_root unit-replica -)
sudo systemd-run --pipe --wait --collect --quiet --service-type=exec \
  "${mprops[@]}" --setenv=PATH="$(psc_probe_path)" \
  /usr/bin/gh auth status
#   期望：同上。`gh` 是原生 ELF，MDWE 不影響它。
#   ⚠️ 這份複本會連 `EnvironmentFile=/opt/cortex/etc/cortex-manager.env` 一起帶上
#      （它是 [Service] 段的一部分）——那是刻意的：Manager 的 PSC_* 設定同屬它的
#      執行條件。檔案缺席時 unit 本來就 fail-closed，探針一起失敗是正確行為。

# ✅ 驗證（4）：**不變式「改得了內容、建不了新檔」**（與 4e 的憑證逐條同構）
sudo -u cortex-manager sh -c \
  "cat /var/lib/cortex-manager/.config/gh/hosts.yml > /tmp/psc-hosts.bak && \
   cat /tmp/psc-hosts.bak > /var/lib/cortex-manager/.config/gh/hosts.yml" && echo "改內容: OK"
#   期望：OK（token refresh 走得通）
sudo -u cortex-manager sh -c \
  "touch /var/lib/cortex-manager/.config/gh/newfile" 2>&1 | tail -1
#   期望：Permission denied ← **建不了新檔**（同時封掉「暫存檔＋rename」那條路）
sudo -u cortex-manager sh -c \
  "rm -f /var/lib/cortex-manager/.config/gh/config.yml" 2>&1 | tail -1
#   期望：Permission denied ← **刪不掉 root-owned 的鄰居**
sudo -u cortex-manager sh -c \
  "printf 'aliases:\n  x: \"!id\"\n' > /var/lib/cortex-manager/.config/gh/config.yml" 2>&1 | tail -1
#   期望：Permission denied ← **改不了 config.yml 的內容**（`!` shell alias 那條執行面關著）
sudo rm -f /tmp/psc-hosts.bak

# ✅ 驗證（5）：unit 的 ReadWritePaths 只掛**那一個檔**，不是父目錄
python3 -m paulsha_cortex.trust_root unit four-way --manager | grep 'config/gh'
python3 -m paulsha_cortex.trust_root unit four-way --monitor | grep 'config/gh'
#   期望兩者都只有 `ReadWritePaths=/var/lib/cortex-manager/.config/gh/hosts.yml`
#   （出現 `…/.config/gh` 這條目錄＝父目錄被開放，`config.yml` 會跟著可寫 ⇒ 錯）

# ✅ 驗證（6）：job 側必須是**反向**的
python3 -m paulsha_cortex.trust_root unit four-way --job | grep -c 'config/gh' || true
#   期望：0
python3 -m paulsha_cortex.trust_root unit four-way --job | grep 'GH_TOKEN'
#   期望：`Environment=GH_TOKEN=`（空值）
```

**回滾**：`sudo rm -f /var/lib/cortex-manager/.config/gh/{hosts.yml,config.yml}`。

---

### 4h. 窮舉盤點的複核（#666）——**每次部署都跑一次**

前面 4e／4f／4g 三節各自處理一族，但真正要防的是「下一個沒被盤到的相依」。把盤點印出來
逐項對一次，比等症狀出現便宜得多。

```bash
# ✅ 完整盤點（判準：降權帳號在完整加固面下跑完一個 run 需要碰到的所有外部程式與憑證）
python3 -m paulsha_cortex.trust_root toolchain four-way | sed -n '/窮舉盤點/,$p'
#   逐段列出 dispatch／model-call／review／gate／ship／monitor 六段各需要什麼、
#   由哪個 principal 碰到、登記在哪張表。

# ✅ 雙向封閉（兩個都必須是空的；非空＝盤點已漂移，先修再往下裝）
python3 -c "from paulsha_cortex.trust_root import permgen as p
print('uncovered:', p.uncovered_run_dependencies())
print('unlisted :', p.unlisted_roster_entries())"
#   期望：兩行皆為 `()`。

# ✅ 已知未決項（**不是**待辦清單，是「已經被看見的決定」）
python3 -c "from paulsha_cortex.trust_root import permgen as p
for d in p.deferred_run_dependencies(): print('-', d.name, '→', d.disposition)"
#   目前四項；每一項的完整理由與症狀在 permgen.deferred_run_dependencies() 的 note 裡。
#   ⚠️ 這幾項**尚未落地**，不要照著自己補——它們各自需要 operator 的部署面裁決。
```

---

## 第 5 步：降權啟用（**A+B 單一路徑**）

> **前置條件**：執行前提第 9 項。若 `systemd-template` 尚未出現在
> `job_runner.RUNNER_MODES`（部署樹比 #616 舊），則 **(a)(b) 照裝、(c) 跳過、
> (d) 不開**——邊界仍可由 5-7 的反向測試完整證明，只是 Manager 還不會走它；
> 但正確處置是先把部署樹升級到含 #616 的版本，而不是長期停在這個狀態。
> **絕不**因為 (d) 開不了就退回 transient 主路徑；需要臨時降權時走 **附錄 B**
> 並在 #584 記錄殘餘風險與預計關閉時間。

### 5-1. 邊界由三個 root-owned 物件 ＋ 一個帳號事實構成

| # | 物件／事實 | 路徑 | 擁有者 | 它強制什麼 |
|---|---|---|---|---|
| (a) | **polkit 規則** | `/etc/polkit-1/rules.d/49-cortex-downgrade.rules` | root:root 0644 | 只有 `cortex-manager`、只有 `start`／`stop`、只有四個**具名**模板（`cortex-job@` / `cortex-job-jit@` / `cortex-reviewer-job@` / `cortex-reviewer-job-jit@`，皆 `*.service`）。**不授權 `manage-units` 的 transient 建立** |
| (b) | **template unit ×4** | `/etc/systemd/system/cortex-job@.service`（builder, strict）<br>`cortex-job-jit@.service`（builder, jit，#643）<br>`cortex-reviewer-job@.service`（reviewer＋planner, strict，#615）<br>`cortex-reviewer-job-jit@.service`（reviewer＋planner, jit） | root:root 0644 | `User=` 寫死、加固段寫死、`ExecStart=` 寫死。呼叫端**選不了 UID、傳不了屬性**。四份的差異只有兩軸：加固剖面（`MemoryDenyWriteExecute`）與帳號（`User=`／HOME／RWP），見 5-2 |
| (c) | **shim** | `/opt/cortex/bin/cortex-job-shim` | root:root 0755 | `ExecStart=` 的實體。argv 的**形狀**由 root-owned 程式從 Manager-owned job-spec 導出；Manager 只能給參數 |
| — | **三分帳號事實** | — | — | polkit 的 subject 只有 `cortex-manager`，而它**不跑任何模型程式碼**；injection 可達的 job 帳號完全不在授權面上 |

三者缺一都不成立：
- 少了 (a)，`cortex-manager` 起不了 job（fail-closed，不會退回同 UID）。
- 少了 (b)，`User=` 回到呼叫端手上——polkit 看不到它，等於沒守。
  **只裝一份也不成立**：漏裝 `cortex-job-jit@.service` 時，走 node 型 executor
  （`codex`／`copilot`）的派工會在 `prepare_systemd_template()` 的 preflight
  fail-closed（`job-runner-job-template-missing`），不會靜默退回 strict 那份。
- 少了 (c)，argv 的入口落在 Manager 可寫的樹裡；Manager 被攻陷即可換掉執行的東西。

### 5-2. 安裝 (b) template unit（**六份**＝3 角色 × 2 加固剖面）

> **⚠️ 已部署過 #657 之前版本的機器：這一節與第 2 步都必須重跑，且有順序。**
> #657 把 spec spool 從「一個共用根」改成「一個降權身分一格」，因此
> **unit 的 `Environment=PSC_JOB_SPEC_SPOOL=` 與 spool 的落點同時改變**。
> 順序是 **先權限、後 unit**：
>
> 1. 重跑第 2 步的權限 script（建出三格並套 ACL，見 5-3a 的第一個命令）；
> 2. 重落這六份 unit ＋ `daemon-reload`；
> 3. 跑 5-3a 的實測（**以各 job 身分**讀自己的 spec）。
>
> 反過來（先換 unit 後套權限）會讓每個 job 在 shim 讀 spec 時 `EACCES` →
> `78/CONFIG`，而那正是 #657 的症狀，會白白花掉一輪診斷。

> **為什麼是六份**：unit 檔裡寫死兩件事，兩件都不能靠參數傳——
>
> - **`User=`**（#615 M2、#629）：builder／reviewer-planner／gate 是三個不同的
>   OS 帳號 ⇒ 三組檔、三個名字。planner **不另開一份**：方案把它與 reviewer 映到
>   同一個帳號（`cortex-reviewer-planner`），同帳號 ⇒ 同 unit。
> - **加固指令**（#643）：一個模板只有一份加固段 ⇒ 兩種剖面必然是兩個檔。
>
> 六份**共用同一張 `_HARDENING` 表與同一條 `ReadWritePaths` 導出規則**：角色之間的
> 全部差異都是「帳號」帶出來的（`User=`／`Group=`／HOME／cache／登記表上該帳號的
> 可寫面），產生器裡沒有任何一行 `if principal is …`。測試以**集合比對**釘住
> （`tests/test_reviewer_planner_downgrade_615.py::HardeningParityTests` 與
> `tests/test_gate_execution_identity_629.py::GateHardeningParityTests`）。

| unit | `User=` | 給誰 |
|---|---|---|
| `cortex-job@.service` | `cortex-builder` | builder，原生 ELF executor（`claude`／`agy`） |
| `cortex-job-jit@.service` | `cortex-builder` | builder，node 型 executor（`codex`／`copilot`） |
| `cortex-reviewer-job@.service` | `cortex-reviewer-planner` | reviewer＋planner，原生 ELF executor |
| `cortex-reviewer-job-jit@.service` | `cortex-reviewer-planner` | reviewer＋planner，node 型 executor |
| `cortex-gate-job@.service` | **`cortex-gate`** | #629 gate 執行身分，**預設**剖面 |
| `cortex-gate-job-jit@.service` | **`cortex-gate`** | #629 gate 執行身分，宣告了 node 型 gate（`npm test`）時才用 |

> **gate 的剖面由誰選**：不是 executor（gate 不跑模型），而是 **operator** 的
> `PSC_GATE_HARDENING_PROFILE`（預設 `strict`）。這與 #643「剖面不由呼叫端選」
> **不衝突**：#643 擋的是「job 能影響自己的剖面」，而 gate 的命令本身就是 operator
> 宣告的——宣告命令卻不能宣告它需要哪份剖面才是不一致的。值不合法時 fail-closed
> （不落回預設：一個打錯的 `jti` 落回 strict 會讓 node 型 gate 全崩而設定看起來是對的）。

```bash
# ✅ 先看 reviewer 那兩份（與 builder 的差異必須**只有帳號帶出來的那幾行**）
python3 -m paulsha_cortex.trust_root unit four-way --review-job | less
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job) \
     <(python3 -m paulsha_cortex.trust_root unit four-way --review-job) \
  | grep -E "^[<>] [A-Za-z]" | sort
#   期望只出現這幾類指令行的差異（其餘逐字相同）：
#     User= / Group=                      ← 帳號
#     Environment=HOME= / XDG_CACHE_HOME= ← 帳號的 HOME
#     ReadWritePaths=                     ← 登記表上該帳號的可寫面
#   ⚠️ 若 `MemoryDenyWriteExecute` 或任何其他加固鍵出現在差異裡 ⇒ 兩個角色的加固面
#      分岔了，**停下來**：本步驟的前提（四份共用同一張表）不再成立。

# ✅ reviewer 的 ReadWritePaths 必須**恰好兩條**，且不含 builder 的任何面
python3 -m paulsha_cortex.trust_root unit four-way --review-job | grep '^ReadWritePaths='
#   期望恰好兩行：
#     ReadWritePaths=/var/lib/cortex-reviewer-planner/cache
#     ReadWritePaths=/var/lib/cortex/coordinator/review-verdicts
#   ⚠️ 出現 /var/lib/cortex/worktree/%i、commit-spool、runtime/dispatch 任何一條
#      ⇒ 停下來：reviewer 拿到了 builder 的工作面或 Manager 的證據面。
#   ⚠️ 出現任何帶 `%i` 的路徑 ⇒ 停下來：reviewer 的工作樹不在 pool 底下，
#      systemd 對不存在的 ReadWritePaths 目標會讓每一個 reviewer job 起不來。
```

### 5-2a. 兩份 builder 模板（#643 per-executor 加固剖面）

> **為什麼是兩份**：`MemoryDenyWriteExecute=yes` 擋的是 JIT 型 shellcode，而 V8 的
> JIT **必須**有 W+X 記憶體——這一項與 JS runtime 天生互斥。實機逐項隔離的結果是
> 「唯一的阻斷點就是它」：`+MemoryDenyWriteExecute=yes` 下 `node` 直接崩在
> `v8::internal::Runtime_CompileLazy`，其餘每一項（`ProtectSystem=strict`／
> `PrivateTmp`／`RestrictNamespaces`／`SystemCallFilter=@system-service` ＋
> `SystemCallErrorNumber=EPERM`）單獨加上去 `node` 都正常。
>
> 因此 operator 裁決走 **per-executor 剖面**：node 型 executor（`codex`／`copilot`）
> 走 `cortex-job-jit@.service`，原生 ELF（`claude`／`agy`）維持嚴格的
> `cortex-job@.service`。**兩份由同一張加固表產生**，只在 `MemoryDenyWriteExecute`
> 這一項分岔（測試以集合比對釘住，見 `tests/test_trust_root_hardening_profile_643.py`）。
>
> **剖面選不到寬鬆那份**（這是本設計全部的價值）：對應表由
> `permgen.EXECUTOR_TOOLS` 的 `needs_node` 機械導出，唯一的輸入是 **executor**，而
> executor 是 Manager 的 dispatch 決定；job spec 結構性禁止攜帶任何剖面欄位
> （`job_runner.SPEC_FORBIDDEN_KEYS`，寫端與讀端各擋一次），未知 executor
> **fail-closed**（不落到寬鬆那份），`PSC_JOB_TEMPLATE_UNIT` 也不接受已帶剖面
> 後綴的值（否則 operator 可以一鍵把所有 job 推到寬鬆剖面）。
>
> **代價**：走 jit 剖面的 job **失去 MDWE 這層防護**。這是為了保留 provider 多樣性
> （`independence_domain` 的可選空間）付的代價，不是沒有代價——完整說明見 spec
> §R3「per-executor 加固剖面」段，產生出來的 unit 檔頭也逐條寫著。

```bash
# ✅ 先看兩份內容（剖面在檔頭以「=== 加固剖面 ===」段標明，含它接受的代價）
python3 -m paulsha_cortex.trust_root unit four-way --job | less
python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit | less
#   兩份都必須確認的三行：
#     User=cortex-builder      ← 唯一 UID 來源，寫死
#     Group=cortex-builder
#     ExecStart=…              ← 見 5-3；PR 落地後應指向 /opt/cortex/bin/cortex-job-shim

# ✅ 兩份的差異必須**只有一項**（這條先跑；不成立就不要落檔）
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job) \
     <(python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit) \
  | grep -E "^[<>] [A-Za-z]" | sort
#   期望恰好兩行（同一個鍵的兩個值）：
#     < MemoryDenyWriteExecute=yes
#     > MemoryDenyWriteExecute=no
#   ⚠️ 出現任何第三行 ⇒ 兩份剖面在加固表以外也分岔了，**停下來**：
#      那代表產生器被改成兩段各自維護，本步驟的前提不再成立。

# 🔧 sudo：落檔**四份**（root 擁有——這是 User= 不可被竄改的前提）
for W in --job --review-job; do
  for P in "" "--profile jit"; do
    U=$(python3 -m paulsha_cortex.trust_root unit four-way $W $P \
          | sed -n '1s|^# /etc/systemd/system/||p')
    python3 -m paulsha_cortex.trust_root unit four-way $W $P \
      | sudo tee "/etc/systemd/system/$U" >/dev/null
    sudo chown root:root "/etc/systemd/system/$U"
    sudo chmod 0644 "/etc/systemd/system/$U"
    echo "installed: $U"
  done
done
sudo systemctl daemon-reload
#   期望印出四行：cortex-job@ / cortex-job-jit@ /
#                 cortex-reviewer-job@ / cortex-reviewer-job-jit@（.service）

# ✅ 驗證：與產生器逐位元相同、User= 確實寫死
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job) \
     /etc/systemd/system/cortex-job@.service && echo "job unit (strict) in sync: OK"
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit) \
     /etc/systemd/system/cortex-job-jit@.service && echo "job unit (jit) in sync: OK"
diff <(python3 -m paulsha_cortex.trust_root unit four-way --review-job) \
     /etc/systemd/system/cortex-reviewer-job@.service && echo "review unit (strict) in sync: OK"
diff <(python3 -m paulsha_cortex.trust_root unit four-way --review-job --profile jit) \
     /etc/systemd/system/cortex-reviewer-job-jit@.service && echo "review unit (jit) in sync: OK"
for U in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit; do
  echo "--- $U"
  grep -E "^(User|Group|ExecStart|NoNewPrivileges|CapabilityBoundingSet|MemoryDenyWriteExecute)=" \
       "/etc/systemd/system/$U@.service"
done
#   期望：ExecStart 四份皆 /opt/cortex/bin/cortex-job-shim %i；
#         User= 兩份 cortex-builder、兩份 cortex-reviewer-planner；
#         NoNewPrivileges=yes、CapabilityBoundingSet=（空值）四份皆同；
#         MemoryDenyWriteExecute 每個角色各一份 yes、一份 no。

# ✅ 驗證：systemd 解析四份都無「未知鍵」（#645 修的 CollectMode 就是這一族）
sudo systemd-analyze verify /etc/systemd/system/cortex-job@.service \
                            /etc/systemd/system/cortex-job-jit@.service \
                            /etc/systemd/system/cortex-reviewer-job@.service \
                            /etc/systemd/system/cortex-reviewer-job-jit@.service 2>&1 \
  | grep -i "unknown key" && echo "❌ 有未知鍵，停下來" || echo "no unknown keys: OK"

# ✅ 驗證（#615）：四份的加固表除剖面差異外**逐項相同**（集合比對，不硬編）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
keys = {k for k, _v, _w in permgen._HARDENING}
tables = {}
for p in permgen.DOWNGRADED_JOB_PRINCIPALS:
    for prof in permgen.HARDENING_PROFILES:
        u = permgen.build_job_unit(permgen.DEFAULT_SCHEME, principal=p, profile=prof)
        t = {}
        for line in u.content.splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k in keys:
                t[k] = v
        tables[u.unit_name] = t
assert len({frozenset(t) for t in tables.values()}) == 1, "加固鍵集合不一致"
strict = tables["cortex-job@.service"]
for name, t in tables.items():
    diff = {k for k in t if t[k] != strict[k]}
    expect = set() if "-jit@" not in name else permgen.PROFILE_DIVERGENCE_KEYS
    assert diff == expect, (name, diff)
print(f"hardening parity across {len(tables)} units: OK")
PY
#   期望：`hardening parity across 4 units: OK`。任何 AssertionError ⇒ 停下來。
#   ⚠️ 舊版落檔的 unit 會在這裡報
#      `Unknown key name 'CollectMode' in section 'Service', ignoring.`
#      ——那表示「失敗的 instance 自動回收」從來沒有生效過（#645 已把它移回 [Unit]；
#      **產生器修好不代表已落檔的 unit 跟著更新**，所以這條檢查在落檔後跑）。
#      重新落檔即修好；順便清一次殘骸：
#        systemctl list-units --failed 'cortex-job*'
#        sudo systemctl reset-failed 'cortex-job@*' 'cortex-job-jit@*'
```

### 5-2b. 在**真實加固面下**驗證兩種剖面（#643 的核心驗收；#673 改為全矩陣）

**只驗寬鬆環境的 `--version` 會整個溜過去**——四支 executor 在 `sudo -u` 下全部
rc=0、版本全部相符，而其中兩支在真實加固面下是空輸出。

**但「比 production 弱的複本」只是兩種錯法的其中一種。** #673 量到另一個方向：
一份手抄的十 property 複本抄了 `SystemCallFilter=` 卻**漏抄** `SystemCallErrorNumber=EPERM`，
於是它比 production **更嚴格**（seccomp 落回預設的 `SECCOMP_RET_KILL_PROCESS`），
量出一個 production 根本不存在的 `codex`／`copilot` rc=1，並據此開了一張「預設派工
路徑是壞的」的 P0。**手抄子集的假綠與假紅一樣會發生，方向不由人選。**

因此本步驟的規矩是：

1. **加固面一律走 `psc_run_under`**（見第 4e 步的「共用探針」），由
   `permgen.unit_replica_properties()` 從**已落檔的 unit** 全量導出，不手抄、不 grep
   白名單。落檔的 unit 少任一加固鍵時該函式 fail-closed，不會產出半套清單。
2. **驗的是矩陣，不是四段**：每個 executor × 每個剖面都要跑，因為「A 在自己的剖面下
   能動」證明不了「B 在同一份剖面下沒被弄壞」。

```bash
# 前置：--instance probe 需要一格真的 worktree（見第 4e 步的說明）
sudo install -d -o cortex-builder -g cortex-builder -m 0700 /var/lib/cortex/worktree/probe

# ✅ (1) 全矩陣：4 executor × 2 剖面 × 2 角色 unit，一次跑完並列印
for stem in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit; do
  for cli in claude agy codex copilot; do
    out="$(psc_run_under "$stem" "/opt/cortex/toolchain/bin/$cli" --version 2>/dev/null)"; rc=$?
    printf '%-26s %-8s rc=%-3s out=[%s]\n' "$stem" "$cli" "$rc" "$(echo "$out" | tr -d '\n' | cut -c1-34)"
  done
done
```

**期望矩陣**（#673 實機逐格量測，`cortex-job*`＝builder、`cortex-reviewer-job*`＝
reviewer／planner；同一剖面下兩個角色**必須**逐格相同，不同代表帳號面有分岔）：

| executor | `…-job@`（strict） | `…-job-jit@`（jit） |
|---|---|---|
| `claude` | **rc=0** `2.1.233 (Claude Code)` | **rc=0** 同左 |
| `agy` | **rc=0** `1.1.13` | **rc=0** 同左 |
| `codex` | rc=1、stdout 空 | **rc=0** `codex-cli 0.147.0` |
| `copilot` | rc=1、stdout 空 | **rc=0** `0.0.330` |

- **正向**（每個 executor 在**它自己的**剖面下）：`claude`／`agy` 看 strict 欄，
  `codex`／`copilot` 看 jit 欄——**四格皆 rc=0，且版本與 operator 側逐字相同**
  （第 4e 步那條）。
- **負向對照（不可省略）**：`codex`／`copilot` 的 strict 欄**必須失敗**。
  只驗 jit 成功等於什麼都沒驗。
- **反向對照（#673 新增，同樣不可省略）**：`claude`／`agy` 的 **jit** 欄必須
  rc=0。放寬剖面若弄壞了原本好的兩支，這是唯一看得見的地方。

```bash
# ✅ (2) 負向那兩格的**失敗原因**要對得上，不能只看 rc
psc_run_under cortex-job /opt/cortex/toolchain/bin/codex --version
#   期望 stderr 含：`Check failed: 12 == (*__errno_location ())` 與
#                   `v8::internal::Runtime_CompileLazy`
#   ——那是 MDWE：V8 期待 mmap 回 ENOMEM(12)，被擋成 EPERM(1)。
#   ⚠️ **stderr 空**是另一回事，不是這一條：那代表行程被 SIGSYS 當場殺掉，
#      ＝你的複本漏了 `SystemCallErrorNumber=EPERM`（#673 的原始誤判）。
#      看到空 stderr 先回去查複本，不要開票說 executor 壞了。
#   ⚠️ 若這兩格反而**印出版本** ⇒ strict 那份 unit 的 MDWE 沒生效（落檔錯／被
#      drop-in 覆蓋）。此時正向的綠是假的：它證明不了 jit 剖面是必要的。

# ✅ (3) 剖面對應表：確認程式碼看到的分類與上面實測一致
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
for tool in permgen.EXECUTOR_TOOLS:
    p = permgen.executor_hardening_profile(tool.name)
    print(f"{tool.name:8s} needs_node={str(tool.needs_node):5s} "
          f"profile={p.profile_id:6s} unit={permgen.job_unit_stem(profile=p)}@.service")
PY
#   期望：codex/copilot → jit（cortex-job-jit@.service）；claude/agy → strict。
#   ⚠️ 若實測 (1) 與這張表對不上，**以實測為準**並回填 permgen.EXECUTOR_TOOLS
#      的 needs_node（那張表是唯一真相來源），不要在 runbook 裡各記一份。

# ✅ (4) seccomp 過濾語意的不變式（#673）：與剖面**正交**的第二個維度
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
for f in permgen.filtered_syscall_surfaces():
    print(f"{f.program:9s} {','.join(f.syscalls):12s} fatal={f.fatal!s:5s} {f.detail}")
PY
#   期望：四列（codex／copilot／srt／openspec），**fatal 全部 False**。
#   出現 fatal=True ⇒ 那支程式會在該面上靜默死（rc=1、stdout 與 stderr 皆空）。
#   （permgen import 時就會擋下這種狀態，因此這裡看到的是「檢查有跑」的確認。）

sudo rm -rf /var/lib/cortex/worktree/probe   # 收尾
```

> **不要把 `pkey_alloc`／`@pkey`／`@sandbox` 加進 `SystemCallFilter=`。**
> #673 量到：`@system-service` 確實過濾掉 V8 要用的 `pkey_alloc`（x86_64 330），
> 但同一份 unit 上的 `SystemCallErrorNumber=EPERM` 讓它只回錯誤碼、不殺行程，V8
> 走 fallback，四支 executor 全部正常。放行那支 syscall 是**放寬過濾器**且沒有
> 量測支撐；`EPERM` **不放行任何 syscall**。沿用 #643 的先例：量到才改，改的是
> 一份具名剖面，不是全域放寬——而這次量到的結論是**不用改**。
> （順帶：`@sandbox` 對此症狀完全無效，實測加了照樣死。）

### 5-2c. 兩份 gate 模板（#629 gate 執行身分）

```bash
# ✅ 先看內容，並確認與 builder 的差異**只有帳號帶出來的那幾行**
python3 -m paulsha_cortex.trust_root unit four-way --gate-job | less
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job) \
     <(python3 -m paulsha_cortex.trust_root unit four-way --gate-job) \
  | grep -E "^[<>] [A-Za-z]" | sort
#   期望只出現這幾類指令行的差異（其餘逐字相同）：
#     User= / Group=                      ← 帳號
#     Environment=HOME= / XDG_CACHE_HOME= ← 帳號的 HOME
#     ReadWritePaths=                     ← 登記表上該帳號的可寫面
#   ⚠️ 若任何加固鍵出現在差異裡 ⇒ 停下來：本步驟的前提（六份共用同一張表）不成立。

# ✅ gate 的 ReadWritePaths 必須**恰好三條**，且不含 builder／Manager 的任何面
python3 -m paulsha_cortex.trust_root unit four-way --gate-job | grep '^ReadWritePaths='
#   期望恰好三行：
#     ReadWritePaths=/var/lib/cortex-gate/cache
#     ReadWritePaths=/var/lib/cortex/coordinator/gate-ledger-spool
#     ReadWritePaths=/var/lib/cortex/gate-worktree
#   ⚠️ 出現 /var/lib/cortex/runtime/dispatch ⇒ **立即停止**：那是 gate ledger 與
#      exit sentinel 的落點，gate 寫得進去等於它能偽造任何 job 的完成狀態（#628 失效）。
#   ⚠️ 出現 /var/lib/cortex/worktree/%i ⇒ 停下來：gate 只該**讀**被驗的樹（rX ACL），
#      命令一律跑在它自己 pool 底下的拋棄式副本上。
#   ⚠️ 出現 review-verdicts 或 commit-spool ⇒ 停下來：#638／#639 的通道被重新打開。

# 🔧 sudo：落檔（兩份）
sudo install -o root -g root -m 0644 /dev/stdin /etc/systemd/system/cortex-gate-job@.service \
  < <(python3 -m paulsha_cortex.trust_root unit four-way --gate-job)
sudo install -o root -g root -m 0644 /dev/stdin /etc/systemd/system/cortex-gate-job-jit@.service \
  < <(python3 -m paulsha_cortex.trust_root unit four-way --gate-job --profile jit)
sudo systemctl daemon-reload

# ✅ 落檔後檢查（產生器修好 ≠ 已落檔的 unit 跟著更新，#643 的教訓）
sudo systemd-analyze verify cortex-gate-job@.service 2>&1 | grep -i "unknown key" && \
  echo "⚠️ 有未知鍵——停下來" || echo "OK：無未知鍵"
systemctl cat cortex-gate-job@.service | grep -E "^(User|Group|ExecStart)="
#   期望：User=cortex-gate / Group=cortex-gate / ExecStart=/opt/cortex/bin/cortex-job-shim %i
```

### 5-2d. 在**真實加固面下**驗證 gate 身分（#629 的核心驗收）

> **只驗寬鬆環境會整個溜過去**（#642 第 4e 步／#643 第 5-2b 步的同一個教訓）：
> 下面每一條的結論都來自「已落檔的 unit ＋ 真實 ACL ＋ 真實 UID」，在單 UID 的
> 開發機或 CI 容器裡跑同樣的命令一律會綠，而那個綠不代表任何事。

```bash
# ✅ (1) gate 真的以 cortex-gate 執行，而且 dispatch log 目錄對它是唯讀的
sudo systemd-run --uid=cortex-gate --gid=cortex-gate --pipe --wait \
  -p ProtectSystem=strict \
  -p ReadWritePaths=/var/lib/cortex/coordinator/gate-ledger-spool \
  -p ReadWritePaths=/var/lib/cortex/gate-worktree \
  /bin/sh -c 'id -un; : > /var/lib/cortex/runtime/dispatch/probe.$$ && echo "BAD: wrote" || echo "OK: EROFS/EACCES"'
#   期望：第一行 cortex-gate；第二行 OK。
#   ⚠️ 出現 BAD ⇒ 立即停止：#628 的作者歸屬在檔案層失效。

# ✅ (2) gate 讀得到 builder 的工作樹，但寫不進去（rX，沒有 w）
#     先取一個真實存在的 per-job 工作區（沒有就先派一張 build 卡）
JOBDIR=$(sudo ls -1d /var/lib/cortex/worktree/*/ 2>/dev/null | head -1)
sudo -u cortex-gate ls "$JOBDIR" >/dev/null 2>&1 && echo "OK: readable" || echo "BAD: not readable"
sudo -u cortex-gate touch "$JOBDIR/psc-gate-probe" 2>&1 | tail -1
#   期望：第一行 OK；第二行 Permission denied。
#   ⚠️ 「not readable」⇒ gate 跑不了任何 gate（快照會失敗）：檢查
#      `setfacl -m u:cortex-gate:rX` 是否套到該 job 目錄，以及**容器層的 default ACL**
#      （per-job 目錄是 job 帳號在 dispatch 當下建的，繼承的是容器的 default ACL）。
#   ⚠️ 「touch 成功」⇒ 立即停止：gate 能污染 harvest 的來源。

# ✅ (3) gate 寫得進自己那一格 spool，但讀不到別人的（wx 無 r）
sudo install -d -o cortex-manager -g cortex-manager -m 0700 \
  /var/lib/cortex/coordinator/gate-ledger-spool/probe-a \
  /var/lib/cortex/coordinator/gate-ledger-spool/probe-b
sudo setfacl -m u:cortex-gate:wx /var/lib/cortex/coordinator/gate-ledger-spool/probe-a
sudo -u cortex-gate sh -c 'echo x > /var/lib/cortex/coordinator/gate-ledger-spool/probe-a/ledger.json' \
  && echo "OK: wrote own slot" || echo "BAD: cannot write own slot"
sudo -u cortex-gate ls /var/lib/cortex/coordinator/gate-ledger-spool/probe-a 2>&1 | tail -1
#   期望：第一行 OK；第二行 Permission denied（有 wx 沒有 r ⇒ 寫得進、列不出來）。
sudo -u cortex-gate ls /var/lib/cortex/coordinator/gate-ledger-spool/probe-b 2>&1 | tail -1
#   期望：Permission denied（連 traverse 都沒有）。
sudo rm -rf /var/lib/cortex/coordinator/gate-ledger-spool/probe-a \
            /var/lib/cortex/coordinator/gate-ledger-spool/probe-b

# ✅ (4) 端到端：派一張 build 卡，確認 ledger 由 **Manager** 擁有
#     （gate 交付的那一份留在 spool 裡並已封口）
sudo ls -l /var/lib/cortex/runtime/dispatch/*.gates.json | tail -3
#   期望：owner 為 cortex-manager。
#   ⚠️ owner 是 cortex-gate ⇒ 停下來：採信端會以 gate-ledger-foreign-author 拒掉，
#      而且代表權威 ledger 的落地路徑被繞過了。
sudo ls -ld /var/lib/cortex/coordinator/gate-ledger-spool/*/ | tail -3
#   期望：mode 為 dr-x------（0500，已封口）。

# ✅ (5) 負向對照：把 gate 帳號的 ACL 拿掉之後，build 卡必須 **fail closed**
#     （不是靜默通過，也不是退回 Manager 進程內執行）
#   做法：`setfacl -x u:cortex-gate <spool>`，重派一張 build 卡，
#   期望 run 停在 needs_human 且理由指向 gate 執行面（gate-spool-* 診斷碼），
#   **絕不**出現「卡過了」。驗完記得把 ACL 加回去（或重跑第 2 步的權限 script）。
```

### 5-3. 部署 (c) root-owned shim

shim 是 C（code-level argv 保證）從 Manager 端**搬進 root-owned 檔案**的那一步：
job 的 argv 不再由 Manager 行程直接組出並交給 systemd，而是由 root 擁有的程式
從 **Manager-owned 的 job-spec spool** 讀取參數後導出。

| 角色 | 路徑 | 擁有者 | 權限意義 |
|---|---|---|---|
| shim（root-owned 程式） | `/opt/cortex/bin/cortex-job-shim` | root:root 0755 | 三個服務帳號皆**不可寫**（`/opt/cortex` 整棵 root-owned） |
| job-spec spool **容器** | `/var/lib/cortex/coordinator/job-specs` | cortex-manager 0700 ＋ 三條 `--x` traverse ACL | **零讀權**：job 走得進自己那格，列不出這台機器上還有誰的 job。登記表資產 `job-spec-spool` |
| per-principal spool（#657） | `<容器>/{builder,reviewer,gate}` | cortex-manager 0700 ＋ **該帳號** `r-x` ACL（access ＋ default） | 一個降權身分一格。登記表資產 `job-spec-spool-<principal>`，由 `DOWNGRADED_JOB_PRINCIPALS` 機械導出 |
| per-job spec | `<該角色的 spool>/<instance>.json`（**一個檔，不是一個目錄**） | cortex-manager 0640 ＋ 該帳號唯讀 ACL（default 繼承） | job **改不了自己的命令列，也埋伏不了下一個 job**——見下方「守的是寫入面」 |

> **守的是寫入面，不是讀取面**（M1 實測校正，#584／#621）：
> template unit 的 `User=cortex-builder` 由 systemd 在 `ExecStart` **之前**套用，
> 所以 shim 本身就是以 builder 身分執行的——**它必須讀得到 spec，否則 job 起不來**。
> 「builder 讀得到 spool」因此是設計，不是破口。真正載重的是四個**寫入**面實測全拒：
> builder 無法在 spool 內**建立**新 spec、**追加**自己的 spec、用 **symlink 換掉**、
> 或**刪除**任何 spec。「改不了自己的命令列、埋伏不了下一個 job」就落在這四條上。
>
> **per-job 的讀隔離不在本方案範圍**：同一個 persona 的所有 job 共用**同一個 UID**，
> 因此彼此的 spec 本來就互讀得到——這在威脅模型內（同 persona 的 job 之間不設界）。
> 要做到 per-job 讀隔離必須 **per-job UID**（動態 UID／`DynamicUser=` 一類），
> 那是另一個方案，**Phase 2b 不宣稱**。舊版此表寫「job 只讀自己那格」並不精確。
>
> **但 per-persona 的讀隔離在 #657 之後成立**：三個降權身分各有自己的 spool 根，
> 跨 persona 讀不到彼此的 spec（實測步驟在 5-3a）。
>
> `User=` 完全不在 spec 內（`build_job_spec()` 對 `user`／`uid`／`group`／`gid`／
> `properties`／`exec_start` 主動 fail-closed，shim 讀端再驗一次），
> 因此 spool 即使被竄改也選不了 UID。

```bash
# ✅ 產生 shim 內容（#616 已 merge；產生器是唯一真相）
python3 -m paulsha_cortex.trust_root shim four-way > /tmp/cortex-job-shim

# ✅ 檢查安裝好的 template unit 實際的 ExecStart
systemctl cat cortex-job@.service | grep -E "^ExecStart="
#   期望：ExecStart=/opt/cortex/bin/cortex-job-shim %i
#   若仍為 /bin/sh /var/lib/cortex/jobs/%i/run.sh ⇒ 部署樹比 #616 舊
#   （那是 run.sh 時代的形狀，spool 路徑也還是舊的）——先升級部署樹再繼續。

# ✅ 檢查 spec 從哪裡讀（unit 用 Environment= 寫死，呼叫端改不了）
#   #657：**六份 unit 各指自己那格**，三條路徑必須互不相同。
for U in cortex-job cortex-job-jit \
         cortex-reviewer-job cortex-reviewer-job-jit \
         cortex-gate-job cortex-gate-job-jit; do
  printf '%-28s ' "$U"
  systemctl cat "$U@.service" | grep -E "^Environment=PSC_JOB_SPEC_SPOOL="
done
#   期望：
#     cortex-job / cortex-job-jit                   → …/job-specs/builder
#     cortex-reviewer-job / cortex-reviewer-job-jit → …/job-specs/reviewer
#     cortex-gate-job / cortex-gate-job-jit         → …/job-specs/gate
#   任一份仍指向容器 `…/job-specs`（沒有尾段）⇒ 那是 #657 之前的 unit，
#   該角色的每一個 job 都會以 78/CONFIG 收場。重跑 5-2 的落檔步驟。

# ✅ 檢查 job 工作區的 ReadWritePaths（#645）
systemctl cat cortex-job@.service | grep -E "^ReadWritePaths=/var/lib/cortex/worktree/%i$"
#   期望：命中。`%i` 這個 segment 與 provisioning 產生的**目錄名**是同一個推導點
#   （`coordinator/job_workspace.py` 的 `job_segment(job_id)`，`job_runner`
#   的 instance 名走的也是它）。#645 之前 provisioning 用的是 branch slug
#   （`feature-<slice_id>`），與 `%i` 永遠差一個前綴 ⇒ ReadWritePaths 指向不存在
#   的路徑 ⇒ `Failed to set up mount namespacing` / `226/NAMESPACE`，job 起不來。
#
#   #648：canonical（workflow）lane 的工作區在 #646 之前是 **per-run** 的（build 卡
#   provision 之後，同一個 run 後續的卡沿用同一棵樹），一個工作區對多個 job_id ⇒
#   同一個症狀在那條 lane 上對「第二張卡起」必然重現。已改為 per-job：每一張 build
#   卡自己 clone 一份，卡與卡之間的交接走 bundle ＋ append-only spool（#637）。
#   實機稽核：一個多卡 run 跑完後，pool 底下該有**每張 build 卡各一個**目錄，
#   且每個目錄名都能在 `systemctl list-units 'cortex-job*@*'` 的 instance 名裡找到。
#     ls /var/lib/cortex/worktree
#
#   #649（範圍界線，避免誤查）：**ship phase 的兩張卡不在這條稽核裡**。
#   `openspec-archive`／`policy-commit` 的 persona 是 `manager`，而
#   `manager._dispatch_workflow_card()` 對 `current_phase == "ship"` 一律回 None
#   ——它們不經 launcher、不 spawn job，因此 pool 底下**不會**、也不該出現 ship 卡的
#   目錄，`systemctl list-units` 上也不會有它們的 instance。ship 卡由 Manager 自己
#   在 `work_bridge` 內以 deterministic 身分執行，它的 commit 走的是 #649 補上的
#   回收通道（bundle ＋ commit-spool → 來源樹的 `refs/heads/<branch>`）。
#
#   #653（已修）：ship 段在此之前全程在 `_builder_binding()` 交回來的 **builder 的
#   clone** 裡動手（`git commit`／preflight／push／`_ship_action`），而 #641 已把
#   登記表裡 Manager 對 job 工作樹的讀取授權全部收掉（見第 2 步的稽核 5b）⇒ 降權部署
#   下（三分／四分皆然）第一個 `git -C` 就會 `Permission denied`。症狀是**權限**不是
#   `226/NAMESPACE`，別往 mount namespace 的方向查。現在 ship 段以 `run.candidate_head`
#   為 base，在
#   來源樹上 provision 一棵**自己的 Manager-owned clone**（目錄名同樣由
#   `job_workspace.job_segment()` 導出，形如 `wf-<run 摘要>-ship-<candidate 前綴>-…`），
#   commit／preflight／push 全在那裡發生，**不需要**任何指向 job 工作樹的授權。
#
#   實機稽核：ship phase 跑過之後，pool 底下會多出 ship 卡自己的目錄，且
#     stat -c '%U %a' /var/lib/cortex/worktree/wf-*-ship-*
#   期望：`cortex-manager 700`（Manager 自己 clone 的，不是 job 帳號的）。
#     getfacl -p /var/lib/cortex/worktree/wf-*-ship-* | grep -c '^user:'
#   期望：`0`——ship 的樹不需要任何具名 ACL 條目；稽核 5b 的「零 `setfacl`」因此
#   仍然成立。這些目錄與 build 卡的 clone 走同一套回收（`cortex work gc`，branch
#   merge 後才回收），不由 ship 段自己刪：archive 卡的 job 記錄指著它。
#
#   #650（已修）：verify／review 卡在此之前以 `builder_jobs[-1]["worktree"]`（前一張
#   build 卡的工作區）為 candidate 樹，而 Manager 在那棵樹上做的**不只是讀**——
#   `_workflow_input_snapshot()` 會往裡面 seed 缺席的 planning authority 檔、
#   `planning_runtime._tree_snapshot()` 會遞迴走完整棵樹 ⇒ 收掉唯讀 ACL 之後這條 lane
#   同樣是 `Permission denied`（與 ship 段同型，症狀一樣是**權限**）。現在 verify／
#   review 卡也在來源樹上 provision 一棵自己的 Manager-owned clone，識別穩定於
#   **(run, candidate)**（形如 `wf-<run 摘要>-review-<candidate 前綴>-…`）。
#
#   實機稽核：verify／review phase 跑過之後
#     stat -c '%U %a' /var/lib/cortex/worktree/wf-*-review-*
#   期望：`cortex-manager 700`。
#     getfacl -p /var/lib/cortex/worktree/wf-*-review-* | grep -c '^user:'
#   期望：`0`（同上，稽核 5b 的「零 `setfacl`」仍然成立）。
#   注意這棵樹裡會有**未追蹤**的 canonical report（`reports/verify|review/*.md`）
#   ——那是 `code-review` → `adversarial-review` 的宣告輸入，不是殘留，不要手動清。

# ✅ 檢查 unit 沒有被忽略的鍵（#645 附帶；#643 起兩份都要驗）
sudo systemd-analyze verify /etc/systemd/system/cortex-job@.service \
                            /etc/systemd/system/cortex-job-jit@.service
#   期望：**沒有** `Unknown key name 'CollectMode' in section 'Service', ignoring.`
#   `CollectMode` 屬 `[Unit]`；放在 `[Service]` 只是被忽略（不影響行為），但
#   「失敗的 instance 自動回收」這個用意不會生效，失敗殘骸會一直掛在
#   `systemctl list-units --failed` 上、擋住同名 instance 的下一次 start。

# 🔧 sudo：落檔（root 擁有、可執行、對三個服務帳號唯讀）
sudo install -d -o root -g root -m 0755 /opt/cortex/bin
sudo install -o root -g root -m 0755 /tmp/cortex-job-shim /opt/cortex/bin/cortex-job-shim

# ✅ 驗證：與產生器逐位元相同
diff <(python3 -m paulsha_cortex.trust_root shim four-way) /opt/cortex/bin/cortex-job-shim \
  && echo "shim in sync: OK"

# ✅ 驗證：三個服務帳號都改不動 shim（也換不掉它）
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  sudo -u "$U" sh -c 'printf "id\n" >> /opt/cortex/bin/cortex-job-shim' 2>&1 | tail -1
  sudo -u "$U" sh -c 'ln -sf /tmp/evil /opt/cortex/bin/cortex-job-shim' 2>&1 | tail -1
done
#   期望：六條全部 Permission denied
ls -l /opt/cortex/bin/cortex-job-shim
#   期望：-rwxr-xr-x root root
```

### 5-3a. per-principal spec spool（#657）——**以各 job 身分實測讀得到**

> **本節不是「檔案在不在」的檢查。** #657 的實機病灶是：spool 目錄存在、Manager
> 寫得進去、`getfacl` 上也看得到唯讀 ACL——而 gate／reviewer 的每一個 job 仍以
> `78/CONFIG` 死掉，因為 shim 是 systemd 套完 `User=` **之後**才執行的，它以 job
> 身分讀 spec。因此本節的每一條驗收都要**變成那個身分**去做，或至少讀 `getfacl`
> 的 **`#effective:`**，而不是「有沒有那條 ACL」。

**mask 陷阱（實機量到過，務必看這一條）**：`chmod` 會重寫 ACL 的 mask，因此
「先 `setfacl` 再 `chmod`」會讓具名條目靜默失效——`getfacl` 仍列出
`user:cortex-builder:r-x`，後面卻跟著 `#effective:---`，實際權限是零。產生器輸出的
命令順序（`chown` → `chmod` → `setfacl`，父層 traverse 一律殿後）已經避開它；
**手動補過 ACL 之後又 `chmod` 過的機器會踩到**。判準一律看 `mask::` 與
`#effective:`。

> ### ⚠️ 改變 ACL **結構**時：舊的 default ACL 會靜默地跟著新物件走（0817 實機）
>
> **這是升級既有部署才會踩到的，全新安裝不會。** default ACL（`default:` 那幾條）
> 的作用對象是**容器底下之後新建的每一個物件**。因此當一個容器的授權結構被改掉——
> 從「一格、只授某一個 principal」變成「per-principal 多格」——**舊的那條 default 會
> 原封不動地繼承進每一個新建的子目錄**，而它正是新結構要防的東西。
>
> **實機案例**：`job-specs` 原本是 builder 專用，容器上帶著
> `default:user:cortex-builder:r-x`。#657（PR #660）把它改成 per-principal 三格之後，
> 新建的 `builder`／`reviewer`／`gate` **三格全部繼承了那一條**——於是 builder 一開始
> 就讀得到 reviewer 與 gate 的格，而「跨 principal 讀不到彼此的 spec」正是 #657 選
> per-principal 買到的**唯一**那條性質。三格的 `getfacl` 看起來都很正常（自己那條在、
> mask 也對），多出來的那條混在裡面不顯眼。
>
> **處置**：改結構時，先 `setfacl -k <容器>` 清掉**容器層**過時的 default ACL，再重跑
> 權限計畫讓產生器把正確的那組放回去。`-k` 只動 default ACL，不動 access ACL，因此
> 容器自己的 traverse 授權不受影響。順序是「先清、後跑」——反過來會把剛套好的
> default 一起清掉。
>
> **驗收判準同 mask 陷阱**：不是「有沒有那條 ACL」，而是**每一格的具名條目集合恰好
> 只有自己那一條**，且 `mask::` 與 `#effective:` 都對。下面「effective——每格只授
> 自己」那一條的 `**無其他 principal 的條目**` 就是為此存在，不要跳過。

```bash
# 🔧 sudo：清掉容器層過時的 default ACL（#633：改 ACL 結構的必要步驟）
#   `-k` 只刪 default ACL，access ACL（容器自己的 --x traverse）不受影響。
#   全新安裝時這條是 no-op；**升級既有部署時不做，三格會繼承舊的那條 default**。
sudo getfacl -p /var/lib/cortex/coordinator/job-specs | grep '^default:' || echo "(無 default ACL)"
#   ← 先看清楚要清掉什麼。出現 `default:user:cortex-builder:r-x` 就是舊結構的殘留。
sudo setfacl -k /var/lib/cortex/coordinator/job-specs

# 🔧 sudo：重跑權限計畫（#657 之後 spool 從一格變四格，必須重跑）
#   ⚠️ 順序：scaffold 先、permissions 後（同第 2b 步）。這條在**升級既有部署**時
#      同樣成立——理由見 2b「為何要先 scaffold」。
python3 -m paulsha_cortex.trust_root permissions four-way --commands --paths \
  --operator-account "$(id -un)" --external-reader-account none > /tmp/perm-657.sh
less /tmp/perm-657.sh          # 先讀過，這份會以 `sh -e` 整份執行
sudo sh -e /tmp/perm-657.sh

# ✅ 落檔：容器 ＋ 三格
ls -ld /var/lib/cortex/coordinator/job-specs \
       /var/lib/cortex/coordinator/job-specs/{builder,reviewer,gate}
#   期望：四行都是 cortex-manager:cortex-manager 0700

# ✅ effective（不是「有沒有 ACL」）——容器只給 traverse
sudo getfacl -p /var/lib/cortex/coordinator/job-specs | grep -E "^(user:|mask::)"
#   期望：三條 user:cortex-*:--x ＋ mask::--x，且**沒有任何 `#effective:` 註記**

# ✅ effective——每格只授自己
for PAIR in builder:cortex-builder reviewer:cortex-reviewer-planner gate:cortex-gate; do
  P="${PAIR%%:*}"; A="${PAIR##*:}"
  echo "--- $P"
  sudo getfacl -p "/var/lib/cortex/coordinator/job-specs/$P" \
    | grep -E "^(user:|default:user:|mask::|default:mask::)"
done
#   期望（逐格）：`user:<自己>:r-x` ＋ `default:user:<自己>:r-x` ＋
#         `mask::r-x` ＋ `default:mask::r-x`，**無 `#effective:` 註記**、
#         **無其他 principal 的條目**。
#   看到 `#effective:---` ⇒ mask 被 chmod 打掉了，重跑上面那份 script。

# ✅ 機械判準（#633）：每格的具名條目**恰好只有自己**，且沒有任何 `#effective:`
#    ——這條抓的是「舊 default ACL 被繼承進新格」，肉眼掃 getfacl 會漏掉。
for PAIR in builder:cortex-builder reviewer:cortex-reviewer-planner gate:cortex-gate; do
  P="${PAIR%%:*}"; A="${PAIR##*:}"
  FOREIGN=$(sudo getfacl -p "/var/lib/cortex/coordinator/job-specs/$P" \
    | grep -E "^(default:)?user:[a-z]" | grep -cv ":$A:")
  EFF=$(sudo getfacl -p "/var/lib/cortex/coordinator/job-specs/$P" | grep -c "#effective:")
  echo "$P foreign=$FOREIGN effective_notes=$EFF"
done
#   期望：三行都是 `foreign=0 effective_notes=0`。
#   ⛔ `foreign>0` ⇒ 舊結構的 default ACL 被繼承進來了（見上面的警語）：
#      `sudo setfacl -k <容器>` 之後把三格砍掉重跑權限計畫。
#      **不要**只 `setfacl -x` 掉那一條——容器的 default 還在，下次新建照樣復發。
#   ⛔ `effective_notes>0` ⇒ mask 陷阱，重跑上面那份 script。

# ✅✅ **正向實測：每個身分讀得到自己的 spec**（本節的核心，不可以只驗檔案存在）
for PAIR in builder:cortex-builder reviewer:cortex-reviewer-planner gate:cortex-gate; do
  P="${PAIR%%:*}"; A="${PAIR##*:}"
  S="/var/lib/cortex/coordinator/job-specs/$P/probe-657.json"
  # Manager 寫（＝正式路徑的寫端：原子寫入 ＋ chmod 0640，讓繼承的 ACL mask 不被關掉）
  sudo -u cortex-manager sh -c "printf '{\"probe\":1}' > $S && chmod 0640 $S"
  # 那個身分讀（＝shim 在 systemd 套完 User= 之後做的事）
  sudo -u "$A" cat "$S" >/dev/null \
    && echo "$A 讀得到自己的 spec：OK" \
    || echo "$A 讀不到自己的 spec：**FAIL（這就是 #657）**"
done

# ✅✅ **反向實測：跨 principal 讀不到彼此的 spec**
sudo -u cortex-gate cat /var/lib/cortex/coordinator/job-specs/builder/probe-657.json 2>&1 | tail -1
sudo -u cortex-builder cat /var/lib/cortex/coordinator/job-specs/gate/probe-657.json 2>&1 | tail -1
sudo -u cortex-reviewer-planner cat /var/lib/cortex/coordinator/job-specs/gate/probe-657.json 2>&1 | tail -1
#   期望：三行 Permission denied

# ✅ 寫入面仍全拒（M1 起的不變式，本票不得放寬）
for PAIR in builder:cortex-builder reviewer:cortex-reviewer-planner gate:cortex-gate; do
  P="${PAIR%%:*}"; A="${PAIR##*:}"
  sudo -u "$A" sh -c "printf x > /var/lib/cortex/coordinator/job-specs/$P/evil.json" 2>&1 | tail -1
  sudo -u "$A" sh -c "printf x >> /var/lib/cortex/coordinator/job-specs/$P/probe-657.json" 2>&1 | tail -1
  sudo -u "$A" rm -f "/var/lib/cortex/coordinator/job-specs/$P/probe-657.json" 2>&1 | tail -1
done
#   期望：九行全部 Permission denied（job 建不了、改不了、刪不掉自己的命令列）

# 🧹 清理
sudo rm -f /var/lib/cortex/coordinator/job-specs/{builder,reviewer,gate}/probe-657.json
```

> **Manager 端還有一道同語意的閘**：`prepare_systemd_template()` 的 preflight 在
> #657 之後檢查的是「該 job 身分的 **effective** 權限」而不是「目錄存在」——它以
> `os.stat` ＋ POSIX ACL xattr 直接算 kernel 用的那條 access check（含 mask 與
> 整條 traverse 鏈），因此上面任何一條 FAIL 都會在**派工之前**變成
> `job-runner-job-spec-spool-unreadable`，而不是 journal 裡的一行 78/CONFIG。
> spec 落地後另有一次就地複驗（`job-runner-job-spec-unreadable-by-job`）。
>
> 那道閘**算不到**的三件事，正是本節 `sudo -u` 步驟不可省略的理由：mount 選項
> （唯讀掛載）、mount namespace（unit 的 `ProtectSystem=strict` 等）、LSM
> （SELinux／AppArmor）。

### 5-4. 安裝 (a) polkit 規則

```bash
# ✅ 先讀（規則檔開頭把邊界與「為什麼 transient 一律拒」逐條寫出來，勿跳過）
python3 -m paulsha_cortex.trust_root polkit four-way --template | tee /tmp/polkit-cortex.rules
less /tmp/polkit-cortex.rules
#   必須確認的五個條件（規則檔自己列在「審查者的一眼結論」段）：
#     (1) subject 是 cortex-manager；(2) action 是 org.freedesktop.systemd1.manage-units；
#     (3) unit／verb 明細存在；(4) verb ∈ {start, stop}；
#     (5) unit 名匹配（六個具名字幹的交替；逐字以產生器輸出為準）
#         ^(?:cortex-job|cortex-job-jit|cortex-reviewer-job|cortex-reviewer-job-jit
#           |cortex-gate-job|cortex-gate-job-jit)@[a-z0-9][a-z0-9._-]{0,62}\.service$
#   **transient unit 的 StartTransientUnit 檢查不帶明細 ⇒ 條件 (3) 直接把它擋掉。**
#   ⚠️ 條件 (5) 的字幹段是**列舉的交替**，而且是**兩層**列舉，**不是**萬用字元：
#        (a) 加固剖面（#643）：一份剖面一個 root-owned 模板檔 ⇒ 兩個後綴；
#        (b) job 角色（#615 M2／#629）：builder、reviewer/planner、gate 是三個不同的
#            UID，而 User= 同樣寫死在檔裡 ⇒ 三個字幹頭。
#      3 × 2 ＝ 六個具名模板。前後仍然錨定、instance 段的字元類一字未改，仍然是
#      **一條規則、一個 YES 出口**——放行面是「六個具名模板」，不是「任意 unit」。
#      看到 `.*`／`[^`／`\w` 出現在字幹段就是被改壞了。
#      六份模板的 User= 全部是無 sudo、無 root、彼此互不可寫的降權服務帳號，
#      因此「多一個字幹」擴大的是**降權目標的選擇**，不是提權面。
#   ⚠️ 若字幹段**沒有** cortex-gate-job ⇒ 你用的是 three-way／two-way 產出的規則，
#      或部署樹比 #629 舊。那份規則裝上去之後 gate unit 一律被 polkit 拒，
#      症狀是每張 build 卡都停在 gate 執行面的診斷碼上。

# 🔧 sudo：落檔
sudo install -o root -g root -m 0644 /tmp/polkit-cortex.rules \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ✅ 驗證：載入無語法錯誤、與產生器逐位元相同
sudo journalctl -u polkit -n 30 --no-pager | grep -Ei "error|syntax" || echo "polkit loaded clean: OK"
diff <(python3 -m paulsha_cortex.trust_root polkit four-way --template) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules && echo "polkit in sync: OK"

# ✅ 驗證：規則的 subject 是 cortex-manager，且殘餘風險清單為空（template 方案）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
rule = permgen.build_polkit_rule(
    permgen.SCHEMES["four-way"], plan=permgen.PolkitPlan.TEMPLATE
)
print("subject       :", rule.subject_account)
print("targets       :", rule.target_accounts)
print("unit_pattern  :", rule.unit_pattern)
print("allowed_verbs :", rule.allowed_verbs)
print("residual_risks:", rule.residual_risks or "(none — OS 層封閉)")
print("grants        :", rule.content.count("polkit.Result.YES"),
      "addRule:", rule.content.count("polkit.addRule("))
PY
#   期望：subject=cortex-manager、
#         targets=('cortex-builder', 'cortex-reviewer-planner')（#615 M2）、
#         verbs=('start','stop')、residual_risks 為空、grants=1、addRule=1。
#   ⚠️ grants 或 addRule 不是 1 ⇒ 停下來：這份規則檔的可審查性性質就是
#      「全檔只有一個放行出口」。
```

### 5-5. (d) 打開切換點 `PSC_JOB_RUNNER=systemd-template`

```bash
# ✅ 先取 PSC_BUILDER_PATH 的正規值（產生器＝單一真相，**不要手打**）
python3 -m paulsha_cortex.trust_root unit four-way --job | grep PSC_BUILDER_PATH
#   期望：PSC_BUILDER_PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin

# ✅ #615：reviewer／planner 那一組（**與 builder 不共用**，見下方說明）
python3 -m paulsha_cortex.trust_root unit four-way --review-job | grep PSC_REVIEWER_PATH
#   期望：PSC_REVIEWER_PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin

# ✅ #629：gate 執行身分那一組。**PATH 刻意不含 toolchain**——gate 不跑任何模型 CLI，
#    它跑的是 operator 宣告的 gate 命令（pytest／make／npm…）。把 toolchain 排進去
#    只會多開一條「gate 也能起模型」的面，換不到任何東西。
python3 -m paulsha_cortex.trust_root unit four-way --gate-job | grep PSC_GATE_PATH

# 🔧 sudo：把降權模式寫進第 4b 步的 EnvironmentFile
sudo tee -a /opt/cortex/etc/cortex-manager.env >/dev/null <<'ENVFILE'
PSC_JOB_RUNNER=systemd-template
PSC_BUILDER_ACCOUNT=cortex-builder
PSC_BUILDER_HOME=/var/lib/cortex-builder
PSC_BUILDER_PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin
PSC_REVIEWER_ACCOUNT=cortex-reviewer-planner
PSC_REVIEWER_HOME=/var/lib/cortex-reviewer-planner
PSC_REVIEWER_PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin
# --- #629 gate 執行身分 ---
PSC_GATE_ACCOUNT=cortex-gate
PSC_GATE_HOME=/var/lib/cortex-gate
PSC_GATE_PATH=/usr/local/bin:/usr/bin:/bin
# gate 用來跑 ledger writer 的直譯器。必須是**絕對路徑且 gate 讀得到**——
# Manager 的 repo root 在 /home 底下，而 job unit 帶 ProtectHome=yes，
# `PYTHONPATH=<repo>` 那條路在那裡不成立（#623 缺口 1 的同一件事）。
PSC_GATE_PYTHON=/opt/cortex/venv/bin/python3
# gate 的加固剖面（operator 平面的決定）。宣告了 node 型 gate（npm test）才改 jit。
PSC_GATE_HARDENING_PROFILE=strict
ENVFILE
sudo systemctl restart cortex-manager.service

# ✅ 驗證（#615）：兩個角色各自解析到自己的帳號與模板，**不會互相污染**
#   `sh -c` 的腳本來自 argv，stdin 原樣留給下面的 heredoc（`python -` 讀得到）。
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -
' <<'PY'
import os
from paulsha_cortex.coordinator import job_runner as jr
for role in jr.JOB_ROLES:
    print(f"{role:8s} account={jr.resolve_job_account(os.environ, role=role):24s} "
          f"template={jr.resolve_template_unit(os.environ, role=role)}")
PY
#   期望：
#     builder  account=cortex-builder           template=cortex-job@.service
#     review   account=cortex-reviewer-planner  template=cortex-reviewer-job@.service
#     gate     account=cortex-gate              template=cortex-gate-job@.service
#   ⚠️ review 那行若是 cortex-builder ⇒ 停下來：reviewer 會以 builder 身分起跑，
#      而 reviewer 正是寫 verdict 的那一個——那等於把 verdict 通道交還給 builder。
#   ⚠️ gate 那行若與其他兩行**任一個相同** ⇒ 立即停止（#629）：三種併法各自對應
#      一條已知提權路徑（模型自證／授權線內側／verdict 通道），而且都看起來是成功的。

# ✅ 驗證（#629）：gate 的直譯器對 gate 帳號真的可執行
sudo -u cortex-gate /opt/cortex/venv/bin/python3 -c \
  "import paulsha_cortex.coordinator.gate_ledger as g; print('gate ledger writer OK')"
#   ⚠️ ImportError／Permission denied ⇒ 停下來：gate 起得來但跑不了 ledger writer，
#      症狀會是每張 build 卡都停在 gate-spool-empty（空輸出型失敗，最難查）。

# ✅ 驗證：模式確實被解析成 template（值非法必須 fail-closed，不得靜默當成 direct）
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -c "import os; from paulsha_cortex.coordinator import job_runner; print(job_runner.resolve_runner_mode(os.environ))"
'
#   期望：systemd-template
```

> `PSC_JOB_RUNNER` 預設 `direct`＝不降權；值非法時**fail-closed**（不會靜默當成
> `direct`）。
>
> **`PSC_BUILDER_PATH` 是必填，不再是選配（#640 改）**：未設時 job 會拿到 Manager
> 轉發的 `PATH`，而 Manager 以 system unit 跑、拿到的是 systemd 的預設 `PATH`
> （`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`）——裡面**沒有**
> `/opt/cortex/toolchain/bin`。toolchain 必須排在最前面：系統層可能另有一份同名但舊
> 很多的 CLI（本機實測兩份 `codex` 差 100 個以上小版本），排後面的症狀是「跑得起來
> 但版本不是預期的那個」。
>
> **為什麼是 `PSC_BUILDER_PATH` 而不是模板 unit 的 `Environment=PATH=`**：模板 unit
> 的 `ExecStart` 是 root-owned shim，shim 以 `execvpe(argv[0], argv, spec['env'])`
> **整份換掉**環境——job 解析命令用的 `PATH` 來自 **spec 的 env**（即 Manager 端這個
> 變數），不是 unit 的 `Environment=`。寫在 unit 上只會是一個看起來承載作用、實際被
> shim 丟掉的設定。產生的 job unit 內有一段註解把這個取捨寫在產物本身。
>
> **憑證**：見第 4e 步——由 root 複製進 job 帳號 HOME、chown 給該帳號（0600），
> **不是**讓 builder 自己 `login`（toolchain 未落位前 `login` 這個動作本身就跑不起來，
> 而且 job 的 HOME 是 root-owned，CLI 也建不了 `auth.json`）。Manager 不會在執行期把
> 自己的憑證傳過去——job 的登入態是一次性的部署動作。
> **#615 M2 起**：`PSC_JOB_RUNNER` 對**三個** persona 都生效。persona 不再決定
> 「降不降權」，只決定**降到哪個角色**（builder／review）——判定點在
> `launcher.SubprocessLauncher._job_role()`，由 launcher 的建構契約導出，job 側碰不到。
>
> **角色判定的三個來源（缺一即誤判）**：`review_only`（workflow lane reviewer）、
> `read_only`（planner）、**`verdict_spool_dir is not None`（slice lane 的 foreign
> reviewer）**。第三條容易漏：foreign reviewer 走
> `manager._spool_writable_launcher()` → `as_verdict_spool_writer()`，而那支工廠產出的
> launcher 前兩個旗標**都是 False**（verdict spool 放行與 read-only 契約互斥）。只看前
> 兩條會把它判成 builder 並以 `cortex-builder` 起跑——**而它正是寫 verdict 的那一個**。

### 5-6. 正向驗證（**必須成功**）

> **這一段只驗隔離，驗不了功能**（#645 的教訓）：底下的 worktree 是 operator
> **手工**建在 `/var/lib/cortex/worktree/$JOB`——也就是刻意挑了一個與 instance 名
> 相符的路徑。真實派工的工作區由 `seams.ScriptWorktreeCreator.create()` 產生，
> instance 名由 `job_runner.prepare_systemd_template()` 產生；#645 之前這兩條鏈各自
> 導出、永遠差一個 `feature-` 前綴，而手工組 spec 恰好把它繞過去。因此本步驟通過
> **不代表**降權派工能起得來——請務必另跑一次**真實 dispatch 路徑**的功能 smoke。
> 兩條鏈現已收斂到 `coordinator/job_workspace.py` 的 `job_segment()` 單一推導點，
> 並由 `tests/test_worktree_dir_naming_645.py` 的不變式守著。

```bash
JOB=selftest

# 🔧 sudo：job worktree（builder 擁有——job 的 log 與工作區都落在這裡）
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

# 🔧 sudo：寫 job-spec —— **一律用 build_job_spec()／write_job_spec() 產生**，
#   不要自行捏造欄位（手捏的 spec 會被 shim 的白名單 schema 擋掉，而且
#   `spec_version` 一旦對不上就直接 fail-closed）。
#   spec 路徑＝<spool>/<instance>.json，由 job_spec_path() 推導，不手寫。
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$JOB" <<'PY'
import sys
from paulsha_cortex.coordinator import job_runner

instance = sys.argv[1]
# #657：spool 是 per-principal 的——`role=` 決定寫進哪一格。
spool = job_runner.resolve_job_spec_spool({}, role=job_runner.JOB_ROLE_BUILDER)
account = "cortex-builder"
smoke = (
    'echo "== identity =="; id; '
    'echo "== tokens =="; echo "GH_TOKEN=[$GH_TOKEN] GITHUB_TOKEN=[$GITHUB_TOKEN]"; '
    'echo "== inherited fds =="; ls -l /proc/self/fd; '
    'echo "== home =="; echo "HOME=$HOME"; ls -ld "$HOME"; '
    'echo "== deployment writable? =="; (printf x >> /opt/cortex/venv/bin/cortex) 2>&1 | tail -1; '
    'echo "== spec spool writable? =="; '
    '(printf x > /var/lib/cortex/coordinator/job-specs/builder/evil.json) 2>&1 | tail -1'
)
spec = job_runner.build_job_spec(
    job_id=f"{instance}-smoke",
    instance=instance,
    unit=f"cortex-job@{instance}.service",
    command=["/bin/sh", "-c", smoke],
    working_directory=f"/var/lib/cortex/worktree/{instance}",
    log_path=f"/var/lib/cortex/worktree/{instance}/{instance}.log",
    # ⚠️ job 的 env **就是**這一份，不繼承 unit 的 Environment=——shim 是
    #    `execvpe(command, spec["env"])`。因此 PATH／HOME 要在這裡給，
    #    而 token 類的名字 build_job_spec() 直接拒收（見下方註）。
    env={"HOME": "/var/lib/cortex-builder", "PATH": "/usr/local/bin:/usr/bin:/bin"},
)
# `account=` 讓寫端在落地後就地複驗「那個身分讀得到這個檔」（#657）——
# 失敗會是一個 Manager 端的清楚錯誤，而不是 journal 裡的 78/CONFIG。
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(spool, instance), spec, account=account))
PY
#   ↑ 以 **cortex-manager 身分**寫——spool 是 Manager-owned，這一步本身就在證明
#     「writer 只有 Manager」；若這裡就 Permission denied，代表第 2 步權限沒套好。
#
#   ▸ **token 的保證比「scrub」更強**：job 的 env 完全等於 spec 的 `env` 欄位，
#     而 `build_job_spec()` 對 `*TOKEN*`／`*SECRET*`／`*API_KEY*` 這類名字
#     （`CREDENTIAL_ENV_RE`）與 `LD_PRELOAD`／`PYTHONPATH` 這類名字
#     （`DENIED_ENV_NAMES`）**在寫入端就 raise**——不是執行時清掉，是根本進不了 spec。
#     可以當場驗一次這條守衛（期望：拋 DiagnosticReason，spec 不會被寫出）：
#       … build_job_spec(..., env={"GH_TOKEN": "x"})

# ✅ 正向：以 cortex-manager 身分起 instance——**必須成功**
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"

# ✅ job 的輸出在 **spec 的 log_path**，不在 journal
#   （shim 在**已降權之後**用 O_NOFOLLOW 接管 stdout/stderr；journal 只承接
#    「接管之前」的 shim 診斷，例如 spec 缺席／schema 不合。）
sudo cat "/var/lib/cortex/worktree/$JOB/$JOB.log"
#   期望輸出：
#     uid=…(cortex-builder) gid=…(cortex-builder)   ← User= 由 OS 強制，不是呼叫端選的
#     GH_TOKEN=[] GITHUB_TOKEN=[]                    ← 兩個名字根本進不了 spec
#     /proc/self/fd 只有 0/1/2                        ← 無指向受保護資產的可寫 fd（R9 T4.1）
#     HOME=/var/lib/cortex-builder，且該目錄為 root:root
#     deployment writable? → Permission denied / Read-only file system
#     spec spool writable? → Permission denied      ← builder 改不了自己的命令列
sudo journalctl -u "cortex-job@$JOB.service" -n 20 --no-pager
#   期望：**沒有** shim 的錯誤（`job spec …` 開頭的訊息代表 spec 有問題）。

# ✅ 正向：停也必須成功（polkit 放行的兩個 verb）
sudo -u cortex-manager systemctl stop "cortex-job@$JOB.service"; echo "exit=$?"   # 期望 0
```

### 5-6b. 正向驗證（**reviewer／planner 模板**，#615 M2）

> **與 5-6 逐條同構，只換模板名與帳號。** 它要證明的是一件 M1 完全沒有證據的事：
> reviewer 的 job **不是**以 `cortex-manager` 跑的，也**不是**以 `cortex-builder` 跑的。
>
> 同 5-6 的誠實邊界：這一段是手工 spec，**只驗隔離、驗不了功能**；功能面由第 8b-2 步
> 的真實 dispatch 驗。

```bash
RJOB=selftest-review

# 🔧 sudo：寫 reviewer 的 job-spec（同樣以 cortex-manager 身分寫）
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$RJOB" <<'PY'
import sys
from paulsha_cortex.coordinator import job_runner

instance = sys.argv[1]
# #657：spool 是 per-principal 的——`role=` 決定寫進哪一格。
spool = job_runner.resolve_job_spec_spool({}, role=job_runner.JOB_ROLE_BUILDER)
account = "cortex-builder"
smoke = (
    'echo "== identity =="; id; '
    'echo "== tokens =="; echo "GH_TOKEN=[$GH_TOKEN] GITHUB_TOKEN=[$GITHUB_TOKEN]"; '
    'echo "== home =="; echo "HOME=$HOME"; ls -ld "$HOME"; '
    'echo "== verdict spool writable? =="; '
    'D=/var/lib/cortex/coordinator/review-verdicts/probe; '
    '(mkdir -p "$D" && printf "{}" > "$D/verdict.json" && echo "verdict write OK") 2>&1 | tail -1; '
    'echo "== builder workspace writable? =="; '
    '(printf x > /var/lib/cortex/worktree/evil) 2>&1 | tail -1; '
    'echo "== commit spool writable? =="; '
    '(printf x > /var/lib/cortex/coordinator/commit-spool/evil) 2>&1 | tail -1; '
    'echo "== source tree writable? =="; '
    '(printf x > /var/lib/cortex/repos/evil) 2>&1 | tail -1; '
    'echo "== gate ledger writable? =="; '
    '(printf x > /var/lib/cortex/runtime/dispatch/evil) 2>&1 | tail -1; '
    'echo "== source tree readable? =="; ls /var/lib/cortex/repos >/dev/null 2>&1 '
    '&& echo "source tree readable OK" || echo "source tree NOT readable"'
)
spec = job_runner.build_job_spec(
    job_id=f"{instance}-smoke",
    instance=instance,
    unit=f"cortex-reviewer-job@{instance}.service",
    command=["/bin/sh", "-c", smoke],
    # reviewer 的工作樹**不在** pool 底下；這裡用 unit 的 WorkingDirectory（恆存在）。
    working_directory="/var/lib/cortex/worktree",
    log_path=f"/var/lib/cortex-reviewer-planner/cache/{instance}.log",
    env={
        "HOME": "/var/lib/cortex-reviewer-planner",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    },
)
# `account=` 讓寫端在落地後就地複驗「那個身分讀得到這個檔」（#657）——
# 失敗會是一個 Manager 端的清楚錯誤，而不是 journal 裡的 78/CONFIG。
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(spool, instance), spec, account=account))
PY

# ✅ 正向：以 cortex-manager 身分起 reviewer instance——**必須成功**
sudo -u cortex-manager systemctl start "cortex-reviewer-job@$RJOB.service"; echo "exit=$?"

sudo cat "/var/lib/cortex-reviewer-planner/cache/$RJOB.log"
#   期望輸出（逐條）：
#     uid=…(cortex-reviewer-planner) gid=…(cortex-reviewer-planner)
#         ← **這一行就是 M2 的全部**：不是 cortex-manager，也不是 cortex-builder
#     GH_TOKEN=[] GITHUB_TOKEN=[]
#     HOME=/var/lib/cortex-reviewer-planner，且該目錄為 root:root
#     verdict write OK                      ← 正向：verdict 通道通
#     builder workspace writable? → Permission denied / Read-only file system
#     commit spool  writable? → Permission denied / Read-only file system
#     source tree   writable? → Permission denied / Read-only file system
#     gate ledger   writable? → Permission denied / Read-only file system
#     source tree readable OK               ← 唯讀可達（review 要讀 candidate）
#   ⚠️ 任何一條「writable?」印出成功 ⇒ 停下來：RWP 或 ACL 有一層沒套上。
#   ⚠️ `verdict write OK` 沒出現 ⇒ 停下來：verdict 通道在三分下不通，
#      這正是 #638 缺陷 1（mkdir 重設 ACL mask）與父層 traverse ACL（#620）的症狀。

# 🔧 清理
sudo -u cortex-manager systemctl stop "cortex-reviewer-job@$RJOB.service" 2>/dev/null
sudo rm -rf /var/lib/cortex/coordinator/review-verdicts/probe
sudo rm -f "/var/lib/cortex/coordinator/job-specs/reviewer/$RJOB.json" \
           "/var/lib/cortex-reviewer-planner/cache/$RJOB.log"
```

### 5-7. 反向驗證（**11 條全部必須被拒**，＋#643 的第 12 條）

> **#643 起每一條都要對「兩個」字幹跑一次，#615 M2 起是「四個」**：加固剖面帶來
> `-jit` 後綴（#643），job 角色帶來 `cortex-reviewer-job` 字幹頭（#615）。放行面因此是
> 「四個具名模板」——**不是**「任意 unit」——原本 11 條的每一條在**每一個**新字幹上
> 必須有**完全相同**的結果。下面在 (5)(7)(8)(9)(10) 用 `$STEMS` 一次涵蓋四個，
> (7) 增加圍繞兩個新字幹的混淆形式，並有 (12)「Manager 選不了剖面」三小條。
>
> **不要只跑其中一個字幹。** 「新增一個放行的名字」最容易被鑽的就是它周邊的混淆面，
> 而那個面只有在對**新**字幹逐條重跑時才驗得到。

```bash
# ✅ (0) 四個字幹的變數化（下面的條目共用；**不要只跑其中一個**）
#    直接由產生器導出，不手打——手打與產生器漂移時，驗的就不是實際落檔的那組。
STEMS=$(python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
print(" ".join(permgen.job_unit_stems(
    permgen.DEFAULT_LAYOUT, permgen.DOWNGRADED_JOB_PRINCIPALS)))
PY
)
echo "STEMS=$STEMS"
#   期望：cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit

# ✅ (1) 以 cortex-manager 起 transient unit（不指定 UID）：必須被拒
sudo -u cortex-manager systemd-run --pipe --wait /bin/id; echo "exit=$?"

# ✅ (2) 以 cortex-manager 起 transient unit --uid=0：必須被拒
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "exit=$?"
#   期望：非 0；訊息含 "Interactive authentication required" 或 "Access denied"
#   **這一條就是 A+B 相對於單獨 A 多出來的保證**：polkit 沒授 transient 建立。

# ✅ (3) 以 cortex-manager 起 transient unit --uid=cortex-builder：**同樣**必須被拒
sudo -u cortex-manager systemd-run --uid=cortex-builder --pipe --wait /bin/id; echo "exit=$?"

# ✅ (4) transient unit 夾帶特權屬性：必須被拒
sudo -u cortex-manager systemd-run --uid=cortex-builder \
     --property=AmbientCapabilities=CAP_SETUID --pipe --wait /bin/id; echo "exit=$?"

# ✅ (5) 借用 template 名字的 transient unit：必須被拒（明細缺席即拒）
for S in $STEMS; do
  sudo -u cortex-manager systemd-run --unit="$S@evil.service" --uid=0 \
       --pipe --wait /bin/id; echo "(5) $S exit=$?"
done

# ✅ (6) 起別的既存 unit（含 Manager 自己、sshd）：必須被拒
sudo -u cortex-manager systemctl restart cortex-manager.service; echo "exit=$?"
sudo -u cortex-manager systemctl start sshd.service 2>&1 | tail -1; echo "exit=$?"

# ✅ (7) 名稱夾帶（前綴／後綴混淆）：必須被拒
#     前兩條是原本的；接著八條是 #643 `-jit` 周邊的混淆面；
#     最後十條是 #615 `cortex-reviewer-job` 周邊的混淆面
#     （**新增一個字幹，最容易被鑽的就是這裡**）
for BAD in \
    "evil-cortex-job@x.service" \
    "cortex-job@x.service.evil" \
    "evil-cortex-job-jit@x.service" \
    "cortex-job-jit@x.service.evil" \
    "cortex-job-jitx@x.service" \
    "cortex-job-ji@x.service" \
    "cortex-job-jit-evil@x.service" \
    "cortex-jit-job@x.service" \
    "cortex-job-jit@.service" \
    "cortex-job-jit@X.service" \
    "evil-cortex-reviewer-job@x.service" \
    "cortex-reviewer-job@x.service.evil" \
    "cortex-reviewer-jobs@x.service" \
    "cortex-reviewer-jo@x.service" \
    "cortex-reviewer-job-evil@x.service" \
    "cortex-job-reviewer@x.service" \
    "cortex-reviewer@x.service" \
    "cortex-reviewer-planner-job@x.service" \
    "cortex-reviewer-job@.service" \
    "cortex-reviewer-job@X.service"; do
  sudo -u cortex-manager systemctl start "$BAD" 2>/dev/null; echo "(7) $BAD exit=$?"
done
#   期望：**20 條全部非 0**。`cortex-reviewer-planner-job@` 那條特別重要——
#   把帳號名當字幹是最直覺的猜法，而它不在放行的四個字幹裡。

# ✅ (8) 其他 verb：必須被拒
for S in $STEMS; do
  sudo -u cortex-manager systemctl mask "$S@$JOB.service"; echo "(8) $S mask exit=$?"
  sudo -u cortex-manager systemctl set-property "$S@$JOB.service" User=root
  echo "(8) $S set-property exit=$?"
done
sudo -u cortex-manager systemctl daemon-reload; echo "(8) daemon-reload exit=$?"

# ✅ (9) 非授權帳號起 job instance：必須被拒（polkit subject 只有 cortex-manager）
for S in $STEMS; do
  sudo -u cortex-reviewer-planner systemctl start "$S@$JOB.service"; echo "(9) $S rp exit=$?"
  sudo -u cortex-builder systemctl start "$S@$JOB.service"; echo "(9) $S builder exit=$?"
done

# ✅ (10) 改 template unit／shim／polkit 規則：三個服務帳號一律 EACCES
#     **四份 unit 逐一試**——漏掉一份就等於那一份的 User= 沒被證明是不可竄改的。
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  for S in $STEMS; do
    sudo -u "$U" sh -c "printf 'User=root\n' >> /etc/systemd/system/$S@.service"
    echo "(10) $U $S User= exit=$?"
    sudo -u "$U" sh -c "printf 'MemoryDenyWriteExecute=no\n' >> /etc/systemd/system/$S@.service"
    echo "(10) $U $S MDWE exit=$?"
    sudo -u "$U" sh -c "printf 'ReadWritePaths=/\n' >> /etc/systemd/system/$S@.service"
    echo "(10) $U $S RWP exit=$?"
  done
  sudo -u "$U" sh -c 'printf "id\n" >> /opt/cortex/bin/cortex-job-shim'; echo "(10) $U shim exit=$?"
  sudo -u "$U" sh -c 'printf "x\n" >> /etc/polkit-1/rules.d/49-cortex-downgrade.rules'; echo "(10) $U polkit exit=$?"
done
#   期望：全部非 0（3 帳號 × (4 unit × 3 + 2) ＝ 42 條）。

# ✅ (11) 負控制：暫時移除 polkit 規則後，dispatch 必須 fail-closed 而非退回 direct
sudo mv /etc/polkit-1/rules.d/49-cortex-downgrade.rules /tmp/polkit-cortex.disabled
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"; echo "exit=$?"   # 期望非 0
#   → 再觸發一次真正的 dispatch，期望：job 落 needs_human，理由碼指向
#     job-runner 的 unit-start-failed 家族，detail 帶 systemctl 的實際拒絕訊息。
#     **不得**出現以 cortex-manager 身分跑起來的 job。
sudo mv /tmp/polkit-cortex.disabled /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ✅ (12) #643：Manager **選不了**加固剖面——剖面只跟著 executor 走
#     這三條守的是「per-executor 剖面」不退化成「全域移除 MDWE」。
#     ⚠️ 與其他反向條目相反：這三條**期望 exit=0**（0 代表「拒絕確實發生」）。

#     (12a) PSC_JOB_TEMPLATE_UNIT 不接受已帶剖面後綴的值
#           （接受的話，operator 一行 config 就能把**所有** job 推到寬鬆剖面）
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
export PSC_JOB_TEMPLATE_UNIT=cortex-job-jit@.service
exec /opt/cortex/venv/bin/python -
' <<'PY'
import os, sys
from paulsha_cortex.coordinator import job_runner as j
try:
    j.prepare_systemd_template(os.environ, job_id="probe", executor="claude")
except j.JobRunnerError as exc:
    print("refused:", exc.diagnostic.reason); sys.exit(0)
sys.exit(1)
PY
echo "(12a) exit=$?"
#   期望：exit=0，印出 refused: job-runner-template-unit-invalid

#     (12b) 未登記的 executor fail-closed
#           （**不得**落到寬鬆那份；也不得默默給嚴格那份而讓問題再埋一次）
sudo -u cortex-manager /opt/cortex/venv/bin/python -c '
import sys
from paulsha_cortex.coordinator import job_runner as j
try:
    j.resolve_hardening_profile("mystery")
except j.JobRunnerError as exc:
    print("refused:", exc.diagnostic.reason); sys.exit(0)
sys.exit(1)
'; echo "(12b) exit=$?"
#   期望：exit=0，印出 refused: job-runner-hardening-profile-unknown

#     (12c) job spec 帶剖面欄位 ⇒ shim 讀端拒絕執行（寫端已在單元測試覆蓋）
sudo -u cortex-manager /opt/cortex/venv/bin/python -c '
import json, sys
from paulsha_cortex.coordinator import job_shim
spool = "/var/lib/cortex/coordinator/job-specs/builder"
spec = {"spec_version": 1, "instance": "profile-probe", "job_id": "profile-probe",
        "unit": "cortex-job@profile-probe.service", "command": ["/bin/true"],
        "working_directory": "/tmp", "log_path": "/tmp/probe.jsonl",
        "env": {"PATH": "/usr/bin"}, "hardening_profile": "jit"}
open(spool + "/profile-probe.json", "w").write(json.dumps(spec))
try:
    job_shim.load_spec("profile-probe", spool)
except job_shim.ShimError as exc:
    print("refused:", exc); sys.exit(0)
sys.exit(1)
'; echo "(12c) exit=$?"
sudo rm -f /var/lib/cortex/coordinator/job-specs/builder/profile-probe.json
#   期望：exit=0，印出 refused: ... hardening_profile ...
```

**通過條件**：5-6 正向成功且輸出符合期望；**5-2b 的 (1) 正向與 (2) 負向對照皆符合
期望**；5-7 的 (1)–(11) **全部**非 0 退出、(12a)(12b)(12c) **全部** 0 退出。
任一反向測試通過（即攻擊成功）＝**立即停止**，回到第 9 步回滾。

```bash
# 🔧 sudo：清掉 selftest 殘留（spec 是一個檔，worktree 是一個目錄）
sudo systemctl stop "cortex-job@$JOB.service" 2>/dev/null || true
sudo rm -f "/var/lib/cortex/coordinator/job-specs/builder/$JOB.json"
sudo rm -rf "/var/lib/cortex/worktree/$JOB"
sudo systemctl reset-failed "cortex-job@*" "cortex-job-jit@*" 2>/dev/null || true
```

### 5-8. 殘餘風險（A+B 之後重新評估）

**已被關掉的**（第二輪時還是殘餘）：

| 舊殘餘 | A+B 之後 |
|---|---|
| 「polkit 看不到 `User=`，授權後可請求任意 UID」 | **不再成立**——polkit 根本沒授權 transient 建立；唯一能起的是 root-owned template 的 instance，`User=` 在 root-owned 檔內寫死。5-7 (2)(3) 實測 |
| 「跑模型的 reviewer／planner 與 Manager 併帳，其中任一被攻陷即取得 grant」 | **不再成立**——三分把它們移到 `cortex-reviewer-planner`；polkit subject 只有 `cortex-manager`，而它**不跑模型程式碼**。5-7 (9) 實測 |
| 「授權帳號被攻陷即可提權」 | **降級為「無法向上」**——`cortex-manager` 即使被完全攻陷，也改不了 `User=`（unit 是 root-owned）、傳不了屬性（transient 被拒）、換不掉 argv 入口（shim 是 root-owned）。它能做的上界是「以 `cortex-builder` 身分起 job」，而那正是設計要它做的事 |

**polkit 粗粒度仍在，但已不構成提權**：規則只能看到 unit 名與 verb，**這在 A+B 下
足夠**——因為被授權的 unit 名 pattern 只對應一個 root-owned template，該 template 的
每一個安全屬性都不由呼叫端提供。粗粒度授權的「粗」落在 instance 名（`%i`）上，
而 `%i` 只被用作 spool 路徑與 worktree 路徑的 segment，且 pattern 已把字元集錨定為
`[a-z0-9][a-z0-9._-]{0,62}`（無 `/`、無 `..` 起頭）。

**剩下的殘餘 = `cortex-manager` 帳號的 supply-chain 類**：

| 殘餘 | 具體形狀 | 現有緩解 | 缺口 |
|---|---|---|---|
| **部署樹供應鏈** | 惡意相依／被竄改的 wheel 進到 `/opt/cortex/venv`，之後以 `cortex-manager` 身分執行 | `/opt/cortex` 全 root-owned、對服務唯讀（4a）；升級走第 6 步 operator 手動驗證＋hash diff；**不 codify** root 命令（裁決 6） | 無簽章驗證（屬 **Phase 3**）；hash diff 靠 operator 目視 |
| **Manager 自身邏輯被攻陷** | Manager 程式碼路徑被誘導寫出惡意 job-spec | root-owned shim 限定 argv 形狀（5-3）；spec 的 schema 是**白名單**且身分欄位 fail-closed（寫端 `build_job_spec()`、讀端 `job_shim.load_spec()` 各驗一次）；job 仍降到 `cortex-builder`、拿不到 token | shim 只能保證「身分／入口不可選」，**不**保證 command 內容良性——惡意 spec 仍可讓 builder 跑任意命令（上界＝builder 權限）。這條要靠 Manager 端的派工邏輯與 R9 族 2 的檔案邊界共同壓住 |
| **operator 帳號** | 有 `sudo`，可改任何東西 | 設計上信任邊界之外（本 runbook 全部 root 操作都由 operator 親自輸入） | 不在本階段範圍 |
| **polkit 不可用** | polkit 掛掉 ⇒ 全部 job 起不來 | fail-closed（安全但功能全停）；執行前提第 6 項＋WSL2 段第 5 項複驗 | 需監控，否則表現為「靜默停擺」 |
| ~~**M2 未完成**~~ | ~~reviewer／planner 仍在 Manager 行程內以 `cortex-manager` 身分跑~~ | **已關閉（#615）**：三個會跑模型的 persona 啟動面全部離開 Manager 的 UID；5-6b／8b-2 為其驗收 | — |
| **gate 執行身分**（#629） | gate 命令在 builder 掌控的 worktree 裡跑，`pytest` 載入該 worktree 的 `conftest.py` ⇒ 執行者取得任意程式碼執行 | 降權模式下 job **不跑 gate**（`_should_run_gates()` 對三個 persona 皆 False），build 卡對 `require_ledger` **fail closed**——沒有獨立證據就不採信 | 需要**第四個帳號**（既非 builder 也非 reviewer／planner，更非 Manager）。**刻意不掛在 `cortex-reviewer-planner` 上**：那會讓被攻陷的 builder 經由 gate 執行影響寫 verdict 的帳號，抵銷 #638／#639。屬 #629 |
| **reviewer 憑證無法就地 refresh** | `cortex-reviewer-planner` 的 executor 憑證不在其模板 unit 的 `ReadWritePaths=` 內 | 憑證檔本身由 root 於第 4e 步放置並 chown（0600），父目錄 root-owned；讀取不受影響 | token 過期需 operator 重跑第 4e 步。登記表目前只登記 `builder-executor-credential` 一份，理由見該資產 note（二分部署上登記第二份會讓 Manager unit 的 RWP 指向不存在的路徑而起不來） |

> **記錄要求**：完成第 5 步後，把 5-7 的 12 組 exit code（**四個字幹各一輪**）、
> 5-4 的 `targets`／`residual_risks`／`grants` 輸出、5-6b 的 `id` 那一行、
> 以及 8b-2 的 (3)(4)(5)(7) 四組結果貼到 #584。D6 的通過判定引用這份紀錄。

### 5-9. 產生邏輯的離線對照

```bash
# ✅ polkit 無法本機模擬時的第二證據：決策矩陣測試
python3 -m pytest tests/test_trust_root_permgen_p2b.py -q -k polkit

# ✅ 決策矩陣的手動抽點（與 5-7 的實機結果應逐條一致）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
rule = permgen.build_polkit_rule(
    permgen.SCHEMES["four-way"], plan=permgen.PolkitPlan.TEMPLATE
)
cases = [
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "start", "YES"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "stop",  "YES"),
    ("cortex-manager",          permgen.POLKIT_ACTION, None,                     None,    "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-manager.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "evil-cortex-job@x.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "mask",  "NO"),
    # #643：第二個加固剖面的模板同樣放行；圍繞它的混淆同樣拒。
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job-jit@abc.service", "start", "YES"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job-jitx@abc.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job-ji@abc.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job-jit-abc.service", "start", "NO"),
    ("cortex-reviewer-planner", permgen.POLKIT_ACTION, "cortex-job@abc.service", "start", "NOT_HANDLED"),
    ("cortex-builder",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "start", "NOT_HANDLED"),
]
for user, action, unit, verb, want in cases:
    got = permgen.evaluate_polkit(rule, user=user, action_id=action, unit=unit, verb=verb)
    print(f"{'OK ' if got == want else '!! '}{user:24} {str(unit):28} {str(verb):6} -> {got} (want {want})")
PY
#   期望：全部 OK。`NOT_HANDLED` 代表交回 polkit 預設 ⇒ 無授權 ⇒ 實機被拒（5-7 (9) 已實測）。
```

**回滾（第 5 步整段）**：
```bash
sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules \
           /etc/systemd/system/cortex-job@.service \
           /etc/systemd/system/cortex-job-jit@.service \
           /opt/cortex/bin/cortex-job-shim
sudo systemctl daemon-reload
sudo systemctl restart polkit.service 2>/dev/null || true
# 並把 PSC_JOB_RUNNER 那三行移出 EnvironmentFile（回 direct）：
sudo sed -i '/^PSC_JOB_RUNNER=/d;/^PSC_BUILDER_ACCOUNT=/d;/^PSC_BUILDER_HOME=/d' \
     /opt/cortex/etc/cortex-manager.env
sudo systemctl restart cortex-manager.service
```
降權停用後 Manager 以 `PSC_DEGRADED_OPERATION=per-case-approval` 不 spawn job 運轉。

---

## 第 6 步：升級流程（**不 codify**——手動 runbook，cortex 只產生字串）

裁決 6：**不**提供 `cortex install trust-root --system` 子命令。把特權操作寫進
codebase 等於把提權路徑收進攻擊面內；cortex 只負責產生命令字串與驗證，root 由
operator 手動執行。升級因此是下列固定流程：

```bash
# ✅ 1. 在 operator 帳號的 pipx 環境驗新版（完全不碰 /opt/cortex）
pipx upgrade paulsha-cortex     # 或既有 build 流程
"$HOME/.local/share/pipx/venvs/paulsha-cortex/bin/cortex" --version

# ✅ 2. 差異對照（新舊部署的內容 hash）——供應鏈殘餘風險的唯一人工關卡
( cd "$HOME/.local/share/pipx/venvs/paulsha-cortex" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > /tmp/cortex-new.sha
( cd /opt/cortex/venv && sudo find . -type f -print0 | sort -z | sudo xargs -0 sha256sum ) > /tmp/cortex-cur.sha
diff <(sort /tmp/cortex-cur.sha) <(sort /tmp/cortex-new.sha) | head -50
#   ⚠️ 預期會有**兩類與版本無關的固定差異**（第 4a／本步 3a-3b 造成的，不是供應鏈訊號）：
#     (a) `./bin/*` —— 現行部署的 shebang 已改寫成 /opt/cortex/venv/bin/…
#     (b) `./lib/python3.*/site-packages/pipx_shared.pth` —— 只存在於 pipx 樹
#   人工關卡要看的是**扣掉這兩類之後**還有什麼變動：
diff <(sort /tmp/cortex-cur.sha) <(sort /tmp/cortex-new.sha) \
  | grep -vE "^[<>] [0-9a-f]{64}  \./(bin/|lib/python3\.[0-9]+/site-packages/pipx_shared\.pth)" \
  | head -50

# 🔧 3. sudo：旁建新樹（不覆蓋現行）
#   ⚠️ 與第 4a 步**完全相同**的兩個 pipx 殘留必須在硬化前清掉——升級用的是同一條
#   `cp -a`，因此同樣會把 operator 樹的 shebang 與 pipx_shared.pth 帶進來。
#   漏掉的症狀：升級後服務起不來（`Permission denied`），或更糟——起得來但
#   import path 仍受 operator 可寫目錄影響（見 4a 的表）。
sudo rm -rf /opt/cortex/venv.new
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" /opt/cortex/venv.new

# 🔧 3a. sudo：重寫 bin/* 的 shebang 前綴（與 4a 逐字相同）
sudo env OLD_PREFIX="$HOME/.local/share/pipx/venvs/paulsha-cortex" sh -s <<'SH'
set -eu
for f in /opt/cortex/venv.new/bin/*; do
  [ -f "$f" ] || continue
  IFS= read -r first < "$f" || continue
  case "$first" in
    "#!$OLD_PREFIX/bin/"*) ;;
    *) continue ;;
  esac
  interp=${first#"#!$OLD_PREFIX/bin/"}
  sed -i "1s|.*|#!/opt/cortex/venv/bin/$interp|" "$f"
  echo "shebang rewritten: $f -> /opt/cortex/venv/bin/$interp"
done
SH

# 🔧 3b. sudo：移除 pipx_shared.pth
sudo find /opt/cortex/venv.new -name "pipx_shared.pth" -print -delete

# 🔧 3c. sudo：硬化（順序不可調換）
sudo chown -R root:root /opt/cortex/venv.new
sudo find /opt/cortex/venv.new -type d -exec chmod 0755 {} +
sudo find /opt/cortex/venv.new -type f -exec chmod a-w {} +
sudo find /opt/cortex/venv.new/bin -type f -exec chmod 0755 {} +

# ✅ 3d. 總驗收：新樹裡不得殘留任何指回 operator 樹的路徑
sudo grep -rIl -- "$HOME/.local/share/pipx" /opt/cortex/venv.new | head    # 期望：空輸出
sudo find /opt/cortex/venv.new -type l -lname "*/.local/share/pipx/*" | head  # 期望：空輸出

# ✅ 4. 新樹自檢通過才切換
sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root selfcheck
sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root equation

# ✅ 5. 登記表若有變動，unit／template／shim 全部必須重新產生
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root unit four-way --manager) \
     /etc/systemd/system/cortex-manager.service || echo "!! manager unit 需更新"
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root unit four-way --job) \
     /etc/systemd/system/cortex-job@.service || echo "!! job template unit (strict) 需更新"
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit) \
     /etc/systemd/system/cortex-job-jit@.service || echo "!! job template unit (jit) 需更新"
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root polkit four-way --template) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules || echo "!! polkit 規則需更新"
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root shim four-way) \
     /opt/cortex/bin/cortex-job-shim || echo "!! shim 需更新"

# 🔧 6. sudo：原子切換（保留前一版供回滾）
sudo systemctl stop cortex-manager.service
sudo rm -rf /opt/cortex/venv.prev
sudo mv /opt/cortex/venv /opt/cortex/venv.prev
sudo mv /opt/cortex/venv.new /opt/cortex/venv
#   若第 5 步顯示需更新，逐項重新落檔後 `sudo systemctl daemon-reload`
#   （polkit 規則另需 restart polkit）。
sudo systemctl start cortex-manager.service
```

```bash
# ✅ 驗證：新版在跑、自檢綠、降權邊界沒被升級順手改掉
sudo -u cortex-manager /opt/cortex/venv/bin/cortex --version
systemctl status cortex-manager.service --no-pager | head -5
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "exit=$?"   # 期望非 0
```

> **不裸 chown**：升級不是「把 headless 產出的檔 chown 給 manager」，而是「operator 驗證
> 來源後，以 root 身分整棵替換部署樹」。任何 headless 都碰不到 `/opt/cortex`。
> **回滾**：`sudo systemctl stop cortex-manager; sudo rm -rf /opt/cortex/venv;
> sudo mv /opt/cortex/venv.prev /opt/cortex/venv; sudo systemctl start cortex-manager`。

---

## 第 7 步：切換驗收（Phase 1 自檢轉綠）

```bash
# ✅ 以 cortex-manager（Manager 身分）跑自檢——Manager-owned 樹應無 job-writable
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -m paulsha_cortex.trust_root selfcheck
' | tee "/tmp/trust-root-after-$(date +%Y%m%d-%H%M).json"
#   期望：JSON 的 "ok": true、"job_writable_count": 0
#   M1 實測：`job_writable_count` 由 baseline 的 **5** 收斂為 **0**、`remaining` 為空。

# ✅ 與執行前提的 baseline 逐項對照（哪些 job-writable finding 被關掉了）
python3 - <<'PY'
import glob, json
def load(pattern):
    return json.load(open(sorted(glob.glob(pattern))[-1], encoding="utf-8"))
def jw(d):
    return {f["asset_id"] for f in d.get("findings", []) if f.get("status") == "job-writable"}
before, after = load('/tmp/trust-root-baseline-*.json'), load('/tmp/trust-root-after-*.json')
print("baseline job_writable_count =", before.get("job_writable_count"))
print("after    job_writable_count =", after.get("job_writable_count"))
print("closed   :", sorted(jw(before) - jw(after)))
print("remaining:", sorted(jw(after)))
PY
#   期望：after job_writable_count = 0；remaining 為空；
#         closed 涵蓋 baseline 列出的全部 job-writable 項

# ✅ 等式仍綠（登記表沒被順手改壞）
python3 -m paulsha_cortex.trust_root equation
```

### 7b. 功能面檢查（**結構全綠不等於做得了事**）

> **這一節存在的理由**：上面每一條、以及 M1 的全部驗收，都是**結構性**的——
> 誰擁有什麼、誰被拒、攻擊有沒有失敗。**沒有一條是功能性的**。
> 這正是為什麼 M1 全數通過，而部署其實**做不了任何實際工作**：
> 設定沒搬（第 3a-2）、job 跑不完（#623）。結構性驗收看不到這兩件事。
> 從最便宜的一條開始，逐級加重。

```bash
# ✅ F1（最便宜）：monitor 載得到自己的設定——第 3a-2 的守門條款
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/cortex monitor --once
' 2>&1 | head -20
#   期望：正常跑完一輪。
#   ⛔ 出現 `錯誤: 無 project 設定：…皆不存在` ⇒ 第 3a-2 沒做或做錯，回去補。
#   這條**兩秒鐘**，卻是 M1 唯一漏掉的那一類缺口的最短偵測路徑。

# ✅ F2：Manager 的 porcelain 在新樹上答得出話（路徑契約真的解析得到）
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/cortex status
' 2>&1 | head -20
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/cortex jobs
' 2>&1 | head -10
#   期望：兩者都正常輸出（新樹為空是合理的，**報錯不是**）。

# ✅ F2b（#633）：目標 repo 真的被宣告，且指向**來源樹**而不是 operator 的 checkout
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -c "from paulsha_cortex.config import paths; print(paths.repo_root())"
'
#   期望：印出 /var/lib/cortex/repos/<slug>（第 2c 步建立的來源樹）。
#   ⛔ `RepoRootUnresolvedError: PSC_REPO_ROOT 未宣告…` ⇒ 第 4b 的操作變數那段沒補。
#      **服務本身仍是 active 的**（#633 之後 repo 解析是 lazy 的），所以
#      `systemctl is-active` 全綠**不代表**派得了工——這條才是判準。

# ⏳ F3（最重，但這才是「能用」的定義）：真實 intake 一案並跑到 terminal
#   ⛔ **#623 關閉前不要做**——job 現在必然失敗（ProtectHome 讓 repo 不可達、
#      EnvironmentFile 缺八個操作變數），只會燒額度並留下半死的 run 狀態。
#   #623 關閉後：以 operator 觸發一次真實派工，追到 terminal 狀態，並確認
#     (a) job 以 uid=…(cortex-builder) 執行；
#     (b) evidence 落在 /var/lib/cortex/coordinator/evidence 底下；
#     (c) 全程沒有任何一步回頭寫 $HOME/.agents。
#   結果貼進 #584。**在 F3 通過之前，本部署只能宣稱「隔離已就位」，
#   不得宣稱「Phase 2b 可用」。**
```

**通過條件**：自檢 `ok=true`、`remaining` 為空、`equation` 為 `ok: true`
（結構面）**且** F1／F2 通過（功能面）。F3 屬 #623 關閉後的驗收，
未做時必須在 #584 明示記錄「功能面僅到 F2」。

---

## 第 8 步：R9 手動抽驗（operator 逐條攻擊測試）

裁決 7：本階段為**手動抽驗**；完整自動化矩陣（對登記表每一項機械展開族 2）屬另一
工項。三分之後**每族要以兩個模型 job 身分各跑一次**：`cortex-builder` 與
`cortex-reviewer-planner`；`cortex-manager` 只作為 negative control 的受信任身分。
每族末尾都有 **negative control**（以受信任身分做同一件事**必須成功**）——沒有它，
環境壞掉（目錄根本不存在）會產生假綠（spec §R9）。

**族別（A+B 後為五族）**：

| 族 | 名稱 | subject | 新增？ |
|---|---|---|---|
| 1 | capability-theft | builder ／ reviewer-planner | 依三帳號重寫 |
| 2 | durable-state-tampering | builder ／ reviewer-planner | 依三帳號重寫 |
| 3 | enforcement-plane-tampering | builder ／ reviewer-planner | 依三帳號重寫 |
| 4 | 行程間路徑 | builder ／ reviewer-planner | 依三帳號重寫 |
| **5** | **privilege-boundary（A+B 新增）** | **cortex-manager**（授權帳號自身）＋三個 headless | **✅ 新增** |

### 8a. 攻擊腳本（族 1–4，以模型 job 身分執行）

```bash
# 🔧 sudo：R9 攻擊 job 的 worktree ＋ 攻擊腳本（全部由 operator 親自建立）
JOB=r9
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

# 攻擊腳本放在**兩個 subject 都讀得到**的 root-owned 位置。
#   ・不能放 /tmp：template unit 帶 PrivateTmp=yes，operator 寫的 /tmp 檔 job 看不到。
#   ・不能放 worktree/$JOB：那是 builder-owned 0700，reviewer-planner 讀不到。
#   ・/var/lib/cortex 樹根是 root:root 0755，放這裡 root 擁有、全體唯讀，8e 一併清掉。
sudo tee /var/lib/cortex/r9-attack.sh >/dev/null <<'SH'
#!/bin/sh
# t() = 期望**被拒**的攻擊。rc 非 0 ⇒ denied (OK)。
t() { printf '%s :: ' "$1"; shift; if "$@" >/dev/null 2>&1; then echo "!! SUCCEEDED (FAIL)"; else echo "denied (OK) rc=$?"; fi; }
# d() = 設計上**應該成功**的讀取。把它們列出來，是為了讓「可讀是設計、
#       不可寫才是守的東西」有實測背書，而不是靠註解宣稱。
d() { printf '%s :: ' "$1"; shift; if "$@" >/dev/null 2>&1; then echo "readable (BY DESIGN)"; else echo "!! 讀不到——與登記表 rationale 不符，查"; fi; }
# need() = 目標檔必須先存在，否則「刪不掉／讀不到」測的是「檔不存在」而非「被拒」。
need() { [ -e "$1" ] || printf '!! 前置缺失：%s 不存在——對它的刪除／截斷／讀取測項會是假綠，請先由 operator 以 cortex-manager 身分預建\n' "$1"; }
A=/var/lib/cortex
# #657：spool 是 per-principal 的；R9 攻擊腳本以模型 job 身分執行，看的是自己那格。
SPOOL=$A/coordinator/job-specs/builder

# ⛔ 身分鎖（**不可移除**）：本腳本會真的執行破壞性動作——truncate、`rm`、
#    `mv "$HOME/.codex"`、覆寫 hooks.json。在沙箱外（例如 operator 帳號）
#    直接跑會弄壞自己的機器，而且那些「成功」還會被誤讀成邊界失守。
#    只允許以兩個 headless 帳號執行；其他身分一律拒跑。
SELF=$(id -un 2>/dev/null || echo unknown)
case "$SELF" in
  cortex-builder|cortex-reviewer-planner) ;;
  *) echo "⛔ 拒絕執行：本腳本只能以 cortex-builder／cortex-reviewer-planner 身分執行（目前是 $SELF）。"
     echo "   若要在沙箱外檢查腳本內容，請用 less／cat，不要執行它。"
     exit 2 ;;
esac

: "${R9_SUBJECT:?R9_SUBJECT 必須由 operator 注入（builder / reviewer-planner）}"
: "${R9_INSTANCE:?R9_INSTANCE 必須由 operator 注入（本次 job 的 instance 名）}"
: "${MANAGER_PID:?MANAGER_PID 必須由 operator 從外部注入——見族 4 說明}"
echo "subject=$R9_SUBJECT instance=$R9_INSTANCE manager_pid=$MANAGER_PID"

echo "===== 前置：族 2 的目標檔是否存在（不存在 ⇒ 該測項假綠）====="
need "$A/coordinator/jobs.json"
need "$A/monitor/work-items.snapshot.json"

echo "===== 族 1 capability-theft ====="
# ⚠️ T1.1 測的是**寫入**，不是讀取。登記表資產 `runtime-bootstrap-env` 的 rationale
#    明寫「對全部 headless **唯讀**」、mode 就是 0644——**可讀是設計**。
#    守的是「改不了 PSC_* 就重導不了整棵 durable state」。
t "T1.1 寫 EnvironmentFile"        sh -c "printf 'PSC_COORDINATOR_ROOT=/tmp/evil\n' >> /opt/cortex/etc/cortex-manager.env"
t "T1.1 截斷 EnvironmentFile"      sh -c ": > /opt/cortex/etc/cortex-manager.env"
t "T1.1 symlink 換掉 EnvironmentFile" sh -c "ln -sf /tmp/evil /opt/cortex/etc/cortex-manager.env"
t "T1.1 刪 EnvironmentFile"        rm /opt/cortex/etc/cortex-manager.env
d "T1.1 讀 EnvironmentFile（設計為唯讀）" cat /opt/cortex/etc/cortex-manager.env
t "T1.1 讀 manager HOME cache"     ls /var/lib/cortex-manager/cache
t "T1.2 連 Manager control socket" sh -c "ls $A/run/cortex && cat $A/run/cortex/*.sock"
t "T1.3 呼叫 operator CLI"         /opt/cortex/venv/bin/cortex work ship --help
t "T1.4 直寫 control queue"        sh -c "printf x > $A/control/requests/evil.json"

# ⚠️ T1.5：job-spec spool 的載重同樣在**寫入面**。
#    template unit 的 User=cortex-builder 由 systemd 在 ExecStart **之前**套用，
#    shim 本身即以 builder 身分讀 spec——**讀得到是必要條件**，不是破口。
#    所有 builder job 共用同一個 UID，互讀本來就在威脅模型內
#    （per-job 讀隔離需 per-job UID，不在本方案範圍，見 5-3）。
t "T1.5 spool 建立新 spec"         sh -c "printf '{}' > $SPOOL/evil.json"
t "T1.5 spool 追加自己的 spec"     sh -c "printf 'x' >> $SPOOL/$R9_INSTANCE.json"
t "T1.5 spool symlink 換掉 spec"   sh -c "ln -sf /tmp/evil $SPOOL/$R9_INSTANCE.json"
t "T1.5 spool 刪除 spec"           rm "$SPOOL/$R9_INSTANCE.json"
#    讀取面的期望**依 subject 而異**——這正是三分在檔案層生效的直接證據：
case "$R9_SUBJECT" in
  builder)
    d "T1.5 讀自己的 spec"          cat "$SPOOL/$R9_INSTANCE.json"
    d "T1.5 列 spool"               ls "$SPOOL"
    ;;
  *)
    t "T1.5 讀 spec（非 builder 應被拒）" cat "$SPOOL/$R9_INSTANCE.json"
    t "T1.5 列 spool（非 builder 應被拒）" ls "$SPOOL"
    ;;
esac

echo "===== 族 2 durable-state-tampering ====="
t "T2 modify jobs.json"            sh -c "printf '{}' > $A/coordinator/jobs.json"
t "T2 truncate jobs.json"          sh -c ": > $A/coordinator/jobs.json"
# ⚠️ 不可用 `rm -f`：`-f` 對**不存在**的檔回 0，在乾淨新樹上會變成必然的假陽性
#    （測到的是「檔不存在」而不是「權限被拒」）。上面的 need() 是它的配套。
t "T2 delete jobs.json"            rm "$A/coordinator/jobs.json"
t "T2 replace jobs.json"           sh -c "printf '{}' > /tmp/j && mv /tmp/j $A/coordinator/jobs.json"
t "T2 symlink-swap jobs.json"      sh -c "ln -sf /tmp/evil $A/coordinator/jobs.json"
t "T2 寫 verification evidence"    sh -c "printf x > $A/coordinator/evidence/verification/evil.json"
t "T2 寫 maintainer attestation"   sh -c "printf x > $A/coordinator/evidence/maintainer-review/evil.json"
t "T2 寫 completion record"        sh -c "printf x > $A/coordinator/evidence/completion/evil.json"
t "T2 寫 full-suite evidence"      sh -c "printf x > $A/coordinator/evidence/full-suite/evil.json"
t "T2 寫 gate ledger"              sh -c "printf x > $A/runtime/dispatch/evil.gates.json"
t "T2 寫 delivery journal"         sh -c "printf x > $A/coordinator/delivery-journal.json"
t "T2 寫 digest outbox"            sh -c "printf x > $A/coordinator/digest/outbox/evil.json"
t "T2 寫 model-identity overlay"   sh -c "printf x > $A/config/paulsha/model-identities.yaml"
t "T2 寫 combo override"           sh -c "printf x > $A/config/combos/evil.yaml"
t "T2 寫 dispatch specs"           sh -c "printf x > $A/specs/evil.md"
t "T2 寫 skill ledger"             sh -c "printf x >> $A/registry/skill_usage.jsonl"
t "T2 讀 monitor 讀模型"           cat "$A/monitor/work-items.snapshot.json"
t "T2 代寫他人 review verdict"     sh -c "printf '{\"verdict\":\"pass\"}' > $A/worktree/victim/.psc-review-verdict.json"
t "T2 直寫 verdict spool 他格"     sh -c "printf '{\"verdict\":\"pass\"}' > $A/coordinator/review-verdicts/victim/verdict.json"
t "T2 列 verdict spool"            ls "$A/coordinator/review-verdicts"
t "T2 寫他人 worktree"             sh -c "printf x > $A/worktree/victim/evil"
t "T2 列 worktree pool"            ls "$A/worktree"

echo "===== 族 3 enforcement-plane-tampering ====="
t "T3.1 改 site-packages verifier" sh -c "printf x >> /opt/cortex/venv/lib/python3*/site-packages/paulsha_cortex/coordinator/verification.py"
t "T3.2 注入 sitecustomize"        sh -c "printf x > /opt/cortex/venv/lib/python3*/site-packages/sitecustomize.py"
t "T3.2 注入 .pth"                 sh -c "printf x > /opt/cortex/venv/lib/python3*/site-packages/evil.pth"
t "T3.3 改 Manager unit"           sh -c "printf x >> /etc/systemd/system/cortex-manager.service"
t "T3.4 改 EnvironmentFile"        sh -c "printf 'PSC_COORDINATOR_ROOT=/tmp/evil' >> /opt/cortex/etc/cortex-manager.env"
t "T3.5 刪 EnvironmentFile"        rm -f /opt/cortex/etc/cortex-manager.env
t "T3.6 改 venv launcher"          sh -c "printf x >> /opt/cortex/venv/bin/cortex"
t "T3.9 改 codex hooks"            sh -c "printf x > $HOME/.codex/hooks.json"
t "T3.9 換掉 ~/.codex 目錄"        sh -c "mv $HOME/.codex $HOME/.codex.bak"

echo "===== 族 4 行程間路徑 ====="
echo "T4.1 自己的 fd：" ; ls -l /proc/self/fd
# ⚠️ **不可**在這裡用 `pgrep` 取 Manager 的 pid。job unit 帶 ProtectProc=invisible
#    ＋ ProcSubset=pid，job 眼中的 /proc **只看得到自己**——`pgrep` 必回空，
#    後面每一條就變成對 `/proc//…` 操作，因「路徑不存在」而失敗、被記成 denied。
#    那是**假綠**：測到的是 pid 不存在，不是權限被拒。pid 一律由 operator 從外部
#    以 `systemctl show cortex-manager.service -p MainPID --value` 取得後注入。
#    （順帶一提：Manager 的 ExecStart 是 `cortex service run`，
#     `pgrep -f paulsha_cortex` 連在 operator 身上都不一定匹配得到。）
t "T4.0 Manager /proc 可見性（denied＝不可見，正是 ProtectProc 生效）" \
                                   test -d "/proc/$MANAGER_PID"
t "T4.2 ptrace Manager"            sh -c "command -v gdb >/dev/null && gdb -p $MANAGER_PID -batch -ex quit"
t "T4.3 讀 Manager environ"        cat "/proc/$MANAGER_PID/environ"
t "T4.3 讀 Manager mem"            head -c 1 "/proc/$MANAGER_PID/mem"
t "T4.4 對 Manager 送 SIGSTOP"     kill -STOP "$MANAGER_PID"
t "T4.5 讀另一個 job 帳號 cache"   sh -c "ls /var/lib/cortex-builder/cache /var/lib/cortex-reviewer-planner/cache"
t "T4.6 提權：systemd-run root"    systemd-run --uid=0 --pipe /bin/id
t "T4.6 提權：起 job instance"     systemctl start cortex-job@other.service
t "T4.6 提權：sudo"                sudo -n true
SH
sudo chown root:root /var/lib/cortex/r9-attack.sh
sudo chmod 0755 /var/lib/cortex/r9-attack.sh
#   ↑ root 擁有：攻擊腳本本身不得被受測 subject 改寫（否則測的是自己寫的東西）。

# ✅ 驗證：身分鎖有效——以 operator 身分直接跑必須被拒（**不要為了「先看看」而繞過它**）
sh /var/lib/cortex/r9-attack.sh; echo "guard exit=$?"
#   期望：印出 `⛔ 拒絕執行…`、exit=2。
#   這條不是形式主義：腳本裡的 T3.9 會 `mv "$HOME/.codex" "$HOME/.codex.bak"`、
#   T1.1／T2 會 truncate 檔案——在 operator 帳號跑一次就會弄壞自己的環境，
#   而且那些「成功」是身分錯了，不是邊界破了。

# 🔧 sudo：族 2 的目標檔必須先存在——否則「刪不掉／讀不到」是「檔不存在」的假綠
sudo -u cortex-manager sh -c '
  A=/var/lib/cortex
  [ -e "$A/coordinator/jobs.json" ] || printf "{\"jobs\": []}" > "$A/coordinator/jobs.json"
  [ -e "$A/monitor/work-items.snapshot.json" ] || printf "{}" > "$A/monitor/work-items.snapshot.json"
  ls -l "$A/coordinator/jobs.json" "$A/monitor/work-items.snapshot.json"'
#   （這兩個檔本來就會由 Manager 首次寫入時建立；乾淨新樹上要手動預建一次。）

# 準備「他人 worktree」與「他人 verdict 格」作為跨 persona 攻擊目標
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/worktree/victim
sudo -u cortex-manager sh -c 'printf "{\"verdict\":\"fail\"}" > /var/lib/cortex/worktree/victim/.psc-review-verdict.json'
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/coordinator/review-verdicts/victim
```

```bash
# ✅ 由 **operator 從外部**取 Manager 的 pid（族 4 唯一正確的取得方式）
MANAGER_PID="$(systemctl show cortex-manager.service -p MainPID --value)"
echo "MANAGER_PID=$MANAGER_PID"
#   期望：非 0 的 pid。若是 0 ⇒ 服務沒在跑，族 4 整族無效，先把服務起起來。
ps -o user=,pid=,cmd= -p "$MANAGER_PID"     # 期望：user 欄為 cortex-manager

# 🔧 sudo：pass 1——以 **cortex-builder** 身分（經 A+B 的正式路徑：template instance）
#   攻擊腳本走 job-spec 的 command；R9_* 與 MANAGER_PID 走 spec 的 env
#   （job 的 env **完全等於** spec 的 env，不繼承 unit 的 Environment=）。
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$JOB" "$MANAGER_PID" <<'PY'
import sys
from paulsha_cortex.coordinator import job_runner

instance, manager_pid = sys.argv[1], sys.argv[2]
# #657：spool 是 per-principal 的——`role=` 決定寫進哪一格。
spool = job_runner.resolve_job_spec_spool({}, role=job_runner.JOB_ROLE_BUILDER)
account = "cortex-builder"
spec = job_runner.build_job_spec(
    job_id=f"{instance}-attack",
    instance=instance,
    unit=f"cortex-job@{instance}.service",
    command=["/bin/sh", "/var/lib/cortex/r9-attack.sh"],
    working_directory=f"/var/lib/cortex/worktree/{instance}",
    log_path=f"/var/lib/cortex/worktree/{instance}/{instance}.log",
    env={
        "HOME": "/var/lib/cortex-builder",
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "R9_SUBJECT": "builder",
        "R9_INSTANCE": instance,
        "MANAGER_PID": manager_pid,
    },
)
# `account=` 讓寫端在落地後就地複驗「那個身分讀得到這個檔」（#657）——
# 失敗會是一個 Manager 端的清楚錯誤，而不是 journal 裡的 78/CONFIG。
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(spool, instance), spec, account=account))
PY
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"
# ⚠️ 報告在 **spec 的 log_path**，不在 journal——shim 在已降權之後接管 stdout/stderr。
sudo cat "/var/lib/cortex/worktree/$JOB/$JOB.log" | tee /tmp/r9-builder.txt
sudo journalctl -u "cortex-job@$JOB.service" -n 30 --no-pager
#   期望：journal 只有 systemd 的起停紀錄，**沒有** `cortex-job-shim: …` 錯誤。

# 🔧 pass 2——以 **cortex-reviewer-planner** 身分（#615 M2：改走**真實啟動面**）
#   M1 時 reviewer／planner 沒有自己的 template instance，這一趟只能用 operator 的
#   sudo 直接起 transient unit——它測到的是**檔案權限面**的三分，**不是啟動面**，
#   而且那份加固面是手打的（與實際 unit 隨時可能漂移）。
#   M2 之後這一趟改成與 pass 1 **完全同構**：寫 spec → 以 cortex-manager 的 grant
#   起 `cortex-reviewer-job@` instance。加固面因此直接來自已落檔的 unit，不再手打。
RJOB=r9-review
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$RJOB" "$MANAGER_PID" <<'PY'
import sys
from paulsha_cortex.coordinator import job_runner

instance, manager_pid = sys.argv[1], sys.argv[2]
# #657：spool 是 per-principal 的——`role=` 決定寫進哪一格。
spool = job_runner.resolve_job_spec_spool({}, role=job_runner.JOB_ROLE_REVIEW)
account = "cortex-reviewer-planner"
spec = job_runner.build_job_spec(
    job_id=f"{instance}-r9",
    instance=instance,
    unit=f"cortex-reviewer-job@{instance}.service",
    command=["/bin/sh", "/var/lib/cortex/r9-attack.sh"],
    working_directory="/var/lib/cortex/worktree",
    log_path=f"/var/lib/cortex-reviewer-planner/cache/{instance}.log",
    env={
        "HOME": "/var/lib/cortex-reviewer-planner",
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "R9_SUBJECT": "reviewer-planner",
        "R9_INSTANCE": instance,
        "MANAGER_PID": manager_pid,
    },
)
# `account=` 讓寫端在落地後就地複驗「那個身分讀得到這個檔」（#657）——
# 失敗會是一個 Manager 端的清楚錯誤，而不是 journal 裡的 78/CONFIG。
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(spool, instance), spec, account=account))
PY
sudo -u cortex-manager systemctl start "cortex-reviewer-job@$RJOB.service"
sudo cat "/var/lib/cortex-reviewer-planner/cache/$RJOB.log" | tee /tmp/r9-reviewer.txt
sudo journalctl -u "cortex-reviewer-job@$RJOB.service" -n 30 --no-pager
#   期望：journal 只有 systemd 的起停紀錄，**沒有** `cortex-job-shim: …` 錯誤。
#   ⚠️ 這一趟必須以 `cortex-manager` 的 grant 起（不是 operator 的 sudo）——
#      那正是 M2 要證明的事：Manager 起得動 reviewer 的模板，而模板把 UID 寫死。
#   ⚠️ 報告開頭的 `id` 若不是 `uid=…(cortex-reviewer-planner)` ⇒ 停下來。
```

**預期輸出**：兩份報告中**每一條 `t()` 測項**都是 `denied (OK) rc=<非 0>`；
`d()` 測項印 `readable (BY DESIGN)`（那是登記表 rationale 的實測背書，不是破口）；
`need()` 不得印出任何 `!! 前置缺失`；
`T4.1 自己的 fd` 只列 `0`、`1`、`2`（shim 接管後指向 spec 的 log／`/dev/null`），
**沒有任何**指向 `/var/lib/cortex` 或 `/opt/cortex` 的可寫 fd。

> **M1 對照**：修正前的腳本在 builder 上是 46 `denied` ／ 3 條 `SUCCEEDED`，
> 三條分別是 T1.1（期望寫反）、T1.5（測錯面）、T2 delete（`rm -f` 假陽性）——
> 三條**全部判定為非破口**，而且成因就是本節這次修掉的三處。
> 按本節修正後的腳本重跑，兩份報告都應是**全數 denied**。

```bash
# ✅ 一眼判讀（兩份都要看）
for F in /tmp/r9-builder.txt /tmp/r9-reviewer.txt; do
  printf '%s: denied=%s by-design-readable=%s\n' "$F" \
    "$(grep -c 'denied (OK)' "$F")" "$(grep -c 'BY DESIGN' "$F")"
  grep "!! 前置缺失" "$F" && echo "!! $F 有測項是假綠（目標檔不存在），補建後重跑"
  grep "!! 讀不到" "$F" && echo "!! $F 的 by-design 讀取失敗，與登記表 rationale 不符，查"
  grep "SUCCEEDED (FAIL)" "$F" && echo "!! $F 有攻擊成功，立即停止並回滾" || echo "   $F 全綠"
done
grep -A5 "自己的 fd" /tmp/r9-builder.txt
```

### 8b. 族 5：privilege-boundary（**A+B 新增的兩個攻擊族**）

這兩族測的不是「job 能不能亂寫」，而是「**降權/提權分界線本身能不能被推開**」。

```bash
# ---------------------------------------------------------------------------
# 族 5.1：以 cortex-manager（唯一持 spawn 授權的帳號）請求 transient unit --uid=root
#         → 必須被 polkit 拒（規則沒授權 manage-units 的 transient 建立）
# ---------------------------------------------------------------------------
# ✅ 5.1a 最直接的形式
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "5.1a exit=$?"

# ✅ 5.1b 借 job unit 名字包裝（想騙過 pattern 檢查）
sudo -u cortex-manager systemd-run --unit="cortex-job@escalate.service" --uid=0 \
     --pipe --wait /bin/id; echo "5.1b exit=$?"

# ✅ 5.1c 不指定 uid、改用 property 夾帶特權
sudo -u cortex-manager systemd-run --unit="cortex-job@escalate2.service" \
     --property=AmbientCapabilities=CAP_SETUID --property=User=root \
     --pipe --wait /bin/id; echo "5.1c exit=$?"

# ✅ 5.1d 走 --user bus 旁路（user manager 的 transient unit）
sudo -u cortex-manager systemd-run --user --pipe --wait /bin/id; echo "5.1d exit=$?"

# ✅ 5.1e 直接打 D-Bus 的 StartTransientUnit（繞開 systemd-run 的 CLI）
sudo -u cortex-manager busctl call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
     org.freedesktop.systemd1.Manager StartTransientUnit "ssa(sv)a(sa(sv))" \
     "cortex-job@bus.service" "replace" 1 "User" s "root" 0 2>&1 | tail -2
echo "5.1e exit=$?"

# 期望：5.1a–5.1e **全部非 0**；journal 應可見 polkit 的 denial。
# 期望的拒絕理由（任一）：
#   "Interactive authentication required" / "Access denied" / "Permission denied"
sudo journalctl -u polkit --since "-3 min" --no-pager | tail -20
# ✅ negative control：同樣的請求由 **operator（有 sudo）** 發出必須成功——
#    否則上面的紅是「systemd-run 根本壞了」的假綠。
sudo systemd-run --quiet --collect --pipe --wait --uid=0 /bin/id; echo "5.1-neg exit=$?"   # 期望 0

# ---------------------------------------------------------------------------
# 族 5.2：以任何 headless 身分改寫 template unit 或 shim
#         → 必須被 root-owned 檔案權限拒
# ---------------------------------------------------------------------------
# ✅ 5.2 逐帳號 × 逐物件 × 逐手法（改寫／截斷／換檔／換父目錄／drop-in）
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  echo "--- subject=$U ---"
  sudo -u "$U" sh -c 'printf "User=root\n" >> /etc/systemd/system/cortex-job@.service'; echo "  append-unit    exit=$?"
  sudo -u "$U" sh -c ': > /etc/systemd/system/cortex-job@.service';                      echo "  truncate-unit  exit=$?"
  sudo -u "$U" sh -c 'printf "x" > /tmp/u && mv /tmp/u /etc/systemd/system/cortex-job@.service'; echo "  replace-unit   exit=$?"
  sudo -u "$U" sh -c 'mkdir -p /etc/systemd/system/cortex-job@.service.d && printf "[Service]\nUser=root\n" > /etc/systemd/system/cortex-job@.service.d/evil.conf'; echo "  dropin-unit    exit=$?"
  sudo -u "$U" sh -c 'printf "id\n" >> /opt/cortex/bin/cortex-job-shim';                 echo "  append-shim    exit=$?"
  sudo -u "$U" sh -c 'ln -sf /tmp/evil /opt/cortex/bin/cortex-job-shim';                 echo "  symlink-shim   exit=$?"
  sudo -u "$U" sh -c 'mv /opt/cortex/bin /opt/cortex/bin.bak';                           echo "  rename-bindir  exit=$?"
  sudo -u "$U" sh -c 'printf "x\n" >> /etc/polkit-1/rules.d/49-cortex-downgrade.rules';  echo "  append-polkit  exit=$?"
  sudo -u "$U" sh -c 'rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules';            echo "  delete-polkit  exit=$?"
done
# 期望：**全部非 0**（9 手法 × 3 帳號 = 27 條）。任一條 exit=0 ⇒ 分界線已破，立即停止。

# ✅ 5.2 negative control：root 改得動（且改完必須還原並複驗）
sudo cp /etc/systemd/system/cortex-job@.service /tmp/jobunit.bak
sudo sh -c 'printf "\n# negctl\n" >> /etc/systemd/system/cortex-job@.service'
sudo systemctl daemon-reload && echo "5.2-neg OK"
sudo cp /tmp/jobunit.bak /etc/systemd/system/cortex-job@.service
sudo systemctl daemon-reload
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job) \
     /etc/systemd/system/cortex-job@.service && echo "restored & in sync: OK"

# ✅ 5.2 事後複驗：分界線改過之後仍成立（族 3「重啟後仍綠」的同一條紀律）
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "post-negctl exit=$?"   # 期望非 0
```

### 8b-2. 族 6：verdict 通道端到端（**#615 M2 才第一次驗得到**，#638／#639）

> **為什麼這一族到現在才存在**：#638 修的三個缺陷（`mkdir` 重設 ACL mask、consumer
> 讀不到 producer 建的檔、consumer `chmod` 不了 producer 的檔）**全部只在「producer
> 與 consumer 是不同 UID」時才成立**。M2 之前 reviewer 跑在 Manager 行程內，三個缺陷
> 在那個部署上一個都不會發生——修法也因此一次都沒有被真正驗到。這一族就是那個驗證。
>
> **這一族不可用 `sudo -u` 模擬**：`sudo -u cortex-reviewer-planner` 起的行程沒有降權
> unit 的 `UMask=0077`，而「檔案出生即 0600、consumer 因此讀不到」正是缺陷 2 的成因。
> 用 sudo 模擬會得到一個永遠綠的測試。**必須走真實的 template instance。**

```bash
VJOB=verdict-e2e
SPOOLDIR=/var/lib/cortex/coordinator/review-verdicts/$VJOB

# 🔧 (1) Manager 建那一格（＝ review.prepare_review_verdict_spool() 的實際路徑）
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$SPOOLDIR" <<'PY'
import sys
from paulsha_cortex.coordinator import spool_slot
print("slot:", spool_slot.create_slot(sys.argv[1], reset=False))
PY
#   ⚠️ 若這裡就 Permission denied ⇒ 第 2 步的 spool 權限沒套好，停下來。

# 🔧 (2) 以 reviewer 的**真實 template instance** 寫 verdict（含 publish 那一步）
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$VJOB" "$SPOOLDIR" <<'PY'
import sys
from paulsha_cortex.coordinator import job_runner, spool_slot

instance, slot = sys.argv[1], sys.argv[2]
verdict = f"{slot}/{spool_slot.REVIEW_VERDICT_FILENAME}"
script = (
    f'printf %s \'{{"schema_version":1,"findings":[]}}\' > {verdict}; '
    # producer 自己放寬給 consumer——與 launcher wrapper 的那一段是同一支函式
    f'{spool_slot.publish_file_command(verdict)}; '
    f'echo "wrote as $(id -un)"'
)
spec = job_runner.build_job_spec(
    job_id=f"{instance}-verdict",
    instance=instance,
    unit=f"cortex-reviewer-job@{instance}.service",
    command=["/bin/sh", "-c", script],
    working_directory="/var/lib/cortex/worktree",
    log_path=f"/var/lib/cortex-reviewer-planner/cache/{instance}.log",
    env={"HOME": "/var/lib/cortex-reviewer-planner", "PATH": "/usr/bin:/bin"},
)
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(job_runner.DEFAULT_JOB_SPEC_SPOOL, instance), spec))
PY
sudo -u cortex-manager systemctl start "cortex-reviewer-job@$VJOB.service"
sudo cat "/var/lib/cortex-reviewer-planner/cache/$VJOB.log"
#   期望：`wrote as cortex-reviewer-planner`

# ✅ (3) 檔案的 owner 確實是 reviewer、不是 Manager——**這一條就是「不同 UID」的證據**
sudo stat -c "%U:%G %a %n" "$SPOOLDIR/verdict.json"
#   期望：cortex-reviewer-planner:cortex-reviewer-planner 644 …
#   ⚠️ owner 若是 cortex-manager ⇒ 那個 job 沒有以 reviewer 身分跑，整族作廢。
#   ⚠️ mode 若是 600 ⇒ producer 的 publish 段沒跑到（缺陷 2 會在下一步顯現）。

# ✅ (4) Manager（consumer）讀得到內容
sudo -u cortex-manager cat "$SPOOLDIR/verdict.json"; echo "(4) exit=$?"
#   期望：印出 JSON、exit=0。**這一條就是 #638 缺陷 2 的驗收。**

# ✅ (5) builder 對整條通道零權限（連 traverse 都進不去）
sudo -u cortex-builder ls "$SPOOLDIR" 2>&1 | tail -1; echo "(5) ls exit=$?"
sudo -u cortex-builder sh -c "printf x > $SPOOLDIR/verdict.json" 2>&1 | tail -1
echo "(5) overwrite exit=$?"
#   期望：兩條皆非 0（Permission denied）。**這一條是 spec §3 最短攻擊路徑的驗收**：
#   builder 代寫 reviewer 的 verdict，在 OS 層不成立。

# ✅ (6) reviewer 讀不到**別人**那一格（`wx` 無 `r`）
sudo -u cortex-reviewer-planner ls /var/lib/cortex/coordinator/review-verdicts 2>&1 | tail -1
echo "(6) exit=$?"
#   期望：非 0（Permission denied）——寫得進自己那格，列不出別人有哪些格。

# ✅ (7) Manager seal 之後 reviewer 改不動（#638 缺陷 3）
sudo -u cortex-manager /opt/cortex/venv/bin/python - "$SPOOLDIR" <<'PY'
import sys
from paulsha_cortex.coordinator import spool_slot
print("sealed:", spool_slot.seal_slot(sys.argv[1]))
PY
sudo stat -c "%a %n" "$SPOOLDIR"
#   期望：sealed: True、mode 500
sudo -u cortex-reviewer-planner sh -c "printf x > $SPOOLDIR/verdict.json" 2>&1 | tail -1
echo "(7) rewrite exit=$?"
sudo -u cortex-reviewer-planner sh -c "printf x > $SPOOLDIR/second.json" 2>&1 | tail -1
echo "(7) create exit=$?"
sudo -u cortex-reviewer-planner sh -c "rm -f $SPOOLDIR/verdict.json" 2>&1 | tail -1
echo "(7) delete exit=$?"
#   期望：**三條全部非 0**。
#   ⚠️ 這三條是 #638 缺陷 3 的驗收：修法前 seal 封的是**檔案**，而 Manager `chmod`
#      不了 reviewer 擁有的檔、該處又刻意不 raise ⇒ **無聲失敗**，reviewer 可以在
#      Manager 判讀之後回頭覆寫自己的 verdict。封**目錄**才是 consumer 做得到的那一個。
#   ⚠️ 任一條 exit=0 ⇒ 停下來：verdict 在落地後仍可被改寫，foreign review 不可信。

# ✅ (8) negative control：Manager 自己在 seal 之前寫得進那一格
#     （否則上面的紅可能只是「這棵樹整個不可寫」的假綠）
sudo -u cortex-manager /opt/cortex/venv/bin/python - <<'PY'
from pathlib import Path
from paulsha_cortex.coordinator import spool_slot
slot = Path("/var/lib/cortex/coordinator/review-verdicts/negctl")
spool_slot.create_slot(slot, reset=False)
(slot / "probe.json").write_text("{}", encoding="utf-8")
print("NEG-CONTROL-OK")
PY
#   期望：印出 NEG-CONTROL-OK。

# 🔧 清理
sudo rm -rf "$SPOOLDIR" /var/lib/cortex/coordinator/review-verdicts/negctl
sudo rm -f "/var/lib/cortex/coordinator/job-specs/reviewer/$VJOB.json" \
           "/var/lib/cortex-reviewer-planner/cache/$VJOB.log"
```

### 8b-3. 族 7：gate 執行身分（**#629**；＝spec §R9 的**族 5**）

> **編號對照**：spec §R9 只列四族，本 runbook 另有 A+B 新增的族 5（privilege-boundary）
> 與 #615 M2 的族 6（verdict 通道端到端），因此 #629 這一族在 runbook 裡排到**族 7**，
> 在 spec 裡是**族 5**。兩邊的內容逐條相同（T5.1–T5.10）。

> **本族的前提與其他族不同**：這裡假設 builder **已經**在自己的工作樹裡取得任意
> 程式碼執行——那正是宣告出來的 gate 命令必然給它的東西（`pytest` 會載入該樹的
> `conftest.py`）。要驗的不是「那個執行不會發生」，而是**它被關在哪裡**。
>
> spec §R3 的誠實邊界：第四帳號買到的是**圍堵**，**不是**「ledger 不可偽造」。
> 取得 ACE 的 builder 依然能影響自己那一份 ledger 的內容；下面每一條驗的是它
> **影響不到別的東西**。

```bash
# 以 gate 執行身分執行（模擬「gate 命令內的 conftest.py 拿到 shell」）
sudo -u cortex-gate sh -s <<'SH'
A=/var/lib/cortex
ok(){ printf '%-58s %s\n' "$1" "$2"; }
try(){ if eval "$2" >/dev/null 2>&1; then ok "$1" "❌ BAD: 成功了"; else ok "$1" "✅ 被拒"; fi; }

# T5.1 寫 gate ledger／exit sentinel 的落點
try "T5.1 write dispatch log dir"      ": > $A/runtime/dispatch/psc-probe"
# T5.2 寫其他 Tier-0 Manager-owned 資產
try "T5.2 write jobs registry"         ": > $A/coordinator/jobs.json.probe"
try "T5.2 write evidence tree"         ": > $A/coordinator/evidence/.probe"
try "T5.2 write job-spec spool"        ": > $A/coordinator/job-specs/gate/.probe"
try "T5.2 read another persona spool"  "ls $A/coordinator/job-specs/builder"
try "T5.2 list the spool container"    "ls $A/coordinator/job-specs"
try "T5.2 write repo source tree"      ": > $A/repos/.probe"
# T5.3 寫 builder 的工作樹（讀得到，但不可寫）
for d in $A/worktree/*/; do try "T5.3 write builder worktree" ": > ${d}psc-probe"; break; done
# T5.4 碰 verdict／commit 通道
try "T5.4 read verdict spool"          "ls $A/coordinator/review-verdicts"
try "T5.4 write verdict spool"         ": > $A/coordinator/review-verdicts/.probe"
try "T5.4 read commit spool"           "ls $A/coordinator/commit-spool"
# T5.5 讀 gate spool 容器（自己那格是 wx 無 r ⇒ 列不出來）
try "T5.5 list gate ledger spool"      "ls $A/coordinator/gate-ledger-spool"
# T5.6 起任何 unit（gate 完全沒有 polkit grant）
try "T5.6 systemctl start any unit"    "systemctl start cortex-gate-job@probe.service"
try "T5.6 systemctl start builder unit" "systemctl start cortex-job@probe.service"
SH
#   期望：**每一行都是 ✅ 被拒**。任何一個 ❌ 都要停下來——那一條就是 #629 沒守住的地方。

# T5.7 gate 不污染被驗的樹：跑完一張 build 卡之後，工作樹裡不得有 gate 的產物
JOBDIR=$(sudo ls -1d /var/lib/cortex/worktree/*/ 2>/dev/null | head -1)
sudo find "$JOBDIR" -user cortex-gate -print | head
#   期望：**沒有任何輸出**（副本在 /var/lib/cortex/gate-worktree/，不在這裡）。

# T5.8 builder 預先佔位 ledger 落點
sudo -u cortex-builder sh -c '
  mkdir -p /var/lib/cortex/coordinator/gate-ledger-spool/preseed 2>&1 | tail -1
' 
#   期望：Permission denied（容器 0700 cortex-manager，builder 連 traverse 都沒有）。

# T5.9 builder 竄改 conftest.py 影響不了 Manager 身分
#   做法：在一張 build 卡的工作樹放一個會寫 Manager durable state 的 conftest.py，
#   派卡，然後確認：
sudo find /var/lib/cortex/coordinator /var/lib/cortex/runtime -user cortex-gate | head
#   期望：**沒有任何輸出**（除了 gate 自己那一格 spool，而那一格在收割後已封口）。
sudo ls -l /var/lib/cortex/runtime/dispatch/*.gates.json | tail -3
#   期望：owner 全部是 cortex-manager。

# T5.10 gate 直接寫權威 ledger
sudo -u cortex-gate sh -c 'printf "{}" > /var/lib/cortex/runtime/dispatch/x.gates.json' 2>&1 | tail -1
#   期望：Permission denied。即使以特權植入，採信端也會以 gate-ledger-foreign-author 拒。
```

### 8c. negative control（受信任身分做同樣的事**必須成功**）

```bash
# ✅ 族 7（#629）的 negative control：**該成功的必須成功**
#    (a) cortex-manager 寫得進 gate ledger 的落點；(b) cortex-gate 寫得進自己那格。
sudo -u cortex-manager sh -c ': > /var/lib/cortex/runtime/dispatch/.negctl && \
  rm -f /var/lib/cortex/runtime/dispatch/.negctl' && echo "negctl manager→dispatch: OK"
sudo install -d -o cortex-manager -g cortex-manager -m 0700 \
  /var/lib/cortex/coordinator/gate-ledger-spool/negctl
sudo setfacl -m u:cortex-gate:wx /var/lib/cortex/coordinator/gate-ledger-spool/negctl
sudo -u cortex-gate sh -c 'printf "{}" > /var/lib/cortex/coordinator/gate-ledger-spool/negctl/ledger.json' \
  && echo "negctl gate→own slot: OK" || echo "❌ 測試環境無效：族 7 結果不算數"
sudo rm -rf /var/lib/cortex/coordinator/gate-ledger-spool/negctl
#   ⚠️ 這兩條**任一條失敗**都代表帳號或 ACL 沒套上，族 7 的「全部被拒」是假綠。

# ✅ 族 1／2 的 negative control：cortex-manager 寫得進去
sudo -u cortex-manager sh -c '
set -e
A=/var/lib/cortex
printf "{}" > "$A/coordinator/.negctl.json" && rm -f "$A/coordinator/.negctl.json"
printf x > "$A/control/requests/.negctl" && rm -f "$A/control/requests/.negctl"
printf x > "$A/specs/.negctl" && rm -f "$A/specs/.negctl"
printf x > "$A/config/paulsha/.negctl" && rm -f "$A/config/paulsha/.negctl"
printf x >> "$A/registry/skill_usage.jsonl"
cat /opt/cortex/etc/cortex-manager.env >/dev/null
echo NEG-CONTROL-OK'
#   期望：印出 NEG-CONTROL-OK。若這裡也失敗 ⇒ 環境壞掉，族 1/2 的綠是假綠。

# ✅ 族 2 的第二個 negative control：reviewer 寫得進自己那格 verdict spool
sudo -u cortex-reviewer-planner sh -c \
  'printf "{}" > /var/lib/cortex/coordinator/review-verdicts/.negctl && echo NEG-CONTROL-2-OK' 2>&1 | tail -1
sudo rm -f /var/lib/cortex/coordinator/review-verdicts/.negctl

# ✅ 族 3 的 negative control：root 改得動 enforcement plane（且改完必須重啟複驗）
sudo cp /etc/systemd/system/cortex-manager.service /tmp/unit.bak
sudo sh -c 'printf "\n# negctl\n" >> /etc/systemd/system/cortex-manager.service'
sudo systemctl daemon-reload && sudo systemctl restart cortex-manager.service && echo "NEG-CONTROL-3-OK"
sudo cp /tmp/unit.bak /etc/systemd/system/cortex-manager.service
sudo systemctl daemon-reload && sudo systemctl restart cortex-manager.service

# ✅ 族 4 的 negative control：operator（有 sudo）讀得到 Manager environ
#    ⚠️ 同樣**不要用 pgrep**：Manager 的 ExecStart 是 `cortex service run`，
#       `pgrep -f paulsha_cortex` 匹配不到；空 pid 會讓這條 negative control
#       自己變成假紅。一律從 systemd 拿權威 pid。
MANAGER_PID="$(systemctl show cortex-manager.service -p MainPID --value)"
sudo cat "/proc/$MANAGER_PID/environ" | tr '\0' '\n' | head -3 && echo "NEG-CONTROL-4-OK"

# ✅ 族 5 的 negative control：cortex-manager 起**合法**的 job instance 必須成功
#    （否則族 5 的紅可能只是「polkit 把 cortex-manager 全部擋掉了」）
sudo install -d -o cortex-builder -g cortex-builder -m 0700 /var/lib/cortex/worktree/negctl5
sudo -u cortex-manager /opt/cortex/venv/bin/python - <<'PY'
from paulsha_cortex.coordinator import job_runner
instance = "negctl5"
spec = job_runner.build_job_spec(
    job_id="negctl5", instance=instance, unit=f"cortex-job@{instance}.service",
    command=["/bin/sh", "-c", "id"],
    working_directory=f"/var/lib/cortex/worktree/{instance}",
    log_path=f"/var/lib/cortex/worktree/{instance}/{instance}.log",
    env={"HOME": "/var/lib/cortex-builder", "PATH": "/usr/bin:/bin"},
)
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(job_runner.DEFAULT_JOB_SPEC_SPOOL, instance), spec))
PY
sudo -u cortex-manager systemctl start cortex-job@negctl5.service && echo "NEG-CONTROL-5-OK"
sudo grep -o "uid=[0-9]*(cortex-builder)" /var/lib/cortex/worktree/negctl5/negctl5.log
#   期望：印出 NEG-CONTROL-5-OK，且 job 的 log（**不是 journal**）顯示 uid=…(cortex-builder)
```

### 8d. 族 3 的「重啟後仍綠」複驗（spec §R9 硬性要求）

```bash
# ✅ 每個族 3／族 5.2 案例改完 MUST 實際重啟服務再驗證
sudo systemctl restart cortex-manager.service
sleep 3
systemctl is-active cortex-manager.service
sudo -u cortex-manager sh -c '
set -a; . /opt/cortex/etc/cortex-manager.env; set +a
exec /opt/cortex/venv/bin/python -m paulsha_cortex.trust_root selfcheck
' | head -20
#   期望：服務 active、自檢仍 ok=true（族 3／5 的攻擊沒有留下任何持久效果）
```

### 8e. 清理

```bash
# 🔧 sudo：清掉 R9 抽驗的全部殘留（含 drop-in 目錄——它本身就是提權面）
sudo systemctl stop "cortex-job@r9.service" "cortex-job@negctl5.service" 2>/dev/null || true
sudo rm -f /var/lib/cortex/coordinator/job-specs/builder/r9.json \
           /var/lib/cortex/coordinator/job-specs/builder/negctl5.json \
           /var/lib/cortex/r9-attack.sh
sudo rm -rf /var/lib/cortex/worktree/r9 /var/lib/cortex/worktree/negctl5 \
            /var/lib/cortex/worktree/victim \
            /var/lib/cortex/coordinator/review-verdicts/victim \
            /etc/systemd/system/cortex-job@.service.d \
            /etc/systemd/system/cortex-job-jit@.service.d
sudo systemctl daemon-reload

# ✅ 驗證：spool 內不得殘留任何抽驗用的 spec（它們是 job 的命令列）——**逐格看**
sudo ls -l /var/lib/cortex/coordinator/job-specs/{builder,reviewer,gate}
#   期望：不含 r9.json／negctl5.json／selftest.json／evil.json
```

**通過條件**：8a 兩份報告的 `t()` 測項**全部** `denied (OK)`、`d()` 測項全部
`readable (BY DESIGN)`、**沒有任何** `!! 前置缺失`；8b 族 5.1 五條與族 5.2 的
27 條**全部非 0**；8c 五組 negative control 全部印出 `*-OK`；8d 重啟後仍綠。
任一條不符 ⇒ **D6 不算通過**，`0.2.0` 不得宣告 stable（spec §R12）。

> **判讀紀律（M1 教訓）**：`SUCCEEDED (FAIL)` 出現時，**先確認測項本身測的是不是
> 該守的那一面**，再判定是不是破口。M1 的三條 `SUCCEEDED` 全部是測項寫錯
> （期望寫反／測讀取而非寫入／`rm -f` 對不存在的檔回 0），不是邊界失守——
> 反過來，一條「denied」也可能是假綠（例如 pid 根本不存在）。
> 兩個方向的誤判都要靠 negative control 與 `need()` 前置檢查擋住。

---

## 第 9 步：回滾（每階段可回滾 ＋ 全面退回 Phase 1）

Phase 1 完全不需 root 且含降級運轉安全網（`PSC_DEGRADED_OPERATION=per-case-approval`）。
任一階段出問題即退回「operator 帳號跑 ＋ 降級運轉」。

| 階段 | 症狀 | 回滾動作（`🔧 sudo`） |
|---|---|---|
| 第 1（三帳號） | 帳號建錯／名稱衝突 | `sudo userdel cortex-manager cortex-reviewer-planner cortex-builder`（逐一）；`sudo groupdel` 同名三個 group；`sudo rm -rf /var/lib/cortex-reviewer-planner`（此時尚無檔案屬於它們） |
| 第 2（樹／權限） | 權限套錯、`find -perm /022` 非空 | 重跑 `sudo sh -e /tmp/p2b-permissions.sh`（冪等）；仍不對則 `sudo rm -rf /var/lib/cortex /var/lib/cortex-manager /var/lib/cortex-reviewer-planner /var/lib/cortex-builder` 後從第 2 步重來（舊樹未動） |
| 第 3（legacy-import） | quarantine 內容不符 manifest | `sudo rm -rf /var/lib/cortex/legacy-imported`，重跑 3；`$HOME/.agents` 原地仍完整 |
| 第 4a（部署） | 新 venv 起不來 | `sudo rm -rf /opt/cortex/venv; sudo mv /opt/cortex/venv.prev /opt/cortex/venv; sudo systemctl restart cortex-manager` |
| 第 4c（system unit） | WSL 重啟後未拉起／服務起不來 | `sudo systemctl disable --now cortex-manager.service`；改回 `systemctl --user start cortex-manager.service`（舊部署仍在 `$HOME/.local/share/pipx`） |
| 第 4c（加固誤擋） | 服務起來但功能靜默失效 | 見下方「`ProtectSystem=strict` 誤擋診斷」；**臨時** drop-in 放行、**同一天**把該路徑回填 R1 登記表並重跑 permgen |
| 第 4d（monitor unit） | monitor 起不來／新樹無寫入 | `sudo systemctl disable --now cortex-monitor.service`；改回 `systemctl --user start cortex-monitor.service`（**會與 system-level Manager 雙寫**，僅救急） |
| **第 5-2（template unit ×4）** | instance 起不來／unit 語法錯 | `for S in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit; do sudo rm -f "/etc/systemd/system/$S@.service"; sudo rm -rf "/etc/systemd/system/$S@.service.d"; done; sudo systemctl daemon-reload`；(d) 一併關閉（見下一列）。**四份要一起收**——只留一部分會讓對應的 executor／persona 在 preflight fail-closed |
| **第 5-3（shim）** | job 起得來但 argv 不對／shim crash | `sudo rm -f /opt/cortex/bin/cortex-job-shim`；重新由產生器落檔並 `diff` 對齊；仍不對則關 (d) |
| **第 5-4（polkit）** | 規則語法錯／`cortex-manager` 起不了任何 job | `sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules; sudo systemctl restart polkit.service`；此時降權面完全關閉（fail-closed，job 起不來但無提權） |
| **第 5-5（切換點）** | 降權後 job 全數 needs_human | `sudo sed -i '/^PSC_JOB_RUNNER=/d;/^PSC_BUILDER_ACCOUNT=/d;/^PSC_BUILDER_HOME=/d' /opt/cortex/etc/cortex-manager.env; sudo systemctl restart cortex-manager.service`（回 `direct`）；Manager 以 `per-case-approval` 不 spawn job 運轉 |
| 第 8（R9 有紅） | 任一攻擊成功（含族 5） | **立即**停 job 派工，執行下方「全面回退」，在 #584 記錄該條攻擊路徑；D6 判定未通過 |

### 全面回退到 Phase 1（降級運轉）

```bash
# 🔧 sudo：停掉並移除 Phase 2 的一切（含 A+B 的三個 root-owned 物件與三帳號）
sudo systemctl disable --now cortex-manager.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/cortex-manager.service
for S in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit; do
  sudo rm -f "/etc/systemd/system/$S@.service"
  sudo rm -rf "/etc/systemd/system/$S@.service.d"
done
sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo rm -f /opt/cortex/bin/cortex-job-shim
#   （EnvironmentFile 隨 /opt/cortex 一併移除，PSC_JOB_RUNNER 自然失效）
sudo systemctl daemon-reload
sudo systemctl restart polkit.service 2>/dev/null || true

# 🔧 sudo：新樹整棵丟棄（舊 state 從未被併入，故無資料損失）＋ 移除三帳號
sudo rm -rf /var/lib/cortex /var/lib/cortex-manager /var/lib/cortex-reviewer-planner \
            /var/lib/cortex-builder /opt/cortex
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  sudo userdel "$U" 2>/dev/null || true
  sudo groupdel "$U" 2>/dev/null || true
done

# ✅ 回到 operator 帳號部署 ＋ 降級運轉
export PSC_DEGRADED_OPERATION=per-case-approval
systemctl --user start cortex-manager.service
python3 -m paulsha_cortex.trust_root selfcheck    # 預期回到有 WARN 的 Phase 1 狀態
echo "PSC_DEGRADED_OPERATION=$PSC_DEGRADED_OPERATION"
getent passwd cortex-manager cortex-reviewer-planner cortex-builder || echo "accounts removed: OK"
```

> **回滾原則**：舊 state 走 legacy-import（第 3 步）而非併入，所以回滾時新樹可整棵
> 丟棄而不遺失舊資料；`legacy-imported/` 是唯讀副本，兩邊互不污染。
> **join gate**：回退期間 D6 維持未通過，`0.2.0` **不得**宣告 stable，且**不得**以
> 有界殘餘風險豁免放行（spec §R12）。

---

## WSL2 專屬風險與診斷

### A. system unit 開機拉起的驗證（WSL 重啟測試）

WSL2 沒有傳統 boot；systemd 在第一個 shell 進入時才起。lingering 只對 `--user`
unit 有意義，**system unit 不需要**——但必須實測「WSL 重啟後有沒有自己拉起來」。

```bash
# ✅ 1. 前置：確認 systemd 已在 wsl.conf 啟用
grep -A2 "^\[boot\]" /etc/wsl.conf
#   期望：systemd=true

# ✅ 2. 確認已 enable（不是只有 start）
systemctl is-enabled cortex-manager.service     # 期望 enabled
systemctl show cortex-manager.service -p WantedBy
#   期望：WantedBy=multi-user.target
ls -l /etc/systemd/system/multi-user.target.wants/cortex-manager.service
#   期望：symlink 存在
#   注意：cortex-job@.service／cortex-job-jit@.service 都是 template，
#         **不需要也不應該** enable。

# ---- 3. 在 Windows 端執行：wsl --shutdown ----
#      然後重新開一個 WSL shell，再回來跑本段第 4 項。

# ✅ 4. 重啟後驗證（重點：不需任何人工介入就已 active）
uptime                                          # 確認真的重啟過
systemctl is-system-running                     # 期望 running（degraded 也記錄）
systemctl is-active cortex-manager.service      # 期望 active
systemctl show cortex-manager.service -p ActiveEnterTimestamp -p NRestarts
sudo journalctl -u cortex-manager.service -b --no-pager | head -30
#   期望：本次 boot 的 log 裡服務正常啟動，無 EnvironmentFile／ReadWritePaths 錯誤

# ✅ 5. polkit 也必須在重啟後自己起來（否則降權在重啟後靜默失效）
systemctl is-active polkit.service || systemctl is-active polkitd.service

# ✅ 6. 降權正向：重啟後仍可起 job instance
#    ⚠️ 8e 已清掉 negctl5 的 spec 與 worktree，這裡必須**先重新備好**再起——
#       否則失敗的原因會是「spec 缺席」而不是「降權失效」，診斷指向錯誤的層。
sudo install -d -o cortex-builder -g cortex-builder -m 0700 /var/lib/cortex/worktree/negctl5
sudo -u cortex-manager /opt/cortex/venv/bin/python - <<'PY'
from paulsha_cortex.coordinator import job_runner
instance = "negctl5"
spec = job_runner.build_job_spec(
    job_id="negctl5", instance=instance, unit=f"cortex-job@{instance}.service",
    command=["/bin/sh", "-c", "id"],
    working_directory=f"/var/lib/cortex/worktree/{instance}",
    log_path=f"/var/lib/cortex/worktree/{instance}/{instance}.log",
    env={"HOME": "/var/lib/cortex-builder", "PATH": "/usr/bin:/bin"},
)
print("wrote:", job_runner.write_job_spec(
    job_runner.job_spec_path(job_runner.DEFAULT_JOB_SPEC_SPOOL, instance), spec))
PY
sudo -u cortex-manager systemctl start cortex-job@negctl5.service \
  && echo "降權重啟後仍可用" || echo "!! 降權失效，查 polkit"
sudo grep -o "uid=[0-9]*(cortex-builder)" /var/lib/cortex/worktree/negctl5/negctl5.log
# 🔧 sudo：驗完立刻清掉（spec 是 job 的命令列，不留過夜）
sudo rm -f /var/lib/cortex/coordinator/job-specs/builder/negctl5.json
sudo rm -rf /var/lib/cortex/worktree/negctl5

# ✅ 7. 降權反向：重啟後提權仍被拒（規則沒有因重啟而失效）
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "exit=$?"   # 期望非 0
```

**若重啟後未拉起**：依序查
(1) `/etc/wsl.conf` 的 `systemd=true`；
(2) `systemctl is-enabled`（只 `start` 沒 `enable` 是最常見原因）；
(3) `journalctl -b -u cortex-manager` 的失敗原因；
(4) `systemctl list-units --failed`。
仍不行 → 第 9 步的「第 4c」列回滾（改回 `--user` unit）。

### B. `ProtectSystem=strict` 誤擋的診斷

**症狀**：服務 `active` 但功能靜默失效（寫不進去、evidence 沒落檔、job 起不來）。

```bash
# ✅ 1. 先看有沒有明確的拒絕
sudo journalctl -u cortex-manager.service -b --no-pager | grep -Ei \
  "read-only file system|erofs|eperm|eacces|permission denied|operation not permitted"

# ✅ 2. 看實際生效的白名單（和產生器輸出對照）
systemctl show cortex-manager.service -p ProtectSystem -p ReadWritePaths -p ProtectHome
diff <(python3 -m paulsha_cortex.trust_root unit four-way --manager | grep '^ReadWritePaths=') \
     <(systemctl show cortex-manager.service -p ReadWritePaths | tr ' ' '\n' | sed 's/^/ReadWritePaths=/' | head -50)

# ✅ 3. 在**同一組沙箱條件**下重現（root 用 systemd-run 診斷，不放行給 manager）
sudo systemd-run --pipe --wait --uid=cortex-manager \
  --property=ProtectSystem=strict --property=ProtectHome=yes --property=PrivateTmp=yes \
  --property="ReadWritePaths=/var/lib/cortex/coordinator" \
  /bin/sh -c 'touch /var/lib/cortex/coordinator/.probe && echo WRITE-OK; touch /var/lib/cortex/specs/.probe || echo "specs blocked"'
#   ↑ 逐條加/減 ReadWritePaths，找出到底缺哪一條
#   注意：這條是 **operator 以 sudo** 起的診斷用 transient unit，
#         與「cortex-manager 自己不能起 transient unit」不衝突（族 5.1 測的是後者）。

# ✅ 4. 檢查有沒有落在 ProtectHome 遮住的區域（最常見的靜默失效）
systemctl show cortex-manager.service -p ReadWritePaths | tr ' ' '\n' | grep -E "^/home|^/root" \
  && echo "!! RWP 落在 ProtectHome 遮蔽區，必定失效"

# ✅ 5. job template 側的同一診斷（job 起得來但寫不進 worktree 時）
systemctl show "cortex-job@negctl5.service" -p ReadWritePaths -p ProtectSystem 2>/dev/null
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job | grep '^ReadWritePaths=') \
     <(grep '^ReadWritePaths=' /etc/systemd/system/cortex-job@.service)

# ✅ 6. 臨時放行（僅供診斷；當天必須回填登記表）
sudo systemctl edit cortex-manager.service     # 加入 [Service] ReadWritePaths=<被擋路徑>
sudo systemctl daemon-reload && sudo systemctl restart cortex-manager.service
```

**常見誤擋清單（依發生機率）**：

| 症狀 | 根因 | 正解 |
|---|---|---|
| dispatch log／gate ledger 寫不進 | `log_dir` 是**相對路徑** `runtime/dispatch/<slice>`，隨 `WorkingDirectory` 落在 `/var/lib/cortex/runtime/dispatch` | 已由登記表 `gate-ledger` 導出到 RWP；若改了 `WorkingDirectory` 必須同步 |
| git／gh 報 cache 寫入失敗 | HOME 由 root 擁有，只有 `cache/` 可寫 | 確認 `XDG_CACHE_HOME` 已在 unit 內設定且該路徑在 RWP |
| 服務起不來、Python 直接 crash | `MemoryDenyWriteExecute=yes` 撞到 C extension 的 ctypes trampoline | 先單獨註解該行複測；確認是它之後在 unit 產生器裡記錄例外理由 |
| 任何 `$HOME/.agents` 路徑 | `ProtectHome=yes` 讓 HOME 不可見 | **這是刻意的**——代表還有程式碼在走舊 fallback，回頭修路徑契約，不要放寬 unit |
| socket 建不出來 | `run_root` 不在 RWP | 檢查 `runtime-run-tree` 是否仍在登記表；重跑產生器 |
| job 起得來但寫不進自己的 worktree | template unit 的 `%i` 展開與實際 worktree 名不一致 | 對照 5-6 的 `install -d …/worktree/$JOB` 與 instance 名；兩者必須同名 |

> **鐵律**：被擋的路徑**只能**經「回填 R1 登記表 → 重跑 permgen → 重新落檔 unit」
> 進入 `ReadWritePaths`。drop-in 只能作為**當天的**臨時措施，不得成為長期狀態——
> 否則 unit 就不再是登記表的機械投影，`diff` 對齊檢查會紅。
> **template unit 的 drop-in 更嚴**：`/etc/systemd/system/cortex-job@.service.d/`
> 與 `cortex-job-jit@.service.d/`（#643）一旦
> 存在就是提權面（族 5.2 有一條專門測它建不起來），診斷完**必須立刻刪除**。

### C. WSL2 其他已知風險

1. **polkit 未安裝／未啟動**：systemd 對 unprivileged client 的授權在 polkit 不可用時
   **一律拒絕**（fail-closed）。表現為「Manager 完全 spawn 不了 job」而非提權——
   安全但功能全停。執行前提第 6 項已檢查；重啟後由「A. 第 5 項」複驗。
2. **`sudo` 需密碼**：所有 `🔧 sudo` 步驟皆互動式，**不可假設自動化**；
   本 runbook 刻意不使用 `sudo -n`（族 5 的 `sudo -n true` 例外，那是攻擊測試）。
3. **`/proc` 隱藏會製造族 4 的假綠**：unit 帶 `ProtectProc=invisible` ＋
   `ProcSubset=pid`，**job 眼中的 `/proc` 只有自己**。因此
   (a) 在 job 內跑 `pgrep -u cortex-manager …` **必回空**，用它取得的 pid 是空字串，
   後續每一條都變成對 `/proc//…` 操作，因「路徑不存在」而失敗、被 `t()` 記成
   `denied`——**那是假綠**：測到的是 pid 不存在，不是權限被拒。
   (b) 正確作法：由 **operator 從外部**取
   `systemctl show cortex-manager.service -p MainPID --value`，以 spec 的 `env`
   （pass 1）或 `--setenv`（pass 2）注入，讓攻擊真的打在活著的 pid 上；並另加一條
   `test -d /proc/<pid>`（第 8 步的 T4.0）**直接把「pid 不可見」這件事本身測出來**，
   而不是讓它偽裝成其他測項的 denial。
   (c) 打在活 pid 上之後，讀他人 `/proc/<pid>/environ` 仍可能是 `ENOENT` 或
   `EACCES`——**兩者都算拒絕**（`t()` 只看 rc 非 0），但此時「拒絕」是真的。
4. **WSL2 的 `busctl` 可能不在 PATH**：族 5.1e 若報 `command not found`，
   以 `sudo apt-get install systemd` 補齊後重測；**不可**因為工具缺席就跳過該條
   （它測的是繞開 CLI 的直接 D-Bus 路徑）。

---

## 附錄 A：本 runbook 的自我檢查

```bash
# ✅ unit／polkit／shim 與產生器沒有漂移（建議排程每日跑）
diff <(python3 -m paulsha_cortex.trust_root unit four-way --manager) /etc/systemd/system/cortex-manager.service
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job)     /etc/systemd/system/cortex-job@.service
diff <(python3 -m paulsha_cortex.trust_root unit four-way --job --profile jit) /etc/systemd/system/cortex-job-jit@.service
diff <(python3 -m paulsha_cortex.trust_root polkit four-way --template) /etc/polkit-1/rules.d/49-cortex-downgrade.rules
diff <(python3 -m paulsha_cortex.trust_root shim four-way)            /opt/cortex/bin/cortex-job-shim

# ✅ 部署樹沒有被 pipx 殘留污染（第 4a／第 6 步的兩個必補步驟）
sudo grep -rIl -- "/.local/share/pipx" /opt/cortex/venv | head    # 期望：空輸出
sudo find /opt/cortex/venv -name "pipx_shared.pth" | head          # 期望：空輸出

# ✅ job-spec spool 沒有留下過夜的 spec（每一份都是某個 job 的命令列）——**逐格看**
sudo ls -l /var/lib/cortex/coordinator/job-specs/{builder,reviewer,gate}
# ✅ #657：六份 unit 各指自己那格（共用一條＝該角色每個 job 必以 78/CONFIG 收場）
for U in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit \
         cortex-gate-job cortex-gate-job-jit; do
  systemctl cat "$U@.service" | grep -E "^Environment=PSC_JOB_SPEC_SPOOL="
done | sort -u | wc -l          # 期望：3

# ✅ 沒有殘留的 template drop-in（族 5.2 的持久化面）——**四份都要看**
for S in cortex-job cortex-job-jit cortex-reviewer-job cortex-reviewer-job-jit; do
  ls -la "/etc/systemd/system/$S@.service.d" 2>/dev/null \
    && echo "!! $S 有 drop-in，查來源"
done; echo "drop-in scan done"
# ✅ #643：同一角色兩份 unit 的加固差異必須**只有** MemoryDenyWriteExecute 一項
for PAIR in "cortex-job cortex-job-jit" "cortex-reviewer-job cortex-reviewer-job-jit"; do
  set -- $PAIR
  echo "--- $1 vs $2"
  diff <(grep -E "^[A-Z][A-Za-z]*=" "/etc/systemd/system/$1@.service") \
       <(grep -E "^[A-Z][A-Za-z]*=" "/etc/systemd/system/$2@.service") \
    | grep -E "^[<>]" | sort
done
#   期望每一組恰好兩行：`< MemoryDenyWriteExecute=yes` 與 `> MemoryDenyWriteExecute=no`。
#   出現第三行 ⇒ 剖面在加固表以外分岔了，回第 5-2 步重新落檔。
# ✅ #615：兩個角色的 User= 確實不同，且都不是 cortex-manager
grep -h "^User=" /etc/systemd/system/cortex-job@.service \
                 /etc/systemd/system/cortex-reviewer-job@.service | sort -u
#   期望恰好兩行：User=cortex-builder、User=cortex-reviewer-planner。

# ✅ 三分帳號仍是三個、互不交集、皆無 sudo
for U in cortex-manager cortex-reviewer-planner cortex-builder; do id -nG "$U"; done

# ✅ 權限沒有漂移
sudo find /var/lib/cortex /opt/cortex -perm /022 -print | head

# ✅ executor toolchain 仍可用，且版本沒有跟 operator 側分岔（#640）
sudo -u cortex-builder env HOME=/var/lib/cortex-builder \
  PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin codex --version
codex --version
#   期望：兩行逐字相同。分岔＝某一側被升級了而另一側沒有——那正是裁決 (a) 要防的漂移。

# ✅ 憑證仍是「檔 job-owned／目錄 root-owned」（#640 裁決 (b)）
stat -c '%n %U:%G %a' /var/lib/cortex-builder/.codex /var/lib/cortex-builder/.codex/auth.json
#   期望：目錄 root:root 755、檔案 cortex-builder:cortex-builder 600

# ✅ Manager 的 gh 憑證仍是「兩個檔、兩種 owner」（#666；第 4g 步）
stat -c '%n %U:%G %a' /var/lib/cortex-manager/.config/gh \
  /var/lib/cortex-manager/.config/gh/hosts.yml \
  /var/lib/cortex-manager/.config/gh/config.yml
#   期望：目錄 root:root 755、hosts.yml cortex-manager:cortex-manager 600、
#         config.yml root:root 644。
#   ⚠️ 若 config.yml 變成 cortex-manager owned，`aliases` 的 `!` shell alias 執行面
#      就打開了——那是最容易被「順手統一成同一種 owner」弄壞的一格。
sudo -u cortex-manager env HOME=/var/lib/cortex-manager gh auth status >/dev/null \
  && echo "manager gh: OK"

# ✅ gate 跑得動 pytest，且版本沒有無聲換掉（#666；第 4f 步）
sudo -u cortex-gate env HOME=/var/lib/cortex-gate \
  PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin python3 -m pytest --version
#   期望：與部署紀錄裡記下的那一版逐字相同。換掉了而沒人知道 ⇒ gate 判準已漂移。

# ✅ 窮舉盤點仍雙向封閉（#666；第 4h 步）——非空即代表登記表已落後於程式碼
python3 -c "from paulsha_cortex.trust_root import permgen as p
assert p.uncovered_run_dependencies() == (), p.uncovered_run_dependencies()
assert p.unlisted_roster_entries() == (), p.unlisted_roster_entries()
print('inventory closed:', len(p.RUN_EXTERNAL_DEPENDENCIES), 'deps;',
      len(p.deferred_run_dependencies()), 'deferred')"

# ✅ 產生器本身的等式測試（含 ReadWritePaths 無遺漏無多餘）
python3 -m pytest tests/test_trust_root_permgen_p2a.py tests/test_trust_root_permgen_p2b.py \
  tests/test_trust_root_external_deps_exhaustive_666.py -q
```

---

## 附錄 B：降級備援——transient unit（**附殘餘風險**）

> **這不是主路徑。** 只有在「template 路徑在本機不可用」（例如 template unit 起不來、
> shim 尚未落地而又必須立刻恢復 job 派工）時才臨時使用，且**必須在 #584 記錄
> 啟用時間、原因、預計關閉時間**。

程式碼側 `PSC_JOB_RUNNER=systemd-run` 已於 #603 落地，polkit 側改用 transient 規則：

```bash
# 🔧 sudo：換成 transient 規則（unit 名前綴 cortex-job-，非 @ 實例）
python3 -m paulsha_cortex.trust_root polkit four-way --transient \
  | sudo tee /etc/polkit-1/rules.d/49-cortex-downgrade.rules >/dev/null
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# 🔧 sudo：切換點改回 systemd-run
sudo sed -i 's/^PSC_JOB_RUNNER=.*/PSC_JOB_RUNNER=systemd-run/' /opt/cortex/etc/cortex-manager.env
sudo systemctl restart cortex-manager.service

# ✅ 契約對齊：polkit 規則的 pattern 前綴必須等於 job_runner 的常數
python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
from paulsha_cortex.trust_root import permgen
prefix = permgen.transient_unit_prefix(permgen.DEFAULT_LAYOUT)
assert prefix == job_runner.UNIT_NAME_PREFIX, (prefix, job_runner.UNIT_NAME_PREFIX)
print("unit prefix contract OK:", prefix)
PY

# ✅ 正向：transient job 起得來且降到 cortex-builder
sudo -u cortex-manager systemd-run --quiet --collect --pipe --wait \
  --unit=cortex-job-smoke-00000000.service \
  --uid=cortex-builder --gid=cortex-builder --service-type=exec \
  --property=NoNewPrivileges=yes \
  /bin/sh -c 'id; echo "GH_TOKEN=[$GH_TOKEN]"; ls -l /proc/self/fd'

# ⚠️ 殘餘風險實測：**這一條會成功**，且這正是降級的代價
sudo -u cortex-manager systemd-run --quiet --unit=cortex-job-probe-00000000.service \
     --uid=0 --pipe --wait /bin/id; echo "exit=$?"
#   期望：**成功並印出 uid=0(root)**。polkit 看不到 --uid，因此
#   「只能降到 cortex-builder」這一半在本備援下**只由 Manager 端 argv 產生器保證**。

# ✅ 產生器對本方案的殘餘風險自述（貼進 #584）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
rule = permgen.build_polkit_rule(
    permgen.SCHEMES["four-way"], plan=permgen.PolkitPlan.TRANSIENT
)
for r in rule.residual_risks:
    print("-", r)
PY
```

**降級期間的殘餘風險摘要**（相對於 A+B 主路徑）：

| 面向 | A+B 主路徑 | transient 備援 |
|---|---|---|
| 「降到哪個帳號」由誰強制 | **OS**（root-owned unit 的 `User=`） | **code level**（Manager 的封閉 argv 產生器） |
| `cortex-manager` 被攻陷後能否起 root job | ✘（5-7 (2) 實測被拒） | **✔（可以）** |
| 特權屬性（`AmbientCapabilities=` 等） | 呼叫端提都提不了 | 呼叫端可以傳，靠 argv 產生器不傳 |
| 三分帶來的保護 | 仍完整（injection 可達進程無 grant） | 仍完整——**這是三分在降級時的主要價值** |

**關閉備援**：把 5-4 的 template 規則落回、`PSC_JOB_RUNNER=systemd-template`，
再跑一次 5-7 的 11 條反向測試確認邊界恢復。

---

## 附錄 C：與第二輪裁決的差異（給讀過舊版 runbook 的人）

| 舊版（0816 第二輪） | 本版（0816 第三輪） |
|---|---|
| 第 1 步建**兩**個帳號（`cortex-svc` / `cortex-builder`） | 建**三**個（`cortex-manager` / `cortex-reviewer-planner` / `cortex-builder`） |
| 全文 `two-way` | 全文 `four-way`；`two-way` 僅存於程式碼作為對照組 |
| 第 5 步 A／B 並列，**待 operator 拍板** | 第 5 步 **A+B 合一，無分歧**；transient 降為附錄 B 備援 |
| polkit 授 transient 建立（方案 A） | polkit **不授** transient 建立；只放行四個具名模板的 start/stop：`cortex-job@` / `cortex-job-jit@`（#643 加固剖面）/ `cortex-reviewer-job@` / `cortex-reviewer-job-jit@`（#615 job 角色），皆 `*.service`——**四個具名模板，不是任意 unit** |
| `ExecStart=` 指向 spool 內的 `run.sh` | 指向 **root-owned shim** `/opt/cortex/bin/cortex-job-shim`（C 層搬進 root-owned 檔） |
| `PSC_JOB_RUNNER=systemd-run` | `PSC_JOB_RUNNER=systemd-template`（`systemd-run` 僅備援用） |
| R9 四族，subject 為 `cortex-builder` | R9 **五族**，subject 為 builder ＋ reviewer-planner，新增族 5 privilege-boundary |
| 殘餘風險：授權帳號可起任意 UID | 殘餘風險：**僅剩 `cortex-manager` 的 supply-chain 類**（見 5-8） |
