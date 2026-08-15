# D3：GitHub issues 增量同步——`state=all&since=` ＋ ETag 條件請求

## 問題

`GitHubWorkProvider` 每輪對**每個** configured repo 全量分頁抓 issues
（`issues?state=all&per_page=100` ＋ `--paginate`）。D2（PR #566）把 `contents`／
`compare` 歸零之後，這是 monitor 對 REST 配額剩下的主要常態消耗：configured repos
約 13 個、`github_refresh_interval_seconds` 預設 300s（每日 288 輪），每輪每 repo
至少 1 次計費請求，其中絕大多數回應與上一輪逐位元組相同。

## 改法

新增 `paulsha_cortex/monitor/github_issue_sync.py`，是增量協定與 per-repo durable
狀態（游標／ETag／鏡像投影）的唯一入口：

- **`state=all` 不可退讓**：`state=open&since=` 看不到剛被關閉的 issue，closure
  reducer 因此拿不到 `closed` 證據，manager 可能 auto-claim 一件人類已經在網頁端
  關掉的工作。closed issue 的 `updated_at` 會隨關閉事件更新，`state=all&since=`
  的增量天然攜帶關閉事件且 delta 極小。
- **`sort=updated&direction=desc` 不可退讓**：預設排序是 `created` desc，在那個
  順序下「一個舊 issue 剛被更新」可能落在第 2 頁而**不改變第 1 頁**——第 1 頁的
  ETag 就不再是整個 delta 的變更偵測器，條件請求會漏發。改成 updated desc 後，
  任何 `updated_at` 前進的 issue 必然跳到第 1 筆。
- **ETag 條件請求**：第 1 頁帶 `If-None-Match`；**304 不計入 rate limit 配額**
  （實測 `x-ratelimit-used` 在條件請求前後不變）。ETag 綁定它所屬的 request path
  （`etag_request`），`since` 一前進 path 就變、舊 ETag 立刻作廢。304 一路**不**
  取回應的 ETag——GitHub 的 304 回強形式 `"<hash>"`、200 回 `W/"<hash>"`，覆蓋
  回去會讓往後的條件請求永遠落空而悄悄退化成每輪全額計費。
- **游標紀律**：`since` 取自**回應**中最大的 `updated_at`（不是本機時鐘），只在
  整輪完整成功後推進，且永不倒退；分頁中斷時游標／ETag／鏡像三者原封不動。
- **每日一次全量 anti-entropy**：增量看不到 issue 被刪除／transfer 這類不留
  `updated_at` 痕跡的事件。每 86400s 強制一次不帶 `since`、不帶 `If-None-Match`
  的全量重讀並與鏡像對帳，drift 一律**以全量為準**，同時記 log 與
  `observations["issue_sync"]["drift"]`。
- **fail closed**：durable 狀態缺失／損壞／游標格式不合／entries 形狀不對／ETag
  與 path 失聯——一律退回全量重建，絕不拿半壞的游標去做增量。單一 repo 的紀錄
  壞掉不會拖垮其他 repo。

分頁改為本地依 Link header 逐頁重建，**不跟隨**伺服器給的絕對 URL（跟隨等於讓
對方指定 `gh` 把 token 送去哪）；連帶讓每一頁都經過 `GitHubPressureGate.throttle`
——改動前 `--paginate` 是 gh 在行程內自己連發，閘門完全管不到那些請求。

`observations["auto_label_issues"]`（D1）改由 durable 鏡像導出，因此網頁端的關閉
事件一進增量，該 issue 就在**同一個** refresh 週期內退出 auto 派工名單。

## 每輪 API 呼叫數對比（13 repos、每日 288 輪）

| | 改動前 | 改動後（穩態） |
| --- | --- | --- |
| 每輪每 repo 計費請求 | ≥ 1（issue 數過 100 再按頁加倍） | 0（免費 304） |
| 每日計費請求合計 | 13 × 288 = **3744** | **26**（每 repo：1 次 anti-entropy 全量 ＋ 1 次無法沿用 ETag 的增量） |

## 範圍

只動 monitor 的 issues 讀取路徑。D2 的 `monitor/git_mirror.py` 未動；寫入路徑、
label API、events API 均不在本次。

新增 `tests/test_monitor_incremental_issue_sync_506.py`（34 個測試，含六項驗收與
量化對比樁）。
