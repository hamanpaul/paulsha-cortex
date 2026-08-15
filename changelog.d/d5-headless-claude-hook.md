# D5：headless-only hook 儀器化（claude 先）——D4 spool 的第一個 producer

## 問題

D4 開了本機事件通道（`monitor/event_spool.py`），但**沒有任何 producer**：monitor
每輪掃 spool，掃到的永遠是空目錄。fleet 自己剛動過的 GitHub 物件，仍然只能等下一
次輪詢把整個清單再問一遍——D1–D3 省下配額的代價（發現延遲）因此一分錢也沒買回來。

## 改法

headless claude job 每跑完一次 `Bash` 工具，由 launcher 注入的 PostToolUse hook
呼叫 `cortex headless-hook post-tool-use`，把「我剛動了哪個 GitHub 物件」寫進 D4
spool。monitor 下一輪對被點名的物件做 targeted 條件驗證。

本次**只做 claude**：codex 的 `codex exec --json` JSONL 已被 parse，不需要 hook；
copilot/agy 留後續。D4 消費端一行未動。

## 使用者硬約束：hook 不得影響正常的互動式 agent 使用

這是紅線，因此不靠設定開關，靠**兩道彼此獨立的結構性保證**——任一道成立，互動
session 就是完全的 no-op：

1. **hook 只經 launcher 注入，從不落地任何檔案**。宣告由
   `SubprocessLauncher.launch()` 每次現場組出（`_claude_spool_hook_settings()`），
   經 argv 的 `--settings` 只交給這一個 job 的行程。**不寫 `~/.claude/settings.json`、
   不寫任何 user 層設定、不寫磁碟**。operator 自己開的互動 session 讀的是 operator
   的設定，那裡沒有這個 hook，因此互動 session **連呼叫寫入端的機會都沒有**。
   打包在 `scripts/hooks/claude.json` 的**使用者全域**模板（paulshaclaw thin install
   的切點）刻意不含這個 hook，並有測試釘死它永遠不出現在那裡。
2. **`PSC_JOB_ID` 自守**。就算宣告以任何方式流到互動 session（例如有人手動複製那段
   JSON），`headless_hook.emit_for_tool_use()` 讀不到 `PSC_JOB_ID` 就直接返回——
   **不建 spool 目錄、不寫檔、不起 subprocess、連命令都不解析**。這個變數只由
   launcher 為 cortex 派工的 job 注入。

**只有 builder 掛 hook**：read-only planner 走 `--tools ""`（連 Bash 都沒有），
review-only reviewer 是 read-only 契約且其 `--settings` 是那份 deny 掉 `$HOME` 的
sandbox 政策；兩者都不注入，`PSC_JOB_ID` 也只在 builder/planner 的 env 分支出現，
marker 與注入點成對，不留「有標記卻沒 hook」的半套狀態。

### 為什麼是 `--settings` overlay 而不是 hermetic `CLAUDE_CONFIG_DIR`

#404 為 planning 的純 JSON 回聲任務做了 hermetic per-job `CLAUDE_CONFIG_DIR`
（只播種 credentials，藉此隔離 operator 的 plugin／MCP／user CLAUDE.md）。同樣的做法
搬到 builder 會**一併抽掉 operator 的 `permissions` allowlist**，讓 headless job
卡在無人可核可的授權提示——那是遠超出 D5 範圍的行為變更。`--settings` 是與其他設定
來源合併的一層 overlay：per-job、走 argv、不落地，同時 builder 既有的 operator 設定
原封不動。兩者共享 #404 的核心不變量（per-job，不碰 user 全域），本次取風險較低的
那一種。

## 事件內容：hint 不是 authority

事件只帶「哪個 repo 的哪個編號被動了」，**不帶新狀態**；`action` 純屬診斷。命令解析
因此刻意往「寧可漏報」的方向失準：

- 只認**封閉列舉**的 mutation 動詞（`gh issue close/comment/edit/…`、
  `gh pr merge/review/…`）與會改狀態的 `gh api`（`-X`／`--method`，或帶
  `-f`/`-F`/`--field` 隱含 POST；GET/HEAD 一律不算）。
- 一行內以 `&&`／`||`／`;`／`|` 串接的多個命令全部解析，同一物件收斂成一則事件。
- 旗標一律當成「吃一個值」跳過，因此 `--add-label 3` 的 `3` 不會被誤認成 issue 編號
  （代價：`gh pr merge --squash 45` 這種旗標在前的寫法會漏報）。
- `repos/{owner}/{repo}/issues/comments/{id}` 改的是留言不是 issue，不會被誤認。
- 命令沒寫 `--repo` 時，從 job worktree 的 `origin` 補（只讀本機 git 設定、帶超時）；
  補不到就丟掉這則 hint。

漏報的後果只是退回原本的 refresh 週期延遲，而那正是 D3 每日 anti-entropy 的守備
範圍；誤報的後果是 monitor 白花一次條件請求（多半 304），且**永遠不會污染鏡像**
——鏡像只寫 GitHub 自己回的內容。

## fire-and-forget

hook 掛在別人（job）的工作路徑上，因此每一層都不得外溢：`emit_for_tool_use()` 以
總括的 `except Exception` 把所有失敗吞成 debug log（D4 的 `EventSpool.emit()` 本身
也永不 raise），CLI 一律 exit 0 且 **stdout 保持空**（PostToolUse 的 stdout 會被
Claude Code 當決策讀、非零 exit 會被回報成 hook 失敗甚至回饋給模型），launcher 注入
的命令再以 `|| true` 兜住 CLI 之外的失敗（`cortex` 不在 PATH、套件損壞），並設
`timeout` 上限確保 hook 不可能阻塞 job。

## #536／#488 心跳：本次只預留信封

同一條 hook 是 job 心跳的天然訊號源（每次 tool call 都觸發）。本次只發
`github_object` 事件，但每一則都帶 `job_id`——D4 信封的 `job_id` 欄位與
`RESERVED_EVENT_TYPES` 裡的 `job` 型別因此已備妥，心跳 consumer 落地時不需要改寫入
端契約。心跳消費不在本次。

## 變更

- 新增 `paulsha_cortex/porcelain/headless_hook.py`：命令解析、repo 補值、寫入端與
  `cortex headless-hook post-tool-use` CLI；登記進 `_FAMILY_MODULES`。
- `paulsha_cortex/coordinator/launcher.py`：`_claude_spool_hook_settings()`＋
  builder 分支的 `--settings` 注入；`launch()`／`executor_environment()` 的 job env
  加 `PSC_JOB_ID`（兩處必須同步，否則 preflight 只是安慰劑）。
- `docs/monitor-config.md`：補 spool 的 producer 一節。
- 新增 `tests/test_headless_claude_hook_506.py`（73 個測試），A 節（自守 no-op）與
  E 節（注入面／使用者全域無傷）先於功能面存在。
