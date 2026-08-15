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
