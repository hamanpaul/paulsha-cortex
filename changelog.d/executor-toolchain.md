### Added
- **#640 / trust-root Phase 2b：真實 dispatch 的最後一哩——executor toolchain 與
  per-account 憑證進登記表**——#623 那一族的第五個缺口，比前四個都靠後：前四個解完之後，
  dispatch 會一路走到**呼叫模型**那一步才失敗。job unit 帶 `ProtectHome=yes`，而四個
  executor 原本全在 operator 的 HOME 底下（nvm 樹與 `~/.local/bin`），實測
  `sudo -u cortex-builder env HOME=<job HOME> codex exec --help` →
  `/usr/bin/env: ‘node’: No such file or directory`，rc=127；系統層完全沒有 node，
  而登記表對 toolchain 與 job 帳號憑證**都沒有預留資產**，因此 permgen 也不會產生它們
  的權限。0817 裁決落地為兩個新資產：
  - **`executor-toolchain`**（`<deploy_root>/toolchain`，root-owned 0755，全部 job／
    服務帳號**唯讀＋可執行**）——`node` 走**系統層**（通用 runtime，換版本幾乎不影響
    產出）；四個模型 CLI（`codex`／`claude`／`copilot`／`agy`）落進部署樹，因為「job
    跑的是哪個版本的模型 CLI」**會**影響產出，那必須是**可稽核的部署決定**而不是跟著
    operator 的環境漂移。這不是假設——實機盤點在同一台機器上就有兩份 `codex`（系統層
    0.42.0 vs operator 實際在用的 0.147.0，差 100 個以上小版本），因此安裝來源一律取
    operator 實際在用的那一份，**不**另外 `npm install -g`。四者的實體形態不同、搬移
    方式不能一概而論，固化為 `permgen.EXECUTOR_TOOLS`：`codex` 是 node script（**唯一
    硬需要 node**，且必須整包搬 npm 套件樹）、`claude`／`agy` 自帶原生執行檔、`copilot`
    是 shell script——**因此系統層 node 的版本風險只涵蓋 `codex` 一個**。機械落點是
    `owner_class=DEPLOYMENT`，`ProtectSystem=strict` 下 `/opt` 唯讀只擋寫入不擋執行，
    故本資產機械地不出現在任何 unit 的 `ReadWritePaths` 上。
  - **`builder-executor-credential`**（預設 `<job HOME>/.codex/auth.json`，**檔案由 job
    帳號擁有** 0600、**放它的目錄維持 root-owned** 0755）——job 因此能就地改寫自己那份
    憑證的內容（refresh 過期 token），卻**建不了新檔、刪不掉、也換不掉**同目錄下的其他
    root-owned 檔（`codex-hooks` 就住在同一層）：增／刪／換要的是**目錄**的寫入權。
    `ReadWritePaths` 因此只掛**憑證檔本身**而非父目錄（新的
    `permgen.IN_PLACE_CONTENT_WRITE_ASSETS` 例外），讓「目錄 root-owned」在檔案系統與
    systemd mount 兩層同時成立。已知限制（裁決刻意接受）：以「暫存檔 ＋ rename 原子
    替換」refresh 的 CLI 會失敗，只有就地覆寫走得通。落點由新的部署決定欄位
    `PathLayout.executor_credential_relpath` 導出（形狀在建構當下即驗），骨架目錄的
    root-owned 保護因此**跟著它走**而不是寫死 `.codex`；`scaffold_directories()` 已為
    **每一個** job 帳號建出該父目錄，機制是 per-account 的，登記表只掛 `cortex-builder`
    一份（與 `codex-hooks` 逐條同構——它是兩個 scheme 都相同、且目前唯一真的以模板 unit
    降權起 job 的帳號；登記第二份會讓 Manager unit 的 `ReadWritePaths` 出現一條二分
    部署裡不存在的 HOME 路徑而使 unit 起不來）。
- **`trust_root toolchain` CLI verb ＋ `permgen.build_toolchain_plan()`**——toolchain 的
  **落位**步驟（逐支 CLI 的形態、搬移方式、來源判準、統一收權、`PSC_BUILDER_PATH` 的
  正規值）由產生器出，runbook 不手寫；與 `shim`／`gitconfig` 同一個定位（權限那一半
  仍由登記表經 `plan_to_commands()` 產出）。

### Changed
- **job 的 `PATH` 沿用既有的 `PSC_BUILDER_PATH`，並由 runbook 從「選配」改為「必填」**
  ——`PathLayout.job_path_value()` 給出正規值（`<toolchain>/bin` **排最前面**，尾段是
  系統層 `/usr/local/bin:/usr/bin:/bin`，不含任何 `sbin`）。**刻意不在模板 unit 裡寫
  `Environment=PATH=`**：模板 unit 的 `ExecStart` 是 root-owned shim，shim 以
  `execvpe(argv[0], argv, spec['env'])` 整份換掉環境，job 解析命令用的 `PATH` 來自
  **spec 的 env**（即 Manager 端這個變數）——寫在 unit 上只會是一個看起來承載作用、
  實際被 shim 丟掉的設定。toolchain 必須排最前面，否則系統層那份舊版會蓋掉它，而症狀是
  「跑得起來但版本不是預期的那個」，比 `command not found` 難查得多。取捨連同理由寫進
  產生出來的 job unit 註解裡。
- **spec §R1 明載一個誠實限制**：把 operator 的憑證複製給 job 帳號，代表 job 用的是
  **同一個 provider 帳號**。三分買到的是**檔案系統層**的隔離（job 偷不到 Manager 的
  token、改不了 Manager 的 state、讀不到另一個 job 帳號的憑證），**不是** provider 層
  的獨立——與 `independence_domain`（§R5／§R8 的 anti-collusion 控制，由 Manager 派工時
  決定並寫入 registry）**不是同一件事**，兩者 MUST NOT 互相當作證據。真正的 provider
  層獨立需要每個 job 帳號各自的 provider 帳號，屬未來選項。
- **Phase 2b runbook 新增第 4e 步**「executor toolchain 落位 ＋ per-account 憑證」
  （4 個 sudo 點、9 個驗證點）：系統層 node（版本本身是部署決定，某個 CLI 提高下限時要
  一併升）、逐支 CLI 的搬移方式、**以 job 帳號實跑一次 `--help` 期望 rc=0**、
  **版本與 operator 側逐字相同**（只驗 rc=0 不夠——系統層那份舊的一樣 rc=0）、在真實
  加固面下以 `systemd-run` 複跑一次（`MemoryDenyWriteExecute=yes` 對 node 的 V8 是第一
  嫌疑），以及憑證「能改內容／建不了新檔／刪不掉／換不掉鄰居」的反向不變式。5-5 的
  `PSC_BUILDER_PATH` 與「builder 自己 `login`」兩段一併改寫，附錄 A 補兩條漂移自我檢查
  → 合計 **49** 個 sudo 點、**174** 個驗證點。
