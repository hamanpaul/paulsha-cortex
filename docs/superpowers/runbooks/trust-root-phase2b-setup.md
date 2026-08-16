---
status: executable
work_item: trust-root-isolation
phase: 2b
audience: operator
supersedes: none
tracking: "#584 — trust-root D6 母議題（本票 reference-only，不 auto-close；R-17 走 policy-exempt:issue-link 豁免）"
decision: "operator 0816 第二輪裁決（#584 留言）——7 個未決點全數收斂"
refs:
  - docs/superpowers/specs/trust-root-isolation-spec.md
  - paulsha_cortex/trust_root/registry.py
  - paulsha_cortex/trust_root/permgen.py
---

# trust-root Phase 2b：root 設定 runbook（可執行版）

> **本文件是可執行版**：每一步的命令可直接複製、每一步有驗證、每一步可回滾。
> 文件本身**不執行任何 root 操作**；所有 `sudo` 都由 operator 親自輸入。
> **cortex 任何元件永不具 root**——cortex 只**產生**命令字串與驗證結果，
> root 操作一律由 operator 手動執行（0816 裁決，未決 6）。

實作 `trust-root-isolation-spec.md` 的 **Phase 2**（spec §R10 Phase 2 第 1–8 步）。
Phase 2a 的權限產生器（`paulsha_cortex/trust_root/permgen.py`）把 R1 登記表機械轉成
目標 `owner:group mode`、systemd unit 與 polkit 規則；本 runbook 是把那份計畫**落到
OS** 的手動流程。

---

## 0816 第二輪裁決定案表（本 runbook 的前提）

| 原未決點 | 定案 | 落在本 runbook |
|---|---|---|
| 1 降權機制 | **systemd 降權 job unit ＋ polkit 收窄**（授權面只允許 `cortex-svc` 起 job unit）。**「降到哪個帳號」由誰強制**尚待 operator 在 A（`systemd-run`，code level 保證）／B（root-owned 模板 unit，OS 強制）之間擇一——兩案都已寫成完整可執行 | 第 5 步 |
| 2 durable state 路徑 | **`/var/lib/cortex`**；worktree pool＝**`/var/lib/cortex/worktree`** | 第 2 步 |
| 3 legacy-import | **物理隔離 ＋ hash manifest**（無簽章；簽章屬 Phase 3）。切換前 in-flight job **手動收尾** | 執行前提、第 3 步 |
| 4 Manager 部署 | **`/opt/cortex`**（root 擁有，對服務唯讀） | 第 4 步 |
| 5 `ReadWritePaths` | **由 R1 登記表經 permgen 機械產生**，不手寫 | 第 4c 步 |
| 6 root 命令 codify | **不 codify**——不提供 `cortex install trust-root --system`；cortex 只產生命令字串 | 第 6 步 |
| 7 R9 | **手動抽驗**（完整自動化矩陣屬另一工項） | 第 8 步 |
| UID 方案 | **二分先行**（`cortex-svc` / `cortex-builder`），保留三分彈性（permgen 已參數化）；Manager 走 **system-level unit** | 第 1、4c 步 |

### ⚠️ 唯一還需要 operator 拍板的一點（請先看這段）

裁決寫的是「**systemd-run** transient unit ＋ polkit 收窄到只能 `User=cortex-builder`」。
#603 實測確認一個**硬限制**：polkit 的 `org.freedesktop.systemd1.manage-units` action
**只暴露 unit 名稱與 verb**，**不暴露 `User=`／`--uid=`**。授權之後，systemd 會照請求的
**任意** `User=` 起 unit。因此「只能降到 `cortex-builder`」這一半 **polkit 無法強制**，
必須另外找地方守：

- **方案 A（`systemd-run` transient unit）**——0816 裁決的字面方案，程式碼**已落地**
  （#603 `coordinator/job_runner.py`，`PSC_JOB_RUNNER=systemd-run`）。「降到哪個帳號」
  由 Manager 端**封閉的 argv 產生器**在 code level 保證。
  **殘餘風險**：與 `cortex-svc` **同 UID 的任何行程**都持有這個 grant，可請求任意
  `User=`（含 `User=root`）的 transient unit——而**二分方案下跑模型的 reviewer／planner
  就與 Manager 併帳**。這條風險與「**是否提前三分**」直接相關（三分把它縮回「只有
  Manager 自己」）。
- **方案 B（root-owned 模板 unit）**——`cortex-job@.service` 把 `User=cortex-builder`
  硬寫死在 **root 擁有**的檔案裡，polkit 只放行該模板的實例、transient unit 一律拒。
  「降到哪個帳號」因此由 **OS** 強制，殘餘為零。代價是 Manager 端要從 `systemd-run`
  改成 `systemctl start cortex-job@<id>`，**屬待排的程式碼工項**。

**兩案在第 5 步都寫成完整可執行**（含各自的正向／反向驗證、殘餘風險實測）；
operator 選定後只跑那一節，其餘步驟（1–4、6–9）兩案共用。**同時要一併裁決的是
「是否提前三分」**——permgen 已參數化，只需把命令中的 `two-way` 換成 `three-way`。

---

## 執行前提（開工前逐項確認，全部 `✅ 驗證`）

```bash
# ✅ 1. in-flight job 手動收尾（裁決 3：切換期間不得有半途 job）
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
```

**通過條件**：1–7 全部符合期望；`equation` 回傳 `ok: true`；baseline JSON 已存檔。
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

## 產生器＝權限／unit／polkit 的單一真相

`chown`／`chmod`／`setfacl`／unit 內容／polkit 規則**一律不手寫**，全部由 permgen 由
R1 登記表機械產生：

```bash
# ✅ 完整權限計畫（JSON，含每項 rationale）
python3 -m paulsha_cortex.trust_root permissions two-way

# ✅ 可直接執行的權限命令（帶真實絕對路徑，無 placeholder）
python3 -m paulsha_cortex.trust_root permissions two-way --commands --paths

# ✅ 骨架目錄（非登記表資產的父層）
python3 -m paulsha_cortex.trust_root scaffold two-way

# ✅ Manager system unit 內容（ReadWritePaths 由登記表導出）
python3 -m paulsha_cortex.trust_root unit two-way --manager

# ✅ 降權 polkit 規則內容（方案 A：systemd-run transient unit）
python3 -m paulsha_cortex.trust_root polkit two-way --transient

# ✅ 降權 polkit 規則內容（方案 B：root-owned 模板 unit）
python3 -m paulsha_cortex.trust_root polkit two-way --template

# ✅ 方案 B 的 job 模板 unit 內容（User=cortex-builder 硬寫死）
python3 -m paulsha_cortex.trust_root unit two-way --job

# ✅ 方案 A 的 --property= 建議清單（與 B 同源：同一加固表 ＋ 同一份 RWP）
python3 -m paulsha_cortex.trust_root unit two-way --job-properties
```

> **未來若換三分**：把上列每個 `two-way` 改成 `three-way`，runbook 其餘結構不變
> （permgen 已參數化；第 1 步多建一個帳號）。

---

## 第 1 步：建 UID（二分）

建立兩個 **system service 帳號**，皆 **no-login**、**home 由 root 擁有**。
慣例：每帳號一個同名 primary group（權限產生器的 ACL 以此為前提）。

```bash
# 🔧 sudo：cortex-svc（durable state owner ＋ Manager/monitor/reviewer/planner）
sudo groupadd --system cortex-svc
sudo useradd  --system --gid cortex-svc \
     --home-dir /var/lib/cortex-svc --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex trusted service (Manager/monitor/reviewer/planner)" cortex-svc

# 🔧 sudo：cortex-builder（唯一完全隔離的 headless job 帳號）
sudo groupadd --system cortex-builder
sudo useradd  --system --gid cortex-builder \
     --home-dir /var/lib/cortex-builder --no-create-home \
     --shell /usr/sbin/nologin \
     --comment "cortex headless builder job" cortex-builder
```

**為何 `--no-create-home`**：兩個帳號的 HOME 由第 2 步以 **root 擁有**的方式建立
（`useradd --create-home` 會把 HOME 建成帳號自己擁有）。HOME 若由帳號自己擁有，
它就能 rename 掉 `~/.codex`／`~/.gitconfig` 這類 root-owned 設定的**父目錄**——
父目錄可寫者能 unlink／rename 子物件，等於保護失效。只有 `cache/` 子目錄開放給帳號寫。

**群組設計理由**：跨帳號存取一律走 **per-account POSIX ACL**（見 permgen 輸出的
`setfacl -m u:<acct>:rX`），**不**用共用 group 開放，避免「一個 group 開了就全開」。

```bash
# ✅ 驗證：帳號存在、shell 為 nologin、互不同 group、互不在對方 group
getent passwd cortex-svc cortex-builder
id cortex-svc; id cortex-builder
#   期望：uid/gid 各自成對；cortex-svc 的 groups 不含 cortex-builder，反之亦然
getent group cortex-svc cortex-builder
```

**回滾**：`sudo userdel cortex-svc; sudo userdel cortex-builder;
sudo groupdel cortex-svc; sudo groupdel cortex-builder`（此時尚無任何檔案屬於它們）。

---

## 第 2 步：建目標樹並套用權限（全部機械產生）

裁決：`AGENTS_ROOT=/var/lib/cortex`、`WORKTREE_ROOT=/var/lib/cortex/worktree`、
`DEPLOY_ROOT=/opt/cortex`。這些值已固化在 `permgen.DEFAULT_LAYOUT`，下列命令直接引用。

### 2a. 產生兩份 script 並**先讀過**

```bash
# ✅ 產生骨架目錄 script（非登記表資產的父層：/opt/cortex、HOME、job spool…）
python3 -m paulsha_cortex.trust_root scaffold two-way > /tmp/p2b-scaffold.sh

# ✅ 產生權限 script（登記表每一項的 install -d／chown／chmod／setfacl）
python3 -m paulsha_cortex.trust_root permissions two-way --commands --paths \
  > /tmp/p2b-permissions.sh

# ✅ 逐行讀過再執行——這是 operator 核可的實體動作
less /tmp/p2b-scaffold.sh
less /tmp/p2b-permissions.sh

# ✅ 稽核 1：所有 mode 都不得有 group／other 寫入位（spec §R2）
grep -oE "chmod [0-7]{4}" /tmp/p2b-permissions.sh | sort -u \
 | awk '{m=$2; if (substr(m,3,1) ~ /[2367]/ || substr(m,4,1) ~ /[2367]/) {print "!! group/other writable: " m; bad=1}}
        END{ if (!bad) print "no group/other write: OK" }'

# ✅ 稽核 2：ACL 授「寫」只准出現在 event-spool（producer 只能 append）
grep -E "^setfacl" /tmp/p2b-permissions.sh | grep -E ":[^ ]*w"
#   期望：**恰好兩行**，皆為 u:cortex-builder:wx /var/lib/cortex/monitor/event-spool
#         （access 與 default 各一）。多出任何一行都要停下來查。

# ✅ 稽核 3：setfacl 可用（缺 acl 套件會讓跨帳號唯讀授權整段失效）
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
> `/var/lib/cortex/runtime`、兩個 HOME 以 root 身分就位，權限 script 之後只會補
> 葉節點，不會把 root-owned 父層蓋成服務帳號所有。

```bash
# ✅ 驗證：樹根 root 擁有、Manager-owned 子樹 cortex-svc 0700、無 g+w／o+w
ls -ld /var/lib/cortex /opt/cortex
ls -ld /var/lib/cortex/control /var/lib/cortex/coordinator /var/lib/cortex/specs \
       /var/lib/cortex/monitor /var/lib/cortex/registry /var/lib/cortex/worktree
#   期望：/var/lib/cortex → root:root 0755
#         control/coordinator/specs/monitor/registry → cortex-svc:cortex-svc 0700
#         worktree → cortex-svc:cortex-svc 0701（others 只 traverse，不可列目錄）

# ✅ 驗證：全樹沒有任何 group/other 可寫的路徑（spec §R2 硬性要求）
sudo find /var/lib/cortex /opt/cortex -perm /022 -print | tee /tmp/p2b-world-writable.txt
#   期望：空輸出

# ✅ 驗證：ACL 已就位（跨帳號讀取一律唯讀）
sudo getfacl -p /var/lib/cortex/monitor/event-spool 2>/dev/null | grep -E "^user:"
#   期望：user:cortex-builder:-wx（producer 只能 append，不可讀他人）

# ✅ 驗證：HOME 與 ~/.codex 由 root 擁有（帳號不得替換自己的設定）
ls -ld /var/lib/cortex-builder /var/lib/cortex-builder/.codex /var/lib/cortex-builder/cache
#   期望：前兩者 root:root 0755；cache 為 cortex-builder:cortex-builder 0700
```

**回滾**：`sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-builder /opt/cortex`
（此時新樹仍空，舊樹完全未動）。

---

## 第 3 步：legacy-import（物理隔離 ＋ hash manifest；**不 chown 沿用**）

裁決 3：舊 state **不**併入新樹、**不** `chown` 沿用，而是整包搬到 quarantine，
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

# 🔧 sudo：建 quarantine 並整包搬入（唯讀、cortex-svc 擁有，不併入新樹）
sudo install -d -o cortex-svc -g cortex-svc -m 0700 /var/lib/cortex/legacy-imported
sudo cp -a "$HOME/.agents/." /var/lib/cortex/legacy-imported/
sudo cp "/tmp/legacy-import-manifest-$STAMP.txt" \
        /var/lib/cortex/legacy-imported/.legacy-import-manifest.txt
sudo chown -R cortex-svc:cortex-svc /var/lib/cortex/legacy-imported
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
```

> **重要**：新樹是**乾淨的**。切換後產生的 record 一律走正常 gate；
> `legacy-imported/` 只是唯讀歷史副本，任何從中還原的內容**不得**被 ship gate 採計。
> 正式的 `trust: legacy-imported` 簽章標記屬 **Phase 3**，本階段以「物理隔離＋hash
> manifest」達成同等的不可竄改性主張（裁決 3）。

### 3b. review verdict spool（Phase 2a 已就位的受控通道）

Phase 2a（PR #599）已把 review verdict 的落點從 reviewer worktree 搬到
`/var/lib/cortex/coordinator/review-verdicts/<reviewer_job_id>/verdict.json`
（登記表 `review-verdict-spool`，spec §R2）。**程式碼側已完成**：per-job 目錄由
Manager 在 dispatch 當下以 `0700` 建立、帶 pre-seed 守衛，落地後轉 `0444`；reviewer
身分由 Manager job registry 推導，verdict payload 的自述綁定欄位一律忽略。

spool 根的權限**已包含在第 2 步的權限 script 內**（它是登記表資產），這裡只做確認：

```bash
# ✅ 驗證：spool 根由 cortex-svc 擁有、0700；builder 完全無權限
ls -ld /var/lib/cortex/coordinator/review-verdicts
sudo getfacl -p /var/lib/cortex/coordinator/review-verdicts | grep -E "^user:" || echo "(二分方案無跨帳號 ACL：reviewer 與 Manager 併帳，owner 位已涵蓋)"

# ✅ 驗證：產生器對本資產的計畫（三分方案才會出現 reviewer 的 write-only ACL）
python3 -m paulsha_cortex.trust_root permissions three-way --commands --paths \
  | grep -A6 "review-verdict-spool"
#   期望（三分）：setfacl -m u:cortex-reviewer-planner:wx …
#   **wx 無 r**——寫得進自己那格、讀不到他人 verdict。builder 兩案下都零權限。
```

> 這一步完成後，spec 背景 §3 的「builder 代寫 verdict」最短攻擊路徑才從「結構上
> 不可能被採信」升級為「**OS 層寫不進去**」。
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
sudo -u cortex-svc /opt/cortex/venv/bin/cortex --version
sudo -u cortex-svc test -w /opt/cortex/venv/bin/cortex && echo "!! 可寫，停止" || echo "read-only: OK"
sudo find /opt/cortex/venv -perm /022 -print | head
#   期望：空輸出
# ✅ 沒有可注入點
sudo find /opt/cortex/venv -name "sitecustomize.py" -o -name "*.pth" | xargs -r ls -l
#   期望：若有，全部 root:root 且不可寫
```

> spec §R3：executable／deps／launcher／venv 對 headless **不可寫**、owner=root。
> 這封掉背景 §5 的「改寫 verifier／注入 `sitecustomize.py`／`.pth`」攻擊面。

### 4b. bootstrap env 遷入部署樹（root 擁有、fail-closed）

env 檔放在 **`/opt/cortex/etc/`**（全 root-owned 樹）而**不是** `/var/lib/cortex` 底下：
`/var/lib/cortex` 的子樹由 `cortex-svc` 擁有，**目錄可寫者能 unlink／replace 其中的
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
python3 -m paulsha_cortex.trust_root permissions two-way --commands --paths \
  | grep -A3 "runtime-bootstrap-env"
ls -l /opt/cortex/etc/cortex-manager.env
#   期望：-rw-r--r-- root root

# ✅ 驗證：env 生效後路徑解析全部落在受保護樹內
sudo -u cortex-svc env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
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
python3 -m paulsha_cortex.trust_root unit two-way --manager | less

# 🔧 sudo：寫入 unit（內容一字不改，直接由產生器落檔）
python3 -m paulsha_cortex.trust_root unit two-way --manager \
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
#   期望：User=cortex-svc、NoNewPrivileges=yes、ProtectSystem=strict、
#         ProtectHome=yes、PrivateTmp=yes、CapabilityBoundingSet=（空）

# ✅ 驗證：unit 檔內容與產生器輸出逐位元相同（防手改漂移）
diff <(python3 -m paulsha_cortex.trust_root unit two-way --manager) \
     /etc/systemd/system/cortex-manager.service && echo "unit in sync: OK"

# ✅ 驗證：加固評分（systemd 自己的評估，僅供對照）
systemd-analyze security cortex-manager.service | tail -5

# ✅ 驗證：服務起得來、無 EPERM/EROFS
systemctl status cortex-manager.service --no-pager
sudo journalctl -u cortex-manager.service -n 100 --no-pager | grep -Ei "eperm|erofs|eacces|read-only" || echo "no denial in log: OK"

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

## 第 5 步：降權啟用（`cortex-svc` → `cortex-builder`，關 FD、不傳 token）

> **⏸ 本步有兩個方案，正等 operator 拍板**——這是全 runbook 唯一還需要決定的地方。
> 兩案都寫成完整可執行；**選了哪一個就只跑那一節**，其餘步驟（1–4、6–9）兩案共用。
> 同時要一併裁決的是「**是否提前三分**」（見下方風險比較）。

### 5-0. 共同前提：polkit 到底能收窄什麼（先讀完再選）

**實測事實**（#603 驗證）：polkit 的 `org.freedesktop.systemd1.manage-units` action
**只暴露 unit 名稱與 verb**，**不暴露 `User=`／`--uid=`**。授權之後，systemd 會照請求
的**任意** `User=` 起 unit。因此：

| polkit **能**強制 | polkit **不能**強制 |
|---|---|
| 呼叫者是哪個 UID（`subject.user`） | job 降到哪個帳號（`User=`／`--uid=`） |
| unit 名前綴／pattern（`action.lookup("unit")`） | 任何 unit 屬性（`AmbientCapabilities=`、`ExecStart=`…） |
| verb（只放行 `start`／`stop`） | — |

「只能降到 `cortex-builder`」這一半必須另外找地方強制——這就是 A／B 兩案的分野。
兩案共用的 polkit 骨架（subject 檢查、action 檢查、**明細缺席即拒**、verb 白名單、
unit pattern）由同一個產生器產出，只有 pattern 與說明段不同。

```bash
# ✅ 兩案的規則內容各印一份，並排比較後再選
python3 -m paulsha_cortex.trust_root polkit two-way --transient | tee /tmp/polkit-A.rules
python3 -m paulsha_cortex.trust_root polkit two-way --template  | tee /tmp/polkit-B.rules
diff /tmp/polkit-A.rules /tmp/polkit-B.rules
```

#### 兩案比較（含「是否提前三分」的關聯）

| | **方案 A：`systemd-run` transient unit** | **方案 B：root-owned 模板 unit** |
|---|---|---|
| 裁決對應 | 0816 裁決的**字面**方案 | 同一意圖的 OS 強制版 |
| 程式碼狀態 | **已落地**（#603 `coordinator/job_runner.py`，`PSC_JOB_RUNNER=systemd-run`） | 需 Manager 端改以 `systemctl start cortex-job@<id>` 起 job——**尚待程式碼工項** |
| 「降到哪個帳號」由誰強制 | Manager 端**封閉的 argv 產生器**（code level，`--uid` 受 POSIX 帳號名 pattern 檢查） | **OS**：`User=` 硬寫死在 root-owned 的 `/etc/systemd/system/cortex-job@.service` |
| 特權屬性（`AmbientCapabilities=` 等） | 呼叫端**可以**傳（polkit 看不到），靠 argv 產生器不傳 | 呼叫端**連提都提不了**（屬性只存在於 root-owned 檔內） |
| **殘餘風險** | 與 `cortex-svc` **同 UID 的任何行程**都持有這個 grant，可請求任意 `User=`（含 `User=root`）的 transient unit。**二分方案下 reviewer／planner 跑模型且與 Manager 併帳**——其中任一被攻陷即取得該能力 | 無（OS 層封閉） |
| 與「提前三分」的關係 | **強相關**：改三分後 reviewer／planner 移出 `cortex-svc`，A 的殘餘風險縮回「只有 Manager 自己」 | 不相關（B 本來就不靠 UID 隔離來守這一半） |
| 立即可執行 | ✅ | ⚠️ 規則與模板 unit 可先裝，但 Manager 端要等程式碼 |

**建議判讀**（不代替裁決）：若要**現在**打開降權 → 選 A，並**同時**決定是否提前三分
（三分把 A 的殘餘風險從「三個模型 persona」縮到「Manager 自己」）。若可以等一個程式碼
工項 → B 是唯一能把整個分界線放進 OS 的做法。

```bash
# ✅ 三分方案的權限／unit／polkit 也已可產生（決定提前三分時把 two-way 換成 three-way）
python3 -m paulsha_cortex.trust_root permissions three-way --commands --paths | head -20
python3 -m paulsha_cortex.trust_root polkit three-way --transient | sed -n '/未強制/,/^\/\/$/p'
#   期望：三分下的殘餘風險清單**不再**包含 reviewer／planner 併帳那一條
```

---

### 5-A. 方案 A：`systemd-run` transient unit（0816 裁決的字面方案）

#### 5-A-1. Manager 端會實際發出的 argv（polkit 要收窄的那個面）

`job_runner.build_systemd_run_argv()` 的形狀是**封閉**的（新增旗標要改程式碼並過測試）：

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

- unit 名前綴固定 `cortex-job-`（`job_runner.UNIT_NAME_PREFIX`）——**與 polkit 規則是
  成對契約**，改任一邊都要同步改另一邊，否則所有 job 被拒（fail-closed，不會退回同 UID）。
- `--wait` 讓 client 與 unit 同壽命，Manager 既有的 `pid_alive()` 判活不必改。
- `--quiet` 必要：`systemd-run` 的狀態訊息會被 `--pipe` 導進 job 的 JSONL log，而那份
  log 是 terminal evidence 的來源。
- FD 只有 stdin/stdout/stderr（`close_fds=True`），stdin 顯式接 `/dev/null`。
- **env 是白名單不是黑名單 scrub**：transient unit 不繼承呼叫端 environ，job 只看得到
  `--setenv` 列出的那幾項；gh token、`CLAUDE_CONFIG_DIR`／`GH_CONFIG_DIR` 都**不在**上面。

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

# ✅ 契約對齊：polkit 規則的 pattern 前綴必須等於 job_runner 的常數
python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
from paulsha_cortex.trust_root import permgen
prefix = permgen.transient_unit_prefix(permgen.DEFAULT_LAYOUT)
assert prefix == job_runner.UNIT_NAME_PREFIX, (prefix, job_runner.UNIT_NAME_PREFIX)
print("unit prefix contract OK:", prefix)
PY
```

#### 5-A-2. 安裝 polkit 規則

```bash
# ✅ 先讀（規則檔開頭把殘餘風險逐條寫出來，勿跳過）
less /tmp/polkit-A.rules

# 🔧 sudo：落檔
sudo install -o root -g root -m 0644 /tmp/polkit-A.rules \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ✅ 驗證：載入無語法錯誤、與產生器逐位元相同
sudo journalctl -u polkit -n 30 --no-pager | grep -Ei "error|syntax" || echo "polkit loaded clean: OK"
diff <(python3 -m paulsha_cortex.trust_root polkit two-way --transient) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules && echo "polkit in sync: OK"
```

#### 5-A-3. 打開開關（Manager env）

```bash
# 🔧 sudo：把降權模式寫進第 4b 步的 EnvironmentFile
sudo tee -a /opt/cortex/etc/cortex-manager.env >/dev/null <<'ENVFILE'
PSC_JOB_RUNNER=systemd-run
PSC_BUILDER_ACCOUNT=cortex-builder
PSC_BUILDER_HOME=/var/lib/cortex-builder
ENVFILE
sudo systemctl restart cortex-manager.service
```

> `PSC_JOB_RUNNER` 預設 `direct`＝不降權；值非法時**fail-closed**（不會靜默當成
> `direct`）。`PSC_BUILDER_PATH` 選配——模型 CLI 不在 Manager `PATH` 上時才需要。
> **builder 帳號必須自己有模型 CLI 的登入態**（`sudo -u cortex-builder claude /login`
> 之類）；Manager 不會把自己的憑證傳過去，那正是本步驟的重點。

#### 5-A-4. 驗證（正向必須成功）

```bash
# ✅ 以 cortex-svc 起一個降到 cortex-builder 的 transient job
sudo -u cortex-svc systemd-run --quiet --collect --pipe --wait \
  --unit=cortex-job-smoke-00000000.service \
  --uid=cortex-builder --gid=cortex-builder --service-type=exec \
  --property=NoNewPrivileges=yes \
  /bin/sh -c 'id; echo "GH_TOKEN=[$GH_TOKEN]"; ls -l /proc/self/fd'
#   期望：uid=…(cortex-builder)；GH_TOKEN=[]；/proc/self/fd 只有 0/1/2
```

#### 5-A-5. 驗證（反向必須被拒）

```bash
# ✅ (1) 不符前綴的 unit 名：必須被拒
sudo -u cortex-svc systemd-run --quiet --unit=evil-0001.service --uid=cortex-builder \
     --pipe --wait /bin/id; echo "exit=$?"          # 期望非 0

# ✅ (2) 起既存 unit（含 Manager 自己）：必須被拒
sudo -u cortex-svc systemctl restart cortex-manager.service; echo "exit=$?"   # 期望非 0
sudo -u cortex-svc systemctl daemon-reload; echo "exit=$?"                    # 期望非 0

# ✅ (3) 負控制：暫時移除 polkit 規則後，dispatch 必須 fail-closed 而非退回 direct
sudo mv /etc/polkit-1/rules.d/49-cortex-downgrade.rules /tmp/polkit-A.disabled
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service
#   → 觸發一次 dispatch，期望：job 落 needs_human，理由碼
#     job-runner-transient-unit-start-failed，detail 帶 systemd-run 的實際拒絕訊息。
#     **不得**出現以 cortex-svc 身分跑起來的 job。
sudo mv /tmp/polkit-A.disabled /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ⚠️ (4) 已知**不會**被拒（這就是 A 的殘餘風險，實測確認它確實存在）
sudo -u cortex-svc systemd-run --quiet --unit=cortex-job-probe-00000000.service \
     --uid=0 --pipe --wait /bin/id; echo "exit=$?"
#   期望：**成功並印出 uid=0(root)**。這不是設定錯誤，是 A 方案的本質限制：
#   polkit 看不到 --uid。看到這個結果就代表你確實理解自己接受了什麼。
#   選 B 方案時這條必須改為「期望非 0」。
```

#### 5-A-6. 建議的額外加固（與方案 B 同源，機械產生）

`job_runner` 目前只送 `--property=NoNewPrivileges=yes`。同一套加固表與**由登記表導出
的 `ReadWritePaths`** 可展開成 `--property=` 形式，作為 A 與 B 加固面是否等價的對照：

```bash
# ✅ 印出建議清單（%i 是模板 specifier；A 方案請由呼叫端代入該 job 的實際 worktree）
python3 -m paulsha_cortex.trust_root unit two-way --job-properties
```

---

### 5-B. 方案 B：root-owned 模板 unit（把整條分界線放進 OS）

兩個 root-owned 檔一起構成邊界：

1. **模板 unit** `/etc/systemd/system/cortex-job@.service`——`User=cortex-builder`
   **硬寫死**；呼叫端只能給 instance 名，**選不了 UID、傳不了屬性**。
2. **polkit 規則**——只放行 `cortex-svc` 對 `cortex-job@*.service` 的 `start`/`stop`，
   **transient unit 一律拒**（明細缺席即拒）。

> **前置條件**：Manager 端目前走 `systemd-run`（#603）。要真的用 B，需要一個程式碼
> 工項把 spawn 改成「寫 `run.sh` 進 job spool → `systemctl start cortex-job@<id>.service`」。
> **在那之前，B 的規則與模板 unit 仍可先裝並用下面的手動驗證證明邊界成立**，只是
> Manager 還不會走它。

#### 5-B-1. 安裝模板 unit

```bash
# ✅ 先看內容
python3 -m paulsha_cortex.trust_root unit two-way --job | less

# 🔧 sudo：落檔（root 擁有——這是 User= 不可被竄改的前提）
python3 -m paulsha_cortex.trust_root unit two-way --job \
  | sudo tee /etc/systemd/system/cortex-job@.service >/dev/null
sudo chown root:root /etc/systemd/system/cortex-job@.service
sudo chmod 0644 /etc/systemd/system/cortex-job@.service
sudo systemctl daemon-reload
```

#### 5-B-2. 安裝 polkit 規則

```bash
# 🔧 sudo：落檔
sudo install -o root -g root -m 0644 /tmp/polkit-B.rules \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules
sudo systemctl restart polkit.service 2>/dev/null || sudo systemctl restart polkitd.service

# ✅ 驗證：與產生器逐位元相同、載入無錯
diff <(python3 -m paulsha_cortex.trust_root polkit two-way --template) \
     /etc/polkit-1/rules.d/49-cortex-downgrade.rules && echo "polkit in sync: OK"
sudo journalctl -u polkit -n 30 --no-pager | grep -Ei "error|syntax" || echo "polkit loaded clean: OK"
```

#### 5-B-3. 準備 job spool 並實測降權（正向必須成功）

job 的命令由 Manager 寫進 svc-owned spool，job 帳號只有讀權——因此**改不了自己的
命令列，也埋伏不了下一個 job**。

```bash
JOB=selftest

# 🔧 sudo：per-job spool（svc 擁有、builder 只讀）
sudo install -d -o cortex-svc -g cortex-svc -m 0700 "/var/lib/cortex/jobs/$JOB"
sudo setfacl -m u:cortex-builder:r-x "/var/lib/cortex/jobs/$JOB"
sudo tee "/var/lib/cortex/jobs/$JOB/run.sh" >/dev/null <<'SH'
#!/bin/sh
echo "== identity =="; id
echo "== tokens =="; echo "GH_TOKEN=[$GH_TOKEN] GITHUB_TOKEN=[$GITHUB_TOKEN]"
echo "== inherited fds =="; ls -l /proc/self/fd
echo "== home =="; echo "HOME=$HOME"; ls -ld "$HOME" 2>&1
SH
sudo chown cortex-svc:cortex-svc "/var/lib/cortex/jobs/$JOB/run.sh"
sudo chmod 0600 "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-builder:r-- "/var/lib/cortex/jobs/$JOB/run.sh"

# 🔧 sudo：job 的 worktree（builder 擁有）
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

# ✅ 驗證（正向）：以 cortex-svc 身分起 job——**必須成功**
sudo -u cortex-svc systemctl start "cortex-job@$JOB.service"
sudo journalctl -u "cortex-job@$JOB.service" -n 50 --no-pager
#   期望輸出：
#     uid=…(cortex-builder) gid=…(cortex-builder)
#     GH_TOKEN=[] GITHUB_TOKEN=[]                ← token 已 scrub
#     /proc/self/fd 只有 0/1/2                    ← 無指向受保護資產的可寫 fd（R9 T4.1）
#     HOME=/var/lib/cortex-builder，且該目錄為 root:root
```

#### 5-B-4. 驗證（反向：全部必須失敗）

```bash
# ✅ (1) transient unit 起 root：必須被拒
sudo -u cortex-svc systemd-run --uid=0 --pipe --wait /bin/id; echo "exit=$?"
#   期望：非 0；訊息含 "Interactive authentication required" 或 "Access denied"

# ✅ (2) transient unit 起 builder：**同樣**必須被拒（transient 一律封）
sudo -u cortex-svc systemd-run --uid=cortex-builder --pipe --wait /bin/id; echo "exit=$?"
#   期望：非 0。這就是 B 相對 A 多出來的那一半保證。

# ✅ (3) transient unit 夾帶特權屬性：必須被拒
sudo -u cortex-svc systemd-run --uid=cortex-builder \
     --property=AmbientCapabilities=CAP_SETUID --pipe --wait /bin/id; echo "exit=$?"

# ✅ (4) 起別的既存 unit（含 Manager 自己）：必須被拒
sudo -u cortex-svc systemctl restart cortex-manager.service; echo "exit=$?"
sudo -u cortex-svc systemctl start sshd.service 2>&1 | tail -1; echo "exit=$?"

# ✅ (5) 名稱夾帶（前綴／後綴混淆）：必須被拒
sudo -u cortex-svc systemctl start "evil-cortex-job@x.service"; echo "exit=$?"

# ✅ (6) 其他 verb：必須被拒
sudo -u cortex-svc systemctl mask "cortex-job@$JOB.service"; echo "exit=$?"
sudo -u cortex-svc systemctl daemon-reload; echo "exit=$?"

# ✅ (7) 直接改模板 unit：必須 EACCES
sudo -u cortex-svc sh -c 'echo "User=root" >> /etc/systemd/system/cortex-job@.service'; echo "exit=$?"
```

**通過條件**：5-B-3 正向成功且輸出符合期望；5-B-4 的 (1)–(7) **全部**非 0 退出。
任一反向測試通過（即攻擊成功）＝**立即停止**，回到第 9 步回滾。

```bash
# 🔧 sudo：清掉 selftest 殘留
sudo systemctl stop "cortex-job@$JOB.service" 2>/dev/null || true
sudo rm -rf "/var/lib/cortex/jobs/$JOB" "/var/lib/cortex/worktree/$JOB"
```

---

### 5-C. 產生邏輯的離線對照（兩案共用）

```bash
# ✅ polkit 無法本機模擬時的第二證據：決策矩陣測試（含兩案互不放行對方的 unit 形狀）
python3 -m pytest tests/test_trust_root_permgen_p2b.py -q -k polkit
```

**回滾**（兩案共用）：`sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules
/etc/systemd/system/cortex-job@.service; sudo systemctl daemon-reload;
sudo systemctl restart polkit.service`；A 方案另需把 `PSC_JOB_RUNNER` 移出
EnvironmentFile（回 `direct`）。降權停用後 Manager 以
`PSC_DEGRADED_OPERATION=per-case-approval` 不 spawn job 運轉。

---

## 第 6 步：升級流程（**不 codify**——手動 runbook，cortex 只產生字串）

裁決 6：**不**提供 `cortex install trust-root --system` 子命令。把特權操作寫進
codebase 等於把提權路徑收進攻擊面內；cortex 只負責產生命令字串與驗證，root 由
operator 手動執行。升級因此是下列固定流程：

```bash
# ✅ 1. 在 operator 帳號的 pipx 環境驗新版（完全不碰 /opt/cortex）
pipx upgrade paulsha-cortex     # 或既有 build 流程
"$HOME/.local/share/pipx/venvs/paulsha-cortex/bin/cortex" --version

# ✅ 2. 差異對照（新舊部署的內容 hash）
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
sudo -u cortex-svc /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root selfcheck
sudo -u cortex-svc /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root equation

# ✅ 5. 登記表若有變動，unit 必須重新產生（ReadWritePaths 可能改變）
diff <(sudo -u cortex-svc /opt/cortex/venv.new/bin/python -m paulsha_cortex.trust_root unit two-way --manager) \
     /etc/systemd/system/cortex-manager.service || echo "!! unit 需更新，見下"

# 🔧 6. sudo：原子切換（保留前一版供回滾）
sudo systemctl stop cortex-manager.service
sudo rm -rf /opt/cortex/venv.prev
sudo mv /opt/cortex/venv /opt/cortex/venv.prev
sudo mv /opt/cortex/venv.new /opt/cortex/venv
#   若第 5 步顯示 unit 需更新：
#   python3 -m paulsha_cortex.trust_root unit two-way --manager | sudo tee /etc/systemd/system/cortex-manager.service >/dev/null
#   sudo systemctl daemon-reload
sudo systemctl start cortex-manager.service
```

```bash
# ✅ 驗證：新版在跑、自檢綠
sudo -u cortex-svc /opt/cortex/venv/bin/cortex --version
systemctl status cortex-manager.service --no-pager | head -5
```

> **不裸 chown**：升級不是「把 headless 產出的檔 chown 給 svc」，而是「operator 驗證
> 來源後，以 root 身分整棵替換部署樹」。任何 headless 都碰不到 `/opt/cortex`。
> **回滾**：`sudo systemctl stop cortex-manager; sudo rm -rf /opt/cortex/venv;
> sudo mv /opt/cortex/venv.prev /opt/cortex/venv; sudo systemctl start cortex-manager`。

---

## 第 7 步：切換驗收（Phase 1 自檢轉綠）

```bash
# ✅ 以 cortex-svc（Manager 身分）跑自檢——Manager-owned 樹應無 job-writable
sudo -u cortex-svc env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
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
工項。下列每條都以 **`cortex-builder` 身分**執行，並附**確切命令**與**預期輸出**。
每族末尾都有 **negative control**（以受信任身分做同一件事**必須成功**）——沒有它，
環境壞掉（目錄根本不存在）會產生假綠（spec §R9）。

### 8a. 起一個 R9 攻擊 job

```bash
JOB=r9
sudo install -d -o cortex-svc -g cortex-svc -m 0700 "/var/lib/cortex/jobs/$JOB"
sudo setfacl -m u:cortex-builder:r-x "/var/lib/cortex/jobs/$JOB"
sudo install -d -o cortex-builder -g cortex-builder -m 0700 "/var/lib/cortex/worktree/$JOB"

sudo tee "/var/lib/cortex/jobs/$JOB/run.sh" >/dev/null <<'SH'
#!/bin/sh
t() { printf '%s :: ' "$1"; shift; if "$@" >/dev/null 2>&1; then echo "!! SUCCEEDED (FAIL)"; else echo "denied (OK) rc=$?"; fi; }
A=/var/lib/cortex

echo "===== 族 1 capability-theft ====="
t "T1.1 讀 EnvironmentFile"        cat /opt/cortex/etc/cortex-manager.env
t "T1.1 讀 svc HOME cache"         ls /var/lib/cortex-svc/cache
t "T1.2 連 Manager control socket" sh -c "ls $A/run/cortex && cat $A/run/cortex/*.sock"
t "T1.3 呼叫 operator CLI"         /opt/cortex/venv/bin/cortex work ship --help
t "T1.4 直寫 control queue"        sh -c "printf x > $A/control/requests/evil.json"

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
t "T2 寫他人 worktree"             sh -c "printf x > $A/worktree/victim/evil"
t "T2 列 worktree pool"            ls "$A/worktree"

echo "===== 族 3 enforcement-plane-tampering ====="
t "T3.1 改 site-packages verifier" sh -c "printf x >> /opt/cortex/venv/lib/python3*/site-packages/paulsha_cortex/coordinator/verification.py"
t "T3.2 注入 sitecustomize"        sh -c "printf x > /opt/cortex/venv/lib/python3*/site-packages/sitecustomize.py"
t "T3.2 注入 .pth"                 sh -c "printf x > /opt/cortex/venv/lib/python3*/site-packages/evil.pth"
t "T3.3 改 Manager unit"           sh -c "printf x >> /etc/systemd/system/cortex-manager.service"
t "T3.3 改 job 模板 unit"          sh -c "printf 'User=root' >> /etc/systemd/system/cortex-job@.service"  # 方案 B 才有此檔；A 下為 ENOENT，同樣算拒絕
t "T3.4 改 EnvironmentFile"        sh -c "printf 'PSC_COORDINATOR_ROOT=/tmp/evil' >> /opt/cortex/etc/cortex-manager.env"
t "T3.5 刪 EnvironmentFile"        rm -f /opt/cortex/etc/cortex-manager.env
t "T3.6 改 venv launcher"          sh -c "printf x >> /opt/cortex/venv/bin/cortex"
t "T3.9 改 codex hooks"            sh -c "printf x > /var/lib/cortex-builder/.codex/hooks.json"
t "T3.9 換掉 ~/.codex 目錄"        sh -c "mv /var/lib/cortex-builder/.codex /var/lib/cortex-builder/.codex.bak"
t "T3 改 polkit 規則"              sh -c "printf x >> /etc/polkit-1/rules.d/49-cortex-downgrade.rules"

echo "===== 族 4 行程間路徑 ====="
echo "T4.1 自己的 fd：" ; ls -l /proc/self/fd
MPID=$(pgrep -u cortex-svc -n -f paulsha_cortex 2>/dev/null || echo 0)
echo "manager pid = $MPID"
t "T4.2 ptrace Manager"            sh -c "command -v gdb >/dev/null && gdb -p $MPID -batch -ex quit"
t "T4.3 讀 Manager environ"        cat "/proc/$MPID/environ"
t "T4.3 讀 Manager mem"            head -c 1 "/proc/$MPID/mem"
t "T4.4 對 Manager 送 SIGSTOP"     kill -STOP "$MPID"
t "T4 提權：systemd-run root"      systemd-run --uid=0 --pipe /bin/id
t "T4 提權：起別的 job"            systemctl start cortex-job@other.service
t "T4 提權：sudo"                  sudo -n true
SH
#   注意：以上三條是以 **cortex-builder 身分**執行——builder 在兩個方案下都**不在**
#   polkit 授權面上（規則的 subject 只有 cortex-svc），故兩案都必須 denied。
#   方案 A 的殘餘風險是「以 **cortex-svc** 身分」起任意 UID，那條在第 5-A-5 (4) 實測。
sudo chown cortex-svc:cortex-svc "/var/lib/cortex/jobs/$JOB/run.sh"
sudo chmod 0600 "/var/lib/cortex/jobs/$JOB/run.sh"
sudo setfacl -m u:cortex-builder:r-- "/var/lib/cortex/jobs/$JOB/run.sh"

# 準備一個「他人 worktree」作為跨 persona 攻擊目標
sudo install -d -o cortex-svc -g cortex-svc -m 0700 /var/lib/cortex/worktree/victim
sudo -u cortex-svc sh -c 'printf "{\"verdict\":\"fail\"}" > /var/lib/cortex/worktree/victim/.psc-review-verdict.json'

# 🔧 sudo：以 svc 身分起攻擊 job（依第 5 步選定的方案二擇一）

# 方案 A（systemd-run transient unit）：
sudo -u cortex-svc systemd-run --quiet --collect --pipe --wait \
  --unit="cortex-job-r9-00000000.service" \
  --uid=cortex-builder --gid=cortex-builder --service-type=exec \
  --property=NoNewPrivileges=yes \
  --working-directory="/var/lib/cortex/worktree/$JOB" \
  /bin/sh "/var/lib/cortex/jobs/$JOB/run.sh" | tee /tmp/r9-report.txt

# 方案 B（root-owned 模板 unit）：
# sudo -u cortex-svc systemctl start "cortex-job@$JOB.service"
# sudo journalctl -u "cortex-job@$JOB.service" -n 300 --no-pager | tee /tmp/r9-report.txt
```

**預期輸出**：`/tmp/r9-report.txt` 中**每一條**都是 `denied (OK) rc=<非 0>`；
`T4.1 自己的 fd` 只列 `0`、`1`、`2`（指向 journal socket 或 `/dev/null`），
**沒有任何**指向 `/var/lib/cortex` 或 `/opt/cortex` 的可寫 fd。

```bash
# ✅ 一眼判讀
grep -c "denied (OK)" /tmp/r9-report.txt
grep "SUCCEEDED (FAIL)" /tmp/r9-report.txt && echo "!! 有攻擊成功，立即停止並回滾" || echo "R9 抽驗全綠"
grep -A5 "自己的 fd" /tmp/r9-report.txt
```

### 8b. negative control（受信任身分做同樣的事**必須成功**）

```bash
# ✅ 族 1／2 的 negative control：cortex-svc 寫得進去
sudo -u cortex-svc sh -c '
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

# ✅ 族 3 的 negative control：root 改得動 enforcement plane（且改完必須重啟複驗）
sudo cp /etc/systemd/system/cortex-manager.service /tmp/unit.bak
sudo sh -c 'printf "\n# negctl\n" >> /etc/systemd/system/cortex-manager.service'
sudo systemctl daemon-reload && sudo systemctl restart cortex-manager.service && echo "NEG-CONTROL-3-OK"
sudo cp /tmp/unit.bak /etc/systemd/system/cortex-manager.service
sudo systemctl daemon-reload && sudo systemctl restart cortex-manager.service

# ✅ 族 4 的 negative control：operator（有 sudo）讀得到 Manager environ
MPID=$(pgrep -u cortex-svc -n -f paulsha_cortex); sudo cat "/proc/$MPID/environ" | tr '\0' '\n' | head -3 && echo "NEG-CONTROL-4-OK"
```

### 8c. 族 3 的「重啟後仍綠」複驗（spec §R9 硬性要求）

```bash
# ✅ 每個族 3 案例改完 MUST 實際重啟服務再驗證
sudo systemctl restart cortex-manager.service
sleep 3
systemctl is-active cortex-manager.service
sudo -u cortex-svc env $(grep -v '^#' /opt/cortex/etc/cortex-manager.env | xargs) \
  /opt/cortex/venv/bin/python -m paulsha_cortex.trust_root selfcheck | head -20
#   期望：服務 active、自檢仍 ok=true（族 3 的攻擊沒有留下任何持久效果）
```

### 8d. 清理

```bash
sudo systemctl stop "cortex-job@r9.service" 2>/dev/null || true   # 方案 B 才需要
sudo rm -rf /var/lib/cortex/jobs/r9 /var/lib/cortex/worktree/r9 /var/lib/cortex/worktree/victim
```

**通過條件**：8a 全部 `denied (OK)`；8b 三組 negative control 全部印出 `*-OK`；
8c 重啟後仍綠。任一條不符 ⇒ **D6 不算通過**，`0.2.0` 不得宣告 stable（spec §R12）。

---

## 第 9 步：回滾（每階段可回滾 ＋ 全面退回 Phase 1）

Phase 1 完全不需 root 且含降級運轉安全網（`PSC_DEGRADED_OPERATION=per-case-approval`）。
任一階段出問題即退回「operator 帳號跑 ＋ 降級運轉」。

| 階段 | 症狀 | 回滾動作（`🔧 sudo`） |
|---|---|---|
| 第 1（UID） | 帳號建錯／名稱衝突 | `sudo userdel cortex-svc; sudo userdel cortex-builder; sudo groupdel cortex-svc; sudo groupdel cortex-builder`（此時尚無檔案屬於它們） |
| 第 2（樹／權限） | 權限套錯、`find -perm /022` 非空 | 重跑 `sudo sh -e /tmp/p2b-permissions.sh`（冪等）；仍不對則 `sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-builder` 後從第 2 步重來（舊樹未動） |
| 第 3（legacy-import） | quarantine 內容不符 manifest | `sudo rm -rf /var/lib/cortex/legacy-imported`，重跑 3；`$HOME/.agents` 原地仍完整 |
| 第 4a（部署） | 新 venv 起不來 | `sudo rm -rf /opt/cortex/venv; sudo mv /opt/cortex/venv.prev /opt/cortex/venv; sudo systemctl restart cortex-manager` |
| 第 4c（system unit） | WSL 重啟後未拉起／服務起不來 | `sudo systemctl disable --now cortex-manager.service`；改回 `systemctl --user start cortex-manager.service`（舊部署仍在 `$HOME/.local/share/pipx`） |
| 第 4c（加固誤擋） | 服務起來但功能靜默失效 | 見下方「`ProtectSystem=strict` 誤擋診斷」；**臨時** drop-in 放行、**同一天**把該路徑回填 R1 登記表並重跑 permgen |
| 第 5（降權） | polkit／transient unit／模板 unit 在 WSL2 不如預期 | `sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules /etc/systemd/system/cortex-job@.service; sudo systemctl daemon-reload; sudo systemctl restart polkit.service`；**方案 A 另需**把 `PSC_JOB_RUNNER` 移出 EnvironmentFile（回 `direct`）並 `sudo systemctl restart cortex-manager`；Manager 以 `per-case-approval` 不 spawn job 運轉 |
| 第 8（R9 有紅） | 任一攻擊成功 | **立即**停 job 派工，執行下方「全面回退」，在 #584 記錄該條攻擊路徑；D6 判定未通過 |

### 全面回退到 Phase 1（降級運轉）

```bash
# 🔧 sudo：停掉並移除 Phase 2 的一切
sudo systemctl disable --now cortex-manager.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/cortex-manager.service /etc/systemd/system/cortex-job@.service
sudo rm -f /etc/polkit-1/rules.d/49-cortex-downgrade.rules
#   （方案 A：EnvironmentFile 隨 /opt/cortex 一併移除，PSC_JOB_RUNNER 自然失效）
sudo systemctl daemon-reload
sudo systemctl restart polkit.service 2>/dev/null || true

# 🔧 sudo：新樹整棵丟棄（舊 state 從未被併入，故無資料損失）
sudo rm -rf /var/lib/cortex /var/lib/cortex-svc /var/lib/cortex-builder /opt/cortex

# ✅ 回到 operator 帳號部署 ＋ 降級運轉
export PSC_DEGRADED_OPERATION=per-case-approval
systemctl --user start cortex-manager.service
python3 -m paulsha_cortex.trust_root selfcheck    # 預期回到有 WARN 的 Phase 1 狀態
echo "PSC_DEGRADED_OPERATION=$PSC_DEGRADED_OPERATION"
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
#   方案 A：
sudo -u cortex-svc systemd-run --quiet --collect --pipe --wait \
  --unit=cortex-job-smoke-00000000.service --uid=cortex-builder --gid=cortex-builder \
  --service-type=exec /bin/id && echo "降權重啟後仍可用（A）"
#   方案 B：
# sudo -u cortex-svc systemctl start cortex-job@selftest.service && echo "降權重啟後仍可用（B）"
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
diff <(python3 -m paulsha_cortex.trust_root unit two-way --manager | grep '^ReadWritePaths=') \
     <(systemctl show cortex-manager.service -p ReadWritePaths | tr ' ' '\n' | sed 's/^/ReadWritePaths=/' | head -50)

# ✅ 3. 在**同一組沙箱條件**下重現（root 用 systemd-run 診斷，不放行給 svc）
sudo systemd-run --pipe --wait --uid=cortex-svc \
  --property=ProtectSystem=strict --property=ProtectHome=yes --property=PrivateTmp=yes \
  --property="ReadWritePaths=/var/lib/cortex/coordinator" \
  /bin/sh -c 'touch /var/lib/cortex/coordinator/.probe && echo WRITE-OK; touch /var/lib/cortex/specs/.probe || echo "specs blocked"'
#   ↑ 逐條加/減 ReadWritePaths，找出到底缺哪一條

# ✅ 4. 檢查有沒有落在 ProtectHome 遮住的區域（最常見的靜默失效）
systemctl show cortex-manager.service -p ReadWritePaths | tr ' ' '\n' | grep -E "^/home|^/root" \
  && echo "!! RWP 落在 ProtectHome 遮蔽區，必定失效"

# ✅ 5. 臨時放行（僅供診斷；當天必須回填登記表）
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

> **鐵律**：被擋的路徑**只能**經「回填 R1 登記表 → 重跑 permgen → 重新落檔 unit」
> 進入 `ReadWritePaths`。drop-in 只能作為**當天的**臨時措施，不得成為長期狀態——
> 否則 unit 就不再是登記表的機械投影，`diff` 對齊檢查會紅。

### C. WSL2 其他已知風險

1. **polkit 未安裝／未啟動**：systemd 對 unprivileged client 的授權在 polkit 不可用時
   **一律拒絕**（fail-closed）。表現為「Manager 完全 spawn 不了 job」而非提權——
   安全但功能全停。執行前提第 6 項已檢查；重啟後由「A. 第 5 項」複驗。
2. **`sudo` 需密碼**：所有 `🔧 sudo` 步驟皆互動式，**不可假設自動化**；
   本 runbook 刻意不使用 `sudo -n`。
3. **`/proc` 隱藏造成 R9 判讀差異**：`ProtectProc=invisible` 下讀他人
   `/proc/<pid>/environ` 會是 `ENOENT`（No such file）而非 `EACCES`——
   兩者都算**拒絕**（第 8 步的 `t()` 只看 rc 非 0，判定不受影響）。

---

## 附錄：本 runbook 的自我檢查

```bash
# ✅ unit／polkit 與產生器沒有漂移（建議排程每日跑）
#   PLAN=transient（方案 A）或 template（方案 B）——填第 5 步選定的那個
PLAN=transient
diff <(python3 -m paulsha_cortex.trust_root unit two-way --manager) /etc/systemd/system/cortex-manager.service
diff <(python3 -m paulsha_cortex.trust_root polkit two-way --$PLAN) /etc/polkit-1/rules.d/49-cortex-downgrade.rules
[ "$PLAN" = template ] && diff <(python3 -m paulsha_cortex.trust_root unit two-way --job) \
     /etc/systemd/system/cortex-job@.service

# ✅ 方案 A 專屬：unit 名前綴契約沒有漂移（改任一邊都會讓所有 job 被 polkit 拒）
[ "$PLAN" = transient ] && python3 - <<'PY'
from paulsha_cortex.coordinator import job_runner
from paulsha_cortex.trust_root import permgen
assert permgen.transient_unit_prefix(permgen.DEFAULT_LAYOUT) == job_runner.UNIT_NAME_PREFIX
print("unit prefix contract OK")
PY

# ✅ 權限沒有漂移
sudo find /var/lib/cortex /opt/cortex -perm /022 -print | head

# ✅ 產生器本身的等式測試（含 ReadWritePaths 無遺漏無多餘）
python3 -m pytest tests/test_trust_root_permgen_p2a.py tests/test_trust_root_permgen_p2b.py -q
```
