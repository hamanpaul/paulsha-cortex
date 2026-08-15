# monitor 設定檔載入規則

`paulsha_cortex.monitor.config.load_config` 提供兩種載入模式：

- `config_path` 明確傳入：
  - 只載入指定的 `project-cortex.yaml`。
  - 不會合併任何 ambient 的 `project-hippo.yaml`（完全顯式）。
- `config_path` 未傳入：
  - 依序使用 `PSC_MONITOR_CONFIG`、`PAULSHACLAW_CONFIG`、標準 `project-cortex.yaml` 或 legacy `paulshaclaw.yaml`。
  - legacy config 的警告仍會提醒，但每個 process 每種 legacy 路徑僅會提示一次，且不影響既有訊息內容與設定解析順序。
  - 合併對應路徑下可見的 `project-hippo.yaml`，以提供 ambient projects。

這個行為是為了讓「給定 explicit config」與「主機預設 ambient 設定」之間維持明確邊界：

- 在測試與腳本中指定 `config_path`，可避免意外引入外部環境專案清單。
- 未指定時仍保留既有監控預設行為，會套用 ambient `project-hippo.yaml`（若存在）。

## GitHub 掃描壓力設定（#506）

`_github_refresh_loop` 每 `github_refresh_interval_seconds` 會對每個 GitHub repo
跑一次 `GitHubWorkProvider` 與 `GitHubTerminalProvider`。repo 一多，一輪數百次
請求齊發就會觸發 GitHub 的 secondary（abuse detection）rate limit，兩個 provider
一起 degraded，連帶擋掉 `cortex work` 的 claim。

D2 之後（見下節）`GitHubTerminalProvider` 的 REST 呼叫數已降為每輪固定 2 次
（graphql PR 分頁 ＋ 1 次 git tree）；`GitHubWorkProvider` 仍是 O(issues 分頁)。

`monitor:` 區段新增下列鍵（全部可省略，預設值即保守值）：

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `github_request_interval_ms` | `200` | 每次 `gh` 請求前的固定間隔，`0` 代表完全停用節流 |
| `github_request_jitter_ms` | `100` | 疊加在間隔上的隨機抖動上限，`0` 代表不抖動 |
| `github_throttle_budget_seconds` | `120` | 單輪掃描花在節流的睡眠總上限；實際生效值另夾在 `github_refresh_interval_seconds` 的一半以下 |
| `github_backoff_base_seconds` | `60` | 命中 rate limit 後的退避基準（指數退避的第一階） |
| `github_backoff_max_seconds` | `1800` | 退避上限 |

預算計算：40 個 repo × 約 5 次呼叫／repo × 0.2s ≈ 40s，遠低於一輪的 300s。
預算用盡後節流自動失效——寧可讓該輪尾段恢復齊發，也不讓節流本身把掃描週期撐爆。

命中 403 時會再查一次 `gh api rate_limit`（此端點不計入配額）分辨兩種限流，
並寫入不同的 diagnostic：

- `github secondary rate limit`：配額還有剩，是 burst 觸發的 abuse detection。
- `github primary rate limit exhausted`：配額耗盡，只能等 reset。
- `github rate limit exceeded`：探測本身失敗時的保守退回值。

三者都仍會被 `paulsha_cortex.github_rate_limit.is_rate_limit_signal` 認得，
`coordinator/claim.py` 的 `provider-authority-rate-limited-canonical` 行為不變。
退避期間 provider 的 `scan()` 直接跳過、不發任何請求；退避窗綁的是 token
（帳號層級）而非單一 repo，因為 GitHub 的 secondary limit 本來就綁 token。

## git 的資料走 git：remote 讀取不吃 REST 配額（#506 / D2）

`GitHubTerminalProvider` 過去有兩類「每個……一次」的 REST 讀取，讀的都是本機
git checkout 本來就有的東西：

- 每個 remote `todo.md` / archived `tasks.md` 一次 `repos/{repo}/contents/...`
  （實測生產 workspace 一輪 **91 次**）
- 每個 workflow-linked merged PR 一次 `repos/{repo}/compare/{merge}...{default}`
  （判「merge commit 還在不在 default branch 上」）

兩者已改由 `paulsha_cortex.monitor.git_mirror.LocalGitMirror` 以本機 git 回答，
**一輪的 REST `contents` / `compare` 呼叫數固定為 0**。git 協定（fetch）不受
REST rate limit 管轄。

- **讀哪個 checkout**：`work_api` 把該 repo 在 workspace 的 canonical checkout
  （與 `RepoWorkProvider` 同一個 root）傳給 provider。
- **身分驗證**：`git config --get remote.origin.url` 必須解析成同一個
  `owner/name`（讀 raw config，不套 `url.*.insteadOf` 改寫），否則 fail closed。
- **fetch 頻率**：沿用既有 refresh 週期——一輪最多 fetch 一次，而且只在本機真的
  缺物件時才 fetch。refspec 一律寫進私有 namespace `refs/cortex/mirror/<hash>/*`
  並帶 `--refmap=`，不動 `refs/remotes/origin/*`、工作區與任何本地分支。
- **fail closed**：ref 不存在、fetch 失敗、blob 讀不到、shallow checkout 無法判
  ancestry——一律 degraded（diagnostic 前綴 `github terminal git mirror
  unavailable:`），由 `_retain_last_good` 保留上一份鏡像，**絕不**把讀不到當成
  「檔案不存在」或「不是 ancestor」。
- **provenance**：`github-terminal:` provider 的 observations 多一個 `remote_reads`
  欄位，記 transport、checkout 路徑、本輪 fetch 的 refspec、缺席物件、blob 讀取數
  與 ancestry 判定數。

## 事件入口（spool）：事件是 hint，不是 authority（#506 / D4）

D1–D3 把常態讀取壓下來的代價是**發現延遲**——fleet 自己剛動過的物件，也只能等下
一次輪詢把整個清單再問一遍。`paulsha_cortex.monitor.event_spool` 開一條本機通道：
別的行程把「我剛動了哪個 GitHub 物件」寫成一個事件檔，monitor 每輪消費它，對被
點名的物件做 targeted 條件驗證後才更新鏡像。

- **spool 位置**：`monitor_event_spool_root()`，預設 `<agents>/monitor/event-spool/`
  （隨 `PSC_MONITOR_STATE_ROOT` / `PSC_AGENTS_ROOT` 移動）。壞事件檔隔離到同層的
  `quarantine/`。目錄由**寫入端**建立——monitor 掃到目錄不存在就是「這台機器沒有
  事件 producer」，不是錯誤。
- **一事件一檔、原子寫入**：temp 檔（`.` 前綴，掃描端跳過）→ fsync → `os.replace`，
  0600。消費就是 per-file `unlink`，不需要鎖或 offset 檔。
- **fire-and-forget**：`EventSpool.emit()` 永不 raise，失敗只回 `None`。寫入端掛在
  別人的工作路徑上，spool 寫不進去不得影響工作本體。
- **事件不帶新狀態**：`github_object` 事件只說「哪個 repo 的哪個編號被動了」，鏡像
  只寫 GitHub 自己回的內容（`correlation` 的 inferred→confirmed 語彙）。
- **targeted 驗證**：單物件 `repos/{repo}/issues/{number}`，帶 per-object ETag 的
  條件請求（304 不計配額）。驗不到就不寫鏡像、不消費事件，留給每日 anti-entropy。
  targeted 讀取**不推進** `since` 游標。
- **上限**：預設一輪最多驗 20 個物件，超出的留到下一輪（依 `emitted_at` FIFO）。
- **記帳**：`github:` provider 的 observations 多一個 `event_spool` 欄位（未接 spool
  時不出現）。
- **其他事件型別**：`steering` / `job`（#498）與未知型別一律**原地保留只記 log**，
  等各自的 consumer 落地。

### producer：headless job 的 hook（#506 / D5）

spool 目前唯一的 producer 是 headless **claude builder** job 的 PostToolUse hook
（`cortex headless-hook post-tool-use`，實作在
`paulsha_cortex/porcelain/headless_hook.py`）。job 每跑完一次 `Bash` 工具，hook 就
從命令解析出被動過的 GitHub 物件並寫一則 `github_object` 事件。

- **只在 headless 觸發（使用者硬約束：不得影響正常互動 agent）**，兩道獨立保證：
  1. hook 宣告由 `SubprocessLauncher.launch()` 每次現場組出，經 argv 的
     `--settings` 只交給該 job 的行程，**從不寫入任何檔案**——尤其不寫
     `~/.claude/settings.json`。互動 session 讀 operator 自己的設定，那裡沒有這個
     hook。打包的使用者全域模板 `scripts/hooks/claude.json` 刻意不含它。
  2. 寫入端以 `PSC_JOB_ID` 自守：讀不到就直接返回，不建目錄、不寫檔、不起
     subprocess、不解析命令。這個變數只由 launcher 為派工的 job 注入。
- **只有 builder 掛**：read-only planner（`--tools ""`，沒有 Bash）與 review-only
  reviewer（read-only 契約）都不注入。
- **只認會改狀態的命令**：封閉列舉的 `gh issue`／`gh pr` 動詞，以及非 GET/HEAD 的
  `gh api` 單物件路徑。命令沒帶 `--repo` 時從 job worktree 的 `origin` 補；補不到
  就丟掉這則 hint（漏報退回輪詢週期，是安全的方向）。
- **fire-and-forget**：解析／寫入的任何失敗都只記 debug log，CLI 一律 exit 0 且
  stdout 保持空，注入的命令另以 `|| true` 與 `timeout` 兜底——hook 不得讓 job 看到
  非零 exit，也不得阻塞它。
- **心跳預留**：每則事件都帶 `job_id`，供 #536／#488 的心跳 consumer 日後消費；
  本次不發 `job` 型別事件。
- codex 免 hook（`codex exec --json` 的 JSONL 已被 parse）；copilot／agy 未接。
