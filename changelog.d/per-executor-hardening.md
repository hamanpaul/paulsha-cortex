### Added
- **#643 / trust-root Phase 2b：per-executor 加固剖面——`MemoryDenyWriteExecute` 與
  node 型 executor 的互斥，只讓需要的那一類付代價**——#640／#642 落地後做實機驗證時
  測出來的：**加固面本身與 toolchain 相衝**。以 `cortex-builder` 身分在真實加固面下
  逐項隔離（`systemd-run` 一次只加一個 property），結果是無加固時 `node` 正常、
  `+MemoryDenyWriteExecute=yes` 時 V8 直接崩在 `v8::internal::Runtime_CompileLazy`，
  其餘每一項（`ProtectSystem=strict`／`PrivateTmp`／`RestrictNamespaces`／
  `SystemCallFilter=@system-service` ＋ `SystemCallErrorNumber=EPERM`）單獨加上去
  `node` 都正常——**唯一的阻斷點就是它**，V8 的 JIT 必須有 W+X 記憶體。影響面是四個
  executor 掛掉兩個（`codex` node script、`copilot` shell → node 皆空輸出；`claude`／
  `agy` 原生 ELF 正常），而預設的 `PSC_MANAGER_EXECUTOR=codex` 正是掛掉那一個。
  operator 裁決走**方向 2（per-executor 剖面）**：
  - **兩份 job 模板 unit，共用同一張 `_HARDENING` 表**——`cortex-job@.service`
    （`strict`，完整 27 項，給原生 ELF）與 `cortex-job-jit@.service`（`jit`，給 node
    型）。兩份**不是**兩段複製貼上的加固段：`permgen.HARDENING_PROFILES` 只帶
    `overrides`，`_hardening_lines()` 現場套用，因此日後往加固表加一項時兩份自動同時
    拿到。分岔面由 `permgen.PROFILE_DIVERGENCE_KEYS`（目前＝`{MemoryDenyWriteExecute}`）
    結構性框住，覆寫一個不存在的鍵、或覆寫白名單以外的鍵，在 **import 時**就是
    `ValueError`——「一個看起來有效、實際毫無作用的覆寫」不可能悄悄存在。
  - **剖面由 executor 決定，且 job 選不到**（這是本設計全部的價值，做不到就退化成
    「全域移除 MDWE」）。四道守法：(1) 對應表由既有的 `permgen.EXECUTOR_TOOLS` 的
    `needs_node` **機械導出**，不另立第二張清單；(2) 唯一的輸入是 `executor`，而它是
    Manager 的 dispatch 決定（`SubprocessLauncher(executor=...)`，在任何 per-job 產物
    之前就固定），`prepare_systemd_template()` 的 `executor` 參數**必填無預設**；
    (3) job spec **結構性禁止**攜帶剖面欄位（`hardening_profile`／`profile`／`template`
    ／`template_unit`／`unit_suffix`／`MemoryDenyWriteExecute` 全進
    `SPEC_FORBIDDEN_KEYS`，與「身分欄位不入 spec」同一條原則，寫端與讀端各掃一次、
    且掃的是新抽出的**同一支** `forbidden_spec_keys()`）；(4) 部署 config
    `PSC_JOB_TEMPLATE_UNIT` 只接受**基底**模板名，帶剖面後綴的值一律拒——否則一行
    config 就能把所有 job 推到寬鬆剖面。
  - **未知 executor fail-closed**：`resolve_hardening_profile()` 不回傳任何剖面。方向
    刻意不是「不確定就給嚴格的」——那會讓一個未被盤點過的 node 型 CLI 在真實加固面下
    靜默起不來（症狀是空輸出，離原因很遠，#643 本身就是這樣被埋掉的）；也絕不是
    「不確定就給寬鬆的」（那等於沒做）。唯一正確的行為是要求它先進 `EXECUTOR_TOOLS`。
  - **polkit 用同一條規則、同一個 YES 出口**：unit pattern 的字幹段改為**列舉的交替**
    （`^(?:cortex-job|cortex-job-jit)@[a-z0-9][a-z0-9._-]{0,62}\.service$`），由
    `HARDENING_PROFILES` 機械導出。放行面從「一個具名模板」變成「兩個具名模板」，
    **不是**「任意 unit」：前後仍錨定、instance 段字元類一字未改。5-7 的反向測試
    （transient 五形式、名稱前後綴混淆）對新字幹逐條同樣成立，並新增圍繞 `-jit` 的
    十種混淆形式。
- **`trust_root unit --job --profile strict|jit` CLI 旗標**：剖面只對 job 模板有意義，
  用在 `--manager`／`--monitor` 上會直接拒絕（靜默忽略會產出一份與旗標不符的內容）。
  `trust_root toolchain` 的輸出也逐支列出該 executor 的剖面與對應 unit 名。

### Fixed
- **`CollectMode` 放錯 section 的落檔後檢查**——產生器側的修正（由 `[Service]` 搬到
  `[Unit]`）已由 **#645 附帶完成**，本 PR 不重複；#643 補的是它缺的另外兩半：
  (a) runbook 第 5-2 步新增 `systemd-analyze verify | grep -i "unknown key"` 的落檔後
  檢查與 `systemctl reset-failed` 清理——**舊部署落檔的 unit 不會因為產生器修好就自己
  更新**，殘骸仍掛在 `systemctl list-units --failed` 上擋住同名 instance 的下一次
  start；(b) 測試把這條守衛擴到**兩份**剖面（新增的 `cortex-job-jit@.service` 若是複製
  貼上來的就可能複製到舊的放法），並改以「真正的指令行」判段而非字串 split——產生出來
  的註解本身就含有 `[Unit]`／`[Service]` 字樣（在解釋這個坑），naive split 會切錯。

### Changed
- **`permgen.EXECUTOR_TOOLS` 的 `copilot.needs_node` 由 `False` 改為 `True`**——#640
  落表時只知道它是 shell script、還沒查它內部 exec 什麼（表上的 note 當時就寫著
  「安裝時務必 `head -n 20` 查一次它實際 exec 什麼」）。#643 在真實加固面下量到
  `copilot --version` 在 `MemoryDenyWriteExecute=yes` 下**空輸出**、拿掉即正常，與
  `codex` 的症狀逐字相同——它內部 exec 的就是 node。因此「系統層 node 的版本風險只
  涵蓋 `codex` 一個」這句話在 spec／runbook／表註解三處同步改為**涵蓋 `codex` 與
  `copilot` 兩個**。把量到的事實回填既有那張表，而不是為剖面另開一張。
- **spec §R3 新增「per-executor 加固剖面」段，並明載誠實的取捨**：走 `jit` 剖面的 job
  **失去 `MemoryDenyWriteExecute` 這一層**——取得任意程式碼執行的攻擊者可在該 job 自己
  的位址空間內配置 W+X 記憶體，JIT 型 shellcode 在此可行。**沒有失去的部分同樣寫明**：
  其餘 26 項逐項不變（`NoNewPrivileges`／`CapabilityBoundingSet` 空／
  `ProtectSystem=strict`／`RestrictNamespaces`／`SystemCallFilter`…），`User=` 一樣寫死
  在 root-owned unit 檔裡——W+X 只讓攻擊者在**自己這個 UID** 內執行程式碼，跨 UID／
  跨檔案系統／提權那幾層完全沒有鬆動，而 §R2／§R3 保護的 Tier-0／Tier-1 資產靠的正是
  後者。**換到的是**保住 `codex`／`copilot` 兩個 provider，即 §R5／§R8 的
  `independence_domain` 仍有可選空間（方向 3「只用原生 ELF」會讓 build 域可選空間當場
  減半；方向 1「全域移除」則讓不需要放寬的 `claude`／`agy` 一起失去該層）。spec 因此
  **明文禁止**把本系統敘述成「所有 job 都有完整加固」，準確敘述是「原生 ELF executor
  的 job 有 27 項；node 型的有 26 項，少的那一項是 `MemoryDenyWriteExecute`」，並在
  風險表補兩列（被讀成完整加固／剖面被改成可由呼叫端選擇）。同時明載退出條件：若某天
  node 型 executor 能在無 W+X 下執行（V8 jitless、或 CLI 改原生編譯），`jit` 剖面
  SHALL 被移除而非長期保留。
- **Phase 2b runbook 第 5-2 步改為落兩份 unit ＋ 新增第 5-2b 步**「在**真實加固面下**
  驗證兩種剖面」——形態比照 #640 的第 4e 步，且**含負向對照**：node 型 executor 在
  `strict` 剖面下**必須失敗**。只驗寬鬆環境的 `--version` 會整個溜過去（四支在
  `sudo -u` 下全部 rc=0、版本全部相符，而其中兩支在真實加固面下是空輸出）；只驗 `jit`
  剖面成功也證明不了剖面分岔是必要的。5-2 另加一條「兩份 unit 的差異必須恰好兩行」的
  落檔前 gate，5-7 新增第 12 條（config 選不了剖面／未知 executor fail-closed／spec 帶
  剖面欄位被讀端拒），並讓 (5)(7)(8)(9)(10) 對兩個字幹各跑一次
  → 合計 **50** 個 sudo 點、**184** 個驗證點。
