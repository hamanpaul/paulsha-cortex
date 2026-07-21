# Porcelain CLI UX 設計規格

- status: accepted
- 日期：2026-07-21
- 對應 issue：#85（epic #84）
- 來源研究：《強化 paulsha-cortex 的 CLI 最後一哩路研究計劃》（cortex deep research）

本規格凍結 porcelain CLI 七家族的命令詞彙、exit code 契約、`--json` schema 穩定策略與 request_id UX。B1–B9 各批次的實作範圍以本文件為準；與本文件衝突的實作視為規格違反，需先回到本文件修訂。

## 1. 目的與範圍

cortex 既有命令面（`fanout/tick/complete/slice-action/work/status/jobs/stat/…`）對內部模型準確，但對一般 CLI 使用者暴露了過多必須先理解的概念（systemd、specs、dispatch hold/auto、slice lifecycle）。本規格依 Git plumbing/porcelain 分層慣例，在**不動核心**的前提下新增高階任務導向命令面。

**不可動假設（non-goals）**：

1. 不改 control 檔案契約與 manager control plane。
2. 不改 spec/job/slice/work 資料模型與權責邊界。
3. 不改 daemon single-writer：porcelain 只能發 request 或呼叫既有 CLI/client，禁止直接寫 workflow 狀態檔。
4. 不新增 runtime 依賴（stdlib-only；`PyYAML` 為既有唯一例外）。
5. 既有低階命令全部保留，行為不變（porcelain 是新增層，不是替換）。

## 2. 設計原則

1. **任務導向動詞**：使用者說得出口的操作（bootstrap、run、recover），而不是內部模型名。
2. **雙軌輸出**：預設人類可讀摘要；所有命令支援 `--json`。腳本只准解析 `--json`，人類輸出不承諾穩定。
3. **可追溯**：所有高階輸出必須能回溯到底層 request/status/log 來源；porcelain 不得產生底層查不到的「合成狀態」。
4. **危險旗標不外露**：`--allow-unsafe` 等旁路旗標僅存在於低階命令；porcelain 不提供。專家永遠可以退回低階命令。
5. **失敗訊息帶下一步**：每個錯誤訊息附至少一個可執行的建議命令。

## 3. Exit code 契約

| code | 語意 | 適用 |
|---|---|---|
| 0 | 成功（terminal success） | 全家族 |
| 1 | terminal failure（操作完成但結果為失敗；或執行錯誤） | 全家族 |
| 2 | 用法錯誤（argparse 層） | 全家族 |
| 3 | accepted / pending（request 已受理但尚未 terminal；含 wait timeout） | request、run、recover |
| 4 | 環境不滿足（preflight 未過：缺 executor、systemd 不可用且無 fallback、路徑不合法…） | bootstrap、service、doctor 類 |

規則：exit 3 一律伴隨 `request_id` 輸出與追蹤提示；exit 4 一律列出「缺什麼、怎麼補」。

## 4. `--json` schema 穩定策略

沿用 repo 既有版本化慣例（`cortex-work/v1`、`cortex-doctor/v1`）：

1. 每個 `--json` 輸出頂層必含 `"schema": "cortex-porcelain/<family>/v1"`。
2. 演進只允許 additive（新增欄位）；語意變更或欄位移除必須 bump `/v2` 並保留 `/v1` 至少一個 MINOR 週期。
3. `--json` 時 stdout 僅含單一 JSON 文件；診斷訊息走 stderr。
4. 欄位命名 snake_case；時間一律 UTC ISO-8601。

## 5. request_id UX

現況：mutation request 由 CLI 寫入 control queue 後最多等 5 秒，逾時僅報錯，req_id 不外露。本規格將 request 顯性化：

1. 所有 mutation 提交（run/recover 家族）成功寫入 queue 後，**立即**輸出：

   ```
   request_id: req_<...>
   action: tick
   accepted: true
   status: pending
   hint: cortex request wait req_<...>   # 或 cortex request show req_<...>
   ```

2. 未帶 `--wait`：以 exit 3 結束（accepted-pending）。帶 `--wait [--timeout N]`（預設 120s）：輪詢 done 檔，成功 exit 0、terminal failure exit 1、timeout exit 3（並再次印出追蹤提示）。
3. `request` 家族為純唯讀（讀 `requests/`、`done/`、`status.json`、job registry），不經 control queue，daemon degraded 時仍可用。

## 6. 命令詞彙表（七家族）

### 6.1 `cortex request` — request 追蹤

| 命令 | 語意 | 資料來源 |
|---|---|---|
| `request list [--recent N]` | 最近 requests（pending + done） | `requests/`、`done/` |
| `request show <request_id>` | 型別、參數摘要、建立/完成時間、result/error、關聯 job/slice/work | done payload + job registry |
| `request wait <request_id> [--timeout N]` | 等待 terminal（exit 0/1/3） | done 檔輪詢 |
| `request logs <request_id>` | best-effort 聚合該 request 相關的 manager log / job log 片段 | manager.log、job logs |

設計決策：**不提供 `request submit`**。提交一律走語意化的 `run`/`recover` 命令；request 家族只負責追蹤。理由：generic submit 會繞過語意層驗證，且與 run 家族重複。

### 6.2 `cortex run` — 高階工作語意（映射既有 request types）

| 命令 | 映射 |
|---|---|
| `run tick [--specs-dir D] [--executor E --model M] [--review-executor E --review-model M] [--wait]` | `tick` request |
| `run fanout [...]`（參數同上，無 review） | `fanout` request |
| `run complete [--review-executor E --review-model M] [--wait]` | `complete` request |
| `run work <start\|resume\|auto\|ship\|…> <work_id> --repo R [...] [--wait]` | `work-action` request |

輸出一律含 request_id（第 5 節格式）。executor/model 省略時採 daemon 部署設定，並於輸出中回顯實際生效值。

### 6.3 `cortex inspect` — 統一唯讀檢視

| 命令 | 包裝 |
|---|---|
| `inspect status` | control `status.json`（含 daemon pid/idle/last_tick、ready/held/in_flight/recent_done） |
| `inspect job <job_id>` | `stat` |
| `inspect ready` | `ready` |
| `inspect work <work_id> [--repo R] [--explain]` | Monitor socket `work show` |
| `inspect doctor [--repo R]` | `doctor` |
| `inspect service` | service 模式（systemd/fallback/none）、unit 狀態、**運行中版本** |

全部唯讀、不經 control queue（`status.json` 直讀）。`inspect service` 必須顯示運行中程式版本與安裝來源，避免「daemon 跑過期快照無人察覺」。

### 6.4 `cortex service` — 服務生命週期

| 命令 | 行為 |
|---|---|
| `service install [--instance I] [--repo-root P] [--interval N]` | 包裝既有 installer（render/copy units、daemon-reload、enable） |
| `service start / stop / restart` | systemd：**service + timer 成對操作**；fallback：管理 daemon 行程 |
| `service status` | 模式、unit 狀態、pid、運行版本、env 摘要（executor/interval/specs-dir） |
| `service logs [--follow] [-n N]` | systemd：journalctl；fallback：log 檔 tail |
| `service uninstall [--purge]` | 停用並移除 units（`--purge` 才清 runtime env） |

契約：明確區分三種模式（systemd / fallback / 未安裝），禁止假成功——systemd 不可用時必須說明並指出 fallback 路徑。stop/start 必含 timer，避免「只停 service 被 timer 拉回」。

### 6.5 `cortex bootstrap` — 從乾淨機器到第一次成功

流程（每步失敗訊息附修法；`--dry-run` 只檢查不動作）：

1. 環境檢查：Python/Git/repo-root/executor CLI 在 PATH 且已登入（只報狀態，不代登入）→ 不滿足 exit 4。
2. `service install`（透傳 `--instance/--interval`）。
3. `--start`（預設開）：啟動 service+timer；systemd 不可用 → 說明 fallback。
4. 健檢：`inspect status` + `inspect doctor` 摘要。
5. 選配 `--sample <combo> --task "..."`：呼叫 init-sample（sample 失敗不影響 bootstrap 本身的成功判定，但明確標示）。
6. 輸出下一步指引（人類可讀；`--json` 為機器格式）。

### 6.6 `cortex recover` — 操作員恢復語彙

| 命令 | 映射 |
|---|---|
| `recover slice <slice_id> <retry-build\|retry-verify\|retry-review\|abandon> --actor A [--wait]` | `slice-action` |
| `recover work <work_id> <retry-build\|resume\|abandon> --repo R [--wait]` | `work-action` |
| `recover brokers reap [--apply]` | `reap-brokers` |
| `recover service restart` | `service restart` 別名 |

mutation 類輸出一律含 request_id。`--actor` 為必填（審計要求），不提供預設值。

### 6.7 `cortex init-sample` — 第一個 sample workflow

```
cortex init-sample --task "<描述>" [--combo feature-oneshot] [--change NAME]
```

包裝 `deck compile --emit`：產出 spec 一律 `dispatch: hold`，輸出 spec 路徑、必要的人工欄位清單、`deck verify` 檢核命令、與翻 auto 的說明。**絕不**自動翻 auto。

## 7. TUI 邊界契約

1. paulshaclaw（cockpit/TUI）只透過兩個穩定介面消費 cortex：porcelain `--json` 輸出、Monitor Unix socket read API。
2. TUI 的任何 mutation 一律經 porcelain 命令（或既有 control client request），**禁止**直接讀寫 cortex 內部狀態檔（jobs registry、delivery journal、spec frontmatter…）。
3. 相容性以 `--json` 的 schema 版本為準；TUI pin 最低 cortex 版本。
4. TUI 的實作與發佈不在本 repo 與 v0.1.0 範圍。

## 8. 實作限制

1. `paulsha_cortex/porcelain/` 子模組承載七家族；頂層 `cli.py` 路由表外掛化（B1），各家族自行登記。
2. stdlib-only；`test_zero_dependency_runtime` 必須續綠。
3. 每個家族附 unit tests（mock control client / 檔案 fixture），CI（`tests.yml`）自動涵蓋。
4. 新命令落地的同一 PR 必須同步 README 命令面章節與 CLI help（policy R-16）。

## 9. 名詞速查（供 B8 Concepts 文件展開）

`spec`（deck 產出的派工單，frontmatter 控制 hold/auto）→ `job`（一次 executor 執行）→ `slice`（工作切片，含 verify/review gate）→ `work`（跨 PR/issue 的統一生命週期 read model）。Manager daemon 是唯一 writer；Monitor 把多來源事實投影成 work read model。
