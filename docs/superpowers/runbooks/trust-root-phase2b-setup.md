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
| **降權機制** | **A+B 並行，單一路徑**。A＝UID **三分提前**；B＝**root-owned template unit**（`cortex-job@.service`，`User=` 寫死）。C（code-level argv 保證）自動保留為第三層，由 **root-owned shim** 承接 | 第 1、5 步 |
| **UID 方案** | **三分為唯一路徑**：`cortex-manager`（Manager＋monitor，durable state owner，持 spawn 授權，**不跑任何模型程式碼**）／`cortex-reviewer-planner`（reviewer＋planner 模型 job）／`cortex-builder`（builder 模型 job）。`permgen.THREE_WAY_SCHEME` 由備選轉為**定案方案** | 第 1 步 |
| durable state 路徑 | **`/var/lib/cortex`**；worktree pool＝**`/var/lib/cortex/worktree`** | 第 2 步 |
| legacy-import | **物理隔離 ＋ hash manifest**（無簽章；簽章屬 Phase 3）。切換前 in-flight job **手動收尾** | 執行前提、第 3 步 |
| Manager 部署 | **`/opt/cortex`**（root 擁有，對服務唯讀）；system-level unit，`User=cortex-manager` | 第 4 步 |
| `ReadWritePaths` | **由 R1 登記表經 permgen 機械產生**，不手寫 | 第 4c 步 |
| root 命令 codify | **不 codify**——不提供 `cortex install trust-root --system`；cortex 只產生命令字串 | 第 6 步 |
| R9 | **手動抽驗**（五族；完整自動化矩陣屬另一工項） | 第 8 步 |

> **歷史註記（二分）**：0816 第二輪曾以二分（`cortex-svc` / `cortex-builder`）先行、
> 三分為保留彈性；第三輪裁決後二分（`permgen.TWO_WAY_SCHEME`）**不再是可選路徑**，
> 僅保留在程式碼中作為方案參數化的對照組，本 runbook 全文一律 `three-way`。

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
| **M1** | 三帳號建立、**檔案權限面完整三分**、builder job 經 `cortex-job@.service` 降權、polkit 只授 `cortex-manager` 對 `cortex-job@*` | ✅ 本 runbook 全程 |
| **M2** | reviewer／planner job 也改經 template instance（`User=cortex-reviewer-planner`）落到自己的帳號 | ⏳ 程式碼工項（#603 follow-up；範圍以該 PR 實際落地為準） |

**M1 下的誠實邊界**：`launcher.SubprocessLauncher._degraded_runner()` 目前只對 **builder
persona** 降權（`review_only`＝reviewer、`read_only`＝planner 兩者皆非才降權）；因此在
M2 之前，reviewer／planner 仍在 Manager 行程內以 `cortex-manager` 身分執行。
「**injection 可達的進程皆無 spawn 授權**」這條在 M1 **只對 builder 成立**——而 builder
正是攻擊面最大的那個（唯一會跑 untrusted repo code 的 persona）。M2 落地前，
reviewer／planner 的三分只在**檔案權限層**成立（第 8 步族 2 會實測這一層）。

---

## 執行前提（開工前逐項確認，全部 `✅ 驗證`）

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
s = permgen.SCHEMES["three-way"]
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
#   期望（#603 follow-up PR 落地後）：True。
#   若 False：第 5 步的 (a)(b)(c) 仍可全部安裝並用 5-7 的反向測試證明邊界，
#   只有 (d) 切換點暫不打開（Manager 維持 per-case-approval 不 spawn job）。
```

**通過條件**：1–8 全部符合期望；`equation` 回傳 `ok: true`；baseline JSON 已存檔；
第 9 項無論真假都**記錄**在 #584（決定本次是否走到 (d)）。
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
| 執行前提 | 0 | 9 |
| 產生器＝單一真相 | 0 | 6 |
| 第 1 步：建三帳號 | 4 | 5 |
| 第 2 步：目標樹與權限 | 2 | 12 |
| 第 3 步：legacy-import | 2 | 8 |
| 第 4 步：Manager 部署與 unit | 5 | 12 |
| **第 5 步：降權（A+B）** | **7** | **24** |
| 第 6 步：升級流程 | 2 | 5 |
| 第 7 步：切換驗收 | 0 | 3 |
| 第 8 步：R9 抽驗（五族） | 4 | 17 |
| 第 9 步：回滾 | 3 | 1 |
| WSL2 風險與診斷 | 1 | 12 |
| 附錄 A：自我檢查 | 0 | 5 |
| 附錄 B：降級備援 | 2 | 3 |
| **合計** | **32** | **122** |

（統計方式：全文 `🔧`／`✅` 標記出現次數，扣除說明性用法——「標記約定」的定義行、
段落標題內的標記、以及表格裡當狀態記號用的那幾個。）

- **步驟數**：9 步（未變）＋ 3 個附錄；第 5 步由「兩個並列方案（5-A 六節／5-B 四節，
  共 10 節）」收斂為**一條九節路徑**（5-1…5-9）。
- **反向測試**：原本分散在 5-A-5（4 條，其中 1 條「已知不會被拒」）與 5-B-4（7 條），
  收斂後集中在 5-7（**11 條**），且**全部期望為「被拒」**——不再有任何一條期望成功。
- **R9 族數**：4 → **5**（新增族 5 privilege-boundary），且族 1–4 各跑**兩個 subject**
  （builder ／ reviewer-planner），實測條數約為舊版的兩倍。

---

## 產生器＝權限／unit／polkit／shim 的單一真相

`chown`／`chmod`／`setfacl`／unit 內容／polkit 規則**一律不手寫**，全部由 permgen 由
R1 登記表機械產生。**三分（`three-way`）是唯一 scheme**：

```bash
# ✅ 完整權限計畫（JSON，含每項 rationale）
python3 -m paulsha_cortex.trust_root permissions three-way

# ✅ 可直接執行的權限命令（帶真實絕對路徑，無 placeholder）
python3 -m paulsha_cortex.trust_root permissions three-way --commands --paths

# ✅ 骨架目錄（非登記表資產的父層）
python3 -m paulsha_cortex.trust_root scaffold three-way

# ✅ Manager system unit 內容（User=cortex-manager，ReadWritePaths 由登記表導出）
python3 -m paulsha_cortex.trust_root unit three-way --manager

# ✅ job template unit 內容（User=cortex-builder 硬寫死；B 的核心）
python3 -m paulsha_cortex.trust_root unit three-way --job

# ✅ 降權 polkit 規則內容（只放行 cortex-job@*.service 的 start/stop）
python3 -m paulsha_cortex.trust_root polkit three-way --template
```

**尚未由已 merge 介面提供、將由 #603 follow-up PR（template-instance 模式）提供的兩項**：

```bash
# ⏳ root-owned shim 內容（/opt/cortex/bin/cortex-job-shim）
#    將由該 PR 以下列形式提供（命令名以該 PR 實際落地為準）：
#      python3 -m paulsha_cortex.trust_root shim three-way
#    在它落地前：第 5-3 節不執行，template unit 的 ExecStart 維持產生器當下的形狀。

# ⏳ job-spec spool 的欄位契約（<job spool>/<id>/ 底下由 Manager 寫的執行規格）
#    同一 PR 定義；本 runbook **不自行捏造 spec 格式**——正向 smoke 一律以
#    「產生器印出什麼 ExecStart，就照那個形狀準備」（見 5-6）。
```

> **一律以產生器輸出為準**：本 runbook 的每個落檔步驟後面都跟一條 `diff` 驗證，
> 因此上述兩項一旦落地、內容改變，`diff` 會立刻抓到漂移，runbook 不需改寫。

---

## 第 1 步：建 UID（**三分**）

建立三個 **system service 帳號**，皆 **no-login**、**home 由 root 擁有**。
慣例：每帳號一個同名 primary group（權限產生器的 ACL 以此為前提）。

| 帳號 | 跑什麼 | durable state owner | 持 spawn 授權（polkit subject） |
|---|---|:--:|:--:|
| `cortex-manager` | Manager ＋ monitor。**不跑任何模型程式碼** | ✔ | ✔ |
| `cortex-reviewer-planner` | reviewer ＋ planner 模型 job | ✘ | ✘ |
| `cortex-builder` | builder 模型 job（唯一會跑 untrusted repo code） | ✘ | ✘ |

```bash
# ✅ 先取產生器對三分方案的 HOME／cache 計畫——**不手寫路徑**
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
s = permgen.SCHEMES["three-way"]
for path, owner, group, mode in permgen.DEFAULT_LAYOUT.scaffold_directories(s):
    if path.startswith("/var/lib/cortex-"):
        print(f"{path}\t{owner}:{group}\t{mode:04o}")
PY
#   期望（#603 follow-up PR 落地後）：三組 HOME＋cache，名稱與帳號一致。
#   ⚠️ 目前已知缺口（permgen 的 PathLayout 尚未跟上三分定案）：
#      (a) Manager 的 HOME 仍沿用二分時代的 `/var/lib/cortex-svc`；
#      (b) `cortex-reviewer-planner` 沒有 HOME／cache 條目。
#      **以產生器輸出為準**：下方 useradd 的 --home-dir 一律填它印出的值；
#      待該 PR 更名後以 `sudo usermod --home <新值> cortex-manager` 同步並重跑第 2 步。

# 🔧 sudo：cortex-manager（Manager＋monitor；durable state owner；持 spawn 授權；不跑模型）
sudo groupadd --system cortex-manager
sudo useradd  --system --gid cortex-manager \
     --home-dir /var/lib/cortex-svc --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex manager+monitor (durable state owner, spawn grant, no model code)" \
     cortex-manager
#   ↑ --home-dir 取自上面產生器輸出的那一行（目前為 /var/lib/cortex-svc）。

# 🔧 sudo：cortex-reviewer-planner（reviewer＋planner 模型 job）
sudo groupadd --system cortex-reviewer-planner
sudo useradd  --system --gid cortex-reviewer-planner \
     --home-dir /var/lib/cortex-reviewer-planner --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex reviewer/planner model job" cortex-reviewer-planner

# 🔧 sudo：cortex-builder（builder 模型 job；唯一跑 untrusted repo code）
sudo groupadd --system cortex-builder
sudo useradd  --system --gid cortex-builder \
     --home-dir /var/lib/cortex-builder --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex headless builder job" cortex-builder

# 🔧 sudo：reviewer-planner 的 HOME／cache（產生器尚未涵蓋時手動補；模式與 builder 對齊）
sudo install -d -o root -g root -m 0755 /var/lib/cortex-reviewer-planner
sudo install -d -o root -g root -m 0755 /var/lib/cortex-reviewer-planner/.codex
sudo install -d -o cortex-reviewer-planner -g cortex-reviewer-planner -m 0700 \
     /var/lib/cortex-reviewer-planner/cache
#   ↑ 這四行在 #603 follow-up PR 把 reviewer-planner 納入 scaffold 後即可刪除，
#     改由第 2 步的 scaffold script 一併產生（屆時 diff 會提醒）。
```

**為何 `--no-create-home`**：三個帳號的 HOME 由第 2 步以 **root 擁有**的方式建立
（`useradd --create-home` 會把 HOME 建成帳號自己擁有）。HOME 若由帳號自己擁有，
它就能 rename 掉 `~/.codex`／`~/.gitconfig` 這類 root-owned 設定的**父目錄**——
父目錄可寫者能 unlink／rename 子物件，等於保護失效。只有 `cache/` 子目錄開放給帳號寫。

**群組設計理由**：跨帳號存取一律走 **per-account POSIX ACL**（見 permgen 輸出的
`setfacl -m u:<acct>:rX`），**不**用共用 group 開放，避免「一個 group 開了就全開」。
三分之後這條更關鍵：`cortex-reviewer-planner` 對 verdict spool 的授權是
**write-only（`wx` 無 `r`）**，只有 per-account ACL 表達得出來。

```bash
# ✅ 驗證：三帳號存在、shell 為 nologin、三個 group 互不交集
getent passwd cortex-manager cortex-reviewer-planner cortex-builder
id cortex-manager; id cortex-reviewer-planner; id cortex-builder
#   期望：uid/gid 各自成對；三者的 groups 互不包含對方
getent group cortex-manager cortex-reviewer-planner cortex-builder

# ✅ 驗證：三個帳號都登不進去（nologin），且都不在 sudo／wheel 群組
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  printf '%s shell=%s groups=%s\n' "$U" "$(getent passwd "$U" | cut -d: -f7)" "$(id -nG "$U")"
done
#   期望：shell 全為 /usr/sbin/nologin；groups 只含自己的同名 group

# ✅ 驗證：模型 job 帳號兩兩互不可讀對方 HOME（三分的第一條不變式）
sudo -u cortex-builder ls /var/lib/cortex-reviewer-planner/cache 2>&1 | tail -1
sudo -u cortex-reviewer-planner ls /var/lib/cortex-builder/cache 2>&1 | tail -1
#   期望：兩者皆 Permission denied（cache 為 0700 且 owner 不同）

# ✅ 驗證：三帳號皆無 sudo 授權
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  sudo -u "$U" sudo -n true 2>&1 | tail -1
done
#   期望：全部失敗（不可有任何一個成功）
```

**回滾**：
```bash
sudo userdel cortex-manager; sudo userdel cortex-reviewer-planner; sudo userdel cortex-builder
sudo groupdel cortex-manager; sudo groupdel cortex-reviewer-planner; sudo groupdel cortex-builder
sudo rm -rf /var/lib/cortex-reviewer-planner
```
（此時尚無任何檔案屬於它們。）

---

## 第 2 步：建目標樹並套用權限（全部機械產生）

裁決：`AGENTS_ROOT=/var/lib/cortex`、`WORKTREE_ROOT=/var/lib/cortex/worktree`、
`DEPLOY_ROOT=/opt/cortex`。這些值已固化在 `permgen.DEFAULT_LAYOUT`，下列命令直接引用。

### 2a. 產生兩份 script 並**先讀過**

```bash
# ✅ 產生骨架目錄 script（非登記表資產的父層：/opt/cortex、HOME、job spool…）
python3 -m paulsha_cortex.trust_root scaffold three-way > /tmp/p2b-scaffold.sh

# ✅ 產生權限 script（登記表每一項的 install -d／chown／chmod／setfacl）
python3 -m paulsha_cortex.trust_root permissions three-way --commands --paths \
  > /tmp/p2b-permissions.sh

# ✅ 逐行讀過再執行——這是 operator 核可的實體動作
less /tmp/p2b-scaffold.sh
less /tmp/p2b-permissions.sh

# ✅ 稽核 1：所有 mode 都不得有 group／other 寫入位（spec §R2）
grep -oE "chmod [0-7]{4}" /tmp/p2b-permissions.sh | sort -u \
 | awk '{m=$2; if (substr(m,3,1) ~ /[2367]/ || substr(m,4,1) ~ /[2367]/) {print "!! group/other writable: " m; bad=1}}
        END{ if (!bad) print "no group/other write: OK" }'

# ✅ 稽核 2：ACL 授「寫」只准出現在兩個 append-only 出口（三分下**恰好四行**）
grep -E "^setfacl" /tmp/p2b-permissions.sh | grep -E ":[^ ]*w"
#   期望（三分）：**恰好四行**
#     setfacl -m u:cortex-builder:wx           /var/lib/cortex/monitor/event-spool
#     setfacl -d -m u:cortex-builder:wx        /var/lib/cortex/monitor/event-spool
#     setfacl -m u:cortex-reviewer-planner:wx  /var/lib/cortex/coordinator/review-verdicts
#     setfacl -d -m u:cortex-reviewer-planner:wx /var/lib/cortex/coordinator/review-verdicts
#   **wx 無 r**——寫得進自己那格、讀不到他人的。多出任何一行都要停下來查。

# ✅ 稽核 3：三分的帳號名確實出現在計畫裡（權限 script 不得殘留 cortex-svc）
grep -c "cortex-svc" /tmp/p2b-permissions.sh
#   期望：0（若非 0，代表 scheme 傳錯或 permgen 仍有二分殘留，停下來查）
grep -oE "cortex-(manager|reviewer-planner|builder)" /tmp/p2b-permissions.sh | sort | uniq -c
#   期望：三個帳號名皆出現，且**不含** cortex-svc。
grep -c "cortex-svc" /tmp/p2b-scaffold.sh
#   期望：**2**（`/var/lib/cortex-svc` 與其 `cache/`）——這是第 1 步已標註的
#   已知缺口：Manager 的 HOME 目錄名尚未跟上三分改名，但**擁有者已是 cortex-manager**。
#   > 0 且非 2、或出現在 owner 欄位 ⇒ 停下來查。

# ✅ 稽核 4：setfacl 可用（缺 acl 套件會讓跨帳號唯讀授權整段失效）
command -v setfacl >/dev/null && echo "setfacl: OK" || echo "!! 請先 sudo apt-get install acl"
```

### 2b. 執行（順序固定：先骨架、後權限）

```bash
# 🔧 sudo：骨架目錄（root-owned 父層先就位，權限 script 才不會建出錯誤的中間層）
sudo sh -e /tmp/p2b-scaffold.sh && echo "scaffold applied"

# 🔧 sudo：登記表每一項的目標權限（冪等，可重複執行）
sudo sh -e /tmp/p2b-permissions.sh 2>&1 | tee /tmp/p2b-permissions.log
echo "exit=${PIPESTATUS[0]}"     # 期望 0
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

# ✅ 驗證：三個 HOME 與 ~/.codex 由 root 擁有（帳號不得替換自己的設定）
ls -ld /var/lib/cortex-svc /var/lib/cortex-svc/cache
ls -ld /var/lib/cortex-reviewer-planner /var/lib/cortex-reviewer-planner/.codex \
       /var/lib/cortex-reviewer-planner/cache
ls -ld /var/lib/cortex-builder /var/lib/cortex-builder/.codex /var/lib/cortex-builder/cache
#   期望：HOME 與 .codex 皆 root:root 0755；三個 cache 各自 <帳號>:<帳號> 0700

# ✅ 驗證：job spool 根 cortex-manager 擁有、0711（job 帳號只 traverse，不可列目錄）
ls -ld /var/lib/cortex/jobs
#   期望：cortex-manager:cortex-manager 0711
sudo -u cortex-builder ls /var/lib/cortex/jobs 2>&1 | tail -1
#   期望：Permission denied（0711 不可列目錄——job 不能枚舉別人的 spool）
```

**回滾**：`sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-reviewer-planner
/var/lib/cortex-builder /opt/cortex`（此時新樹仍空，舊樹完全未動）。

---

## 第 3 步：legacy-import（物理隔離 ＋ hash manifest；**不 chown 沿用**）

裁決：舊 state **不**併入新樹、**不** `chown` 沿用，而是整包搬到 quarantine，
留下內容 hash manifest。`legacy-imported` 來源 **MUST NOT** 滿足任何 ship gate。

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

> **重要**：新樹是**乾淨的**。切換後產生的 record 一律走正常 gate；
> `legacy-imported/` 只是唯讀歷史副本，任何從中還原的內容**不得**被 ship gate 採計。
> 正式的 `trust: legacy-imported` 簽章標記屬 **Phase 3**，本階段以「物理隔離＋hash
> manifest」達成同等的不可竄改性主張。

### 3b. review verdict spool（Phase 2a 已就位的受控通道；三分下才完整）

Phase 2a（PR #599）已把 review verdict 的落點從 reviewer worktree 搬到
`/var/lib/cortex/coordinator/review-verdicts/<reviewer_job_id>/verdict.json`
（登記表 `review-verdict-spool`，spec §R2）。**程式碼側已完成**：per-job 目錄由
Manager 在 dispatch 當下以 `0700` 建立、帶 pre-seed 守衛，落地後轉 `0444`；reviewer
身分由 Manager job registry 推導，verdict payload 的自述綁定欄位一律忽略。

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

## 第 4 步：Manager 遷 root-owned 部署 ＋ system-level unit

### 4a. venv 遷入 `/opt/cortex`

```bash
# 🔧 sudo：複製（不是原地 chown）到 root-owned 部署路徑
sudo rm -rf /opt/cortex/venv.new
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" /opt/cortex/venv.new
sudo chown -R root:root /opt/cortex/venv.new
sudo find /opt/cortex/venv.new -type d -exec chmod 0755 {} +
sudo find /opt/cortex/venv.new -type f -exec chmod a-w {} +
sudo find /opt/cortex/venv.new/bin -type f -exec chmod 0755 {} +

# 🔧 sudo：切成 active（venv 目錄本身即 ExecStart 的目標；保留舊的供回滾）
sudo rm -rf /opt/cortex/venv.prev
[ -d /opt/cortex/venv ] && sudo mv /opt/cortex/venv /opt/cortex/venv.prev
sudo mv /opt/cortex/venv.new /opt/cortex/venv
```

```bash
# ✅ 驗證：對服務帳號唯讀、可執行、版本正確
sudo -u cortex-manager /opt/cortex/venv/bin/cortex --version
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

### 4b. bootstrap env 遷入部署樹（root 擁有、fail-closed）

env 檔放在 **`/opt/cortex/etc/`**（全 root-owned 樹）而**不是** `/var/lib/cortex` 底下：
`/var/lib/cortex` 的子樹由 `cortex-manager` 擁有，**目錄可寫者能 unlink／replace 其中的
root-owned 檔案**——把 env 檔放進去等於沒保護。

```bash
# 🔧 sudo：寫 EnvironmentFile（instance-scoped，修掉背景 §7 的多 instance 共用漏洞）
sudo tee /opt/cortex/etc/cortex-manager.env >/dev/null <<'ENVFILE'
PSC_INSTANCE=cortex
PSC_AGENTS_ROOT=/var/lib/cortex
PSC_CONTROL_ROOT=/var/lib/cortex/control
PSC_COORDINATOR_ROOT=/var/lib/cortex/coordinator
PSC_SPECS_ROOT=/var/lib/cortex/specs
PSC_MONITOR_STATE_ROOT=/var/lib/cortex/monitor
PSC_PROJECT_CONFIG_ROOT=/var/lib/cortex/config/paulsha
PSC_RUN_ROOT=/var/lib/cortex/run/cortex
PSC_WORKTREE_ROOT=/var/lib/cortex/worktree
PSC_DEGRADED_OPERATION=per-case-approval
ENVFILE
sudo chown root:root /opt/cortex/etc/cortex-manager.env
sudo chmod 0644 /opt/cortex/etc/cortex-manager.env
```

```bash
# ✅ 驗證：owner/mode 與 permgen 的 deployment 區塊一致
python3 -m paulsha_cortex.trust_root permissions three-way --commands --paths \
  | grep -A3 "runtime-bootstrap-env"
ls -l /opt/cortex/etc/cortex-manager.env
#   期望：-rw-r--r-- root root

# ✅ 驗證：env 生效後路徑解析全部落在受保護樹內
sudo -u cortex-manager env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
  /opt/cortex/venv/bin/python -c \
  "from paulsha_cortex.config import paths; print(paths.agents_root(), paths.control_root(), paths.coordinator_root())"
#   期望：三者都在 /var/lib/cortex 底下
```

> **為何 unit 的 `EnvironmentFile` 一定生效**：`config/runtime.py` 的
> `resolve_runtime_root()` **先看行程 env**，其次才看 `$HOME/.agents/core/runtime/`
> 的 bootstrap 檔。unit 注入的 `PSC_*` 因此優先，且 `ProtectHome=yes` 讓 HOME fallback
> 根本不可見——雙保險。

### 4c. Manager system-level unit（內容由 permgen 產生）

```bash
# ✅ 先看內容（ReadWritePaths 逐條附「涵蓋哪些登記表資產」註解）
python3 -m paulsha_cortex.trust_root unit three-way --manager | less

# 🔧 sudo：寫入 unit（內容一字不改，直接由產生器落檔）
python3 -m paulsha_cortex.trust_root unit three-way --manager \
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
diff <(python3 -m paulsha_cortex.trust_root unit three-way --manager) \
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

---

## 第 5 步：降權啟用（**A+B 單一路徑**）

> **前置條件**：執行前提第 9 項。若 `systemd-template` 尚未出現在
> `job_runner.RUNNER_MODES`（#603 follow-up PR 未落地），則 **(a)(b) 照裝、(c) 跳過、
> (d) 不開**——邊界仍可由 5-7 的反向測試完整證明，只是 Manager 還不會走它。
> **絕不**因為 (d) 開不了就退回 transient 主路徑；需要臨時降權時走 **附錄 B**
> 並在 #584 記錄殘餘風險與預計關閉時間。

### 5-1. 邊界由三個 root-owned 物件 ＋ 一個帳號事實構成

| # | 物件／事實 | 路徑 | 擁有者 | 它強制什麼 |
|---|---|---|---|---|
| (a) | **polkit 規則** | `/etc/polkit-1/rules.d/49-cortex-downgrade.rules` | root:root 0644 | 只有 `cortex-manager`、只有 `start`／`stop`、只有 `cortex-job@*.service`。**不授權 `manage-units` 的 transient 建立** |
| (b) | **template unit** | `/etc/systemd/system/cortex-job@.service` | root:root 0644 | `User=cortex-builder` 寫死、加固段寫死、`ExecStart=` 寫死。呼叫端**選不了 UID、傳不了屬性** |
| (c) | **shim** | `/opt/cortex/bin/cortex-job-shim` | root:root 0755 | `ExecStart=` 的實體。argv 的**形狀**由 root-owned 程式從 Manager-owned job-spec 導出；Manager 只能給參數 |
| — | **三分帳號事實** | — | — | polkit 的 subject 只有 `cortex-manager`，而它**不跑任何模型程式碼**；injection 可達的 job 帳號完全不在授權面上 |

三者缺一都不成立：
- 少了 (a)，`cortex-manager` 起不了 job（fail-closed，不會退回同 UID）。
- 少了 (b)，`User=` 回到呼叫端手上——polkit 看不到它，等於沒守。
- 少了 (c)，argv 的入口落在 Manager 可寫的樹裡；Manager 被攻陷即可換掉執行的東西。

### 5-2. 安裝 (b) template unit

```bash
# ✅ 先看內容
python3 -m paulsha_cortex.trust_root unit three-way --job | less
#   必須確認的三行：
#     User=cortex-builder      ← 唯一 UID 來源，寫死
#     Group=cortex-builder
#     ExecStart=…              ← 見 5-3；PR 落地後應指向 /opt/cortex/bin/cortex-job-shim

# 🔧 sudo：落檔（root 擁有——這是 User= 不可被竄改的前提）
python3 -m paulsha_cortex.trust_root unit three-way --job \
  | sudo tee /etc/systemd/system/cortex-job@.service >/dev/null
sudo chown root:root /etc/systemd/system/cortex-job@.service
sudo chmod 0644 /etc/systemd/system/cortex-job@.service
sudo systemctl daemon-reload

# ✅ 驗證：與產生器逐位元相同、User= 確實寫死
diff <(python3 -m paulsha_cortex.trust_root unit three-way --job) \
     /etc/systemd/system/cortex-job@.service && echo "job unit in sync: OK"
grep -E "^(User|Group|ExecStart|NoNewPrivileges|CapabilityBoundingSet)=" \
     /etc/systemd/system/cortex-job@.service
#   期望：User=cortex-builder、Group=cortex-builder、NoNewPrivileges=yes、
#         CapabilityBoundingSet=（空值）
```

### 5-3. 部署 (c) root-owned shim

shim 是 C（code-level argv 保證）從 Manager 端**搬進 root-owned 檔案**的那一步：
job 的 argv 不再由 Manager 行程直接組出並交給 systemd，而是由 root 擁有的程式
從 **Manager-owned 的 job-spec spool** 讀取參數後導出。

| 角色 | 路徑 | 擁有者 | 權限意義 |
|---|---|---|---|
| shim（root-owned 程式） | `/opt/cortex/bin/cortex-job-shim` | root:root 0755 | 三個服務帳號皆**不可寫**（`/opt/cortex` 整棵 root-owned） |
| job-spec spool 根 | `/var/lib/cortex/jobs` | cortex-manager 0711 | Manager 寫；job 帳號只 traverse，**不可列目錄**（不能枚舉他人 job） |
| per-job spec | `/var/lib/cortex/jobs/<id>/` | cortex-manager 0700 ＋ builder `r-x` ACL | job 只讀自己那格——**改不了自己的命令列，也埋伏不了下一個 job** |

```bash
# ⏳ 內容由 #603 follow-up PR 的產生器提供（命令名以該 PR 實際落地為準）：
#      python3 -m paulsha_cortex.trust_root shim three-way > /tmp/cortex-job-shim
#    在它落地前**跳過本節**，並確認 5-2 的 ExecStart 指向哪裡（見下方檢查）。

# ✅ 檢查安裝好的 template unit 實際的 ExecStart——決定本節做不做
systemctl cat cortex-job@.service | grep -E "^ExecStart="
#   期望（PR 落地後）：ExecStart=/opt/cortex/bin/cortex-job-shim %i
#   若仍為 /bin/sh /var/lib/cortex/jobs/%i/run.sh ⇒ PR 未落地：
#     本節跳過；5-6 的正向 smoke 改用 run.sh 形式；(d) 切換點不打開。

# 🔧 sudo：落檔（root 擁有、可執行、對三個服務帳號唯讀）
sudo install -d -o root -g root -m 0755 /opt/cortex/bin
sudo install -o root -g root -m 0755 /tmp/cortex-job-shim /opt/cortex/bin/cortex-job-shim

# ✅ 驗證：與產生器逐位元相同
diff <(python3 -m paulsha_cortex.trust_root shim three-way) /opt/cortex/bin/cortex-job-shim \
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

### 5-4. 安裝 (a) polkit 規則

```bash
# ✅ 先讀（規則檔開頭把邊界與「為什麼 transient 一律拒」逐條寫出來，勿跳過）
python3 -m paulsha_cortex.trust_root polkit three-way --template | tee /tmp/polkit-cortex.rules
less /tmp/polkit-cortex.rules
#   必須確認的五個條件（規則檔自己列在「審查者的一眼結論」段）：
#     (1) subject 是 cortex-manager；(2) action 是 org.freedesktop.systemd1.manage-units；
#     (3) unit／verb 明細存在；(4) verb ∈ {start, stop}；
#     (5) unit 名匹配 ^cortex-job@[a-z0-9][a-z0-9._-]{0,62}\.service$
#   **transient unit 的 StartTransientUnit 檢查不帶明細 ⇒ 條件 (3) 直接把它擋掉。**

# 🔧 sudo：落檔
sudo install -o root -g root -m 0644 /tmp/polkit-cortex.rules \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ✅ 驗證：載入無語法錯誤、與產生器逐位元相同
sudo journalctl -u polkit -n 30 --no-pager | grep -Ei "error|syntax" || echo "polkit loaded clean: OK"
diff <(python3 -m paulsha_cortex.trust_root polkit three-way --template) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules && echo "polkit in sync: OK"

# ✅ 驗證：規則的 subject 是 cortex-manager，且殘餘風險清單為空（template 方案）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
rule = permgen.build_polkit_rule(
    permgen.SCHEMES["three-way"], plan=permgen.PolkitPlan.TEMPLATE
)
print("subject       :", rule.subject_account)
print("target        :", rule.target_account)
print("unit_pattern  :", rule.unit_pattern)
print("allowed_verbs :", rule.allowed_verbs)
print("residual_risks:", rule.residual_risks or "(none — OS 層封閉)")
PY
#   期望：subject=cortex-manager、target=cortex-builder、verbs=('start','stop')、
#         residual_risks 為空。
```

### 5-5. (d) 打開切換點 `PSC_JOB_RUNNER=systemd-template`

```bash
# 🔧 sudo：把降權模式寫進第 4b 步的 EnvironmentFile
sudo tee -a /opt/cortex/etc/cortex-manager.env >/dev/null <<'ENVFILE'
PSC_JOB_RUNNER=systemd-template
PSC_BUILDER_ACCOUNT=cortex-builder
PSC_BUILDER_HOME=/var/lib/cortex-builder
ENVFILE
sudo systemctl restart cortex-manager.service

# ✅ 驗證：模式確實被解析成 template（值非法必須 fail-closed，不得靜默當成 direct）
sudo -u cortex-manager env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
  /opt/cortex/venv/bin/python -c \
  "import os; from paulsha_cortex.coordinator import job_runner; print(job_runner.resolve_runner_mode(os.environ))"
#   期望：systemd-template
```

> `PSC_JOB_RUNNER` 預設 `direct`＝不降權；值非法時**fail-closed**（不會靜默當成
> `direct`）。`PSC_BUILDER_PATH` 選配——模型 CLI 不在 Manager `PATH` 上時才需要。
> **builder 帳號必須自己有模型 CLI 的登入態**（`sudo -u cortex-builder claude /login`
> 之類）；Manager 不會把自己的憑證傳過去，那正是本步驟的重點。
> **M2 之前**：`PSC_JOB_RUNNER` 只影響 builder persona；reviewer／planner 仍在 Manager
> 行程內執行（見開頭「分段落地」）。

### 5-6. 正向驗證（**必須成功**）

```bash
JOB=selftest

# 🔧 sudo：per-job spool（manager 擁有、builder 只讀）＋ job worktree
sudo install -d -o cortex-manager -g cortex-manager -m 0700 "/var/lib/cortex/jobs/$JOB"
sudo setfacl -m u:cortex-builder:r-x "/var/lib/cortex/jobs/$JOB"
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

# 🔧 sudo：放一份 smoke 執行規格。**形式取決於 5-3 檢查到的 ExecStart**：
#   (i) shim 已落地（ExecStart=/opt/cortex/bin/cortex-job-shim %i）：
#       用該 PR 提供的產生器寫 job-spec，**不要自行捏造欄位**。
#   (ii) shim 未落地（ExecStart=/bin/sh …/%i/run.sh）：用下列 run.sh 形式，
#        它同樣足以證明 (a)(b) 的邊界（身分／token／fd／HOME）。
sudo tee "/var/lib/cortex/jobs/$JOB/run.sh" >/dev/null <<'SH'
#!/bin/sh
echo "== identity =="; id
echo "== tokens =="; echo "GH_TOKEN=[$GH_TOKEN] GITHUB_TOKEN=[$GITHUB_TOKEN]"
echo "== inherited fds =="; ls -l /proc/self/fd
echo "== home =="; echo "HOME=$HOME"; ls -ld "$HOME" 2>&1
echo "== deployment writable? =="; (printf x >> /opt/cortex/venv/bin/cortex) 2>&1 | tail -1
SH
sudo chown cortex-manager:cortex-manager "/var/lib/cortex/jobs/$JOB/run.sh"
sudo chmod 0600 "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-builder:r-- "/var/lib/cortex/jobs/$JOB/run.sh"

# ✅ 正向：以 cortex-manager 身分起 instance——**必須成功**
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"
sudo journalctl -u "cortex-job@$JOB.service" -n 50 --no-pager
#   期望輸出：
#     uid=…(cortex-builder) gid=…(cortex-builder)   ← User= 由 OS 強制，不是呼叫端選的
#     GH_TOKEN=[] GITHUB_TOKEN=[]                    ← token 已 scrub
#     /proc/self/fd 只有 0/1/2                        ← 無指向受保護資產的可寫 fd（R9 T4.1）
#     HOME=/var/lib/cortex-builder，且該目錄為 root:root
#     deployment writable? → Permission denied / Read-only file system

# ✅ 正向：停也必須成功（polkit 放行的兩個 verb）
sudo -u cortex-manager systemctl stop "cortex-job@$JOB.service"; echo "exit=$?"   # 期望 0
```

### 5-7. 反向驗證（**11 條全部必須被拒**）

```bash
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
sudo -u cortex-manager systemd-run --unit="cortex-job@evil.service" --uid=0 \
     --pipe --wait /bin/id; echo "exit=$?"

# ✅ (6) 起別的既存 unit（含 Manager 自己、sshd）：必須被拒
sudo -u cortex-manager systemctl restart cortex-manager.service; echo "exit=$?"
sudo -u cortex-manager systemctl start sshd.service 2>&1 | tail -1; echo "exit=$?"

# ✅ (7) 名稱夾帶（前綴／後綴混淆）：必須被拒
sudo -u cortex-manager systemctl start "evil-cortex-job@x.service"; echo "exit=$?"
sudo -u cortex-manager systemctl start "cortex-job@x.service.evil"; echo "exit=$?"

# ✅ (8) 其他 verb：必須被拒
sudo -u cortex-manager systemctl mask "cortex-job@$JOB.service"; echo "exit=$?"
sudo -u cortex-manager systemctl daemon-reload; echo "exit=$?"
sudo -u cortex-manager systemctl set-property "cortex-job@$JOB.service" User=root; echo "exit=$?"

# ✅ (9) 非授權帳號起 job instance：必須被拒（polkit subject 只有 cortex-manager）
sudo -u cortex-reviewer-planner systemctl start "cortex-job@$JOB.service"; echo "exit=$?"
sudo -u cortex-builder systemctl start "cortex-job@$JOB.service"; echo "exit=$?"

# ✅ (10) 改 template unit／shim／polkit 規則：三個服務帳號一律 EACCES
for U in cortex-manager cortex-reviewer-planner cortex-builder; do
  sudo -u "$U" sh -c 'printf "User=root\n" >> /etc/systemd/system/cortex-job@.service'; echo "$U unit exit=$?"
  sudo -u "$U" sh -c 'printf "id\n" >> /opt/cortex/bin/cortex-job-shim'; echo "$U shim exit=$?"
  sudo -u "$U" sh -c 'printf "x\n" >> /etc/polkit-1/rules.d/49-cortex-downgrade.rules'; echo "$U polkit exit=$?"
done

# ✅ (11) 負控制：暫時移除 polkit 規則後，dispatch 必須 fail-closed 而非退回 direct
sudo mv /etc/polkit-1/rules.d/49-cortex-downgrade.rules /tmp/polkit-cortex.disabled
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"; echo "exit=$?"   # 期望非 0
#   → 再觸發一次真正的 dispatch，期望：job 落 needs_human，理由碼指向
#     job-runner 的 unit-start-failed 家族，detail 帶 systemctl 的實際拒絕訊息。
#     **不得**出現以 cortex-manager 身分跑起來的 job。
sudo mv /tmp/polkit-cortex.disabled /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service
```

**通過條件**：5-6 正向成功且輸出符合期望；5-7 的 (1)–(11) **全部**非 0 退出。
任一反向測試通過（即攻擊成功）＝**立即停止**，回到第 9 步回滾。

```bash
# 🔧 sudo：清掉 selftest 殘留
sudo systemctl stop "cortex-job@$JOB.service" 2>/dev/null || true
sudo rm -rf "/var/lib/cortex/jobs/$JOB" "/var/lib/cortex/worktree/$JOB"
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
| **Manager 自身邏輯被攻陷** | Manager 程式碼路徑被誘導寫出惡意 job-spec | root-owned shim 限定 argv 形狀（5-3）；job 仍降到 `cortex-builder`、拿不到 token | shim 對 spec 的檢查強度＝該 PR 的實作品質，需在 PR review 時單獨把關 |
| **operator 帳號** | 有 `sudo`，可改任何東西 | 設計上信任邊界之外（本 runbook 全部 root 操作都由 operator 親自輸入） | 不在本階段範圍 |
| **polkit 不可用** | polkit 掛掉 ⇒ 全部 job 起不來 | fail-closed（安全但功能全停）；執行前提第 6 項＋WSL2 段第 5 項複驗 | 需監控，否則表現為「靜默停擺」 |
| **M2 未完成** | reviewer／planner 仍在 Manager 行程內以 `cortex-manager` 身分跑 ⇒ 這兩個 persona 的 injection 可達行程**目前**仍與 grant 同 UID | 檔案權限面已三分（第 3b 步實測）；builder（最大攻擊面）已完全移出 | **這是 M1 唯一的行程面殘餘**，必須在 #584 明示記錄，並隨 M2 關閉 |

> **記錄要求**：完成第 5 步後，把 5-7 的 11 條 exit code、5-4 的 `residual_risks` 輸出、
> 以及本表最後一列（M2 是否已完成）貼到 #584。D6 的通過判定引用這份紀錄。

### 5-9. 產生邏輯的離線對照

```bash
# ✅ polkit 無法本機模擬時的第二證據：決策矩陣測試
python3 -m pytest tests/test_trust_root_permgen_p2b.py -q -k polkit

# ✅ 決策矩陣的手動抽點（與 5-7 的實機結果應逐條一致）
python3 - <<'PY'
from paulsha_cortex.trust_root import permgen
rule = permgen.build_polkit_rule(
    permgen.SCHEMES["three-way"], plan=permgen.PolkitPlan.TEMPLATE
)
cases = [
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "start", "YES"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "stop",  "YES"),
    ("cortex-manager",          permgen.POLKIT_ACTION, None,                     None,    "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-manager.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "evil-cortex-job@x.service", "start", "NO"),
    ("cortex-manager",          permgen.POLKIT_ACTION, "cortex-job@abc.service", "mask",  "NO"),
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

# 🔧 3. sudo：旁建新樹（不覆蓋現行）
sudo rm -rf /opt/cortex/venv.new
sudo cp -a "$HOME/.local/share/pipx/venvs/paulsha-cortex" /opt/cortex/venv.new
sudo chown -R root:root /opt/cortex/venv.new
sudo find /opt/cortex/venv.new -type d -exec chmod 0755 {} +
sudo find /opt/cortex/venv.new -type f -exec chmod a-w {} +
sudo find /opt/cortex/venv.new/bin -type f -exec chmod 0755 {} +

# ✅ 4. 新樹自檢通過才切換
sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root selfcheck
sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root equation

# ✅ 5. 登記表若有變動，unit／template／shim 全部必須重新產生
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root unit three-way --manager) \
     /etc/systemd/system/cortex-manager.service || echo "!! manager unit 需更新"
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root unit three-way --job) \
     /etc/systemd/system/cortex-job@.service || echo "!! job template unit 需更新"
diff <(sudo -u cortex-manager /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root polkit three-way --template) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules || echo "!! polkit 規則需更新"
#   （shim 落地後同樣加一條 `… shim three-way` 對 /opt/cortex/bin/cortex-job-shim 的 diff）

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
sudo -u cortex-manager env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
  /opt/cortex/venv/bin/python -m paulsha_cortex.trust_root selfcheck \
  | tee "/tmp/trust-root-after-$(date +%Y%m%d-%H%M).json"
#   期望：JSON 的 "ok": true、"job_writable_count": 0

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

**通過條件**：自檢 `ok=true`、`remaining` 為空、`equation` 為 `ok: true`。

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
# 🔧 sudo：R9 攻擊 job 的 spool／worktree／攻擊目標（全部由 operator 親自建立）
JOB=r9
sudo install -d -o cortex-manager -g cortex-manager -m 0700 "/var/lib/cortex/jobs/$JOB"
sudo setfacl -m u:cortex-builder:r-x "/var/lib/cortex/jobs/$JOB"
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

sudo tee "/var/lib/cortex/jobs/$JOB/run.sh" >/dev/null <<'SH'
#!/bin/sh
t() { printf '%s :: ' "$1"; shift; if "$@" >/dev/null 2>&1; then echo "!! SUCCEEDED (FAIL)"; else echo "denied (OK) rc=$?"; fi; }
A=/var/lib/cortex

echo "===== 族 1 capability-theft ====="
t "T1.1 讀 EnvironmentFile"        cat /opt/cortex/etc/cortex-manager.env
t "T1.1 讀 manager HOME cache"     ls /var/lib/cortex-svc/cache
t "T1.2 連 Manager control socket" sh -c "ls $A/run/cortex && cat $A/run/cortex/*.sock"
t "T1.3 呼叫 operator CLI"         /opt/cortex/venv/bin/cortex work ship --help
t "T1.4 直寫 control queue"        sh -c "printf x > $A/control/requests/evil.json"
t "T1.5 枚舉他人 job spool"        ls "$A/jobs"

echo "===== 族 2 durable-state-tampering ====="
t "T2 modify jobs.json"            sh -c "printf '{}' > $A/coordinator/jobs.json"
t "T2 truncate jobs.json"          sh -c ": > $A/coordinator/jobs.json"
t "T2 delete jobs.json"            rm -f "$A/coordinator/jobs.json"
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
MPID=$(pgrep -u cortex-manager -n -f paulsha_cortex 2>/dev/null || echo 0)
echo "manager pid = $MPID"
t "T4.2 ptrace Manager"            sh -c "command -v gdb >/dev/null && gdb -p $MPID -batch -ex quit"
t "T4.3 讀 Manager environ"        cat "/proc/$MPID/environ"
t "T4.3 讀 Manager mem"            head -c 1 "/proc/$MPID/mem"
t "T4.4 對 Manager 送 SIGSTOP"     kill -STOP "$MPID"
t "T4.5 讀另一個 job 帳號 cache"   sh -c "ls /var/lib/cortex-builder/cache /var/lib/cortex-reviewer-planner/cache"
t "T4.6 提權：systemd-run root"    systemd-run --uid=0 --pipe /bin/id
t "T4.6 提權：起 job instance"     systemctl start cortex-job@other.service
t "T4.6 提權：sudo"                sudo -n true
SH
sudo chown cortex-manager:cortex-manager "/var/lib/cortex/jobs/$JOB/run.sh"
sudo chmod 0600 "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-builder:r-- "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-reviewer-planner:r-- "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-reviewer-planner:r-x "/var/lib/cortex/jobs/$JOB"

# 準備「他人 worktree」與「他人 verdict 格」作為跨 persona 攻擊目標
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/worktree/victim
sudo -u cortex-manager sh -c 'printf "{\"verdict\":\"fail\"}" > /var/lib/cortex/worktree/victim/.psc-review-verdict.json'
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/coordinator/review-verdicts/victim
```

```bash
# 🔧 sudo：pass 1——以 **cortex-builder** 身分（經 A+B 的正式路徑：template instance）
sudo -u cortex-manager systemctl start "cortex-job@$JOB.service"
sudo journalctl -u "cortex-job@$JOB.service" -n 400 --no-pager | tee /tmp/r9-builder.txt

# 🔧 sudo：pass 2——以 **cortex-reviewer-planner** 身分
#   M2 之前 reviewer／planner 沒有自己的 template instance，因此這一趟用 **operator 的
#   sudo** 直接起（不是用 cortex-manager 的 grant）。它測的是**檔案權限面**的三分，
#   不是啟動面；啟動面由 5-7 (9) 覆蓋。
sudo systemd-run --quiet --collect --pipe --wait \
  --uid=cortex-reviewer-planner --gid=cortex-reviewer-planner --service-type=exec \
  --property=NoNewPrivileges=yes \
  --setenv=HOME=/var/lib/cortex-reviewer-planner \
  --working-directory=/var/lib/cortex/worktree \
  /bin/sh "/var/lib/cortex/jobs/$JOB/run.sh" | tee /tmp/r9-reviewer.txt
```

**預期輸出**：兩份報告中**每一條**都是 `denied (OK) rc=<非 0>`；
`T4.1 自己的 fd` 只列 `0`、`1`、`2`（指向 journal socket 或 `/dev/null`），
**沒有任何**指向 `/var/lib/cortex` 或 `/opt/cortex` 的可寫 fd。

```bash
# ✅ 一眼判讀（兩份都要看）
for F in /tmp/r9-builder.txt /tmp/r9-reviewer.txt; do
  printf '%s: denied=%s\n' "$F" "$(grep -c 'denied (OK)' "$F")"
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
diff <(python3 -m paulsha_cortex.trust_root unit three-way --job) \
     /etc/systemd/system/cortex-job@.service && echo "restored & in sync: OK"

# ✅ 5.2 事後複驗：分界線改過之後仍成立（族 3「重啟後仍綠」的同一條紀律）
sudo -u cortex-manager systemd-run --uid=0 --pipe --wait /bin/id; echo "post-negctl exit=$?"   # 期望非 0
```

### 8c. negative control（受信任身分做同樣的事**必須成功**）

```bash
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
MPID=$(pgrep -u cortex-manager -n -f paulsha_cortex); sudo cat "/proc/$MPID/environ" | tr '\0' '\n' | head -3 && echo "NEG-CONTROL-4-OK"

# ✅ 族 5 的 negative control：cortex-manager 起**合法**的 job instance 必須成功
#    （否則族 5 的紅可能只是「polkit 把 cortex-manager 全部擋掉了」）
sudo install -d -o cortex-manager -g cortex-manager -m 0700 /var/lib/cortex/jobs/negctl5
sudo setfacl -m u:cortex-builder:r-x /var/lib/cortex/jobs/negctl5
sudo install -d -o cortex-builder -g cortex-builder -m 0700 /var/lib/cortex/worktree/negctl5
sudo tee /var/lib/cortex/jobs/negctl5/run.sh >/dev/null <<'SH'
#!/bin/sh
id
SH
sudo chown cortex-manager:cortex-manager /var/lib/cortex/jobs/negctl5/run.sh
sudo chmod 0600 /var/lib/cortex/jobs/negctl5/run.sh
sudo setfacl -m u:cortex-builder:r-- /var/lib/cortex/jobs/negctl5/run.sh
sudo -u cortex-manager systemctl start cortex-job@negctl5.service && echo "NEG-CONTROL-5-OK"
sudo journalctl -u cortex-job@negctl5.service -n 5 --no-pager | grep -o "uid=[0-9]*(cortex-builder)"
#   期望：印出 NEG-CONTROL-5-OK，且 journal 顯示 uid=…(cortex-builder)
```

### 8d. 族 3 的「重啟後仍綠」複驗（spec §R9 硬性要求）

```bash
# ✅ 每個族 3／族 5.2 案例改完 MUST 實際重啟服務再驗證
sudo systemctl restart cortex-manager.service
sleep 3
systemctl is-active cortex-manager.service
sudo -u cortex-manager env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
  /opt/cortex/venv/bin/python -m paulsha_cortex.trust_root selfcheck | head -20
#   期望：服務 active、自檢仍 ok=true（族 3／5 的攻擊沒有留下任何持久效果）
```

### 8e. 清理

```bash
# 🔧 sudo：清掉 R9 抽驗的全部殘留（含 drop-in 目錄——它本身就是提權面）
sudo systemctl stop "cortex-job@r9.service" "cortex-job@negctl5.service" 2>/dev/null || true
sudo rm -rf /var/lib/cortex/jobs/r9 /var/lib/cortex/jobs/negctl5 \
            /var/lib/cortex/worktree/r9 /var/lib/cortex/worktree/negctl5 \
            /var/lib/cortex/worktree/victim \
            /var/lib/cortex/coordinator/review-verdicts/victim \
            /etc/systemd/system/cortex-job@.service.d
sudo systemctl daemon-reload
```

**通過條件**：8a 兩份報告全部 `denied (OK)`；8b 族 5.1 五條與族 5.2 的 27 條**全部非 0**；
8c 五組 negative control 全部印出 `*-OK`；8d 重啟後仍綠。
任一條不符 ⇒ **D6 不算通過**，`0.2.0` 不得宣告 stable（spec §R12）。

---

## 第 9 步：回滾（每階段可回滾 ＋ 全面退回 Phase 1）

Phase 1 完全不需 root 且含降級運轉安全網（`PSC_DEGRADED_OPERATION=per-case-approval`）。
任一階段出問題即退回「operator 帳號跑 ＋ 降級運轉」。

| 階段 | 症狀 | 回滾動作（`🔧 sudo`） |
|---|---|---|
| 第 1（三帳號） | 帳號建錯／名稱衝突 | `sudo userdel cortex-manager cortex-reviewer-planner cortex-builder`（逐一）；`sudo groupdel` 同名三個 group；`sudo rm -rf /var/lib/cortex-reviewer-planner`（此時尚無檔案屬於它們） |
| 第 2（樹／權限） | 權限套錯、`find -perm /022` 非空 | 重跑 `sudo sh -e /tmp/p2b-permissions.sh`（冪等）；仍不對則 `sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-reviewer-planner /var/lib/cortex-builder` 後從第 2 步重來（舊樹未動） |
| 第 3（legacy-import） | quarantine 內容不符 manifest | `sudo rm -rf /var/lib/cortex/legacy-imported`，重跑 3；`$HOME/.agents` 原地仍完整 |
| 第 4a（部署） | 新 venv 起不來 | `sudo rm -rf /opt/cortex/venv; sudo mv /opt/cortex/venv.prev /opt/cortex/venv; sudo systemctl restart cortex-manager` |
| 第 4c（system unit） | WSL 重啟後未拉起／服務起不來 | `sudo systemctl disable --now cortex-manager.service`；改回 `systemctl --user start cortex-manager.service`（舊部署仍在 `$HOME/.local/share/pipx`） |
| 第 4c（加固誤擋） | 服務起來但功能靜默失效 | 見下方「`ProtectSystem=strict` 誤擋診斷」；**臨時** drop-in 放行、**同一天**把該路徑回填 R1 登記表並重跑 permgen |
| **第 5-2（template unit）** | instance 起不來／unit 語法錯 | `sudo rm -f /etc/systemd/system/cortex-job@.service; sudo rm -rf /etc/systemd/system/cortex-job@.service.d; sudo systemctl daemon-reload`；(d) 一併關閉（見下一列） |
| **第 5-3（shim）** | job 起得來但 argv 不對／shim crash | `sudo rm -f /opt/cortex/bin/cortex-job-shim`；重新由產生器落檔並 `diff` 對齊；仍不對則關 (d) |
| **第 5-4（polkit）** | 規則語法錯／`cortex-manager` 起不了任何 job | `sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules; sudo systemctl restart polkit.service`；此時降權面完全關閉（fail-closed，job 起不來但無提權） |
| **第 5-5（切換點）** | 降權後 job 全數 needs_human | `sudo sed -i '/^PSC_JOB_RUNNER=/d;/^PSC_BUILDER_ACCOUNT=/d;/^PSC_BUILDER_HOME=/d' /opt/cortex/etc/cortex-manager.env; sudo systemctl restart cortex-manager.service`（回 `direct`）；Manager 以 `per-case-approval` 不 spawn job 運轉 |
| 第 8（R9 有紅） | 任一攻擊成功（含族 5） | **立即**停 job 派工，執行下方「全面回退」，在 #584 記錄該條攻擊路徑；D6 判定未通過 |

### 全面回退到 Phase 1（降級運轉）

```bash
# 🔧 sudo：停掉並移除 Phase 2 的一切（含 A+B 的三個 root-owned 物件與三帳號）
sudo systemctl disable --now cortex-manager.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/cortex-manager.service /etc/systemd/system/cortex-job@.service
sudo rm -rf /etc/systemd/system/cortex-job@.service.d
sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo rm -f /opt/cortex/bin/cortex-job-shim
#   （EnvironmentFile 隨 /opt/cortex 一併移除，PSC_JOB_RUNNER 自然失效）
sudo systemctl daemon-reload
sudo systemctl restart polkit.service 2>/dev/null || true

# 🔧 sudo：新樹整棵丟棄（舊 state 從未被併入，故無資料損失）＋ 移除三帳號
sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-reviewer-planner \
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
#   注意：cortex-job@.service 是 template，**不需要也不應該** enable。

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
sudo -u cortex-manager systemctl start cortex-job@negctl5.service \
  && echo "降權重啟後仍可用" || echo "!! 降權失效，查 polkit"

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
diff <(python3 -m paulsha_cortex.trust_root unit three-way --manager | grep '^ReadWritePaths=') \
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
diff <(python3 -m paulsha_cortex.trust_root unit three-way --job | grep '^ReadWritePaths=') \
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
> **template unit 的 drop-in 更嚴**：`/etc/systemd/system/cortex-job@.service.d/` 一旦
> 存在就是提權面（族 5.2 有一條專門測它建不起來），診斷完**必須立刻刪除**。

### C. WSL2 其他已知風險

1. **polkit 未安裝／未啟動**：systemd 對 unprivileged client 的授權在 polkit 不可用時
   **一律拒絕**（fail-closed）。表現為「Manager 完全 spawn 不了 job」而非提權——
   安全但功能全停。執行前提第 6 項已檢查；重啟後由「A. 第 5 項」複驗。
2. **`sudo` 需密碼**：所有 `🔧 sudo` 步驟皆互動式，**不可假設自動化**；
   本 runbook 刻意不使用 `sudo -n`（族 5 的 `sudo -n true` 例外，那是攻擊測試）。
3. **`/proc` 隱藏造成 R9 判讀差異**：`ProtectProc=invisible` 下讀他人
   `/proc/<pid>/environ` 會是 `ENOENT`（No such file）而非 `EACCES`——
   兩者都算**拒絕**（第 8 步的 `t()` 只看 rc 非 0，判定不受影響）。
4. **WSL2 的 `busctl` 可能不在 PATH**：族 5.1e 若報 `command not found`，
   以 `sudo apt-get install systemd` 補齊後重測；**不可**因為工具缺席就跳過該條
   （它測的是繞開 CLI 的直接 D-Bus 路徑）。

---

## 附錄 A：本 runbook 的自我檢查

```bash
# ✅ unit／polkit／shim 與產生器沒有漂移（建議排程每日跑）
diff <(python3 -m paulsha_cortex.trust_root unit three-way --manager) /etc/systemd/system/cortex-manager.service
diff <(python3 -m paulsha_cortex.trust_root unit three-way --job)     /etc/systemd/system/cortex-job@.service
diff <(python3 -m paulsha_cortex.trust_root polkit three-way --template) /etc/polkit-1/rules.d/49-cortex-downgrade.rules
# shim 落地後再加：
# diff <(python3 -m paulsha_cortex.trust_root shim three-way) /opt/cortex/bin/cortex-job-shim

# ✅ 沒有殘留的 template drop-in（族 5.2 的持久化面）
ls -la /etc/systemd/system/cortex-job@.service.d 2>/dev/null && echo "!! 有 drop-in，查來源" || echo "no drop-in: OK"

# ✅ 三分帳號仍是三個、互不交集、皆無 sudo
for U in cortex-manager cortex-reviewer-planner cortex-builder; do id -nG "$U"; done

# ✅ 權限沒有漂移
sudo find /var/lib/cortex /opt/cortex -perm /022 -print | head

# ✅ 產生器本身的等式測試（含 ReadWritePaths 無遺漏無多餘）
python3 -m pytest tests/test_trust_root_permgen_p2a.py tests/test_trust_root_permgen_p2b.py -q
```

---

## 附錄 B：降級備援——transient unit（**附殘餘風險**）

> **這不是主路徑。** 只有在「template 路徑在本機不可用」（例如 template unit 起不來、
> shim 尚未落地而又必須立刻恢復 job 派工）時才臨時使用，且**必須在 #584 記錄
> 啟用時間、原因、預計關閉時間**。

程式碼側 `PSC_JOB_RUNNER=systemd-run` 已於 #603 落地，polkit 側改用 transient 規則：

```bash
# 🔧 sudo：換成 transient 規則（unit 名前綴 cortex-job-，非 @ 實例）
python3 -m paulsha_cortex.trust_root polkit three-way --transient \
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
    permgen.SCHEMES["three-way"], plan=permgen.PolkitPlan.TRANSIENT
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
| 全文 `two-way` | 全文 `three-way`；`two-way` 僅存於程式碼作為對照組 |
| 第 5 步 A／B 並列，**待 operator 拍板** | 第 5 步 **A+B 合一，無分歧**；transient 降為附錄 B 備援 |
| polkit 授 transient 建立（方案 A） | polkit **不授** transient 建立；只放行 `cortex-job@*.service` 的 start/stop |
| `ExecStart=` 指向 spool 內的 `run.sh` | 指向 **root-owned shim** `/opt/cortex/bin/cortex-job-shim`（C 層搬進 root-owned 檔） |
| `PSC_JOB_RUNNER=systemd-run` | `PSC_JOB_RUNNER=systemd-template`（`systemd-run` 僅備援用） |
| R9 四族，subject 為 `cortex-builder` | R9 **五族**，subject 為 builder ＋ reviewer-planner，新增族 5 privilege-boundary |
| 殘餘風險：授權帳號可起任意 UID | 殘餘風險：**僅剩 `cortex-manager` 的 supply-chain 類**（見 5-8） |
