### Fixed
- **#661：實機四分部署 doctor 剩的兩個 FAIL——`review-sandbox` 與 `preflight`，同一個
  成因（job／服務需要的外部程式不在登記表上）**。

  **`review-sandbox`：`srt` 被單檔複製，而它是 npm 套件樹。** 實機把 `srt` 補進
  `/opt/cortex/toolchain/bin` 之後 `command -v` 找得到，但執行仍失敗。實測成因：
  `@anthropic-ai/sandbox-runtime` 的 `dist/cli.js` 是 ESM，第一行就
  `import { quote } from './utils/shell-quote.js'`，單檔複製後相對 import 解到
  `<toolchain>/bin/utils/…` ⇒ `ERR_MODULE_NOT_FOUND`、`srt --version` rc=1，doctor 的
  `dependency_commands` 因此報 `Claude sandbox dependency execution failed`。**第二個
  後果是無聲的**：`launcher._srt_runtime_root()` 從 `which("srt")` 往上找
  `name == "@anthropic-ai/sandbox-runtime"` 的 `package.json` 來解套件根、再加進 reviewer
  sandbox 政策的 `allowRead`，單檔形態下它解出 `None`——政策少一條放行且不報錯。修法與
  `codex` 同形：整包搬套件樹、`bin/srt` 是指進 `lib/` 的 symlink（實機以修正後的形狀複驗，
  probe 的 static 與 live 兩路皆 pass，`_srt_runtime_root()` 解得到套件根）。

  **`preflight`：`PSC_PREFLIGHT_CMD` 落定為 typed argv ＋ 部署 venv 模組。** 舊值
  `~/.local/bin/cortex-preflight-ci` 是 shell wrapper，它指向的 backend（另一個 repo 的
  `preflight.sh`）也在 `/home` 底下——`ProtectHome=yes` 之後兩層都不可達。**票上「整包搬
  進部署樹」這個選項的前提在查證中就過期了**：那支 shell script 的功能，
  `paulsha-conventions` 自 1.0.17 起已上游化為 typed-argv 的 python 模組
  `policy_check.preflight`。因此落點不是 toolchain（那是給自帶內容的獨立程式的），而是
  **既有的 root-owned 部署 venv**——新增 `paulsha_cortex.preflight_ci` 轉接器（cortex 契約
  → 引擎契約），backend 以 `pip install 'policy-check==<policy_version>'` 進同一個 venv
  （唯一相依 PyYAML 已在裡面）。引擎解析走 `--offline`＋已安裝發行版並由引擎驗
  `installed == policy_version`：**不走**「執行期 clone 引擎原始碼再執行它」那條路，因為
  那會在降權部署裡造出一塊服務帳號寫得到又執行得到的執行面（與 spec §R3 直接衝突）。
  版本一致性由 R-23（workflow pin ⟷ `policy_version`）遞移涵蓋 CI 那一半。

  **順帶修正票上一處前提**：舊值並不是被 `doctor.py` 的 `shell-wrapper-not-allowed` 擋下
  的——那個類別只在 argv 第一段真的是 `bash`／`sh` 且帶 `-c` 時才成立，一個
  `#!/usr/bin/env bash` 的腳本檔不落在裡面。實機報的是 `PSC_PREFLIGHT_CMD is required`
  （EnvironmentFile 根本沒設它），而把舊值填回去會得到 `executable-unavailable`。

### Changed
- **#661：登記表的外部程式盤點由「四個 executor」擴為完整名冊，但**刻意分成兩張表**。**
  `permgen` 新增 `SERVICE_TOOLS`（`srt`／`openspec`）與 `SYSTEM_PROGRAMS`
  （`node`／`git`／`gh`／`bwrap`／`socat`），落位計畫改由 `TOOLCHAIN_PROGRAMS`
  （＝`EXECUTOR_TOOLS ∪ SERVICE_TOOLS`）導出；`TOOLCHAIN_SYSTEM_RUNTIMES` 由寫死的
  `("node",)` 改為 `SYSTEM_PROGRAMS` 的導出值。**不直接擴充 `EXECUTOR_TOOLS` 的理由**：
  那張表同時是 dispatch 的 executor 名字判準（`executor_hardening_profile()` 對表外的名字
  fail-closed，spec §R8），把 `srt` 併進去等於讓 `executor: srt` 這種派工變成合法；有測試
  正面釘住「`SERVICE_TOOLS` 的名字仍必須被 `executor_hardening_profile()` 拒絕」。
  另有一條測試把 `doctor.REVIEW_SANDBOX_EXECUTABLES`（本 PR 由行內字面值提成常數）與登記表
  對照，讓「probe 要求的程式」與「登記表涵蓋的程式」不能再各走各的——#661 的實機症狀正是
  這條不成立。
- **#661：`openspec` 是同一族**第三個**沒被盤到的成員**（盤點時發現，非本票原始症狀）：
  它是 `@fission-ai/openspec` 的 node script，同樣住在 operator 的 nvm 樹底下，
  `ProtectHome=yes` 之後同樣不可達，而它是 ship 段 `archive`／preflight `validate` 的採信
  判準——版本直接決定一筆交付能不能被接受，因此與模型 CLI 同級進部署樹。
- **#661：runbook 兩項實機修正。**（1）第 2c 步來源樹建立補
  `git remote set-url origin <上游>`——從 operator 的 checkout clone 會讓 `origin` 指向本機
  路徑，兩個後果：doctor 的 `repo-identity` 判 drift，以及 #656 的 ship 段 `push origin`
  會把交付安靜地推進本機那棵樹。（2）第 4b 步 EnvironmentFile 模板移除五個顯式覆寫
  （`PSC_CONTROL_ROOT`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT`／`PSC_MONITOR_STATE_ROOT`／
  `PSC_RUN_ROOT`）——`PSC_CONTROL_ROOT` 的模板值 `control` 與 installer managed_env 的
  `control/<instance>` 不相等 ⇒ doctor 判 `managed-path-drift`；拿掉之後由
  `PSC_AGENTS_ROOT` 導出的值**逐字等於 `permgen.PathLayout.control_root`**，也就是登記表與
  unit `ReadWritePaths` 實際保護的那條路徑。**顯式列出反而讓解析結果與保護面分岔。**

### Added
- **#661：`permgen.node_execution_surfaces()`／`unresolved_node_execution_surfaces()`
  ——#643 剖面推導的盲區變成可列舉、不會靜默消失的東西。** #643 由
  `EXECUTOR_TOOLS.needs_node` 機械導出加固剖面，而那條推導唯一的輸入是 **executor 名**：
  它涵蓋不了「executor 在執行途中再 exec 出來的 node 程式」，也涵蓋不了 Manager 的 system
  unit。完整盤點正好撞出兩格——`srt` 由 `claude`（`strict` 剖面）exec、`openspec` 由 Manager
  unit exec，兩者目前都是 `MemoryDenyWriteExecute=yes`，而 #643 已在實機量到 V8 的
  `Runtime_CompileLazy` 在該項下直接崩。**本 PR 只讓它可列舉，不做裁決也不放寬任何一面**：
  這是 OS／systemd 層語意，本 repo 的測試環境沒有那個加固面，對應的測試**明確 skip 並寫明
  理由**（#638／#657 的教訓），實機量測步驟寫進 runbook 第 4e 步（`systemd-run` 帶該 unit
  的關鍵 property，附「量到才改、不得就地放寬」的處置規矩）。
