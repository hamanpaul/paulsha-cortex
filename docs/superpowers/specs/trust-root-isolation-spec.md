---
status: accepted
work_item: trust-root-isolation
---

# trust-root-isolation Specification

v4 重構計畫 **R0.5 D6**：把所有 security-relevant durable state、mutation ingress
**與 Manager／consumer 的執行面**（executable／dependencies／service unit／launcher／
env／信任設定）收進不受信任 headless persona 不可寫的邊界，並以
capability-theft／durable-state-tampering／enforcement-plane-tampering E2E 實測。
本項為 **0.2.0 穩定版不可豁免的 join gate**（計畫「貫穿工項」第 5 條，且明文排除在
第 7 條「有界殘餘風險」豁免通道之外）。

**本票是 spec-only：不實作任何一行程式碼、不改任何部署、不動 systemd、不改 auth。**
R1–R12 全為契約定案，供後續實作票（Phase 1／2／3）作為 Requirements 依據。

## 背景（main @ `3a62f55`，逐檔核對；部署現況為唯讀觀測）

威脅模型不是推測。headless job（builder／reviewer／planner）與 operator **同一個
OS UID、同一個 HOME**，本票逐檔核對後確認下列全部成立。

### 1. 路徑解析鏈本身就是信任根，且可被同 UID 重導

所有 durable 路徑掛在單一解析鏈：`config/runtime.py:89` 的
`resolve_runtime_root()`，優先序為 (1) process env `PSC_<NAME>`
（`runtime.py:98-103`）→ (2) process env `PSC_AGENTS_ROOT` 推導
（`runtime.py:105-115`）→ (3) installed bootstrap env
`$HOME/.agents/core/runtime/<instance>-manager.env`（`runtime.py:117-123`）→
(4) `$HOME/.agents` 預設（`runtime.py:126`）。

- bootstrap env 唯一的加固是 `runtime.py:30-31` 拒絕 symlink 與非 regular file；
  **無 owner 檢查、無 mode 檢查、無簽章**。
- installer 以裸 `write_text` 寫入該檔且**不設 mode**（`deploy/installer.py:162`）；
  unit 檔同樣裸寫（`installer.py:298`）。
- 路徑是**每次呼叫才求值的函式**（`config/paths.py:21-120`），process 中途改 env
  即改變落點。

實地觀測（唯讀）：bootstrap env 實際為 `-rw-rw-r--`（group-writable、world-readable），
`$HOME/.agents/` 底下 `control`／`coordinator`／`core`／`specs` 為
`drwxrwxr-x`（group-writable）。改掉這份檔的 `PSC_AGENTS_ROOT`／
`PSC_COORDINATOR_ROOT`，就把**整棵 durable state 重導**到攻擊者控制的目錄。

### 2. durable state 全是同使用者可寫的一般檔案

| 資產 | 路徑 | 定義位置 |
|---|---|---|
| Job／Slice／workflow-run registry | `<coordinator_root>/jobs.json` | `coordinator/registry.py:185-186` |
| **review verdict（模型寫在 worktree 內）** | `<review_worktree>/.psc-review-verdict.json` | `coordinator/review.py:22-23,176-185` |
| verification evidence | `<coordinator_root>/evidence/verification/<slice>-<sha>.json` | `coordinator/verification.py:305-316` |
| maintainer attestation | `<coordinator_root>/evidence/maintainer-review/<run_id>-<candidate>.json` | `coordinator/work_actions.py:1027-1060` |
| completion record | `<coordinator_root>/evidence/completion/<slice>-<sha>.json` | `coordinator/completion.py:74-83` |
| full-suite evidence | `<coordinator_root>/evidence/full-suite/<tree_hash>.json` | `coordinator/preflight.py:68-76` |
| workflow inputs（content-addressed） | `<coordinator_root>/evidence/workflow-inputs/<sha256>.json` | `coordinator/manager.py:4039` |
| workflow evidence（job-addressed） | `<coordinator_root>/evidence/workflow/<sha256(job_id)>.json` | `coordinator/work_bridge.py:947` |
| PR metadata／planning／report-cleanup 等 evidence | `<coordinator_root>/evidence/<category>/` | `coordinator/work_bridge.py:514,553-579,605-620,1245,1503` |
| gate ledger | `<manager log_dir>/<stem>.gates.json` | `coordinator/terminal_contract.py:488-494` |
| delivery journal | `<coordinator_root>/delivery-journal.json` | `coordinator/work_actions.py:516-517`（另五處重複推導） |
| provider backoff | `<coordinator_root>/provider-rate-limit-backoff.json` | `coordinator/provider_backoff.py:22,38-39` |
| workflow report 發佈 journal | `<coordinator_root>/workflow-report-transactions/<sha256>.json` | `coordinator/manager.py:4434` |
| digest outbox | `<coordinator_root>/digest/outbox/<ts>-<rand>.json` | `coordinator/digest.py:158-161` |
| engineering-outcome outbox | `<coordinator_root>/engineering-outcomes/<repo-slug>.jsonl` | `coordinator/engineering_outcome.py:508-524` |
| control request／done queue | `<control_root>/requests/`、`done/` | `control/constants.py:21-26` |
| control status／daemon lock | `<control_root>/status.json`、`manager.lock` | `control/constants.py:29-34` |
| monitor 讀模型 | `<monitor_state_root>/work-items.snapshot.json` | `config/paths.py:46-47`（另 `coordinator/claim.py:225-228` 重複推導） |
| GitHub 同步游標／ETag | `<monitor_state_root>/github-issue-sync.json` | `config/paths.py:50-57` |
| **work-item 對應覆寫（git-tracked）** | `<repo_root>/.cortex/work-items.yaml` | `monitor/correlation.py:79-80` |
| **model identity overlay（決定 independence_domain）** | `<project_config_root>/model-identities.yaml` | `coordinator/model_identities.py:564-565,604-631` |
| **combo／card override（instance-local 壓過 packaged）** | `<agents_root>/config/combos/` | `deck/schema.py:70-87` |
| dispatch specs（staged artifact） | `<agents_root>/specs/` | `deck/compile.py:201-208,740-800` |
| handoff manifest | `<repo_root>/runtime/handoff/<slice_id>.json` | `coordinator/autonomy.py:23`、`coordinator/manager.py:185` |
| skill ledger／park state／proposal | `<agents_root>/registry/…` | `config/paths.py:60-84` |
| runtime bootstrap env | `$HOME/.agents/core/runtime/<instance>-manager.env` | `config/runtime.py:61-66` |
| codex hooks | `$HOME/.codex/hooks.json` | `deploy/hooks.py:48-60` |

現行保護手段只有兩類，**兩類都對 owner 無效**：

- **mode 加固**：`registry.py:476`／`preflight.py:173`（`0o400`）、
  `manager.py:4051`（`0o444`）、`work_actions.py:1039-1045`（`O_EXCL 0600` →
  `fchmod 0o444`）、`work_snapshot.py:309-315`／`github_issue_sync.py:515`
  （`0o600`）。owner 可 `chmod` 回去、`unlink` 後重建、整檔 `os.replace`。
- **結構性守衛**：`O_EXCL` 先寫先贏、`st_mode & 0o222 == 0` 斷言
  （`manager.py:4044,4249`、`preflight.py:87`、`work_actions.py:1034,1055`）、
  symlink 拒絕、content-addressed 檔名。這些全部假設「檔案命名空間未被攻陷」，
  而在同 UID 下該假設不成立。

**完全不存在鍵控完整性**：`grep -rn 'import hmac' paulsha_cortex/` 零命中；
`hashlib` 只用於 content hash。所有 evidence 都是**自我雜湊**——把
`canonical_json_hash()` 算出的摘要存在檔案旁邊，只證明「這些位元組沒壞掉」，
不證明「誰產生的」。任何同 UID 行程都可以：寫一份新 evidence JSON → 自己算
`canonical_json_hash` → 以 `0444` 寫進 `evidence/` → 用一個 control request 讓
`gate_refs` 指過去，全部下游檢查照樣通過。

### 3. review verdict 由模型寫在 worktree 內，且同 UID 可被他人竄改

`review.py:22-23` 定義 `.psc-review-verdict.json` 與 `.psc-review-worktrees`；
`review.py:320-322` 只在**啟動 reviewer 前**檢查該路徑不存在且非 symlink。
verdict 由 reviewer 模型自己寫在其 worktree 內，Manager 事後以
`validate_review_verdict()`（`review.py:540-610`）驗結構。

在同 UID 下，**builder job 可以直接寫 reviewer 的 verdict 檔**——不需要偷任何
capability、不需要繞過任何 gate，就能為自己的 candidate 產生一份「通過」的
foreign review。這是本票認定的最短攻擊路徑，也是 `#484`（reviewer 實際跑
`--sandbox workspace-write`）之外**更根本**的一層：即使 reviewer 被正確限制成
read-only，builder 仍可代寫 verdict。

### 4. mutation ingress 完全未認證

`<control_root>/requests/*.json` 是全部 7 種 request type 與 22 種 work-action
的入口（`control/contract.py:13-23`），**唯一的門檻是「能不能在該目錄建檔」**：

- `control/contract.py:229-231`：`requested_by` 只驗「是字串且非空」——比 30 行
  之上的 `actor` 檢查（`contract.py:194-207`，長度／strip／printable）還弱。
- 該欄位卻是 `abandon`／`retire-delivered`／`reset-reclaim-budget` 的
  **actor fallback**（`work_actions.py:3649,3766,3961`），並被烘進不可變的
  `cortex-maintainer-review/v1` attestation（`work_actions.py:1166`）。
- `work_actions.py:1118-1131` 的 `_review_attest_action()`：`actor` 由 caller
  提供，`verdict` 只准 `"approved"`——等於「誰能寫 queue，誰就能發 maintainer
  review attestation」。
- **處理順序由 mtime 決定**（`manager_daemon.py:1431-1436` 的
  `(st_mtime_ns, name)`）——本地寫入者可操控 request 的處理次序。
- queue 目錄在 `UMask=0022`（`deploy/templates/manager.service.tmpl:14`）下建立
  （`manager_daemon.py:1274-1275`），落成 world-readable。
- control 端的原子寫（`control/contract.py:35-47`）連 `fsync` 與 dir fsync 都沒有，
  與 registry／evidence 的三步驟原子寫紀律不一致。

### 5. enforcement plane 自身在攻擊者可寫範圍內

`deploy/templates/manager.service.tmpl`：

```
EnvironmentFile=-%h/.agents/core/runtime/__INSTANCE__.env
EnvironmentFile=-%h/.agents/core/runtime/__INSTANCE__-manager.env
ExecStart=/usr/bin/env bash __SERVICE_SCRIPT__
WorkingDirectory=__REPO_ROOT__
UMask=0022
```

- unit 落在 `$HOME/.config/systemd/user/`（`installer.py:285,298`），
  實測 owner 為執行帳號、mode `-rw-rw-r--`。
- `__SERVICE_SCRIPT__` 解析為套件內的 `paulsha_cortex/scripts/service-manager.sh`
  （`installer.py:33-34,38-39`）；`__PY__` 為 `sys.executable`（`installer.py:43`）。
  實測兩者都在 `$HOME/.local/share/pipx/venvs/paulsha-cortex/` 底下，該目錄實測為
  `drwxrwxr-x`——**同 UID 可寫，且 group 亦可寫**。
- `EnvironmentFile=-` 的 `-` 表示缺檔靜默容忍，於是刪掉 env 檔即讓 daemon 落回
  `$HOME/.agents` 預設，是一條無聲的重導路徑。
- **三個 unit 都沒有任何 systemd 加固指令**：無 `ProtectHome`、`ProtectSystem`、
  `PrivateTmp`、`NoNewPrivileges`、`ReadWritePaths`、`CapabilityBoundingSet`、
  `SystemCallFilter`。
- **env 直接指名並執行任意程式**的三個入口未受 typed-argv 保護：
  `PSC_MANAGER_INSTALLER`（`scripts/service-manager.sh:105-107`）、
  `PSC_REPLY_BRIDGE`（`scripts/psc-relay-hook.sh:19-22`）、
  `PSC_DIGEST_DELIVERY_CMD`（`digest.py:191`）。相對地
  `PSC_GATE_CMD_*`（`gate_ledger.py:68-80`）與 `PSC_PREFLIGHT_CMD`
  （`preflight.py:26`）**有**拒絕 shell wrapper 的守衛——證明這個守衛模式在
  repo 內已成立，只是覆蓋不全。

攻擊者可改寫 verifier 原始碼、注入 `sitecustomize.py`／`.pth`、降版相依套件、
改 unit 或 env、換 launcher，然後重啟服務，直接移除驗簽或放寬 ship gate。

### 6. persona 邊界是宣告式，且 anti-collusion 控制本身可寫

`persona/personas.yaml`：manager／planner／reviewer 各有窄 `write_paths`，
**builder 為 `["**"]`**（`personas.yaml:25`），唯一的實際限制是
`persona/guardrail.py:32-42` 拒絕絕對路徑與 `..`，即「worktree 內全可寫」。
注意 `**` 也涵蓋 `.github/**` 與 `.cortex/work-items.yaml`——後者是 git-tracked 的
**correlation authority**（`monitor/correlation.py:79-80`），於是「builder 寫 repo」
本身就是一次 correlation authority 變更。

`effective_tools` 由 `persona/context.py:37-39` 計算為
`allowed_tools ∪ overlay.tool_allowlist_additions`——**overlay 只能加、不能減，
且無上限、無合法新增白名單**，本身即是工具維度的無界提權面。

`independence_domain`（唯一的 anti-collusion 控制，`review.py:125-131`：builder 與
reviewer 同域即 `state: "absent", reason: "same-independence-domain"`）**不是
persona 欄位，而是 model identity 的欄位**（`model_identities.py:221`），而 host
overlay `<project_config_root>/model-identities.yaml` 可以壓過 packaged registry
（`model_identities.py:604-631`）。同理 `<agents_root>/config/combos/` 的
instance-local combo 壓過 packaged（`deck/schema.py:80-87`）。也就是說：
**anti-collusion 與 workflow 定義本身，都是同 UID 可寫的 YAML。**

### 7. 多 instance 隔離本來就不完整

`scripts/service-manager.sh:137-145` 與 `installer.py:328-330` 明文記載：
`PSC_MANAGER_SPECS_DIR`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT` **未被
instance-scope**，因此共用同一個 HOME 的兩個 instance 會共用 `~/.agents/specs`
與 `~/.agents/coordinator`——即共用 `jobs.json`、evidence 樹與 delivery journal。
實測部署的 bootstrap env 甚至沒有 `PSC_CONTROL_ROOT`（較早版本安裝後未重跑
install 的殘留），可見這條路徑契約在實機上已經漂移。

### 8. 既有裁決的邊界

計畫第六輪已明文：**互動 broker 只解決 operator 身分，不能替代 durable-state
隔離**——攻擊者根本不需要偷 broker capability。Unix peer credential 在同 UID 下
證明不了任何事；`0600` token 檔對 owner 等同公開。

### 9. repo 內已有的正面前例（Phase 2 可直接沿用的設計語彙）

- **gate ledger 放在模型拿不到的地方**：`terminal_contract.py:488-494` 刻意把
  ledger 放進 manager 自己的 `log_dir`，理由明載「模型的 cwd 是 worktree，
  它從未收到這個路徑」——這正是 D6 要系統化的思路。
- **monitor socket**：`monitor/server.py:166-168` bind 後 `chmod 0o600`，
  run dir `chmod 0o700`（`monitor/service.py:104`）。
- **typed-argv 守衛**：`gate_ledger.py:68-80` 拒絕 `bash -c` 型 wrapper。
- **repo identity 綁定**：`installer.py:213-236,262-273` 以
  `PSC_REPO_IDENTITY` 戳記，不符即 fail-closed 要求 `--rebind`。

## Goals

- 產出**完整**的 trust-root 資產盤點與 writer／reader／consumer inventory，涵蓋
  **所有**不受信任 headless persona（builder／reviewer／planner），不是只針對 builder。
- 把 enforcement plane（executable／deps／unit／launcher／env／信任設定）納入同一個
  保護邊界，並綁定可驗證的部署身分。
- 對兩條實作路線（OS/MAC 邊界 vs 簽章＋強制驗簽）做完整比較並**給出裁決建議**；
  簽章路線即使不作為主線，規格也必須訂完整（供 Phase 3 與 Elevated tier 使用）。
- 定案 operator 授權通道的性質（action-bound／single-use／短效／不可重放），
  以及 reviewer 身分由 Manager registry 推導的規則。
- 定案 E2E 測試矩陣，使「通過 D6」成為可機械驗收的事實而非宣稱。
- 定案與現行部署（pipx、`systemd --user`、單一 OS 使用者帳號、WSL2）的遷移路徑與
  分階段交付，明確標示哪些可在不需 root 的前提下先行。
- 明確標示 `#484`／`#480`／`#489` 各自被哪個機制**取代**、哪個機制**補強**。

## 資產盤點與 writer／reader／consumer inventory

### A. 資產分級

- **Tier-0（authority-bearing）**：竄改後可直接偽造 acceptance 或取得 ship
  authority。`jobs.json`、`.psc-review-verdict.json`、maintainer attestation、
  verification evidence、completion record、full-suite evidence、
  workflow inputs／workflow evidence、gate ledger、delivery journal、
  control request／done／status／lock、model-identity overlay
  （`independence_domain` 來源）、combo／card override、dispatch specs、
  handoff manifest、runtime bootstrap env、unit／launcher／executable／
  site-packages／codex hooks。
- **Tier-1（integrity-bearing）**：竄改後導致錯誤決策但不直接授權發佈。
  digest outbox、engineering-outcome outbox、workflow report journal、
  provider backoff、monitor 讀模型與同步游標、`.cortex/work-items.yaml`、
  skill ledger／park state／proposal。
- **Tier-2（advisory）**：frontmatter `status`、文字 marker、persona 宣告式
  `write_paths`／`effective_tools`。計畫已裁定 frontmatter status 降為顯示性欄位。

### B. writer／reader／consumer inventory

| 資產 | writer（現況） | reader／consumer（現況） | 目標 writer | 目標 reader |
|---|---|---|---|---|
| `jobs.json` | `JobRegistry`（`registry.py:825-2571` 全部 mutation 方法）；`work_actions.py:4184,5097`、`manager_daemon.py:1199`、`porcelain/request.py:11` 各自建 registry | `monitor/providers.py:264`（繞過 `JobRegistry` 直讀）、`coordinator/cli.py:524`、`scripts/coordinator_telegram_notifier.py:83-111`（第二套較弱的 parser） | 僅 Manager UID | Manager；其餘 consumer 唯讀 |
| review verdict | **reviewer 模型**（worktree 內），同 UID 任何行程亦可 | `review.py:540-627` 驗證後入 gate | Manager（由 reviewer 經受控通道回傳） | Manager |
| verification／completion／full-suite／workflow evidence | Manager | Manager gate 鏈、engineering outcome | 僅 Manager UID | Manager 唯讀 |
| maintainer attestation | `work_actions.py:1027-1060` | `_ship_with_maintainer_review()`、`gate_refs` | 僅 Manager UID | Manager 唯讀 |
| gate ledger | launcher wrapper（`launcher.py:153-205`）寫入 manager log_dir | `terminal_contract.py:448-470` 驗 claim | 僅 Manager UID | Manager 唯讀 |
| delivery journal | `work_actions.py:550-567` | `work_bridge.py`、`manager.py` 六處 | 僅 Manager UID | Manager 唯讀 |
| control request queue | **任何同 UID 行程**（含全部 headless persona）＋ operator CLI | `manager_daemon.py:1290-1345` drain loop | operator／authenticated channel | Manager |
| control done／status／lock | Manager daemon（`manager_daemon.py:1400-1428`）；lock `0o644` | `control/client.py:57-126`、`porcelain/request.py`、`service-manager.sh:61-73` | 僅 Manager UID | operator 唯讀 |
| digest／engineering-outcome outbox | `digest.py:181-188`、`engineering_outcome.py:451-468` | 外送管線／外部學習系統 | 僅 Manager UID | 外部唯讀 |
| monitor 讀模型／游標 | Monitor（`work_snapshot.py:299-326`、`github_issue_sync.py:493-520`，皆 0600） | Manager（D1 鏡像讀）、`doctor` | 僅 Monitor UID | Manager 唯讀 |
| `.cortex/work-items.yaml` | `correlation.py:463-487`；**且 builder 因 `write_paths:["**"]` 可寫** | monitor correlation | Manager／operator | Monitor 唯讀 |
| model-identity overlay | operator（現況任何同 UID 行程） | `review.py:125-131` 的 independence 判定 | 僅 Manager UID／root | Manager 唯讀 |
| combo／card override、dispatch specs | operator／`deck/compile.py:740-800` | `manager_daemon.py:130-134`、派工鏈 | 僅 Manager UID | Manager 唯讀 |
| handoff manifest | `persona/handoff.py:17-20`（裸 `write_text`，無原子性、無 mode） | `persona/gate.py:50-60` fail-closed 驗證 | 對應 headless UID（staging）→ Manager 收 | Manager |
| skill ledger／park／proposal | `skill_ledger.py:141-183`、`skill_janitor.py:312-387`（皆無 chmod） | Manager、operator | 僅 Manager UID | operator 唯讀 |
| runtime bootstrap env | `installer.py:162`（裸寫、無 mode） | 每個 Manager／Monitor 行程啟動 | root 或 Manager UID | 全部行程唯讀 |
| unit／launcher／executable／site-packages／codex hooks | `installer.py:298`、`hooks.py:53-60`、pipx | systemd、Python import、codex | root（部署身分） | 全部行程唯讀 |
| worktree（派工工作區） | 對應 headless job | **無跨帳號 reader**（成果走 bundle spool，#637／#641） | 對應 headless UID | owner-only，Manager **不讀** |

**三個 headless persona 必須分別處理**，不能只封 builder：

- **builder**：`write_paths: ["**"]`（`personas.yaml:25`）。可寫 repo 內任何路徑，
  包含 `.cortex/work-items.yaml`（correlation authority）與 `.github/**`。同 UID 下
  更可直接寫 reviewer 的 verdict 檔（見背景 §3）。
- **reviewer**：其輸出即 gate 的直接輸入。`#484` 已證實 legacy slice lane 的
  reviewer 實際以 `--sandbox workspace-write` 在候選 worktree 內執行——reviewer
  可改候選檔案。reviewer 若與 builder 同 UID，`independence_domain` 在執行期
  完全不成立。**reviewer 必須與 builder 分屬互不可寫的域**，這是 D6 對
  independence 的最低要求。
- **planner**：其輸出是 staged planning artifact 與 spec／plan，直接餵給 Compact
  reuse。planner 若可寫已發佈的 artifact、其 hash 記錄、或 combo／spec override，
  「acceptance ＝外部 attestation」的整條裁決失效。

### C. mutation ingress 盤點

1. **control file queue**（`<control_root>/requests/`）——完全未認證，且處理順序
   由 mtime 決定（`manager_daemon.py:1431-1436`）。
2. **CLI 直接指定 state path**（`work_actions.py:4184,5097` 接受 `--state`）。
3. **直接檔案寫入**（任何同 UID 行程）。
4. **環境變數／bootstrap env 重導**（`PSC_*`，`runtime.py:98-134`）。
5. **env 指名執行任意程式**：`PSC_MANAGER_INSTALLER`、`PSC_REPLY_BRIDGE`、
   `PSC_DIGEST_DELIVERY_CMD`（三者無 typed-argv 守衛）。
6. **設定 overlay 覆蓋**：model-identity overlay、combo override、
   persona overlay `tool_allowlist_additions`（只加不減、無上限）。
7. **部署面寫入**（unit／venv／launcher／`sitecustomize`／`.pth`／`PATH`／
   `~/.codex/hooks.json`）。
8. **行程間路徑**（inherited FD、`ptrace`、`/proc/<pid>/mem`、signal）。

盤點清單本身必須固化為程式碼中的單一真相（見 R1），否則下一次新增 durable state
時會靜默漏保護——本票撰寫期間 D1／D3 即各新增一份 monitor state。

## 路線比較與裁決建議

### 路線 A：OS／MAC 邊界（headless job 降權至獨立 UID／user namespace）

**做法**：Manager 與所有 consumer 跑在受信任身分；builder／reviewer／planner 各自
降權到獨立、互不可寫的 UID；Tier-0／Tier-1 資產目錄 owner 為受信任身分、
mode `0700`（需 headless 讀取者以 group 唯讀 `0750` 開放）；headless 只對自己的
worktree 與 staging 區有寫入權。同時補上 systemd 加固指令
（`ProtectHome`／`ProtectSystem=strict`／`ReadWritePaths`／`NoNewPrivileges`／
`PrivateTmp`），把「Manager 自己能寫什麼」也收斂。

**覆蓋面**：
- 一次涵蓋**全部**資產與**全部** ingress，不需逐一改寫 consumer。
- 涵蓋 enforcement plane：executable／deps／unit／launcher／env 全部 root 或
  Manager UID 擁有。
- **預設封閉**——未來新增的 durable state 只要落在受保護目錄就自動受保護。
- 涵蓋 `ptrace`／`/proc/<pid>/mem`／signal：跨 UID 由 kernel 直接拒絕。
- 涵蓋背景 §3 的最短攻擊路徑（builder 代寫 verdict）與 §6 的 overlay 提權面。
- 不需要金鑰管理、canonical encoding、rotation、revocation 這一整套密碼學工程。

**代價**：
- 需要一次性 root 設定（建 UID、目錄 owner、sudoers/polkit、lingering）。
  實測執行帳號在 `sudo` group 內，WSL2 可行。
- `systemd --user` 模型要改：Manager 不能再是 operator 的 user service，須改
  system-level unit（`User=…`）或 manager UID 的 lingering user service。
- 檔案交換面要重新設計：headless 寫 worktree、Manager 要讀。走 setgid 目錄＋
  group 唯讀，或讓 Manager 一律以 candidate SHA 走 git 讀——**D2 已把 contents／
  ancestry git 化，交換面剛好可縮到 git object**，這是 A 在本專案特別便宜的原因。
- `~/.agents` 路徑契約要拆成「Manager-owned」與「job-visible」兩棵樹；順帶必須
  修掉背景 §7 的 instance-scope 漏洞（否則兩個 instance 仍共用 `jobs.json`）。
- gh token 須由 Manager 持有，headless 不得讀 → GitHub 寫入改經 Manager 代理
  （**D1 的 outbox 寫入記帳已朝此方向**）。

### 路線 B：簽章＋所有 consumer 強制驗簽

**做法**：Manager 持 Ed25519 私鑰，headless 不可讀；每筆 Tier-0／Tier-1 record 附
detached signature；所有 consumer 驗簽 fail-closed。

**結構性缺陷（本票認定為決定性）**：
1. **B 的前提是 A**。「headless 讀不到金鑰」在同 UID 下不成立——`0600` 對 owner
   等於公開。要讓金鑰真的讀不到，只有 (i) OS UID 邊界（＝路線 A）、或
   (ii) 硬體／TPM sealing＋外部簽章 daemon；而該 daemon 若以 Unix socket 提供，
   同 UID 的 peer credential 無法區分呼叫者，仍需 A。
2. **B 不覆蓋 enforcement plane**。攻擊者不必偽造簽名——改寫 verifier 把驗簽拿掉
   即可（site-packages 實測 `drwxrwxr-x`）。要保護 verifier，仍需 A。
3. **簽章不擋刪除與回滾**。可刪掉不利的 attestation，或用一份舊的、簽名仍有效的
   record 覆蓋新的。需要單調計數器／transparency log，而該 log 本身又需要保護
   ——第三次回到 A。
4. **對 queue 直寫沒有增益**。headless 仍可往 queue 丟未簽 request；fail-closed
   拒絕的結果，功能上等同於 A 的「不可寫」，卻多付一整套密碼學維護成本。
5. **預設開放**：漏改任何一個 consumer 就破功。本 repo 光是
   `delivery-journal.json` 就有六處重複推導、`jobs.json` 四處、
   `work-items.snapshot.json` 兩處且 fallback 分歧——這種重複度下要保證「所有
   consumer 都驗簽」，維護面遠大於 A。

**B 不可取代但有價值之處**：跨主機／跨信任域的**可攜證據**。OS 邊界只在單機
成立；evidence 要送到別台機器、或未來出現多 operator／多 fleet 節點時，簽章是
唯一能讓對方獨立驗證的形式。這與 v4 計畫既有裁決一致——「Compact/Standard 以
Manager immutable evidence＋content hash 為底線，**signature 留 Elevated**」。

### 裁決建議

**以路線 A 為 0.2.0 join gate 的必要且充分基礎（primary）；路線 B 降為
defense-in-depth 與跨信任域可攜性，排入 Phase 3，不列為 0.2.0 blocker。**

理由（依權重）：
1. B 的三個前提（金鑰保密、verifier 完整性、單調狀態）全都必須由 A 提供，因此
   「只做 B」在本環境是**不成立的方案**，不是成本較高的方案。
2. A 是預設封閉、B 是預設開放；本 repo 每週新增 durable state，且路徑推導高度
   重複，A 的長期維護面顯著較小。
3. A 對 enforcement-plane-tampering 這一族測試（計畫第七輪新增、最難的一族）
   是**唯一**能通過的機制。
4. A 的一次性成本集中在部署，與 D1／D2 已完成的「Manager 讀鏡像／git 化」方向
   相容，不需回頭改 consumer 邏輯。
5. repo 內已有 A 型設計的成功前例（gate ledger 放在模型拿不到的路徑、
   monitor socket `0600`／run dir `0700`、typed-argv 守衛），D6 是把這個既有語彙
   系統化，而非引進陌生機制。

即使如此，**B 的規格仍在本票完整訂定**（R6），理由是 Phase 3 要落地、Elevated
tier 要用，且 B 的細節（canonical encoding、domain separation、anti-replay）在 A
之下同樣適用於 evidence 的內容綁定，不是白寫。

## Requirements

### R1 trust-root 資產清單 MUST 固化為單一機器可讀真相，且新增 durable state MUST 強制登記

系統 SHALL 新增一份 trust-root 資產登記表（單一模組常數，非散落字串），對每項
資產宣告：`asset_id`、`tier`、`path_resolver`、`writers`、`readers`、`ingress_kind`。
`config/paths.py`、`control/constants.py` 與各模組中每一個回傳 durable path 的
函式 SHALL 在登記表中有對應項目；CI SHALL 以機械測試釘住此雙向等式——新增一個
path 函式而未登記即 FAIL。

登記表 SHALL 同時作為**去重的單一真相**：現況 `delivery-journal.json` 有六處獨立
路徑推導（`work_actions.py:516-517`、`work_bridge.py:771,838,1382,1815`、
`manager.py:7289,8290`）、`jobs.json` 四處、`work-items.snapshot.json` 兩處且
fallback 分歧（`config/paths.py:46-47` vs `coordinator/claim.py:225-228`）。
重複推導 SHALL 收斂到登記表，否則權限產生器與驗簽覆蓋都會有盲點。

盤點 MUST 涵蓋 builder／reviewer／planner **三個** persona 的 writer 身分。

若不做：本票的盤點會在數週內過期；本票撰寫期間 D1／D3 各新增一份 monitor durable
state，證明清單漂移速度快於人工複核週期。

#### Scenario: 新增 durable path 未登記

- **WHEN** 有人新增一個回傳 durable 路徑的函式但未加入登記表
- **THEN** 登記表等式測試 MUST FAIL，錯誤訊息 MUST 指名該函式

#### Scenario: 重複路徑推導

- **WHEN** 同一個 durable 資產在兩處以字面量重複推導路徑
- **THEN** 登記表一致性測試 MUST FAIL

#### repo 源碼樹的放置（#623 裁決；登記表資產 `repo-source-tree`）

Phase 2b 的登記表原本定義了 durable state 樹與部署樹，**沒有定義 repo 源碼樹該放
哪**——實機因此撞到「`ProtectHome=yes` 讓 `/home` 完全不可見 ⇒ Manager 看不到自己的
repo」。裁決前先排除了一條看似最省事的路：**`git worktree` 在三分下不成立**。實測
（#623）：worktree 的 `.git` 是指向**共用 object store** 的指標，builder 只要 `git add`
就必須能寫該 store，而 store 在 Manager-owned 樹內——「builder 能 commit」與「三分
隔離」互斥，不是權限沒調好。

因此裁決為 **per-job 完整 clone**（實測 0.5 秒／35MB per job），登記表新增：

- `repo-source-tree`：`<agents_root>/repos/<slug>`，**working checkout**（不是 bare——
  monitor 掃的是工作樹裡的檔案）。同時是 monitor 的掃描目標與每個 job 的 clone 來源。
  **writer 是 Manager**（0817 裁決）：`owner_class=MANAGER_STATE`、owner＝
  `durable_state_owner`、mode 0700，兩個 job 帳號各獲一條**唯讀** ACL（`rX`）。
- `builder-gitconfig`／`reviewer-planner-gitconfig`／`manager-gitconfig`：對應帳號 HOME
  下的 **root-owned** `.gitconfig`（0644），內容含來源 repo 的 `safe.directory`。跨擁有者
  的 git 操作會被 dubious-ownership 保護擋下，而那些 HOME 都是 root-owned、帳號自己放不了
  這個檔——與既有的 `codex-hooks`（root-owned、在帳號 HOME 下）同一個模式，不是新概念。
  內容 SHALL 由權限產生器產生（比照 shim／polkit），MUST NOT 手寫；每個來源 repo SHALL
  產生**兩條** `safe.directory`（工作樹根 ＋ `<root>/.git`）——實測從**非 bare** 來源
  clone 時 git 檢查的是後者，而 `git -C <repo> …` 報的是前者。
- `commit-spool`：`<coordinator_root>/commit-spool/<job-id>/`，builder 成果回收的
  **bundle spool**。形態 SHALL 逐條比照 `review-verdict-spool`：容器 owner＝
  `durable_state_owner`、mode 0700，producer 僅獲 **`wx` 無 `r`** 的 per-account ACL，
  per-job 目錄由 Manager 在 dispatch 當下建立、落地後轉唯讀。

##### 兩個 spool 的 per-job 生命週期（#638）

`review-verdict-spool` 與 `commit-spool` 的那一格 SHALL 走**同一套**實作
（`coordinator/spool_slot.py`）——形態相同而各自實作，缺陷就會有兩個實例，#638 的
三個缺陷正是這樣出現的。三條規則：

- **per-job 目錄 MUST NOT 以明確 mode 建立。** 在帶 default ACL 的容器下，明確
  mode 會連 **ACL mask** 一起重設，把繼承來的具名條目壓成 `#effective:---`，
  producer 因此連建檔都不行。初始權限交給 default ACL，事後只**檢查**並收窄
  `other` 位；`group` 位在有 ACL 時**就是 mask**，MUST NOT 被收窄。
- **producer SHALL 在寫完後自行把成果放寬到 consumer 讀得到**（0644）。`wx` 無
  `r` 的那一格上檔案由 producer 擁有，容器 owner 的身分不給檔案內容的讀取權。
  放寬不擴張暴露面——容器是 `0700 <durable_state_owner>`，別的帳號連 traverse
  都進不來。
- **seal SHALL 封目錄，MUST NOT 只 `chmod` 成果檔。** 只有檔案 owner 或 root 能
  `chmod`，而成果是 producer 擁有的；封目錄則是 consumer 對自己**擁有**的那一項
  操作，且 `chmod` 同時把 mask 收成 `---`，producer 具名條目的 traverse 一併失效。

##### job 工作樹對 Manager 完全不可讀（#641，0817 裁決）

交換面改成 bundle spool 之後，登記表裡「Manager 讀 job 樹」那一族授權**全部沒有
消費者**。實機複驗（operator，0817）證實它讓 #637 建立的不變式在部署上不成立：

```
只有 0700、無 ACL          → Manager `ls` 得到 Permission denied（#637 的不變式成立）
套上登記表要求的那條 ACL    → Manager 讀得到（同一條不變式在實機上不成立）
```

因此 job 工作樹底下的每一項資產 SHALL 是 **owner-only、零跨帳號 ACL**。三項一起
收，不得只收其中一條——traverse ACL 是由跨帳號 ACL **機械導出**的（#620），留任何
一條，`<job 樹>` 乃至其子目錄的 `--x` 就會自己長回來：

| 資產 | 落點 | 原授權 | 真正的消費者 |
|---|---|---|---|
| `repo-worktree` | `<job 樹>` | `u:<manager>:rX` | 成果改走 `commit-spool` 的 bundle（#637） |
| `review-verdict` | `<job 樹>/.psc-review-verdict.json` | `u:<manager>:r` | 權威通道是 `review-verdict-spool`（#599／#639）；legacy fallback 只對 Phase 2a 之前派工的 job 生效，而那批 job 不可能出現在套了 Phase 2b ACL 的機器上 |
| `work-items-yaml` | `<job 樹>/.cortex/work-items.yaml` | `u:<manager>:r` | monitor 讀的是**來源樹**那一份（`repo-source-tree`）；builder 的修改是 git-tracked，循 bundle 以 commit 旅行 |

**這不只是最小權限，也是一條提權路徑的成立條件。** `coordinator/verification.py`
以 `cwd=<job 樹>` **執行**宣告出來的 check／test／full-suite；`pytest` 會載入該樹的
`conftest.py`／plugin，等於 builder 在 Manager 身分下取得任意程式碼執行——與 #629
在 gate 執行面描述的是同一條路徑。收掉這條讀取權之後，那些命令在三分下連 spawn
都不會成功。

**verification 那組讀工作樹的檢查** SHALL 搬到 #629 的第三執行身分（既非 builder
也非 Manager，在受控 checkout 判讀）。在那之前它 SHALL **明確 fail-closed**，理由碼
`candidate-worktree-unreadable-pending-gate-identity`、evidence 帶 `blocked_on:
"#629"` 與可操作處置。三條 MUST NOT：**不得**靜默略過；**不得**改以 bundle 內容替代
（bundle 同樣由 builder 產生，兩邊同源會讓檢查退化成「builder 說什麼就是什麼」）；
**不得**採信 builder 自報的工作樹狀態（違反 §R5 acceptance chain 與 #628 的證據
作者歸屬）。

##### `repo-source-tree` 的 owner：從 root 改為 Manager（0817 裁決）

本節初版（PR #636 第一版）把 writer 定為部署身分，理由是「Manager 被攻陷也改不了每個
job clone 的來源」。實機複驗後**推翻**：

```
$ sudo -u cortex-manager git -C /var/lib/cortex/repos/<slug> fetch <bundle> …
error: cannot open '.git/FETCH_HEAD': Permission denied
```

`git fetch` MUST 把 `FETCH_HEAD` 寫進**目標 repo**，而成果回收正是「fetch 進來源樹」；
provisioning 那半邊的 `git branch -f <branch> <base>`（`coordinator/seams.py`）同樣是對
來源樹的寫入。**「Manager 唯讀」與「Manager 回收成果」互斥**，裁決取後者。

隔離不因此變弱：威脅模型裡不受信任的是 **job 帳號**，它們對來源樹只有唯讀 ACL；而
Manager 本來就擁有 gate ledger、evidence 樹與 `jobs.json`——Manager 被攻陷的話那些全都
完了，多這一棵樹不改變攻擊面。root-owned 買到的是一條**供應鏈**保護（Manager 被攻陷後
仍污染不了下一輪 job 的原始碼），但它讓回收整個不成立，因此取回收。

monitor 仍**不得**寫這棵樹：monitor 與 Manager 同帳號，檔案層權限相同，差別由 monitor
unit 的 persona 過濾（§R3／#622）產生——`ReadWritePaths` 只由 monitor persona 在登記表上
的 writer／spool-consumer 面導出，來源樹因此機械地不會出現在那份 unit 上。

##### 成果回收走 bundle spool，而不是 Manager 伸手進 job 的 clone（0817 裁決）

#634 現行的回收是 `git -C <來源樹> fetch <builder 的 clone> …`。那條路要求 Manager
(a) traverse 進 builder-owned 的 `0700` 樹——實測 `Permission denied`；(b) 為**每個 job
路徑**加 `safe.directory`——而 git 2.43 實測不吃路徑 glob，等於把 Manager 的 Tier-0
gitconfig 變成執行期可變狀態。

改走 bundle：builder 在**自己的** clone `git bundle create <spool>/<job-id>/<name>.bundle`，
Manager 從那個 **bundle 檔**（不是 repo）fetch。Manager 全程不碰 builder 的樹，讀的又是
一個普通檔案，dubious-ownership 與 traverse 兩個問題同時消失。

#### executor 執行面：toolchain 與 per-account 憑證（#640 裁決；登記表資產 `executor-toolchain`／`builder-executor-credential`）

`ProtectHome=yes` 讓 job 帳號看不到 `/home`，而四個模型 executor（`codex`／`claude`／
`copilot`／`agy`）原本**全部**落在 operator 的 HOME 底下（nvm 樹與 `~/.local/bin`）。
實測：

```
$ sudo -u cortex-builder env HOME=<job HOME> codex exec --help
/usr/bin/env: ‘node’: No such file or directory        rc=127
```

登記表對 toolchain 與 job 帳號憑證**都沒有預留資產**，因此 permgen 也不會產生它們的
權限——這是「provisioning → clone → bundle → harvest 全通、但 dispatch 走到呼叫模型
那一步才失敗」的最後一哩。

##### (a) toolchain：node 走系統層、模型 CLI 進部署樹

- `node` SHALL 由**系統層**提供（apt）。它是通用 runtime，換版本幾乎不影響產出；
  但版本本身仍是**部署決定**，某個 CLI 提高下限時 SHALL 一併升，否則它會變成下一個
  無聲漂移點。
- 四個模型 CLI SHALL 落進 `<deploy_root>/toolchain`（登記表資產 `executor-toolchain`）：
  `owner_class=DEPLOYMENT`、owner＝root、mode `0755`——**全部 job／服務帳號唯讀＋
  可執行**，一個 `w` 都沒有。`ProtectSystem=strict` 下 `/opt` 唯讀只擋寫入，讀取與
  執行不受影響，因此本資產 MUST NOT 出現在任何 unit 的 `ReadWritePaths` 上。
- 安裝來源 SHALL 是 **operator 實際在用的那一份**（`command -v` 解得到的），
  MUST NOT 另外 `npm install -g` 裝一份系統的。理由不是假設：實機盤點在同一台機器上
  就有兩份 `codex`——系統層 0.42.0、operator 實際在用的 0.147.0（差 100 個以上小版本）。
  「job 跑的是哪個版本的模型 CLI」**會**影響產出，因此它必須是一個可稽核的部署決定，
  而不是跟著 operator 的環境漂移。
- 四者的實體形態不同，搬移方式不能一概而論（表固化於
  `permgen.EXECUTOR_TOOLS`）：`codex` 是 `#!/usr/bin/env node` 的 node script（必須整包
  搬 npm 套件樹）；`claude`／`agy` 自帶原生執行檔；`copilot` 是 shell script，而它**內部
  再 exec node**（#640 落表時尚未查證，#643 在真實加固面下量到與 `codex` 逐字相同的
  症狀後回填）。**因此系統層 node 的版本風險涵蓋 `codex` 與 `copilot` 兩個。**
  同一個 `needs_node` 欄位也是 §R3 per-executor 加固剖面的分類來源——**一張表，兩個
  用途**，不另立第二份清單。
- job 的 `PATH` SHALL 由 Manager 端既有的 `PSC_BUILDER_PATH` 注入，且 toolchain
  SHALL 排在**最前面**——系統層可能另有一份同名但舊很多的 CLI，排在後面的症狀是
  「跑得起來但版本不是預期的那個」，比 `command not found` 難查得多。**MUST NOT**
  改以模板 unit 的 `Environment=PATH=` 提供：模板 unit 的 `ExecStart` 是 root-owned
  shim，shim 以 `execvpe(argv[0], argv, spec['env'])` 整份換掉環境，job 解析命令用的
  `PATH` 來自 **spec 的 env**——寫在 unit 上只會是一個看起來承載作用、實際被丟掉的設定。

##### (b) 憑證：檔案 job-owned、目錄 root-owned

憑證**檔**（預設 `<job HOME>/.codex/auth.json`，＝本部署 `PSC_MANAGER_EXECUTOR` 的那
一個）SHALL 由該 job 帳號擁有、mode `0600`；**放它的目錄** SHALL 維持 root-owned
`0755`（既有的 `<job HOME>/.codex/` 就是這個形態，`codex-hooks` 住在同一層）。
淨效果：

- job **能**就地改寫自己那份憑證的內容（過期 token 可自行 refresh）；
- job **建不了新檔、刪不掉、也換不掉**同目錄下的其他 root-owned 檔——建立／unlink／
  rename 需要的是**目錄**的寫入權，而目錄對它只有 `r-x`。

`ReadWritePaths` SHALL 只掛**憑證檔本身**而非其父目錄（`permgen`
的 `IN_PLACE_CONTENT_WRITE_ASSETS`；一般規則把檔案折算成父目錄，這一族是明示例外），
使「目錄 root-owned」在**檔案系統**與 **systemd mount** 兩層同時成立。

**已知限制（裁決刻意接受）**：以「暫存檔 ＋ rename 原子替換」形式 refresh 的 CLI 會
失敗，因為那需要在同目錄建檔；只有就地 `O_TRUNC` 覆寫的 refresh 走得通。

##### 這買到的是檔案系統層的隔離，**不是** provider 層的獨立

把 operator 的憑證複製給 job 帳號，代表 job 用的是**同一個 provider 帳號**。三分買到
的是**檔案系統層**的隔離——job 偷不到 Manager 的 token、改不了 Manager 的 state、也讀
不到另一個 job 帳號的憑證（HOME 互不可讀）。它**不是** provider 層的獨立：同一個
provider 帳號下的兩個 job 在 provider 眼中是同一個主體，配額、速率限制、稽核紀錄與
帳號層的封鎖全部共用。

這與 `independence_domain`（§R5／§R8）想表達的**不是同一件事**。`independence_domain`
是 anti-collusion 控制，講的是「審的人與被審的人不得是同一個模型身分」；它由 Manager
在派工時決定並寫入 registry，與 OS 帳號、與憑證檔在哪一個 HOME 全無關係。本節的檔案
系統隔離既不蘊含它、也不被它蘊含——**兩者 MUST NOT 互相當作證據**，spec 在此明載是為
了避免把「三分 ＋ 各自的憑證檔」讀成比實際更強的保證。

真正的 provider 層獨立需要**每個 job 帳號各自的 provider 帳號**，成本最高（帳號、
計費、配額各自管理），屬**未來選項**，不在本票範圍。

### R2 所有 Tier-0／Tier-1 durable state 與 mutation ingress MUST 位於不受信任 headless persona 不可寫的 OS 邊界內

系統 SHALL 使 builder／reviewer／planner 對登記表中 tier 0 與 1 的全部路徑
**無寫入權**（`EACCES`／`EPERM`），且該不可寫性 SHALL 由 OS 強制，MUST NOT 依賴
檔案 mode `0400`／`0444`／immutable 屬性作為唯一手段——這些對 owner 無效。

具體 MUST：

- **reviewer 與 builder SHALL 分屬互不可寫的域**；任一 persona MUST NOT 能寫入
  另一 persona 的 worktree 或其 verdict／輸出路徑。
- **review verdict MUST NOT 停留在同 UID 可寫的 worktree 內作為權威來源**
  （背景 §3）。verdict SHALL 由 reviewer 經受控通道交付、由 Manager 落地為
  Tier-0 evidence；worktree 內的檔案至多是 staging 副本。
- planner 對已發佈的 planning artifact、combo／card override、model-identity
  overlay、dispatch specs SHALL 無寫入權。
- 現有 group-writable 現況（`$HOME/.agents/{control,coordinator,core,specs}` 實測
  `drwxrwxr-x`、pipx venv 實測 `drwxrwxr-x`、bootstrap env 實測 `-rw-rw-r--`）
  SHALL 一併收斂，group 寫入權 MUST 移除。
- 背景 §7 的 instance-scope 漏洞（`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT`／
  `PSC_MANAGER_SPECS_DIR` 未 instance-scope）SHALL 在同一階段修正，否則兩個
  instance 共用 `jobs.json` 會使邊界失效。

若不做：現況即 `chmod 0400` 一線，owner 可 `chmod`／`unlink`／`os.replace` 繞過，
等於零保護；且 builder 可直接代寫 reviewer verdict 取得 foreign review。

#### Scenario: builder 直寫 jobs.json

- **WHEN** 以 builder 身分執行 `printf '{}' > <coordinator_root>/jobs.json`
- **THEN** 寫入 MUST 以 `EACCES` 失敗，且 `jobs.json` 內容 MUST 位元不變

#### Scenario: builder 代寫 review verdict

- **WHEN** builder job 對 reviewer 的 `.psc-review-verdict.json` 路徑寫入一份
  結構合法、`verdict: pass` 的內容
- **THEN** 寫入 MUST 被拒；且即使以特權植入，Manager MUST NOT 採計該檔——
  權威 verdict 僅來自 Manager 落地的 Tier-0 evidence

#### Scenario: builder unlink 後重建 evidence

- **WHEN** builder 對 `evidence/**` 下任一檔執行 `unlink` 後重建
- **THEN** `unlink` MUST 以 `EACCES` 失敗（目錄無寫入權）

### R3 enforcement plane MUST 對所有不受信任 headless persona 不可寫，且 MUST 綁定可驗證的部署身分

下列全部 SHALL 對 builder／reviewer／planner 不可寫：

1. **executable**：`scripts/service-manager.sh`、`cortex` console script、
   `__PY__` 直譯器與 venv `bin/`；
2. **dependencies**：pipx venv 的 `site-packages`，含任何 `sitecustomize.py`／
   `.pth` 注入點；
3. **service unit**：`$HOME/.config/systemd/user/*.service`／`*.timer`，或遷移後的
   system-level unit；
4. **launcher**：`installer.py:37-51` 產生的 `ExecStart` 目標與其 `WorkingDirectory`；
5. **環境檔**：`$HOME/.agents/core/runtime/<instance>.env` 與
   `<instance>-manager.env`；
6. **信任設定**：R1 登記表、model-identity overlay、combo／card override、
   persona catalog 與 overlay、`~/.codex/hooks.json`、未來的 trust store。

此外：

- systemd unit SHALL 加上加固指令（至少 `NoNewPrivileges=yes`、
  `ProtectSystem=strict`、`ProtectHome` 與明列的 `ReadWritePaths`、`PrivateTmp=yes`）。
  現況三個 unit **一項都沒有**。加固表固化於 `permgen._HARDENING`（27 項，逐項附
  「為何」註解）；job 模板 unit 的加固面另受 **per-executor 剖面**約束，見下節。
- `EnvironmentFile=-` 的缺檔靜默容忍 SHALL 改為 fail-closed（缺檔即拒絕啟動），
  否則刪檔即是一條無聲的重導路徑。
- `PSC_MANAGER_INSTALLER`、`PSC_REPLY_BRIDGE`、`PSC_DIGEST_DELIVERY_CMD` 三個
  「env 指名並執行任意程式」的入口 SHALL 比照 `gate_ledger.py:68-80` 加上
  typed-argv 與 shell-wrapper 拒絕守衛，或直接移除。
- **Manager 啟動自檢**：SHALL 驗證 (a) `sys.executable` 與 `site-packages` 根的
  owner／mode 對 headless 不可寫、(b) 生效的 `PSC_*` 根全部落在受保護樹內、
  (c) 兩個 `EnvironmentFile` 的 owner／mode 符合預期、(d) 已安裝版本與其部署
  attestation 相符。任一項不符 SHALL fail-closed 拒絕啟動並輸出結構化診斷，
  MUST NOT 降級為警告後繼續。

若不做：攻擊者不必碰任何 state——改寫 `verification.py`、注入 `sitecustomize.py`、
或把 `<instance>-manager.env` 的 `PSC_COORDINATOR_ROOT` 指到自己的目錄，再重啟
服務，即可整套繞過。這是計畫第七輪 critical 修正的核心，也是路線 B 單獨無法解決
的一族。

#### per-executor 加固剖面（#643 裁決；`permgen.HARDENING_PROFILES`）

**`MemoryDenyWriteExecute=yes` 與 JS runtime 天生互斥。** 實機逐項隔離（以
`cortex-builder` 身分、`systemd-run` 帶單一 property）的結果是：無加固時 `node` 正常；
`+MemoryDenyWriteExecute=yes` 時 V8 直接崩在 `v8::internal::Runtime_CompileLazy`；其餘
每一項（`ProtectSystem=strict`／`PrivateTmp`／`RestrictNamespaces`／
`SystemCallFilter=@system-service` ＋ `SystemCallErrorNumber=EPERM`）單獨加上去 `node`
都正常。**唯一的阻斷點就是它**——V8 的 JIT 必須有 W+X 記憶體。

四個 executor 因此分成兩類（分類來源＝`permgen.EXECUTOR_TOOLS` 的 `needs_node`，
與 §R1 executor 執行面那張表**同一張**，不另立清單）：

| executor | 形態 | 完整加固面 | 加固剖面 | 模板 unit |
|---|---|:--:|---|---|
| `claude` | 原生 ELF | ✅ | `strict` | `cortex-job@.service` |
| `agy` | 原生 ELF | ✅ | `strict` | `cortex-job@.service` |
| `codex` | node script | ⛔ 空輸出 | `jit` | `cortex-job-jit@.service` |
| `copilot` | shell → node | ⛔ 空輸出 | `jit` | `cortex-job-jit@.service` |

**規範**：

- job 模板 unit SHALL 有且僅有 `permgen.HARDENING_PROFILES` 列舉的那幾份，且**全部由
  同一張 `_HARDENING` 表產生**；剖面之間的差異 SHALL 侷限於
  `permgen.PROFILE_DIVERGENCE_KEYS`（目前是 `{MemoryDenyWriteExecute}` 這一項）。
  複製兩段各自維護的加固表 MUST NOT 出現——那會讓「日後改一份忘另一份」變成必然。
- 剖面 SHALL 由 **executor** 決定，而 executor SHALL 是 Manager 的 dispatch 決定
  （`SubprocessLauncher(executor=...)`，在任何 per-job 產物之前就固定）。
- job spec **MUST NOT** 攜帶任何決定剖面的欄位（`job_runner.SPEC_FORBIDDEN_KEYS` 的
  剖面族），且 SHALL 在寫端（`build_job_spec`）與讀端（`job_shim.load_spec`）各掃一次
  ——**這與「身分欄位不入 spec」是同一條原則**：身分只有一個來源（root-owned unit 的
  `User=`），剖面也只有一個來源（executor）。
- 未登記的 executor SHALL **fail-closed**，MUST NOT 預設落到放寬的那一份剖面。
  （也刻意不預設落到嚴格那份：一個未被盤點過的 node 型 CLI 會在真實加固面下靜默
  起不來，症狀是空輸出，離原因很遠——#643 本身就是這樣被埋掉的。）
- 部署 config（`PSC_JOB_TEMPLATE_UNIT`）SHALL 只接受**基底**模板名；帶剖面後綴的值
  MUST 被拒——否則一行 config 就能把**所有** job 推到寬鬆剖面，per-executor 設計當場
  退化成「全域移除 MDWE」。
- polkit 規則的 unit pattern SHALL **列舉**這些模板字幹（錨定的交替），MUST NOT 放寬
  成萬用字元；`instance` 段的字元類 SHALL 維持不變。

##### 誠實的取捨：哪一類 job 有哪一種保護

**走 `jit` 剖面的 job（`codex`／`copilot`）失去 `MemoryDenyWriteExecute` 這一層。**
具體代價：取得任意程式碼執行的攻擊者可在**該 job 自己的位址空間內**配置 W+X 記憶體，
JIT 型 shellcode（不落地、不經 `exec`、因此不觸發任何依賴檔案或 exec 的偵測）在此可行。
這是真的少了一層，不是換個寫法的等價物。

**沒有失去的部分**（同樣要寫清楚，否則會被讀成比實際更弱）：其餘 26 項加固在 `jit`
剖面下**逐項不變**——`NoNewPrivileges=yes`、`CapabilityBoundingSet=`（空）、
`ProtectSystem=strict`、`ProtectHome=yes`、`RestrictNamespaces=yes`、
`SystemCallFilter=@system-service`、`RestrictSUIDSGID=yes`… 全部照舊，`User=` 一樣寫死在
root-owned 的 unit 檔裡。W+X 讓攻擊者能在**自己這個 UID** 內執行程式碼；**跨 UID、
跨檔案系統、提權**那幾層完全沒有鬆動。換句話說：`jit` 剖面守不住的是「job 內部的
程式碼完整性」，守得住的仍然是「job 出不了自己那個 UID 的邊界」——而 §R2／§R3 保護的
Tier-0／Tier-1 資產靠的是後者。

**換到什麼**：保住 `codex`／`copilot` 兩個 provider。若採方向 3（只用原生 ELF
executor），build 域只剩 `claude`／`agy`，§R5／§R8 的 `independence_domain`
（reviewer 不得與 builder 同 domain）可選空間當場減半。方向 1（全域移除 MDWE）則會讓
**不需要**放寬的 `claude`／`agy` 一起失去這層——方向 2 的全部價值就在於「只讓需要的
那一類付這個代價」。

**因此 spec 明載**：本系統**不宣稱**「所有 job 都有完整加固」。準確的敘述是——
**原生 ELF executor 的 job 有完整 27 項加固；node 型 executor 的 job 有 26 項，
少的那一項是 `MemoryDenyWriteExecute`。** 任何引用本 spec 的稽核、報告或 PR 描述
SHALL 用這句話，MUST NOT 簡化成「job 已完整加固」。

**未來選項（不在本票範圍）**：若某天 node 型 executor 有辦法在無 W+X 下執行
（V8 的 jitless 模式、或 CLI 改為原生編譯），`jit` 剖面 SHALL 被移除而不是長期保留；
移除的判準就是上表「完整加固面」欄全部變 ✅。

#### Scenario: node 型 executor 在嚴格剖面下起不來

- **WHEN** 以 `codex`／`copilot` 為 executor 的 job 被派到 `cortex-job@.service`
  （strict 剖面）
- **THEN** 它 MUST 失敗（V8 崩在 `Runtime_CompileLazy`），MUST NOT 被視為可接受狀態；
  驗收 SHALL 包含這條**負向對照**——只驗 `jit` 剖面成功無法證明剖面分岔是必要的

#### Scenario: job 試圖選擇加固剖面

- **WHEN** job spec（或任何 job 可影響的輸入）攜帶 `hardening_profile`／`profile`／
  `template_unit` 等欄位
- **THEN** 寫端與讀端 MUST 各自 fail-closed 拒絕執行，MUST NOT 忽略該欄位後照常執行

#### Scenario: 未登記的 executor

- **WHEN** dispatch 使用一個不在 `permgen.EXECUTOR_TOOLS` 內的 executor 名
- **THEN** 降權啟動器 MUST fail-closed，MUST NOT 落到任一剖面（放寬的那份會讓設計退化，
  嚴格的那份會讓問題再被埋一次）

#### Scenario: 竄改 EnvironmentFile 重導 state 根

- **WHEN** builder 改寫 `<instance>-manager.env` 使 `PSC_COORDINATOR_ROOT` 指向
  自己可寫的目錄，然後重啟 Manager 服務
- **THEN** 寫入 MUST 先以 `EACCES` 失敗；若以特權預先植入，Manager 啟動自檢
  MUST fail-closed 拒絕啟動

#### Scenario: 刪除 EnvironmentFile

- **WHEN** `<instance>-manager.env` 被刪除
- **THEN** Manager MUST 拒絕啟動，MUST NOT 靜默落回 `$HOME/.agents` 預設

#### Scenario: verifier downgrade

- **WHEN** 攻擊者把套件降版到不含驗簽／不含自檢的版本後重啟服務
- **THEN** 降版寫入 MUST 被拒；且部署身分驗證 MUST 涵蓋已安裝版本，使降版在
  特權植入的情況下仍被偵測

### R4 mutation ingress MUST 認證 issuer，payload 自述欄位 MUST NOT 參與授權判斷

`requested_by`（`control/contract.py:229-231`）與 `actor`
（`work_actions.py:1118-1131` 等處）SHALL 降為**顯示性欄位**，MUST NOT 作為任何
授權判斷的輸入，且 MUST NOT 作為 audit actor 的 fallback——現況
`work_actions.py:3649,3766,3961` 讓未驗證的 `requested_by` 成為
`abandon`／`retire-delivered`／`reset-reclaim-budget` 的記錄身分，
`work_actions.py:1166` 更把它烘進不可變的 maintainer attestation。

敏感 action SHALL 僅接受經認證通道送達的請求，MUST NOT 接受未認證的 file-queue
request。封閉清單（新增須改此 spec）：`ship`、`review-attest`、`abandon`、
`retire-delivered`、`reset-reclaim-budget`、`intake`、`auto` 開關、
發佈類 outbox mutation、trust-root 設定變更。

request 處理順序 MUST NOT 由本地可控的 mtime 決定
（現況 `manager_daemon.py:1431-1436`）；SHALL 改用 Manager 自己發放的單調序號。

若不做：control queue 現況等於「能建檔就能執行 22 種 work-action」，
`_review_attest_action()` 等於「誰能寫 queue，誰就能核發 maintainer review
attestation」；role enum 檢查擋不住自稱 operator 的合法格式 request。

#### Scenario: headless 提交自稱 operator 的 ship request

- **WHEN** builder 寫入一份 `requested_by: "operator"` 的 `ship` request
- **THEN** 寫入 MUST 被 OS 拒絕；若以特權植入，Manager MUST 因「來源未認證」
  拒絕消費並記錄結構化拒絕事件

#### Scenario: 顯示性欄位不影響判定

- **WHEN** 一份經認證通道送達的請求其 `requested_by` 為任意字串
- **THEN** 授權判定與 audit actor MUST 與該字串內容無關

#### Scenario: 順序不可由 mtime 操控

- **WHEN** 攻擊者以 `touch` 調整佇列中檔案的 mtime
- **THEN** 處理順序 MUST 不改變

### R5 設定 overlay 與 anti-collusion 來源 MUST 位於保護邊界內，且 overlay MUST NOT 具備無界提權能力

`independence_domain` 是唯一的 anti-collusion 控制（`review.py:125-131`），其來源為
model identity；host overlay `<project_config_root>/model-identities.yaml` 可壓過
packaged registry（`model_identities.py:604-631`）。該 overlay、
`<agents_root>/config/combos/` 的 instance-local combo override
（`deck/schema.py:80-87`）、以及 persona overlay，SHALL 全部視為 Tier-0 信任設定，
納入 R3 的不可寫邊界。

persona overlay 的 `tool_allowlist_additions`（`persona/context.py:37-39`）現況
**只能加、不能減、無上限、無合法新增白名單**。SHALL 改為受封閉白名單約束，
且 overlay MUST NOT 能授予未在 packaged persona 契約中宣告的工具族。

若不做：攻擊者不必改 evidence——改一行 overlay YAML 讓 builder 與 reviewer 落在
不同 `independence_domain`，foreign review 的獨立性即憑空成立；或以
`tool_allowlist_additions` 給自己加上任意工具。

#### Scenario: 竄改 independence_domain

- **WHEN** builder 改寫 model-identity overlay 使自己與 reviewer 分屬不同域
- **THEN** 寫入 MUST 被拒；若以特權植入，Manager MUST 因設定檔完整性檢查失敗
  而 fail-closed

#### Scenario: overlay 無界提權

- **WHEN** persona overlay 宣告一個不在白名單內的 `tool_allowlist_additions`
- **THEN** 載入 MUST 失敗，MUST NOT 靜默採納

### R6 簽章路線的完整規格（Phase 3 落地；本票定案，不作為 0.2.0 blocker）

採用簽章時，SHALL 滿足下列全部條款。

**(a) canonical encoding**：簽名輸入 SHALL 為 canonical JSON 的 **bytes 本身**，
非其 hexdigest。canonical 形式沿用既有 `verification.canonical_json_hash()`
（`verification.py:56-58`）的參數（`sort_keys=True`、`separators=(",", ":")`、
`ensure_ascii=False`、UTF-8）。額外 MUST：拒絕 `NaN`／`Infinity`；以
`object_pairs_hook` 拒絕重複 key；**被簽 payload MUST NOT 含 float**——現況
`work_actions.py:1168` 的 `"reviewed_at_epoch": float(now_epoch)` 必須改為整數
epoch 毫秒或 RFC3339 字串，因為 float 的 JSON 表示不可攜、跨版本不穩定。
本 repo 現存兩套不同的 canonical 參數（`verification.py:56-58` 用
`ensure_ascii=False`；`terminal_contract.py:497-503` 用 `ensure_ascii=True`），
SHALL 收斂為單一定義，否則同一份內容會有兩個合法摘要。

**(b) domain separation**：簽名輸入 SHALL 為
`b"psc-sig-v1\x00" || record_type || b"\x00" || canonical_bytes`，`record_type`
取自封閉 enum（`job-registry`、`verification-evidence`、`review-verdict`、
`maintainer-attestation`、`completion-record`、`full-suite-evidence`、
`workflow-evidence`、`gate-ledger`、`outbox-entry`、`control-response`、
`skill-ledger-entry`、`legacy-import`）。跨 `record_type` 重用簽名 MUST 驗證失敗。

**(c) anti-replay**：被簽 payload SHALL 含
`(subject, run_id, authority_revision, seq, key_id, signed_at, not_after)`。
consumer SHALL 驗證：`subject` 等於當前 candidate／head；`authority_revision`
等於當前 `work_authority_digest()`（`work_actions.py:1092,1162` 已存在）；
`seq` 對該 subject 嚴格單調遞增且由 Manager 的單調計數器發放；
`signed_at < not_after` 且未過期。**單調計數器 SHALL 位於 R2 的 OS 邊界內**
——此為簽章方案依賴 OS 邊界的第三個結構性依賴，必須在 spec 明載而非留待實作發現。

**(d) key rotation／revocation**：trust store SHALL 為 Manager-owned 的
append-only 記錄，每筆含 `key_id`（公鑰指紋）、`algo`、`public_key`、
`valid_from`、`valid_until`、`revoked_at`、`revocation_reason`。驗簽時
`signed_at` MUST 落在 `[valid_from, valid_until)` 且早於 `revoked_at`。
rotation SHALL 有明定 overlap window（建議 ≤14 天），期間新舊 key 皆可信；
overlap 結束後舊 key MUST 失效。撤銷後由該 key 簽出的 record 一律失效，
**MUST NOT 自動 re-sign**——自動 re-sign 會把被竊金鑰簽出的偽造品洗白；
恢復途徑僅有 operator 明示 re-attest。

**(e) 舊 unsigned state 遷移**：SHALL 由 operator 在**已完成 R2／R3 隔離的環境**
中，以一次性命令對現存 state 逐項產生 `legacy-import` attestation，內容含 import
時的內容 hash、import 者身分、當時的 policy version、import 時間，並標記
`trust: legacy-imported`。此標記 MUST NOT 滿足任何 ship gate 或 acceptance 判定，
僅供讀取與歷史查詢。遷移點之後產生而無簽的 record MUST fail-closed。
遷移 MUST NOT 自動執行——自動遷移會把遷移前已被竄改的狀態一次合法化。

**(f) 缺簽／壞簽 fail-closed**：consumer 遇到缺簽、簽名驗證失敗、未知 `key_id`、
已撤銷 key、`record_type` 不符、`seq` 回退、`subject`／`authority_revision` 不符時，
MUST 以結構化錯誤中止該筆消費並將對應 work 轉入 blocked 狀態；MUST NOT 降級為
警告、MUST NOT 略過該筆繼續處理其餘項目。fail-closed 事件本身 MUST 寫入
Manager-owned 的 append-only 稽核記錄。

若不做：把簽章留給實作票自行決定，最可能的結果是「簽 hexdigest 字串、無 domain
separation、無 anti-replay、壞簽只記 warning」——這樣的簽章不提高攻擊成本，卻讓
系統看起來已受保護，比不做更危險。

#### Scenario: 跨 record_type 重放

- **WHEN** 取一份合法的 `verification-evidence` 簽名，套用到內容相同的
  `review-verdict` record
- **THEN** 驗簽 MUST 失敗

#### Scenario: 跨 run 重放

- **WHEN** 取上一個 run 的合法 maintainer attestation，放進當前 run 的 evidence 目錄
- **THEN** consumer MUST 因 `run_id`／`authority_revision` 不符而拒絕

#### Scenario: rollback 至較舊的合法 record

- **WHEN** 以一份簽名仍有效但 `seq` 較小的舊 record 覆蓋新 record
- **THEN** consumer MUST 因 `seq` 回退而 fail-closed

#### Scenario: 壞簽不得降級

- **WHEN** 任一 Tier-0 record 的簽名損毀
- **THEN** consumer MUST 中止並使 work 進入 blocked，MUST NOT 僅輸出警告後繼續

### R7 operator 授權通道 MUST NOT 依賴同 UID 可讀的 secret，逐案核可 MUST 為 action-bound＋single-use＋短效＋不可重放

operator 授權 SHALL NOT 使用同 UID 可讀的檔案 secret 或 env secret 作為身分證明。
逐案核可 SHALL 以 capability 形式表達，且滿足：

- **action-bound**：綁定 `(action, work_id, run_id, subject_hash,
  authority_revision)`；任一項不符即無效，MUST NOT 可轉用到其他 action 或 work。
- **single-use**：Manager 側維護 nonce ledger（位於 R2 邊界內），消費後即作廢；
  重送同一 capability MUST 被拒。
- **短效**：TTL 以分鐘計（建議 ≤300 秒），逾時 MUST 拒絕。
- **不可經 durable state 重放**：capability 本體 MUST NOT 落地到任何 durable
  state（`jobs.json`、evidence、journal、queue、log、done 結果皆不得含其可還原
  形式）；落地的只有「已消耗 nonce」與不可還原的授權 attestation。

通道 SHALL 為 Manager-owned 目錄（`0700`、owner 為受信任身分）下的 Unix socket，
使 headless UID 因目錄 traverse 被拒而無法 connect——此模式在 repo 內已有前例
（`monitor/service.py:104` run dir `0700`、`monitor/server.py:168` socket `0600`）。
互動 broker（需 TTY 確認、headless 結構性拿不到）SHALL 疊加於其上作為 operator
**身分**通道，MUST NOT 被視為 durable-state 隔離的替代（計畫第六輪已明文）。

若不做：capability 若落地到 durable state，攻擊者讀出後即可重放；若非
action-bound，一次核可 `abandon` 就能被轉用去 `ship`。

#### Scenario: capability 重放

- **WHEN** 攻擊者取得一份已被消費的 capability 並重送
- **THEN** Manager MUST 因 nonce 已消耗而拒絕

#### Scenario: capability 不得出現在 durable state

- **WHEN** 對全部 Tier-0／Tier-1 資產做內容掃描
- **THEN** MUST 找不到任何 capability 的可還原形式；此為一條常設測試

#### Scenario: headless 連 control socket

- **WHEN** builder 嘗試 connect Manager 的 control socket
- **THEN** MUST 以 `EACCES`／`ECONNREFUSED` 失敗

### R8 reviewer 身分 MUST 由 Manager registry 推導，MUST NOT 取自 payload

review verdict 的有效性判定 SHALL 僅依據 Manager registry 中的
`(job_id, persona, independence_domain, immutable verdict)` 四元組推導；
payload 中的 `actor`／`persona`／`requested_by` MUST NOT 參與判定。
`independence_domain` SHALL 由 Manager 在派工時決定並寫入 registry，
MUST NOT 由 job 自述、MUST NOT 由同 UID 可寫的 overlay 決定（見 R5）；
Manager SHALL 拒絕 reviewer job 與被審 builder job 落在同一
`independence_domain` 的組合（沿用 `review.py:125-131` 的既有語意，但改以
registry 為權威來源）。

若不做：`_review_attest_action()` 現況接受 caller 提供的 `actor`
（`work_actions.py:1119`），reviewer 可自我背書，`independence_domain` 淪為裝飾。

#### Scenario: 偽造 approver role

- **WHEN** 一份 review verdict 的 payload 宣稱 `persona: "reviewer"` 但其
  `job_id` 在 registry 中登記為 builder
- **THEN** Manager MUST 拒絕，並記錄 registry 推導出的實際 persona

#### Scenario: independence domain 相同

- **WHEN** reviewer job 與被審 builder job 的 registry `independence_domain` 相同
- **THEN** 該 verdict MUST 不被採計為 foreign review evidence

### R9 E2E 測試矩陣 MUST 涵蓋四族，且 MUST 含 negative control

系統 SHALL 交付下列測試矩陣，全部以「給 headless persona 一個任意 shell」為前提
執行；每族 MUST 對 builder／reviewer／planner 三者分別執行（或明確論證等價性）。

**族 1：capability-theft（四路）**

| ID | 動作 | 期望 |
|---|---|---|
| T1.1 | 讀 operator token／gh token／簽章私鑰／trust store | `EACCES` |
| T1.2 | connect Manager control socket | `EACCES`／`ECONNREFUSED` |
| T1.3 | 呼叫 operator CLI（`cortex work ship` 等敏感 action） | 拒絕（無 capability） |
| T1.4 | 直寫 `<control_root>/requests/` | 寫入被拒；若特權植入則消費被拒 |

**族 2：durable-state-tampering**——對登記表中**每一項** Tier-0／Tier-1 資產各
執行：`modify`、`truncate`、`delete`、`replace`（整檔 `os.replace`）、
`symlink-swap`、`rollback`（以較舊的合法內容覆蓋）。全部 MUST 被 OS 拒絕，或
（簽章路線下）被 consumer 偵測為無效。逐項至少涵蓋：`jobs.json`、
**`.psc-review-verdict.json`**、verification evidence、maintainer attestation、
completion record、full-suite evidence、workflow inputs／workflow evidence、
gate ledger、delivery journal、digest outbox、engineering-outcome outbox、
control queue（含 mtime 重排）、monitor 讀模型與游標、`.cortex/work-items.yaml`、
model-identity overlay、combo override、dispatch specs、handoff manifest、
skill ledger、runtime bootstrap env。

**族 3：enforcement-plane-tampering**——每案 MUST **實際重啟服務**後再驗證：

| ID | 動作 |
|---|---|
| T3.1 | 改寫 `site-packages` 內的 verifier 原始碼 |
| T3.2 | 注入 `sitecustomize.py`／`.pth` 到 venv |
| T3.3 | 改寫 user unit 的 `ExecStart` |
| T3.4 | 改寫 `<instance>-manager.env` 重導 `PSC_AGENTS_ROOT`／`PSC_COORDINATOR_ROOT` |
| T3.5 | 刪除 `EnvironmentFile`（測 `-` 靜默容忍已被移除） |
| T3.6 | 改寫 `service-manager.sh` launcher |
| T3.7 | 竄改 `PATH`／`PY` 指向偽造直譯器 |
| T3.8 | 設定 `PSC_MANAGER_INSTALLER`／`PSC_REPLY_BRIDGE`／`PSC_DIGEST_DELIVERY_CMD` 指向任意程式 |
| T3.9 | 改寫 `~/.codex/hooks.json` |
| T3.10 | verifier downgrade：降版到不含驗簽／自檢的版本 |

**族 4：行程間路徑**

| ID | 動作 | 期望 |
|---|---|---|
| T4.1 | 檢查 headless job 的 `/proc/self/fd` | MUST NOT 含任何指向受保護資產的可寫 fd |
| T4.2 | `ptrace` attach Manager 行程 | `EPERM` |
| T4.3 | 讀 `/proc/<manager_pid>/mem`、`environ` | `EACCES` |
| T4.4 | 對 Manager 送 signal（`SIGSTOP`／`SIGKILL`） | `EPERM` |

**negative control（必要）**：每一族 MUST 附一組以受信任身分執行相同動作且
**必須成功**的對照案例。無 negative control 的測試會在環境壞掉（例如目錄根本
不存在）時假綠。

若不做：「通過 D6」會變成宣稱而非事實。計畫已把這三族測試列為 join gate 的判準。

#### Scenario: 測試矩陣完整性

- **WHEN** R1 登記表新增一項 Tier-0 資產但族 2 未新增對應案例
- **THEN** 矩陣完整性測試 MUST FAIL

#### Scenario: negative control 反證

- **WHEN** 以受信任身分執行族 2 的寫入動作
- **THEN** MUST 成功——若同樣失敗，表示測試環境無效，該族結果 MUST 視為未通過

### R10 落地 MUST 分三階段交付，Phase 1 MUST 可在不需 root 的前提下先行

**Phase 1（不需 root、不改部署拓撲；可與 R0.5 其餘 D 併行）**

1. R1 資產登記表＋雙向等式測試＋重複路徑推導收斂。
2. R4：`requested_by`／`actor` 降為顯示性欄位（含移除 audit actor fallback）；
   敏感 action 封閉清單凍結；queue 順序改用 Manager 單調序號。
3. R8：reviewer 身分改由 registry 推導；`independence_domain` 由 Manager 決定。
4. R5 的 overlay 白名單化（`tool_allowlist_additions` 受封閉白名單約束）。
5. R3 的**可先行部分**：`EnvironmentFile` 缺檔改 fail-closed；三個未守衛的
   command env 入口加 typed-argv 守衛；systemd 加固指令加入 unit template；
   Manager 啟動自檢先以 WARN 上線收集現況。
6. **降級運轉**（計畫 join gate 第 5 條）：D6 未通過前，**headless acceptance、
   outbox mutation、ship／merge 路徑一律停用，或需 operator 逐案明示核可**。

**驗收**：登記表等式測試綠；敏感 action 在無 capability 時 100% 被拒的單元測試；
自檢在現行部署上輸出的診斷與本 spec 背景段的盤點一致（用以反證盤點正確）；
降級運轉開關預設為「停用」。

**Phase 2（需一次性 root 設定；0.2.0 join gate 的實體）**

1. 建立受信任身分與 headless UID（reviewer 與 builder **必須**分離，見 R2）。
2. 路徑分樹：`~/.agents` 拆為 Manager-owned 樹與 job-visible 樹；目錄 owner／mode
   由 R1 登記表產生（權限產生器以登記表為輸入，不手寫）；一併修掉
   instance-scope 漏洞與現存 group-writable 現況。
3. review verdict 改由受控通道交付、Manager 落地（R2）。
4. 部署遷移：Manager 從 operator pipx tree 遷到 root-owned 部署路徑；unit 改
   system-level 或 manager UID lingering user service；`EnvironmentFile` 遷入
   受保護樹；unit 加固指令切實生效。
5. 降權啟動器：Manager 以降權方式 spawn headless job，明確關閉 FD 傳遞、
   不傳遞 gh token。
6. 交換面：headless 產出改走 **append-only spool**——builder 的 commit 以 git
   bundle 交付（`commit-spool`，#637）、reviewer 的 verdict 走
   `review-verdict-spool`（#599）；Manager 讀的一律是 Manager-owned 樹裡的一個
   **檔案**，**不**伸手進 job 的工作樹（#641 把登記表殘留的讀取 ACL 收掉）。
   planning artifact 走 staging→publish 兩段，publish 只由 Manager 執行。
7. R3 自檢切 fail-closed。
8. R9 四族測試矩陣全綠。

**驗收**：R9 全族綠燈含 negative control；服務重啟後族 3 全案仍綠；以 builder
shell 手動嘗試背景段列出的每一條攻擊路徑皆失敗並留下稽核紀錄。

**Phase 3（defense-in-depth，非 0.2.0 blocker）**

1. R6 簽章方案落地（Ed25519、trust store、單調 seq、canonical 參數收斂、
   `legacy-import` 遷移）。
2. Elevated tier 與跨主機／跨信任域的證據可攜。

**驗收**：R6 的四個 Scenario 全綠；`legacy-imported` 標記無法滿足任何 ship gate
的測試綠。

若不做：把 Phase 2 的 root 設定與 Phase 1 的程式碼改動綁在同一張票，會讓可先行的
部分被部署排程卡住；而 Phase 1 的降級運轉正是 join gate 未達成期間的安全網。

#### Scenario: 降級運轉生效

- **WHEN** D6 尚未通過且 `auto` 派工被觸發到 ship 路徑
- **THEN** 該路徑 MUST 停用或要求 operator 逐案核可，MUST NOT 靜默放行

### R11 `#484`／`#480`／`#489` 的吸收對照 MUST 明載取代／補強關係

| issue | 訴求（原文核心） | 與 D6 的關係 |
|---|---|---|
| **`#484`** slice foreign reviewer 實際跑 `--sandbox workspace-write` 而非強制 read-only。根因：`build_request_executor()` 解析出的 `active_review_launcher` 被 `apply_slice_action()`／`_launch_foreign_review()` 直接使用，未經 `as_review_only()` 特化（workflow lane 有做、legacy slice lane 沒做）。 | **Phase 2 取代，但既有修法照做、不等 D6**——0.2.0 join gate 第 4 條獨立要求 `#484` 修復＋foreign-review E2E 通過。D6 Phase 2 的 reviewer 獨立 UID 是 read-only 的執行期強制版本；`#484` 的 `as_review_only()`／argv 斷言降為 defense-in-depth。**注意 D6 揭露了 `#484` 未涵蓋的一層**：即使 reviewer 被正確限制成 read-only，**builder 仍可代寫 verdict**（背景 §3），因此 `#484` 單獨修復**不足以**使 foreign review 可信，R2 才是完整解。 |
| **`#480`** Claude headless builder 未把 persona `effective_tools` 翻成 executor 實際授權，只給 `--permission-mode acceptEdits`、不產生 `--allowedTools`，導致測試與 commit 永遠等不到批准。 | **降級為補強**：`effective_tools` 由 agent 進程自己執行，而攻擊模型中 agent **就是**那個進程——它可以不呼叫工具、直接執行 shell。D6 明文將其定位為 UX／可用性／誤觸防護，MUST NOT 被引用為 trust boundary。R2 的 OS 邊界為其執行期強制版本。**D6 另補強 `#480` 未觸及的缺口**：overlay 的 `tool_allowlist_additions` 只加不減、無上限（R5），`#480` 若只加窄 `--allowedTools` 而不封 overlay，仍可被繞過。 |
| **`#489`** `persona-scope` gate 只證明變更「在 repo 裡某處」，未驗 task 邊界：builder persona `write_paths: ["**"]` 讓任何路徑都過，slice 契約無 per-slice changed-path allowlist。 | **補強且成為輸入**：宣告式 scope 是「應然」的唯一來源，Phase 2 的權限產生器 SHALL 以其（含 `#489` 要求新增的 per-slice allowlist）為輸入產生實際目錄權限；事後檢核保留為漂移偵測。builder 的 `["**"]` 在 D6 下不再等於「可寫全機」——值域被限縮為「自己 worktree 內的全部路徑」。**D6 補上 `#489` 的一個具體後果**：`["**"]` 目前涵蓋 `.cortex/work-items.yaml`（correlation authority）與 `.github/**`，即「builder 寫 repo」本身就是一次治理設定變更。 |

三者屬同一缺陷家族：**契約在紙面上宣告了邊界，但 runtime 的信任根沒有把它變成
可執行的強制**。D6 是該家族的根治層，三張 issue 是其症狀層——MUST NOT 因三張
issue 關閉而視為 trust 問題已解決。

若不做：三張 issue 會各自被當成「已解決 trust 問題」而關閉，實際上都停在
advisory 層。

#### Scenario: effective_tools 不得作為 trust boundary

- **WHEN** 任何設計文件或程式碼註解引用 `effective_tools` 作為安全邊界的依據
- **THEN** 該引用 MUST 被視為缺陷；安全邊界的唯一依據是 R2／R3

### R12 D6 未通過前 MUST NOT 宣告 0.2.0 穩定，且 MUST NOT 適用有界殘餘風險豁免

D6 SHALL 為 0.2.0 的不可豁免 join gate。在 R9 四族測試矩陣全綠之前，版本
MUST NOT 標記為 stable；本項 MUST NOT 引用計畫「貫穿工項」第 7 條的「明文核可的
有界殘餘風險」通道——計畫第六輪已明文將 trust-root 排除在該豁免之外。

若不做：「任意 builder shell 可偽造授權」不是有界殘餘風險，是無界的；把它塞進
豁免通道等於用文件宣告解決了一個未解決的問題。

#### Scenario: 嘗試以豁免通道放行

- **WHEN** release checklist 嘗試以「有界殘餘風險」標記 D6
- **THEN** 該標記 MUST 被拒；D6 僅接受 R9 全綠作為通過依據

## 非目標

- 不實作任何一行程式碼：不改 `config/paths.py`、`config/runtime.py`、
  `control/contract.py`、`coordinator/*`、`deploy/installer.py`、`persona/*`、
  `deploy/templates/*.tmpl` 任何一行。
- 不執行任何部署動作：不建 UID、不改 systemd unit、不動 pipx tree、不改 auth。
- 不決定 Phase 2 的 root 設定由誰在何時執行（排程屬 operator 權責，見未決問題）。
- 不重做 `#484` 的既有修法——`#484` 依 join gate 第 4 條獨立推進，D6 只定義其
  最終被何者取代，並補記其未涵蓋的一層。
- 不涵蓋 operator 自身被入侵的情境。D6 的信任根是「operator 身分未失守」；
  operator 失守屬於另一個威脅模型。
- 不涵蓋 supply-chain（上游套件本身惡意）與硬體信任根（TPM／measured boot）。
  R6(d) 的 trust store 設計不排斥未來以 TPM sealing 承接，但本票不定案。
- 不承諾 Phase 3 的簽章方案在 0.2.0 前落地。
- 不處理 `paulsha-hippo` 等外部系統經 `<project_config_root>/project-hippo.yaml`
  的跨系統檔案契約——已記錄為 ingress，但其治理歸屬待定（見未決問題）。

## 驗收面

- R1–R12 逐條可對照回 v4 計畫 R0.5 D6 條目與其第四／五／六／七輪修正段落；
  計畫明列的 spec 必須涵蓋七項各有對應 Requirement：資產盤點→R1、
  enforcement plane 入界→R3、兩路線比較與建議→「路線比較與裁決建議」段＋R6、
  operator 授權通道→R4＋R5＋R7＋R8、E2E 測試矩陣→R9、落地計畫→R10、
  issue 吸收對照→R11。
- 本票為 spec-only：`git diff --stat` 對 `paulsha_cortex/` 應為零；
  `grep -rn "trust_root\|trust-root" paulsha_cortex/` 現況應零命中。
- 背景段每一條現況主張皆附 `檔案:行號`（部署觀測項另註明為唯讀實測），可由後續
  實作票逐條複驗；任一條與 main 不符即視為盤點缺陷，須先修正本 spec 再進 Phase 1。
- 全套 `python3 -m pytest tests/ -q` 不受本票影響（docs-only）。

## 風險與緩解

| 風險 | 影響 | 緩解 |
|---|---|---|
| Phase 2 的 root 設定在 WSL2 上與宿主整合出問題（lingering、systemd user session） | join gate 被部署問題卡住 | Phase 1 完全不需 root 且含降級運轉安全網；Phase 2 先在拋棄式環境跑完 R9 再上正式環境 |
| 路徑分樹破壞既有 `PSC_*` 契約，導致既有 instance 無法啟動 | fleet 停擺 | 分樹以登記表產生、保留舊路徑為唯讀相容層一個 release；`doctor` 增加分樹健檢；實測已發現部署中的 env 缺 `PSC_CONTROL_ROOT`，遷移前須先對帳 |
| headless 失去 gh token 後功能退化 | headless 無法自行推 PR | 由 Manager 代理 GitHub 寫入（D1 outbox 記帳已是此方向）；Phase 2 前先量測哪些 headless 動作真的需要 token |
| 資產盤點漏項 | 漏保護的 state 成為新破口 | R1 的雙向等式測試把漏項變成 CI FAIL；同時收斂六處重複的 journal 路徑推導 |
| **per-executor 加固剖面（#643）被讀成「所有 job 都有完整加固」** | 稽核／報告高估實際保護面，日後出事時對不上 | §R3 的「誠實的取捨」段明載準確敘述（原生 ELF 27 項／node 型 26 項，少的是 `MemoryDenyWriteExecute`）；產生出來的 unit 檔頭逐條寫著它接受的代價；runbook 第 5-2 步要求執行者先讀那一段 |
| **剖面被改成可由呼叫端選擇**（config、spec、或新增一個「方便」的參數） | 退化成「全域移除 MDWE」——`claude`／`agy` 一起失去該層，方向 2 的價值歸零 | 剖面族進 `SPEC_FORBIDDEN_KEYS`（寫端＋讀端各掃一次）；`PSC_JOB_TEMPLATE_UNIT` 拒絕帶剖面後綴的值；未知 executor fail-closed；polkit pattern 只列舉具名字幹。四條都有對應測試 |
| Phase 3 簽章方案被誤當成可替代 Phase 2 | 投入密碼學工程卻仍可被繞過 | 「路線比較與裁決建議」段已論證 B 的三個前提都依賴 A；R6(c) 明載單調計數器必須在 OS 邊界內 |
| reviewer 與 builder 分離推高資源與複雜度 | Phase 2 工期拉長 | independence 是 `#484` 與 D6 的共同要求，不可省；可先以兩個 UID（builder／非 builder）起步，planner 併入非 builder 域，待驗證後再細分 |
| 降級運轉期間 fleet 產能下降 | 交付變慢 | 降級只涵蓋 acceptance／outbox mutation／ship 三條路徑，define／plan／build／verify 不受影響 |
| systemd 加固指令（`ProtectSystem=strict` 等）誤擋既有寫入路徑 | 服務起不來或功能靜默失效 | 加固指令與 `ReadWritePaths` 白名單由 R1 登記表產生；Phase 1 先在 unit template 加入並以既有 E2E 驗證，不等 Phase 2 |

## 未決問題（需 operator 拍板，本票不擅自決定）

1. **受信任身分的形態**：Manager 跑在專屬 UID，或沿用 operator UID 而只降權
   headless？前者隔離更徹底（operator 誤操作也碰不到 state），後者部署改動小得多。
   本票傾向前者，但成本差距顯著。
2. **headless UID 的粒度**：三個 persona 各一個 UID，或僅二分（builder／非
   builder）？R2 只硬性要求 reviewer 與 builder 互不可寫。
3. **Phase 2 的 root 設定執行方式**：手動 runbook，或提供
   `cortex install trust-root --system` 這類需 root 的子命令？後者較可重複，但等於
   把特權操作寫進 codebase，本身需要審查。
4. **舊 state 的處置**：Phase 2 切換時，現存 `jobs.json` 與 evidence 直接 `chown`
   沿用，或視為不可信、以 R6(e) 的 `legacy-import` 方式重新入帳？前者快，但把
   「切換前可能已被竄改」的風險吞下去。
5. **降級運轉的預設值**：D6 未通過期間，ship 路徑是**完全停用**，還是**允許
   operator 逐案核可**？後者需要 R7 的 capability 通道先於 Phase 2 可用。
6. **review verdict 的交付通道形態**：reviewer 經 Manager socket 回傳、或寫入
   per-job 的單向 spool 目錄（headless 可寫、不可讀不可改他人）？後者實作簡單但
   需要 append-only 語意支援。
7. **簽章路線的排程**：Phase 3 納入 0.2.x 的哪一個 minor，或延到 0.3？本票僅
   裁定其非 0.2.0 blocker。
8. **跨系統檔案契約的治理歸屬**：`<project_config_root>/project-hippo.yaml`
   （`monitor/registry.py:16-17`）由外部系統寫入、cortex 讀取，零認證。歸 D6
   邊界內（則 hippo 需改交付方式），或留作已知有界風險？
9. **多 instance 共用 HOME 的長期形態**：修掉 instance-scope 漏洞後，多個 instance
   是各自獨立 UID，或共用一個 Manager UID 但分樹？影響 Phase 2 的 UID 數量。
10. **WSL2 之外的目標環境**：若 fleet 未來遷到容器或雲主機，Phase 2 的 UID 方案
    是否直接沿用（容器內 UID 隔離＋唯讀掛載通常更便宜）？影響 Phase 2 的投入
    是否值得。
