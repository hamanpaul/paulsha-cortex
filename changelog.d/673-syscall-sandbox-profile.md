# seccomp 過濾**語意**是剖面之外的第二個維度；加固面複本改為全量機械導出（#673）

## 先講量測結論：本票的因果診斷不成立，production 沒有壞

#673 的診斷是「`SystemCallFilter=@system-service` 讓 `codex`／`copilot` 在全部八份
unit 下 rc=1，需要放行 `@sandbox`（`landlock_*`／`seccomp`）」。實機逐條量完，這條
推論的**每一步**都被推翻：

| 命題 | 實機結果 |
|---|---|
| 被過濾的是 `@sandbox` 那四支 | **否**。是 **`pkey_alloc`**（x86_64 syscall 330，systemd 歸在 `@pkey`）。kernel audit `type=1326 … comm="node" sig=31 syscall=330 code=0x80000000` 直接證據，全程**沒有任何一筆** `landlock_*`（444–446）或 `seccomp`（317）的 record |
| 加 `@sandbox` 可以修好 | **否**。在致命過濾語意下加了 `@sandbox`，`codex`／`copilot` 照樣 rc=1、輸出全空 |
| 真 unit 上 codex／copilot 是壞的 | **否**。以**完整 37 條** property 複製 `cortex-reviewer-job-jit@.service`（＝它們實際跑的剖面），四支 executor **全部 rc=0**；`codex debug models`、`copilot --help`、`openspec`、`srt` 亦全部 rc=0 |
| `DEFAULT_EXECUTOR = "copilot"` 讓預設派工路徑靜默壞掉 | **否**，同上。該疑慮撤銷 |

真正發生的事：`@system-service` 確實不含 `pkey_alloc`（V8 啟動時會叫它），但**同一份
unit 上的 `SystemCallErrorNumber=EPERM`** 把「被過濾 ⇒ `SECCOMP_RET_KILL_PROCESS`」
變成「被過濾 ⇒ 回 `EPERM`」，V8 收到 `EPERM` 走 fallback，一切正常。那一行從 permgen
的**第一個** commit 就在加固表裡，八份 unit 全部都有。

**本票的 repro 是一份手抄的十 property 複本**，它抄了 `SystemCallFilter=` 卻**漏抄**
`SystemCallErrorNumber=EPERM` ⇒ 複本落回 systemd 預設的致命語意 ⇒ **比 production
更嚴格** ⇒ 量出一個 production 沒有的 rc=1。逐字重現該複本可得本票矩陣；**只**補上
那一條，四支 executor 立刻全綠。

#638（單 UID 讓 ACL 斷言真空）、#657、本票是同一族事故的第一、二、三次。
**本票的方向是假紅，前兩次是假綠——手抄子集的錯法不由人選。**

## 因此**沒有**放寬任何 syscall

`SystemCallFilter=@system-service` 在八份 unit 上逐字不動。沿用 #643 的先例
「量到才改，改的是一份具名剖面，不是全域放寬」——而這次量到的結論是**不用改**。
放行 `pkey_alloc`／`@pkey`／`@sandbox` 都是無量測支撐的放寬：`EPERM` **不放行任何
syscall**，被擋的照樣擋，只是不殺行程。（若哪天真的需要，最小必要集合已量出來是
**單一具名 syscall `pkey_alloc`**，連 `@pkey` 整群都不必——但今天不需要。）

## 改的是「這條為什麼不能被靜默拿掉」

`SystemCallErrorNumber=EPERM` 是**承重**的，但在此之前沒有任何東西知道它承重：加固表
裡它的理由只寫「失敗可觀測」。任何人為了「更 fail-closed」把它刪掉或清空，`codex`／
`copilot`／`srt`／`openspec` 會在**全部**執行面上同時靜默死，而 diff 看起來只是
「移除一個放寬」。

- **`ToolchainProgram.filtered_syscalls`**（新欄位）：該程式在 `@system-service` 下
  **實機量到**會撞上的被過濾 syscall。`codex`／`copilot`／`srt`／`openspec` 各填
  `("pkey_alloc",)`，每一列都有 audit record 背書，不填形態推論。
  **刻意不共用 `needs_node`**：兩者處置方向相反（換一份放寬的剖面 vs. 鎖定一個所有
  剖面都不得分岔的鍵），適用面也不同——`openspec` 跑在 **Manager unit** 上，那一格
  根本沒有剖面，綁在剖面上會整個漏掉它。
- **`SECCOMP_FATALITY_KEY` ／ `PROFILE_LOCKED_KEYS`**：`SystemCallFilter` 與
  `SystemCallErrorNumber` 列為**任何剖面都不得分岔**的鎖定鍵，與
  `PROFILE_DIVERGENCE_KEYS` 恆為互斥（import 時強制）。
- **`filtered_syscall_surfaces()`**：程式 × 它實際跑的加固面，由
  `TOOLCHAIN_PROGRAMS` 機械導出（executor 走自己的剖面、非 executor 走 `consumed_by`
  指到的消費者面），**不另立第二張人工對照表**。
- **`_validate_seccomp_tolerance()`**：import 時強制——任何 `filtered_syscalls` 非空
  的程式，其執行面若是致命過濾語意，**permgen 直接炸**，並在錯誤訊息裡把處置講清楚
  （設 errno，**不是**把 syscall 加進白名單）。
- **加固表的註解改走既有的 `_wrap_comment`**：那支 helper 的 docstring 早就寫著
  「數百字元的單行註解會讓 `systemctl cat` 完全失去可審查性」，但 `_hardening_lines`
  一直是 `f"# {why}"` 直出。「這一行為什麼在這裡」正是這份 root-owned 檔存在的理由，
  讀不了等於沒有。指令值一字未改，純粹是 unit 的呈現。兩個既有測試（manager／monitor
  的「每項加固都要帶註解」）改為比對 directive 上方**整段連續註解**——比原本的「正上方
  那一行」更強，也對折行穩健。

## 更重要的一半：加固面複本必須全量機械導出

- **`permgen.unit_replica_properties()`** ＋ CLI **`trust_root unit-replica <unit|->`**：
  把一份**已落檔**的 unit 讀成 `systemd-run --property=` 的**完整**清單。契約是
  **「全帶，不選」**——`[Service]` 段除執行面指令（`ExecStart=` 之類）外全部帶出，
  含 `User=`／`Group=`／`WorkingDirectory=`／`Environment=`／`ReadWritePaths=`。
  日後往加固表加一項，runbook 不必改也不會漏，這正是它與 grep 白名單的差別。
- **落檔的 unit 少任一加固鍵 ⇒ `UnitReplicaDriftError`，stdout 保持空**。半套清單就是
  把 #673 再演一次，因此寧可不產出。展不開的 systemd specifier 同樣拒絕。
- 讀的是**磁碟上那一份**（`systemctl cat` 含 drop-in），不是產生器展開的——兩者會漂移，
  而漂移正是要被驗出來的東西（runbook 自己就記著「產生器修好 ≠ 已落檔的 unit 跟著更新」）。

## runbook（`trust-root-phase2b-setup.md`）

- 新增 **4e「共用探針 `psc_run_under`」**一節，之後每一條「在真實加固面下」的驗證都走它。
  原本 4e 的三條、5-3 的 `pytest`、5-4 的 `gh` 各自手抄**四條** property；5-2b 稍好但
  是一份**手維護的 27 鍵 grep 白名單**，且不含 `ReadWritePaths=`／`WorkingDirectory=`／
  per-account `Environment=`。全部改為機械導出。
- **5-2b 由「正向四段」改為全矩陣**：4 executor × 2 剖面 × 2 角色 unit 逐格量，並新增
  **反向對照**（`claude`／`agy` 在 jit 剖面下仍須 rc=0——放寬剖面若弄壞原本好的兩支，
  這是唯一看得見的地方）。通過條件表一併更正措辭：該宣稱現在是在**完整導出**的
  property 集合下取得的。
- 負向那兩格新增「**失敗原因要對得上**」：strict 剖面下 `codex` 的 stderr 必須是 V8 的
  `Check failed: 12 == (*__errno_location ())` ／ `Runtime_CompileLazy`。
  **stderr 空是另一回事**——那代表複本漏了 `SystemCallErrorNumber=EPERM`，是本票誤判的
  指紋。看到空 stderr 先回去查複本，不要開票說 executor 壞了。
- 兩個「弱複本從來看不到、全量複本立刻撞到」的實例寫進 runbook：
  (i) `ReadWritePaths=…/%i` 的目標不存在時 systemd 在 exec **前**就 rc=226、輸出全空；
  (ii) `PrivateTmp=yes` 讓 `/tmp` 下的探測目錄在 unit 內根本不存在——5-3 的 `pytest`
  探針因此改放進 gate 自己登記表上的可寫面。

## 測試

新增 `tests/test_trust_root_syscall_profile_673.py`（26 測試）：八份 unit 的
`SystemCallFilter` 逐字比對與「`@sandbox`／`@pkey`／`pkey_alloc` 不得出現在任何指令值上」、
剖面導出（executor → 剖面 → filter 值）、`filtered_syscalls` 的機械導出涵蓋 Manager 面、
鎖定鍵不變式、`unit_replica_properties` 的八份 unit round-trip 與漂移拒絕。

**守衛測試不可省**：把加固表的 `SystemCallErrorNumber` 清空後
`_validate_seccomp_tolerance()` **必須拋**，且訊息要同時點名 `codex` 與 `openspec`
——否則第 3／4 節的綠只是恰好成立。

**OS 層語意具名 skip ＋ 完整理由**（#638／#657 的教訓、PR #671 的做法）：「`@system-service`
是否真的擋掉 `pkey_alloc`」需要 kernel audit，「executor × 剖面全矩陣」需要真 UID ＋
真 ACL ＋ 已落檔的 unit。CI 是單 UID／無 systemd，套不上 seccomp，在那裡跑會**永遠綠**
——而那個綠恰恰是本票誤判的成因。

## 刻意不做

- **Manager／monitor unit 不放寬**（連帶：任何 unit 都不放寬）。詳見 PR body 的裁決理由。
- **不修 Manager 在行程內跑模型 CLI**（那是 #672 的主訴）。
- **`openspec` 在 Manager 面下仍失敗**（MDWE，非 seccomp），維持 #661 的未決狀態；本次
  只把它的量測方法修正並在 runbook 上記清楚，不順手放寬 Manager 的 MDWE。
