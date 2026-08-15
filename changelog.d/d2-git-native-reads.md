# D2：git 的資料走 git——monitor 的 remote 讀取移出 GitHub REST

## 問題

`GitHubTerminalProvider` 一輪掃描對 GitHub REST 發出兩類「每個……一次」的讀取，
讀的全是本機 git checkout 本來就有（或 `git fetch` 一次就有）的東西：

- **`contents`**：每個 remote `todo.md` / archived `tasks.md` 各一次。實測生產
  workspace 一輪 **91 次**。
- **`compare`**：每個 workflow-linked merged PR 各一次，判的是「merge commit 還在
  不在 default branch 上」，也就是一次 ancestry。

REST 有 primary／secondary rate limit；git 協定（fetch/clone）不受它管轄。
0813–0815 三度進 REST 懲罰窗，這兩類是主要配額消耗之一。

## 改法

新增 `paulsha_cortex/monitor/git_mirror.py`（`LocalGitMirror`），是「本機 git 當成
遠端事實來源」的唯一入口：

- **contents → `git cat-file --batch`**：blob 一律用 REST tree 給的 blob sha 定址
  讀取，整批一次讀完（不是每檔一個 process）。sha 定址本身就是內容識別，取代舊
  `contents` 路徑的 `type`／`path`／`sha`／`encoding` 四項比對。
- **compare → `git merge-base --is-ancestor`**：判準與 REST `compare` 的
  `status in {ahead, identical}` 等價。
- **一輪最多一次 fetch**：先一次 `cat-file --batch-check` 批次查缺，有缺才 fetch；
  refspec 帶 `--refmap=` 並寫進私有 namespace `refs/cortex/mirror/<hash>/*`，
  **不動** `refs/remotes/origin/*`、工作區與任何本地分支。merge commit 不在本機的
  PR 會把 `refs/pull/<n>/head` 一併掛進同一次 fetch。fetch 頻率因此沿用 monitor
  既有的 refresh 週期。
- **身分先驗**：`git config --get remote.origin.url`（讀 raw config，不套
  `url.*.insteadOf` 改寫）必須解析成同一個 `owner/name`，否則 fail closed——monitor
  掃的 workspace 目錄不保證是我們以為的那個 repo。

## fail closed

ref 不存在、fetch 失敗、blob 讀不到、沒有本機 checkout、origin 指向別的 repo、
shallow checkout 無法判 ancestry——一律 raise `GitMirrorError`（繼承 `OSError`，
即使日後有人漏接也只會退回既有的通用診斷），provider 轉成 degraded 快照、上層
`_retain_last_good` 保留上一份鏡像。**絕不**把讀取失敗靜默降級成「檔案不存在」
或「不是 ancestor」。

唯一的例外是「default branch 已在本機、repo 非 shallow，而 merge commit 仍不在
本機」——在 git 的可達性語意下這就是「不是 ancestor」的定義（等同 REST `compare`
回 `behind`／`diverged` 的那一格），不是讀取失敗。shallow 破壞這個前提，因此
shallow + 缺物件一律 fail closed。

## 語意差異（行為等價的邊界）

1. **`remote Todo content identity mismatch` 這個失敗模式消失了**——sha 定址下
   內容不可能對不上。取而代之的是「物件不在本機」，兩者都 degraded。
2. **REST `contents` 對不存在的路徑回 404，git 是「物件不存在」**——但這條路徑
   本來就只讀 tree 已經列出的 blob，兩邊都不會走到「路徑不存在」。
3. **ancestry 判的是 merge commit，不是 PR head**——與舊 `compare` 的 base 一致。
   `refs/pull/<n>/head` 只在 merge commit 缺席時一併 fetch，且屬選配：remote 沒有
   該 ref 時退回只 fetch default branch，不讓一條選配 refspec 把整輪打成 degraded。
4. **REST tree 截斷檢查仍在 REST 端**：`git/trees?recursive=1` 這 1 次／輪的呼叫
   不在本次範圍（不屬 contents／compare 兩類）。

## 量化

`tests/test_monitor_git_native_reads_506.py::test_scan_round_issues_zero_rest_contents_and_compare_calls`
把「一輪掃描的 REST contents/compare 呼叫數」釘死在 0：12 個 todo ＋ 3 個 merged PR
的那一輪，改動前是 12 次 `contents` ＋ 3 次 `compare`；改動後整輪 REST 只剩 2 次
（graphql ＋ git tree）。生產基準：91+/輪 → 0。

provenance 落在 `github-terminal:` provider 的 `observations["remote_reads"]`
（transport／checkout 路徑／本輪 fetch 的 refspec／缺席物件／blob 讀取數／ancestry
判定數）。

## 範圍

只動讀取路徑。寫入（issue/PR/label mutation）不在本次；`state=all&since=`＋ETag
增量（D3）是另一個工項。`coordinator/github_delivery.py` 的 `fetch_remote_closure`
也有一組 `contents`／`compare`（每次 PR closure 各 1＋N 次，不在掃描迴圈內），
本次僅盤點未遷移。
